"""Coach: the weekly learning loop.

1. Judge last week's automatic changes: keep them, or revert any whose results
   dropped below `revert_if_below` x baseline.
2. Apply bounded changes on evidence (>= min_comments_per_arm per arm, at most
   max_change_factor per week): topic weights, sub priority, target length,
   freshness window, retiring dead search queries, adding up to 2 new ones.
3. Refresh the exemplar banks: top 10% comments -> Writer style references;
   removed or downvoted -> Critic warnings.
4. Ask Claude for the digest and proposals (learned notes, new subs, blog
   ideas). Proposals wait for Jeet; approved learned notes are regression
   tested on the eval set before they reach the Writer.

Coach can only write tunables.json, the exemplar banks and proposals. It has
no path to checks.py, settings.json or the prompts, so it cannot learn its
way out of the non-negotiables.
"""
from __future__ import annotations

import json
import statistics
from datetime import timedelta

from ..llm import S, arr, enum, obj
from ..store import iso, parse

BOUNDS = {"topic_weights": (0.3, 3.0), "sub_priority": (0.2, 3.0), "target_words": (80, 240), "freshness_max_hours": (24, 96)}
LENGTH_BUCKETS = [("short", 0, 120, 100), ("medium", 120, 200, 160), ("long", 200, 10_000, 230)]
AGE_BUCKETS = [("fresh", 0, 12), ("day", 12, 36), ("older", 36, 10_000)]

COACH_SCHEMA = obj({
    "digest": S,
    "proposals": arr(obj({
        "kind": enum("learned_note", "new_sub", "phase_gate", "blog_idea"),
        "title": S, "detail": S, "evidence": S, "sub": S, "topic": S,
    })),
    "new_queries": arr(obj({"topic": S, "query": S})),
})


def outcome(c: dict) -> float | None:
    if c["status"] == "removed":
        return -5.0
    return None if c.get("score_72h") is None else float(c["score_72h"])


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _get(t: dict, path: tuple):
    cur = t
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    return cur.get(path[-1])


def _set(t: dict, path: tuple, value) -> None:
    cur = t
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    cur[path[-1]] = value


def _apply(ctx, path: tuple, new, reason: str, baseline: float | None, changes: list) -> None:
    old = _get(ctx.cfg.tunables, path)
    if old == new:
        return
    _set(ctx.cfg.tunables, path, new)
    ctx.store.add_tunable_change(key=".".join(path), old=json.dumps(old), new=json.dumps(new), reason=reason, baseline=baseline)
    changes.append({"key": ".".join(path), "old": old, "new": new, "reason": reason})


def _ratio_update(ctx, group: str, key_of, measured: list[dict], overall: float, changes: list) -> None:
    s = ctx.cfg.settings["coach"]
    lo, hi = BOUNDS[group]
    arms: dict[str, list[float]] = {}
    for c in measured:
        k = key_of(c)
        if k:
            arms.setdefault(k, []).append(outcome(c))
    arms = {k: xs for k, xs in arms.items() if len(xs) >= s["min_comments_per_arm"]}
    if not arms:
        return
    # Shift everything above 1 so ratios stay meaningful when medians are 0 or negative (removals score -5).
    floor = min([overall] + [statistics.median(xs) for xs in arms.values()])
    shift = 1 - floor if floor < 1 else 0
    for k, xs in arms.items():
        ratio = (statistics.median(xs) + shift) / (overall + shift)
        factor = _clamp(ratio, 1 / s["max_change_factor"], s["max_change_factor"])
        old = float(_get(ctx.cfg.tunables, (group, k)) or 1.0)
        new = round(_clamp(old * factor, lo, hi), 2)
        if abs(new - old) / old >= 0.05:
            _apply(ctx, (group, k), new, f"median {statistics.median(xs):.1f} vs overall {overall:.1f} over {len(xs)} comments",
                   overall, changes)


