"""Publisher: the approval gate, pacing, and the only code that posts.

Nothing reaches Reddit without Jeet's Approve. Posting is idempotent: a job is
marked `posting` before the API call, and after a crash the job is reconciled
against Jeet's recent comments before any retry, so a thread never gets two
comments. Posts go out at least `min_gap_minutes` apart (plus jitter), never
the same sub twice in a row if another sub is waiting.
"""
from __future__ import annotations

import random
from datetime import timedelta

from ..core import halted, mirror_to_manual_log, on_forbidden
from ..reddit_io import (RedditAuthError, RedditError, RedditForbidden, RedditRateLimited, ThreadClosed)
from ..store import iso, parse
from . import critic

POSTABLE = ("comment", "reply", "lead_reply", "post")


def enqueue(ctx, *, kind: str, dedupe_key: str, brief: dict, draft: dict, review: dict | None, thread,
            run_id: int | None = None, parent_id: str | None = None, inbox_id: str | None = None,
            status: str = "pending_approval", revisions: int = 0) -> int | None:
    hours = ctx.cfg.settings["publisher"]["approval_expiry_hours"]
    return ctx.store.create_job(
        kind=kind, dedupe_key=dedupe_key, thread_id=thread.id, parent_id=parent_id, sub=thread.sub,
        thread_title=thread.title, thread_url=thread.url, comment_type=brief.get("comment_type", "value"),
        mention=int(brief.get("comment_type") == "mention"), link=brief.get("yuvgro_link"),
        fit_score=brief.get("fit_score"), topic=brief.get("topic"), source=brief.get("source"),
        brief=brief, critic=review, draft_text=draft["text"], status=status,
        expires_at=iso(ctx.now() + timedelta(hours=hours)), run_id=run_id, inbox_id=inbox_id, revisions=revisions)


def announce(ctx, n: int, what: str = "comments") -> None:
    if n:
        ctx.notify.send(f"{n} Reddit {what} ready for your approval", "One tap each: approve, edit or skip.", "/")


def approve(ctx, job_id: int, final_text: str | None = None) -> list[str]:
    job = ctx.store.job(job_id)
    if not job:
        return ["job not found"]
    if job["status"] != "pending_approval":
        return [f"job is {job['status']}, not waiting for approval"]
    if job["kind"] not in POSTABLE:
        return ["this item is for you to send yourself (a DM draft); it is never posted automatically"]
    text = (final_text if final_text is not None else job["draft_text"] or "").strip().replace("\r\n", "\n")
    if job["comment_type"] == "talking_points" and text == (job["draft_text"] or "").strip():
        return ["this sub bans AI written comments: replace the talking points with your own words before approving"]
    errors = critic.final_check(ctx, job, text)
    if errors:
        return errors
    ctx.store.update_job(job_id, status="approved", final_text=text, approved_at=iso(ctx.now()))
    ctx.store.event("info", "publisher", f"approved job {job_id}", {"edited": text != (job["draft_text"] or "").strip()})
    return []


def skip(ctx, job_id: int, reason: str = "skipped by Jeet") -> None:
    job = ctx.store.job(job_id)
    if job and job["status"] in ("pending_approval", "approved", "manual"):
        ctx.store.update_job(job_id, status="skipped", error=reason)
        if job["thread_id"]:
            ctx.store.mark_seen(job["thread_id"], job["sub"], "skipped_by_jeet", reason)


def _reconcile_stuck(ctx) -> None:
    stuck_after = timedelta(minutes=ctx.cfg.settings["publisher"]["posting_stuck_minutes"])
    for job in ctx.store.jobs(statuses=("posting",)):
        started = parse(job["posting_started_at"])
        if started and ctx.now() - started < stuck_after:
            continue
        found = None
        if not ctx.cfg.dry_run:
            try:
                parent = f"t1_{job['parent_id']}" if job["parent_id"] else f"t3_{job['thread_id']}"
                found = ctx.reddit.find_my_comment(job["thread_id"], parent)
            except RedditError as e:
                ctx.store.event("warn", "publisher", f"could not reconcile job {job['id']}: {e}")
                continue
        if found:
            _record_posted(ctx, job, found)
            ctx.store.event("warn", "publisher", f"job {job['id']} had posted before a crash; recorded, not reposted")
        else:
            ctx.store.update_job(job["id"], status="approved", not_before=iso(ctx.now()))


def _expire(ctx) -> None:
    now = ctx.now()
    for job in ctx.store.jobs(statuses=("pending_approval", "approved")):
        if job["expires_at"] and parse(job["expires_at"]) < now:
            ctx.store.update_job(job["id"], status="expired", error="not approved and posted in time")
            if job["thread_id"]:
                ctx.store.mark_seen(job["thread_id"], job["sub"], "expired", "approval window passed")


