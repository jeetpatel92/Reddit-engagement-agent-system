"""Watcher: outcomes, account health and the inbox.

Checkpoints at +1h, 24h, 72h and 7d read each comment as a logged-out visitor
would: score, replies, and whether it is still visible. A comment that stops
being visible is marked removed and fed to the breakers. Account health checks
the profile's public visibility (shadowban / suspension) and the karma trend.
Inbox items are classified once and routed:
  mod or admin mail      -> emergency stop, verbatim to Jeet
  question/disagreement  -> reply job through Writer, Critic and approval
  "is this an ad?"       -> short honest reply job
  buying intent          -> Closer
  thanks, spam, other    -> no action
"""
from __future__ import annotations

from datetime import timedelta

from ..core import account_state, headroom, on_removal, trip
from ..llm import B, S, arr, enum, obj
from ..reddit_io import RedditError, RedditNotFound, Thread
from ..store import iso, parse
from . import closer, drafting, publisher

CHECKPOINTS = [("1h", 1, "score_1h"), ("24h", 24, "score_24h"), ("72h", 72, "score_72h"), ("7d", 168, "score_7d")]

TRIAGE_SCHEMA = obj({"items": arr(obj({
    "id": S,
    "category": enum("question", "disagreement", "hostile", "asks_if_ad", "buying_intent", "thanks", "spam", "other"),
    "needs_reply": B,
    "summary": S,
}))})


def checkpoints(ctx) -> int:
    now = ctx.now()
    done = 0
    for c in ctx.store.comments(since=now - timedelta(days=8), statuses=("posted",)):
        if str(c["comment_id"]).startswith(("manual_", "dry")):
            continue  # no real Reddit id to check (manual log or dry run)
        cps = c.get("checkpoints") or {}
        age_h = (now - parse(c["posted_at"])).total_seconds() / 3600
        due = [cp for cp in CHECKPOINTS if age_h >= cp[1] and cp[0] not in cps]
        if not due:
            continue
        label, _, column = due[-1]
        try:
            state = ctx.reddit.public_comment_state(c["comment_id"])
        except RedditNotFound:
            state = {"visible": False, "score": None, "replies": 0}
        except RedditError as e:
            ctx.store.event("warn", "watcher", f"checkpoint failed for {c['comment_id']}: {e}")
            continue
        for lbl, _, _col in due:
            cps[lbl] = {"at": iso(now), "score": state["score"], "visible": state["visible"]}
        fields = {"checkpoints": cps, "replies": state.get("replies", 0)}
        if state["score"] is not None:
            fields[column] = state["score"]
        if not state["visible"]:
            fields.update(status="removed", removed_at=iso(now))
        ctx.store.update_comment(c["id"], **fields)
        done += 1
        if not state["visible"]:
            ctx.store.event("warn", "watcher", f"comment in r/{c['sub']} is no longer publicly visible", {"comment_id": c["comment_id"]})
            ctx.notify.send(f"A comment in r/{c['sub']} was removed", c.get("thread_title") or "", "/status", level="warn")
            on_removal(ctx, {**c, "status": "removed", "removed_at": iso(now)})
    return done


def health(ctx) -> dict:
    me = ctx.reddit.me()
    pub = ctx.reddit.account_public(me["name"])
    ctx.store.add_snapshot(total_karma=me["total_karma"], comment_karma=me["comment_karma"],
                           link_karma=me["link_karma"], created_utc=me["created_utc"], public_visible=int(pub["visible"]))
    if pub.get("suspended"):
        trip(ctx, "the account is suspended")
    elif not pub["visible"]:
        trip(ctx, "the profile is not visible to logged-out visitors (possible shadowban)")
    before = ctx.store.snapshot_before(ctx.now() - timedelta(days=3))
    drop = ctx.cfg.settings["breakers"]["karma_drop_3d"]
    if before and before["total_karma"] is not None and before["total_karma"] - me["total_karma"] >= drop:
        trip(ctx, f"karma fell by {before['total_karma'] - me['total_karma']} in 3 days")
    return {**me, "public": pub}


def _reply_thread(ctx, item) -> tuple[Thread, dict] | None:
    """The thread plus the exact exchange being replied to."""
    try:
        thread = ctx.reddit.thread(item.thread_id, top_n=5)
    except RedditError as e:
        ctx.store.event("warn", "watcher", f"could not load thread for reply {item.id}: {e}")
        return None
    ours = ctx.store.comment_by_reddit_id(item.parent_id[3:]) if item.parent_id.startswith("t1_") else None
    context = {"our_comment": ours["text"] if ours else "", "their_reply": item.body, "their_username": item.author,
               "reply_category": ""}
    return thread, context