def evaluate_changes(ctx, measured: list[dict]) -> list[dict]:
    s = ctx.cfg.settings["coach"]
    out = []
    for ch in ctx.store.tunable_changes("active"):
        applied = parse(ch["applied_at"])
        if ctx.now() - applied < timedelta(days=7):
            continue
        after = [outcome(c) for c in measured if parse(c["posted_at"]) >= applied]
        if len(after) < s["min_after_comments"]:
            if ctx.now() - applied > timedelta(days=28):
                ctx.store.update_tunable_change(ch["id"], status="kept", evaluated_at=iso(ctx.now()))
            continue
        med = statistics.median(after)
        base = ch["baseline"]
        worse = base is not None and (med < base * s["revert_if_below"] if base > 0 else med < base - 1)
        if worse:
            path = tuple(ch["key"].split("."))
            _set(ctx.cfg.tunables, path, json.loads(ch["old"]) if isinstance(ch["old"], str) else ch["old"])
            ctx.store.update_tunable_change(ch["id"], status="reverted", evaluated_at=iso(ctx.now()), after=med)
            out.append({"key": ch["key"], "action": "reverted", "baseline": base, "after": med})
        else:
            ctx.store.update_tunable_change(ch["id"], status="kept", evaluated_at=iso(ctx.now()), after=med)
            out.append({"key": ch["key"], "action": "kept", "baseline": base, "after": med})
    return out


def _thread_age_at_post(ctx, c: dict) -> float | None:
    if not c.get("job_id"):
        return None
    job = ctx.store.job(c["job_id"])
    return (job.get("brief") or {}).get("age_hours") if job else None


