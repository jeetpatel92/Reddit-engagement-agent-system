"""Scout: finds up to N fresh candidate threads.

Mechanical filters in code (seen, archived, locked, NSFW, removed, our own,
too old, too crowded, not English enough), then one cheap batched Claude call
asking only "is there a real question here inside Jeet's expertise?".
Survivors are re-fetched with their top comments.
"""
from __future__ import annotations

from ..llm import B, S, arr, obj
from ..reddit_io import RedditAuthError, RedditError, RedditForbidden, RedditRateLimited, Thread

SCREEN_SCHEMA = obj({"items": arr(obj({"id": S, "keep": B, "reason": S}))})


def _ascii_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    return sum(ch.isascii() for ch in letters) / len(letters) if letters else 1.0


def run(ctx, rc: dict, run_id: int) -> list[Thread]:
    cfg, store, reddit = ctx.cfg, ctx.store, ctx.reddit
    sc = cfg.settings["scout"]
    fresh_limit = float(cfg.tunables.get("freshness_max_hours", 72))
    me = cfg.username.lower()
    now = ctx.now().timestamp()
    raw: dict[str, Thread] = {}
    errors = []

    def collect(threads: list[Thread], evergreen: bool = False) -> None:
        for t in threads:
            if evergreen:
                t.source = f"evergreen:{t.source.split(':', 1)[-1]}"
            raw.setdefault(t.id, t)

    try:
        for sub in rc["subs"]:
            collect(reddit.new_posts(sub, limit=sc["posts_per_sub"]))
        allowed = rc["allowed_subs"]
        for q in rc["queries"]:
            collect(reddit.search(q, subs=allowed, sort="new", time_filter="day", limit=sc["search_limit"]))
        if rc["phase"] == "established":
            for q in rc["queries"][:2]:
                collect(reddit.search(q, subs=allowed, sort="relevance", time_filter="year", limit=10), evergreen=True)
    except (RedditAuthError, RedditForbidden):
        raise
    except RedditRateLimited as e:
        errors.append(f"rate limited while searching, continuing with {len(raw)} threads ({e})")
    except RedditError as e:
        errors.append(f"Reddit error while searching, continuing with {len(raw)} threads ({e})")
    for e in errors:
        store.event("warn", "scout", e)

    allowed_lower = {s.lower() for s in rc["allowed_subs"]}
    kept: list[Thread] = []
    for t in raw.values():
        evergreen = t.source.startswith("evergreen")
        reason = ""
        age = (now - t.created_utc) / 3600
        if store.seen(t.id):
            continue
        if t.sub.lower() not in allowed_lower:
            reason = "sub not allowed today"
        elif t.archived or t.locked:
            reason = "archived or locked"
        elif t.over_18 or t.removed:
            reason = "nsfw or removed"
        elif t.author.lower() == me:
            reason = "our own post"
        elif evergreen and (age > sc["evergreen_max_age_days"] * 24 or t.num_comments > sc["evergreen_max_comments"]):
            reason = "evergreen too old or crowded"
        elif not evergreen and age > fresh_limit:
            reason = f"older than {fresh_limit:.0f}h"
        elif not evergreen and t.num_comments > sc["max_comments_fresh"]:
            reason = "too crowded"
        elif _ascii_ratio(t.title + " " + t.body) < sc["min_ascii_ratio"]:
            reason = "not English"
        if reason:
            store.mark_seen(t.id, t.sub, "filtered", reason, t.source)
            store.log_candidate(run_id, t.id, t.sub, t.source, "filtered")
            continue
        kept.append(t)

    # Freshest first, weighted by the sub priority Coach learned.
    kept.sort(key=lambda t: (-(cfg.sub_priority(t.sub)), (now - t.created_utc)))
    kept = kept[: sc["max_raw_to_screen"]]
    if not kept:
        return []

    items = [{"id": t.id, "sub": t.sub, "title": t.title, "body": t.body[:700],
              "age_hours": round((now - t.created_utc) / 3600, 1), "num_comments": t.num_comments} for t in kept]
    verdicts = ctx.llm.structured("screen", {"topic_of_day": rc["topic"], "threads": items}, SCREEN_SCHEMA)
    keep_ids = {v["id"] for v in verdicts["items"] if v["keep"]}
    reasons = {v["id"]: v["reason"] for v in verdicts["items"]}

    out: list[Thread] = []
    for t in kept:
        if t.id not in keep_ids:
            store.mark_seen(t.id, t.sub, "screened_out", reasons.get(t.id, ""), t.source)
            store.log_candidate(run_id, t.id, t.sub, t.source, "screened_out")
            continue
        if len(out) >= sc["max_candidates"]:
            break  # not marked seen: may be picked up tomorrow while still fresh
        try:
            full = reddit.thread(t.id, top_n=sc["top_comments"])
            full.source = t.source
        except RedditError as e:
            store.event("warn", "scout", f"could not fetch thread {t.id}: {e}")
            continue
        if full.locked or full.archived:
            store.mark_seen(t.id, t.sub, "filtered", "locked on fetch", t.source)
            continue
        out.append(full)
    return out
