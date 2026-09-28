"""Analyst: scores candidates, finds the gap, picks 3–5, writes a brief per pick.

Claude judges expertise, gap and audience and reads what the existing replies
already cover. Code owns everything with a limit attached: visibility (from
age), the pick order, the caps, and above all whether a comment may mention
YuvGro. The model never decides its own promotion budget.
"""
from __future__ import annotations

from ..llm import B, S, arr, enum, obj


def assess_schema(link_topics: list[str]) -> dict:
    return obj({"assessments": arr(obj({
        "id": S,
        "question": S,
        "expertise": enum(0, 1, 2, 3),
        "gap": enum(0, 1, 2, 3),
        "audience": enum(0, 1, 2),
        "already_covered": arr(S),
        "gap_angle": S,
        "op_asked_for_recommendation": B,
        "link_topic": enum(*link_topics, "none"),
        "skip_reason": S,
    }))})


def visibility(thread, now_ts: float) -> int:
    if thread.source.startswith("evergreen"):
        return 2
    age = thread.age_hours(now_ts)
    return 2 if age < 24 else 1 if age < 72 else 0


def assess(ctx, candidates: list[tuple], rc: dict) -> dict[str, dict]:
    recent = [{"sub": c["sub"], "title": c.get("thread_title") or "", "opening": " ".join((c.get("text") or "").split()[:25])}
              for c in ctx.store.comments()[-20:]]
    payload = {
        "phase": rc["phase"],
        "topic_of_day": rc["topic"],
        "our_recent_comments": recent,
        "candidates": [{
            "id": t.id, "sub": t.sub, "posting_mode": mode, "title": t.title, "body": t.body[:4000],
            "age_hours": round(t.age_hours(ctx.now().timestamp()), 1), "num_comments": t.num_comments,
            "top_comments": t.to_dict()["comments"],
        } for t, _policy, mode, _r in candidates],
    }
    out = ctx.llm.structured("analyst", payload, assess_schema(ctx.cfg.link_topics()))
    return {a["id"]: a for a in out["assessments"]}


def pick(ctx, candidates: list[tuple], assessments: dict[str, dict], rc: dict, run_id: int) -> list[dict]:
    cfg, store = ctx.cfg, ctx.store
    d = cfg.settings["drafting"]
    hr = rc["headroom"]
    now_ts = ctx.now().timestamp()
    scored = []
    for t, policy, mode, mode_reason in candidates:
        a = assessments.get(t.id)
        if not a:
            store.mark_seen(t.id, t.sub, "not_assessed", "", t.source)
            continue
        total = a["expertise"] + a["gap"] + a["audience"] + visibility(t, now_ts)
        scored.append((total, cfg.sub_priority(t.sub), t, policy, mode, a))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2].created_utc))

    remaining = hr["remaining_today"]
    minimum = cfg.base["session"]["comments_per_day"]["min"]
    per_sub_run: dict[str, int] = {}
    mention_used = False
    used_links = {c.get("yuvgro_link") for c in store.comments() if c.get("yuvgro_link")}
    picks = []
    for total, _prio, t, policy, mode, a in scored:
        sub = t.sub.lower()
        reason = ""
        if remaining <= 0:
            reason = "daily cap reached"
        elif total < d["fallback_min_score"] or (total < d["min_total_score"] and len(picks) >= minimum):
            reason = f"score {total} too low" + (f": {a['skip_reason']}" if a["skip_reason"] else "")
        elif hr["per_sub_week"].get(sub, 0) + per_sub_run.get(sub, 0) >= hr["sub_week_cap"]:
            reason = "weekly cap for this sub"
        elif per_sub_run.get(sub, 0) >= d["max_per_sub_per_run"]:
            reason = "already two in this sub today"
        if reason:
            store.mark_seen(t.id, t.sub, "not_picked", reason, t.source, total)
            store.log_candidate(run_id, t.id, t.sub, t.source, "not_picked", total)
            continue

        ctype, link = "value", None
        if mode == "talking_points":
            ctype = "talking_points"
        elif (rc["phase"] == "established" and not mention_used and mode == "normal" and total >= 8
              and a["op_asked_for_recommendation"] and hr["mention_ok"]
              and sub not in hr["mention_cooldown_subs"]):
            ctype = "mention"
            mention_used = True
            slugs = cfg.base["links"]["by_topic"].get(a["link_topic"], [])
            if hr["link_ok"] and slugs:
                fresh = [s for s in slugs if cfg.blog_url(s) not in used_links]
                link = cfg.blog_url((fresh or slugs)[0])

        neutral_ok = rc["account"]["age_days"] >= cfg.settings["links"]["neutral_links_from_day"]
        brief = {
            "thread_id": t.id, "sub": t.sub, "title": t.title, "url": t.url,
            "question": a["question"], "already_covered": a["already_covered"], "gap_angle": a["gap_angle"],
            "comment_type": ctype, "yuvgro_link": link, "posting_mode": mode, "sub_policy": policy.get("summary", ""),
            "neutral_links_allowed": neutral_ok and ctype != "talking_points",
            "target_words": int(cfg.tunables.get("target_words", 170)),
            "fit_score": total, "phase": rc["phase"], "topic": rc["topic"], "source": t.source,
            "age_hours": round(t.age_hours(now_ts), 1),
        }
        picks.append({"thread": t, "brief": brief, "policy": policy})
        per_sub_run[sub] = per_sub_run.get(sub, 0) + 1
        remaining -= 1
        store.mark_seen(t.id, t.sub, "picked", "", t.source, total)
        store.log_candidate(run_id, t.id, t.sub, t.source, "picked", total, picked=True)
    return picks