def weekly(ctx) -> dict:
    store, cfg = ctx.store, ctx.cfg
    s = cfg.settings["coach"]
    run_id = store.run_start("coach")
    usage = ctx.llm.new_usage()
    changes: list[dict] = []
    try:
        now = ctx.now()
        window = store.comments(since=now - timedelta(days=28))
        measured = [c for c in window if outcome(c) is not None]
        evaluated = evaluate_changes(ctx, measured)

        overall = _median([outcome(c) for c in measured]) if len(measured) >= s["min_comments_per_arm"] else None
        if overall is not None:
            _ratio_update(ctx, "topic_weights", lambda c: c.get("topic") if c.get("topic") in cfg.base["topics"] else None,
                          measured, overall, changes)
            _ratio_update(ctx, "sub_priority", lambda c: c.get("sub"), measured, overall, changes)

            # Target length: move 20% toward the best performing length bucket.
            buckets = {name: [outcome(c) for c in measured if lo <= len((c.get("text") or "").split()) < hi]
                       for name, lo, hi, _ in LENGTH_BUCKETS}
            ranked = sorted(((statistics.median(v), n) for n, v in buckets.items() if len(v) >= s["min_comments_per_arm"]), reverse=True)
            if ranked:
                centre = next(c for n, _, _, c in LENGTH_BUCKETS if n == ranked[0][1])
                old = int(cfg.tunables.get("target_words", 170))
                step = _clamp(centre - old, -old * (s["max_change_factor"] - 1), old * (s["max_change_factor"] - 1))
                new = int(_clamp(old + step, *BOUNDS["target_words"]))
                if abs(new - old) >= 10:
                    _apply(ctx, ("target_words",), new, f"{ranked[0][1]} comments score best", overall, changes)

            # Freshness: if older threads clearly underperform, tighten the window; if they hold up, widen it.
            ages = [(a, outcome(c)) for c in measured if (a := _thread_age_at_post(ctx, c)) is not None]
            fresh = [o for a, o in ages if a < 12]
            older = [o for a, o in ages if a >= 36]
            if len(fresh) >= s["min_comments_per_arm"] and len(older) >= s["min_comments_per_arm"]:
                old = float(cfg.tunables.get("freshness_max_hours", 72))
                f_med, o_med = statistics.median(fresh), statistics.median(older)
                factor = 1 / s["max_change_factor"] if o_med < f_med * 0.6 else s["max_change_factor"] if o_med >= f_med else 1
                new = round(_clamp(old * factor, *BOUNDS["freshness_max_hours"]))
                if new != old:
                    _apply(ctx, ("freshness_max_hours",), new, f"older threads median {o_med} vs fresh {f_med}", overall, changes)

        # Retire search queries that found nothing worth picking in 4 weeks (max 2 a week).
        retired = 0
        for row in store.candidate_stats(now - timedelta(days=28)):
            src = row["source"] or ""
            if not src.startswith("search:") or row["seen"] < 8 or (row["picked"] or 0) > 0 or retired >= 2:
                continue
            q = src.split(":", 1)[1]
            topic = next((t for t, v in cfg.base["topics"].items() if q in v["queries"]
                          or q in cfg.tunables.get("queries_added", {}).get(t, [])), None)
            if topic and q not in cfg.tunables.setdefault("queries_retired", {}).setdefault(topic, []) \
                    and len(cfg.topic_queries(topic)) > 2:
                old = list(cfg.tunables["queries_retired"][topic])
                _apply(ctx, ("queries_retired", topic), old + [q], f"'{q}' surfaced {row['seen']} threads, 0 picked in 28 days", None, changes)
                retired += 1

        exemplar_note = refresh_exemplars(ctx, measured)

        edits = [{"sub": c["sub"], "draft": c["draft_text"], "final": c["text"]}
                 for c in window if c.get("draft_text") and c.get("text") and c["draft_text"].strip() != c["text"].strip()][-10:]
        kills = [e for e in store.events(limit=300) if e["agent"] == "critic" and parse(e["ts"]) >= now - timedelta(days=7)][:15]
        skipped = [j for j in store.jobs(statuses=("skipped", "expired"), limit=100)
                   if parse(j["created_at"]) >= now - timedelta(days=7)]
        stats = _stats(measured, window)
        payload = {
            "week_ending": ctx.ist().date().isoformat(),
            "stats": stats,
            "automatic_changes_this_week": changes,
            "last_weeks_changes_evaluated": evaluated,
            "exemplars": exemplar_note,
            "jeets_edits": edits,
            "killed_drafts": [{"notes": (k.get("data") or {}).get("notes", "")[:400]} for k in kills],
            "skipped_or_expired": [{"sub": j["sub"], "title": j["thread_title"], "status": j["status"]} for j in skipped][:15],
            "topics_with_no_article": cfg.base["links"]["no_article_yet"],
            "roster": cfg.roster,
            "topics": sorted(cfg.base["topics"].keys()),
            "open_proposals": [p["title"] for p in store.proposals()],
        }
        out = ctx.llm.structured("coach", payload, COACH_SCHEMA)

        added = 0
        for q in out["new_queries"]:
            if added >= s["max_new_queries_per_week"] or q["topic"] not in cfg.base["topics"] or not q["query"].strip():
                continue
            existing = cfg.tunables.setdefault("queries_added", {}).setdefault(q["topic"], [])
            if q["query"] in existing or q["query"] in cfg.base["topics"][q["topic"]]["queries"]:
                continue
            _apply(ctx, ("queries_added", q["topic"]), existing + [q["query"].strip()], "suggested by Coach", None, changes)
            added += 1

        for p in out["proposals"][:5]:
            store.add_proposal(kind=p["kind"], title=p["title"], detail=p["detail"], evidence=p["evidence"],
                               payload={"sub": p["sub"].removeprefix("r/"), "topic": p["topic"]})
        cfg.tunables["version"] = int(cfg.tunables.get("version", 1)) + (1 if changes else 0)
        cfg.save_tunables()

        digest_dir = cfg.data_dir / "digests"
        digest_dir.mkdir(exist_ok=True)
        (digest_dir / f"{ctx.ist().date().isoformat()}.md").write_text(out["digest"])
        ctx.notify.send("Weekly Reddit digest", out["digest"][:3000], "/proposals")
        result = {"status": "ok", "changes": changes, "evaluated": evaluated, "proposals": len(out["proposals"][:5])}
        store.run_finish(run_id, "ok", result, usage.as_dict())
        return result
    except Exception as e:  # noqa: BLE001
        # Automatic changes already applied in memory are only persisted on success.
        store.event("error", "coach", f"weekly review failed: {e}")
        store.run_finish(run_id, "error", {"error": str(e)}, usage.as_dict())
        ctx.notify.send("Weekly Reddit review failed", str(e), "/status", level="warn")
        return {"status": "error", "error": str(e)}


def _stats(measured: list[dict], window: list[dict]) -> dict:
    def summarise(key_of):
        groups: dict[str, list[float]] = {}
        for c in measured:
            groups.setdefault(key_of(c) or "?", []).append(outcome(c))
        return {k: {"n": len(v), "median": statistics.median(v)} for k, v in sorted(groups.items())}
    return {
        "comments_28d": len(window),
        "measured": len(measured),
        "removed": sum(1 for c in window if c["status"] == "removed"),
        "mentions": sum(1 for c in window if c["mention"]),
        "by_sub": summarise(lambda c: c["sub"]),
        "by_topic": summarise(lambda c: c.get("topic")),
        "by_kind": summarise(lambda c: c["kind"]),
        "top": [{"sub": c["sub"], "score": outcome(c), "text": c["text"][:400]}
                for c in sorted(measured, key=lambda c: -(outcome(c) or 0))[:3]],
        "bottom": [{"sub": c["sub"], "score": outcome(c), "text": c["text"][:400]}
                   for c in sorted(measured, key=lambda c: (outcome(c) or 0))[:3]],
    }


