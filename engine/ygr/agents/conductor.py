"""Conductor: the daily run, end to end.

Go/no-go (emergency stop, account health, phase, caps) -> run brief -> Scout
-> Rulekeeper -> Analyst -> Writer/Critic per pick -> Publisher queue ->
approval notification. Plain code, no model: it decides what runs, never what
gets said.

Failure routing:
  Reddit auth rejected       -> emergency stop
  Anthropic unavailable      -> stop drafting, keep what's queued, alert
  run budget reached         -> stop drafting, keep what's queued
  a single thread refused    -> skip that thread
  anything unexpected        -> logged with traceback, alert, run marked error
"""
from __future__ import annotations

import traceback

from ..core import account_state, halted, headroom, sub_pause_state, trip
from ..llm import BudgetExceeded, LLMError, LLMRefusal, LLMUnavailable
from ..reddit_io import RedditAuthError, RedditError
from . import analyst, drafting, publisher, rulekeeper, scout, watcher


def build_run_brief(ctx, acc: dict, hr: dict) -> dict:
    cfg = ctx.cfg
    ist = ctx.ist()
    topic = cfg.topic_for_weekday(ist.weekday())
    weights = cfg.tunables.get("topic_weights", {})
    subs = list(cfg.topic_subs(topic))
    queries = cfg.topic_queries(topic)
    # Rotate the query list by ISO week so the same three don't lead every week.
    if queries:
        start = ist.isocalendar()[1] % len(queries)
        queries = (queries[start:] + queries[:start])[:3]
    # A topic Coach has learned performs well borrows a little airtime every day.
    others = sorted(((w, t) for t, w in weights.items() if t != topic and t in cfg.base["topics"]), reverse=True)
    secondary = others[0][1] if others and others[0][0] > 1.1 else None
    if secondary:
        subs += [s for s in cfg.topic_subs(secondary)[:2] if s not in subs]
        queries += cfg.topic_queries(secondary)[:1]

    allowed = cfg.roster
    if acc["phase"] == "warmup":
        warm = cfg.warmup_subs()
        subs = [s for s in subs if s.lower() in warm]
        allowed = [s for s in allowed if s.lower() in warm]
        for s in sorted(cfg.base["phases"]["warmup"]["prefer_subs"], key=lambda s: -cfg.sub_priority(s)):
            if len(subs) >= 3:
                break
            if s not in subs:
                subs.append(s)
    subs = [s for s in subs if cfg.sub_priority(s) >= 0.5 and not sub_pause_state(ctx.store, s, ctx.now())[0]][:5]
    return {"date": ist.date().isoformat(), "phase": acc["phase"], "topic": topic, "secondary_topic": secondary,
            "subs": subs, "queries": queries, "allowed_subs": allowed, "headroom": hr, "account": acc}


def daily_run(ctx, force: bool = False) -> dict:
    store = ctx.store
    today = ctx.ist().date().isoformat()
    key = f"daily_run:{today}"
    if store.kv_get(key) and not force:
        return {"status": "already_ran"}
    if halted(store):
        store.event("warn", "conductor", "daily run skipped: emergency stop is on")
        return {"status": "halted"}

    run_id = store.run_start("daily")
    usage = ctx.llm.new_usage()
    summary: dict = {"queued": 0, "killed": 0, "refused": 0}
    status = "error"
    try:
        try:
            watcher.health(ctx)
        except RedditAuthError:
            raise
        except RedditError as e:
            store.event("warn", "conductor", f"health check failed, continuing on the last snapshot: {e}")
        if halted(store):
            status = "halted"
            return {"status": status}

        acc = account_state(ctx)
        hr = headroom(ctx, acc["phase"])
        summary.update(phase=acc["phase"], karma=acc["karma"], age_days=acc["age_days"])
        if hr["remaining_today"] <= 0:
            status = "skipped"
            summary["reason"] = "daily cap already used"
            return {"status": status, **summary}

        rc = build_run_brief(ctx, acc, hr)
        summary.update(topic=rc["topic"], subs=rc["subs"], queries=rc["queries"])
        threads = scout.run(ctx, rc, run_id)
        summary["scouted"] = len(threads)
        cands = rulekeeper.annotate(ctx, threads, acc)
        summary["candidates"] = len(cands)
        if not cands:
            status = "no_candidates"
            return {"status": status, **summary}

        assessments = analyst.assess(ctx, cands, rc)
        picks = analyst.pick(ctx, cands, assessments, rc, run_id)
        summary["picked"] = len(picks)

        for p in picks:
            t, brief = p["thread"], p["brief"]
            try:
                res = drafting.write_and_review(ctx, brief, t, None, "comment")
            except LLMRefusal as e:
                store.mark_seen(t.id, t.sub, "refused", str(e))
                summary["refused"] += 1
                continue
            except (LLMUnavailable, BudgetExceeded):
                raise
            except LLMError as e:
                store.event("warn", "conductor", f"drafting failed for {t.id}: {e}")
                continue
            if res["status"] != "pass":
                store.mark_seen(t.id, t.sub, "killed", (res["review"] or {}).get("notes", "")[:500])
                summary["killed"] += 1
                continue
            job_id = publisher.enqueue(ctx, kind="comment", dedupe_key=f"comment:{t.id}", brief=brief,
                                       draft=res["draft"], review=res["review"], thread=t, run_id=run_id,
                                       revisions=res["revisions"])
            if job_id:
                summary["queued"] += 1
        status = "ok"
        return {"status": status, **summary}

    except RedditAuthError as e:
        trip(ctx, f"Reddit rejected the credentials: {e}")
        summary["error"] = str(e)
        return {"status": "error", **summary}
    except (LLMUnavailable, BudgetExceeded) as e:
        summary["error"] = str(e)
        store.event("error", "conductor", f"daily run stopped early: {e}")
        ctx.notify.send("Reddit daily run stopped early", str(e), "/status", level="warn")
        status = "partial" if summary["queued"] else "error"
        return {"status": status, **summary}
    except Exception as e:  # noqa: BLE001 — log everything, never die silently
        summary["error"] = f"{type(e).__name__}: {e}"
        store.event("error", "conductor", f"daily run crashed: {e}", {"traceback": traceback.format_exc()[-4000:]})
        ctx.notify.send("Reddit daily run crashed", f"{type(e).__name__}: {e}", "/status", level="warn")
        return {"status": "error", **summary}
    finally:
        if summary.get("queued"):
            publisher.announce(ctx, summary["queued"])
        store.run_finish(run_id, status, summary, usage.as_dict())
        if status in ("ok", "skipped", "no_candidates", "partial", "halted"):
            store.kv_set(key, {"run_id": run_id, "status": status})