def inbox(ctx) -> dict:
    items = [i for i in ctx.reddit.inbox_unread() if not ctx.store.inbox_known(i.id)]
    stats = {"items": len(items), "replies_drafted": 0, "leads": 0, "halted": False}
    if not items:
        return stats
    for i in items:
        ctx.store.add_inbox(id=i.id, kind=i.kind, author=i.author, body=i.body, thread_id=i.thread_id,
                            parent_id=i.parent_id, comment_id=i.comment_id, subject=i.subject,
                            subreddit=i.subreddit, received_at=iso(ctx.now()))

    mod = [i for i in items if i.kind in ("mod", "admin")]
    for m in mod:
        ctx.notify.send(f"{'Moderator' if m.kind == 'mod' else 'Reddit admin'} message from {m.author or m.subreddit}",
                        f"Subject: {m.subject}\n\n{m.body[:1500]}", "/status", level="critical")
        ctx.store.update_inbox(m.id, classification={"category": m.kind}, handled_at=iso(ctx.now()))
    if mod:
        trip(ctx, f"{len(mod)} moderator/admin message(s) received: read them before anything else posts")
        stats["halted"] = True

    rest = [i for i in items if i.kind not in ("mod", "admin")]
    if rest:
        out = ctx.llm.structured("triage", {"items": [
            {"id": i.id, "kind": i.kind, "author": i.author, "body": i.body[:2000], "subject": i.subject} for i in rest]},
            TRIAGE_SCHEMA)
        verdicts = {v["id"]: v for v in out["items"]}
        acc = account_state(ctx)
        hr = headroom(ctx, acc["phase"])
        for i in rest:
            v = verdicts.get(i.id, {"category": "other", "needs_reply": False, "summary": ""})
            ctx.store.update_inbox(i.id, classification=v)
            cat = v["category"]
            if cat == "buying_intent":
                closer.handle(ctx, i, v)
                stats["leads"] += 1
            elif i.kind == "message" and cat not in ("spam", "thanks"):
                # DMs they started: always Jeet's to answer. Drafted, never sent.
                closer.handle(ctx, i, v, dm_only=True)
            elif cat in ("question", "disagreement", "asks_if_ad") and v["needs_reply"] and i.kind in ("comment_reply", "post_reply"):
                if hr["replies_remaining"] <= 0:
                    ctx.store.event("info", "watcher", f"reply cap reached, not drafting for {i.id}")
                elif _draft_reply(ctx, i, v, acc):
                    stats["replies_drafted"] += 1
                    hr["replies_remaining"] -= 1
            ctx.store.update_inbox(i.id, handled_at=iso(ctx.now()))
    ctx.reddit.mark_read([i.id for i in items])
    if stats["replies_drafted"]:
        publisher.announce(ctx, stats["replies_drafted"], "replies")
    return stats


def _draft_reply(ctx, item, verdict: dict, acc: dict) -> bool:
    key = f"reply:{item.comment_id}"
    if ctx.store.job_by_key(key):
        return False
    if verdict["category"] == "disagreement":
        # Engage on substance once per thread; never argue twice.
        prior = [j for j in ctx.store.jobs(kinds=("reply",)) if j["thread_id"] == item.thread_id
                 and (j.get("brief") or {}).get("reply_category") == "disagreement" and j["status"] in ("posted", "pending_approval", "approved")]
        if prior:
            return False
    loaded = _reply_thread(ctx, item)
    if not loaded:
        return False
    thread, context = loaded
    context["reply_category"] = verdict["category"]
    policy = ctx.store.policy(thread.sub) or {}
    brief = {"thread_id": thread.id, "sub": thread.sub, "title": thread.title, "url": thread.url,
             "question": verdict["summary"], "already_covered": [], "gap_angle": "",
             "comment_type": "reply", "yuvgro_link": None, "posting_mode": "value_only",
             "sub_policy": (policy.get("policy") or {}).get("summary", ""),
             "neutral_links_allowed": acc["age_days"] >= ctx.cfg.settings["links"]["neutral_links_from_day"],
             "target_words": 90, "fit_score": None, "phase": acc["phase"], "topic": "reply",
             "source": "inbox", "reply_category": verdict["category"]}
    res = drafting.write_and_review(ctx, brief, thread, context, "reply")
    if res["status"] != "pass":
        return False
    publisher.enqueue(ctx, kind="reply", dedupe_key=key, brief=brief, draft=res["draft"], review=res["review"],
                      thread=thread, parent_id=item.comment_id, inbox_id=item.id, revisions=res["revisions"])
    return True