def refresh_exemplars(ctx, measured: list[dict]) -> str:
    s = ctx.cfg.settings["coach"]
    good = sorted([c for c in measured if c["status"] == "posted" and (c.get("score_72h") or 0) >= 3],
                  key=lambda c: -c["score_72h"])
    if len(measured) >= 10:
        good = good[: max(1, len(measured) // 10)]
    else:
        good = good[:1]
    bad = [c for c in measured if c["status"] == "removed" or (c.get("score_72h") is not None and c["score_72h"] <= 0)]
    ex = ctx.cfg.exemplars
    known = {e["text"] for e in ex}
    for c in good:
        if c["text"] not in known:
            ex.append({"sub": c["sub"], "thread_title": c.get("thread_title"), "text": c["text"],
                       "source": f"score {c['score_72h']} at 72h"})
    ctx.cfg.exemplars = ex[-s["exemplars_keep"]:]
    neg = ctx.cfg.negatives
    known = {e["text"] for e in neg}
    for c in bad:
        if c["text"] not in known:
            neg.append({"sub": c["sub"], "text": c["text"],
                        "why": "removed by moderators" if c["status"] == "removed" else f"score {c['score_72h']} at 72h"})
    ctx.cfg.negatives = neg[-s["negatives_keep"]:]
    ctx.cfg.save_exemplars()
    return f"{len(good)} added to exemplars, {len(bad)} considered as warnings"


# -- proposals --------------------------------------------------------------------------
def decide(ctx, pid: int, approve: bool) -> str:
    p = ctx.store.proposal(pid)
    if not p or p["status"] != "open":
        return "not open"
    now = iso(ctx.now())
    if not approve:
        ctx.store.update_proposal(pid, status="rejected", decided_at=now)
        return "rejected"
    payload = p.get("payload") or {}
    if p["kind"] == "learned_note":
        ctx.store.update_proposal(pid, status="approved_pending_eval", decided_at=now)
        return "approved: will be regression tested before the Writer uses it"
    if p["kind"] == "new_sub" and payload.get("sub"):
        topic = payload.get("topic") if payload.get("topic") in ctx.cfg.base["topics"] else "agency_and_open"
        ctx.cfg.tunables.setdefault("extra_subs", {})[payload["sub"]] = topic
        ctx.cfg.save_tunables()
        ctx.store.update_proposal(pid, status="applied", decided_at=now, result={"added": payload["sub"], "topic": topic})
        return f"r/{payload['sub']} added under {topic}: its rules are read before the first comment"
    ctx.store.update_proposal(pid, status="approved", decided_at=now, result={"note": "recorded; apply by hand"})
    return "recorded"


def apply_pending_notes(ctx, run_eval) -> list[str]:
    """Approved learned notes go through the eval set first; a regression blocks them."""
    out = []
    for p in ctx.store.proposals(statuses=("approved_pending_eval",)):
        before = ctx.cfg.learned_notes
        ctx.cfg.learned_notes = before.rstrip() + f"\n\n## {p['title']}\n\n{p['detail']}\n"
        try:
            result = run_eval(ctx)
        except Exception as e:  # noqa: BLE001
            result = {"skipped": True, "reason": f"eval failed to run: {e}"}
        ctx.cfg.learned_notes = before
        if result.get("regression"):
            ctx.store.update_proposal(p["id"], status="blocked_by_eval", result=result.get("metrics"))
            ctx.notify.send(f"Learned note blocked by the eval: {p['title']}", json.dumps(result.get("metrics")), "/proposals", level="warn")
            out.append(f"blocked: {p['title']}")
            continue
        ctx.cfg.append_learned_note(p["title"], p["detail"])
        ctx.store.update_proposal(p["id"], status="applied", result=result.get("metrics") or result)
        out.append(f"applied: {p['title']}")
    return out