def _next_job(ctx) -> dict | None:
    now = ctx.now()
    p = ctx.cfg.settings["publisher"]
    approved = [j for j in ctx.store.jobs(statuses=("approved",))
                if not j["not_before"] or parse(j["not_before"]) <= now]
    if not approved:
        return None
    approved.sort(key=lambda j: j["approved_at"] or "")
    last = ctx.store.last_post()
    if not last:
        return approved[0]
    since = now - parse(last["posted_at"])
    gap = timedelta(minutes=p["min_gap_minutes"])
    jitter = timedelta(seconds=ctx.store.kv_get("publisher:jitter", 0))
    if since < gap + jitter:
        return None
    other = [j for j in approved if (j["sub"] or "").lower() != (last["sub"] or "").lower()]
    if other:
        return other[0]
    return approved[0] if since >= 2 * gap else None


def _record_posted(ctx, job: dict, result: dict) -> None:
    now = iso(ctx.now())
    text = job["final_text"] or job["draft_text"]
    brief = job.get("brief") or {}
    row = dict(job_id=job["id"], comment_id=result["id"], kind=job["kind"], sub=job["sub"], thread_id=job["thread_id"],
               thread_url=job["thread_url"], thread_title=job["thread_title"], text=text, draft_text=job["draft_text"],
               mention=job["mention"], yuvgro_link=job["link"], topic=job["topic"], fit_score=job["fit_score"],
               source=job["source"] or brief.get("source"), posted_at=now, status="posted")
    if not ctx.store.comment_by_reddit_id(result["id"]):
        ctx.store.record_comment(**row)
    ctx.store.update_job(job["id"], status="posted", posted_at=now, comment_id=result["id"],
                         permalink=result.get("permalink", ""))
    if job["thread_id"]:
        ctx.store.mark_seen(job["thread_id"], job["sub"], "commented", "")
    # Only a real post on the real API belongs in Jeet's manual log: never a dry run, never a fixture.
    if not ctx.cfg.dry_run and getattr(ctx.reddit, "is_live", False):
        mirror_to_manual_log({**row, "permalink": result.get("permalink", "")})


def post(ctx, job: dict) -> str:
    p = ctx.cfg.settings["publisher"]
    ctx.store.update_job(job["id"], status="posting", posting_started_at=iso(ctx.now()), attempts=(job["attempts"] or 0) + 1)
    text = job["final_text"] or job["draft_text"]
    try:
        if ctx.cfg.dry_run:
            result = {"id": f"dry{job['id']}", "permalink": job["thread_url"] or ""}
        elif job["kind"] in ("reply", "lead_reply"):
            result = ctx.reddit.reply_to_comment(job["parent_id"], text)
        else:
            result = ctx.reddit.reply_to_thread(job["thread_id"], text)
    except RedditRateLimited as e:
        ctx.store.update_job(job["id"], status="approved", not_before=iso(ctx.now() + timedelta(seconds=e.retry_after)),
                             error=f"rate limited: {e}")
        return "rate_limited"
    except ThreadClosed as e:
        ctx.store.update_job(job["id"], status="failed", error=str(e))
        return "thread_closed"
    except RedditAuthError as e:
        ctx.store.update_job(job["id"], status="approved", error=str(e))
        from ..core import trip
        trip(ctx, f"Reddit rejected the credentials: {e}")
        return "auth"
    except RedditForbidden as e:
        ctx.store.update_job(job["id"], status="failed", error=f"403: {e}")
        on_forbidden(ctx, job["sub"], str(e))
        return "forbidden"
    except RedditError as e:
        attempts = (job["attempts"] or 0) + 1
        if attempts >= p["max_post_attempts"]:
            ctx.store.update_job(job["id"], status="failed", error=str(e))
            ctx.notify.send("A Reddit comment failed to post", f"r/{job['sub']}: {e}", "/", level="warn")
            return "failed"
        # Leave it in `posting`: the next tick reconciles against Reddit before retrying.
        ctx.store.update_job(job["id"], error=str(e))
        return "retry"
    _record_posted(ctx, job, result)
    ctx.store.kv_set("publisher:jitter", random.randint(0, p["jitter_seconds"]))
    ctx.store.event("info", "publisher", f"posted job {job['id']} in r/{job['sub']}", {"comment_id": result["id"]})
    return "posted"


def tick(ctx) -> str | None:
    _expire(ctx)
    _reconcile_stuck(ctx)
    if halted(ctx.store):
        return None
    job = _next_job(ctx)
    return post(ctx, job) if job else None
