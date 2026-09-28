"""Shared context, account phase, limit headroom and circuit breakers.

Headroom counts both posted comments and jobs still waiting (pending approval,
approved, posting), so a batch waiting for Jeet can never push the account
over a cap once he approves it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import IST, ROOT, Config
from .store import ACTIVE_JOB_STATUSES, Store, iso, parse, utcnow

HALT_KEY = "breaker:global"
TOP_LEVEL = ("comment", "post")


@dataclass
class Ctx:
    cfg: Config
    store: Store
    reddit: object
    llm: object
    notify: object
    notion: object
    clock: Callable[[], datetime] = utcnow

    def now(self) -> datetime:
        return self.clock()

    def ist(self) -> datetime:
        return self.now().astimezone(IST)


def ist_date(ts: str | datetime):
    dt = parse(ts) if isinstance(ts, str) else ts
    return dt.astimezone(IST).date()


def thread_id_from_url(url: str) -> str:
    m = re.search(r"/comments/([a-z0-9]+)/", url or "")
    return m.group(1) if m else ""


# -- account phase -------------------------------------------------------------------
def account_state(ctx: Ctx) -> dict:
    snap = ctx.store.latest_snapshot()
    if snap and snap.get("created_utc"):
        created = datetime.fromtimestamp(snap["created_utc"], tz=timezone.utc)
    else:
        created = datetime.fromisoformat(ctx.cfg.created_date).replace(tzinfo=IST)
    age_days = (ctx.now() - created).days
    karma = snap["total_karma"] if snap else None
    gate = ctx.cfg.phase_gate()
    established = karma is not None and age_days >= gate["min_account_age_days"] and karma >= gate["min_total_karma"]
    return {
        "created": iso(created), "age_days": age_days, "karma": karma,
        "phase": "established" if established else "warmup",
        "days_to_gate": max(0, gate["min_account_age_days"] - age_days),
        "karma_to_gate": None if karma is None else max(0, gate["min_total_karma"] - karma),
    }


# -- headroom ----------------------------------------------------------------------------
def headroom(ctx: Ctx, phase: str) -> dict:
    cfg, store, now = ctx.cfg, ctx.store, ctx.now()
    today = ctx.ist().date()
    posted = store.comments(since=now - timedelta(days=45))
    active = store.jobs(statuses=ACTIVE_JOB_STATUSES)
    p = cfg.base["phases"][phase]
    per_day = cfg.base["session"]["comments_per_day"]

    top_posted = [c for c in posted if c["kind"] in TOP_LEVEL]
    top_active = [j for j in active if j["kind"] in TOP_LEVEL]
    today_count = sum(1 for c in top_posted if ist_date(c["posted_at"]) == today) + \
        sum(1 for j in top_active if ist_date(j["created_at"]) == today)

    week_start = now - timedelta(days=7)
    per_sub_week: dict[str, int] = {}
    for c in top_posted:
        if parse(c["posted_at"]) >= week_start:
            per_sub_week[c["sub"].lower()] = per_sub_week.get(c["sub"].lower(), 0) + 1
    for j in top_active:
        per_sub_week[j["sub"].lower()] = per_sub_week.get(j["sub"].lower(), 0) + 1

    replies_today = sum(1 for c in posted if c["kind"] in ("reply", "lead_reply") and ist_date(c["posted_at"]) == today) + \
        sum(1 for j in active if j["kind"] in ("reply", "lead_reply") and ist_date(j["created_at"]) == today)

    last30 = posted[-30:]
    mentions30 = sum(1 for c in last30 if c["mention"]) + sum(1 for j in active if j["mention"])
    out = {
        "phase": phase,
        "today_count": today_count,
        "remaining_today": max(0, per_day["max"] - today_count),
        "per_sub_week": per_sub_week,
        "sub_week_cap": p["max_comments_per_sub_per_week"],
        "replies_remaining": max(0, cfg.settings["replies"]["max_per_day"] - replies_today),
        "mentions_last30": mentions30,
        "mention_ok": False,
        "mention_reasons": ["phase 1: no YuvGro mentions"],
        "mention_cooldown_subs": [],
        "link_ok": False,
        "posts_this_week": sum(1 for c in posted if c["kind"] == "post" and parse(c["posted_at"]) >= week_start),
    }
    if phase != "established":
        return out

    reasons = []
    allowed = int(p["yuvgro_mention_ratio_max"] * len(last30))
    if mentions30 + 1 > allowed:
        reasons.append(f"ratio: {mentions30} mention(s) in last {len(last30)}, allowed {allowed}")
    mentions_today = sum(1 for c in posted if c["mention"] and ist_date(c["posted_at"]) == today) + \
        sum(1 for j in active if j["mention"] and ist_date(j["created_at"]) == today)
    if mentions_today >= p["max_mentions_per_day"]:
        reasons.append("already one YuvGro mention today")
    cooldown_start = now - timedelta(days=p["min_days_between_mentions_in_same_sub"])
    cooldown = {c["sub"].lower() for c in posted if c["mention"] and parse(c["posted_at"]) >= cooldown_start}
    cooldown |= {j["sub"].lower() for j in active if j["mention"]}
    links7 = sum(1 for c in posted if c["yuvgro_link"] and parse(c["posted_at"]) >= week_start) + \
        sum(1 for j in active if j["link"] and "yuvgro.com" in (j["link"] or ""))
    out.update({
        "mention_ok": not reasons,
        "mention_reasons": reasons,
        "mention_cooldown_subs": sorted(cooldown),
        "link_ok": links7 < p["max_yuvgro_links_per_7_days"],
    })
    return out


# -- circuit breakers ---------------------------------------------------------------------------
def halted(store: Store) -> dict | None:
    return store.kv_get(HALT_KEY)


def trip(ctx: Ctx, reason: str, data=None) -> None:
    if halted(ctx.store):
        return
    ctx.store.kv_set(HALT_KEY, {"reason": reason, "at": iso(ctx.now()), "data": data})
    ctx.store.event("critical", "breaker", f"EMERGENCY STOP: {reason}", data)
    ctx.notify.send("Reddit engine stopped: nothing will post until you resume",
                    f"Reason: {reason}\nCheck the account, then press Resume on the status page.", "/status", level="critical")


def resume(ctx: Ctx, who: str = "jeet") -> None:
    was = halted(ctx.store)
    ctx.store.kv_del(HALT_KEY)
    ctx.store.event("warn", "breaker", f"resumed by {who}", was)


def sub_pause_state(store: Store, sub: str, now: datetime) -> tuple[bool, str]:
    p = store.policy(sub)
    if p and p.get("paused_until") and parse(p["paused_until"]) > now:
        return True, p.get("pause_reason") or "paused"
    return False, ""


def pause_sub(ctx: Ctx, sub: str, days: int, reason: str) -> None:
    ctx.store.pause_sub(sub, ctx.now() + timedelta(days=days), reason)
    ctx.store.event("warn", "breaker", f"r/{sub} paused {days} days: {reason}")
    ctx.notify.send(f"r/{sub} paused for {days} days", reason, "/status", level="warn")


def on_removal(ctx: Ctx, comment: dict) -> None:
    b = ctx.cfg.settings["breakers"]
    now = ctx.now()
    removed = ctx.store.comments(statuses=("removed",))
    in_sub = [c for c in removed if c["sub"].lower() == comment["sub"].lower()
              and c.get("removed_at") and parse(c["removed_at"]) >= now - timedelta(days=b["sub_removal_window_days"])]
    if len(in_sub) >= b["sub_removal_threshold"]:
        pause_sub(ctx, comment["sub"], b["sub_pause_days"],
                  f"{len(in_sub)} comments removed in {b['sub_removal_window_days']} days")
    recent = [c for c in removed if c.get("removed_at") and parse(c["removed_at"]) >= now - timedelta(hours=24)]
    if len(recent) >= b["global_removals_24h"]:
        trip(ctx, f"{len(recent)} comments removed across Reddit in 24 hours")


def on_forbidden(ctx: Ctx, sub: str, err: str) -> None:
    b = ctx.cfg.settings["breakers"]
    pause_sub(ctx, sub, b["sub_pause_days"], f"Reddit refused the post (403): {err}")
    hits = [h for h in ctx.store.kv_get("breaker:forbidden", []) if parse(h["at"]) >= ctx.now() - timedelta(hours=24)]
    hits.append({"sub": sub, "at": iso(ctx.now())})
    ctx.store.kv_set("breaker:forbidden", hits)
    if len({h["sub"].lower() for h in hits}) >= b["forbidden_subs_24h"]:
        trip(ctx, f"Reddit refused posts in {len(hits)} subreddits within 24 hours: possible account restriction")


# -- one-time import of the manual log ----------------------------------------------------------------
def bootstrap(ctx: Ctx) -> None:
    """Carry the manually posted comments (log/comments.jsonl) into the ledger so the
    limits and the Coach see the account's full history."""
    if ctx.store.kv_get("bootstrap:manual_log"):
        return
    n = 0
    path = ROOT / "log" / "comments.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("status") not in ("posted", "removed"):
                continue
            posted = datetime.fromisoformat(r["ts"]) if r.get("ts") else datetime.fromisoformat(r["date"]).replace(tzinfo=IST)
            tid = thread_id_from_url(r.get("thread_url", ""))
            ctx.store.record_comment(
                comment_id=f"manual_{r['id']}", kind="reply" if r.get("type") == "reply" else "comment",
                sub=r["sub"], thread_id=tid, thread_url=r.get("thread_url"), thread_title=r.get("thread_title"),
                text=r.get("text"), draft_text=r.get("text"), mention=int(bool(r.get("mentions_yuvgro"))),
                yuvgro_link=r.get("yuvgro_link"), fit_score=r.get("fit_score"), source="manual",
                posted_at=iso(posted), status=r["status"])
            if tid:
                ctx.store.mark_seen(tid, r["sub"], "commented", "manual session", "manual")
            n += 1
    acc = ROOT / "log" / "account.jsonl"
    if acc.exists():
        created = None
        for line in acc.read_text().splitlines():
            if line.strip():
                a = json.loads(line)
                created = a.get("created") or created
                if a.get("total_karma") is not None:
                    c_utc = datetime.fromisoformat(created).replace(tzinfo=IST).timestamp() if created else None
                    ctx.store.add_snapshot(ts=iso(datetime.fromisoformat(a["date"]).replace(tzinfo=IST)),
                                           total_karma=a["total_karma"], created_utc=c_utc)
    ctx.store.kv_set("bootstrap:manual_log", {"at": iso(ctx.now()), "comments": n})
    ctx.store.event("info", "bootstrap", f"imported {n} manually posted comments")


def mirror_to_manual_log(row: dict) -> None:
    """Keep log/comments.jsonl (read by scripts/reddit.py) in step with the engine."""
    path = ROOT / "log" / "comments.jsonl"
    try:
        existing = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
        posted = parse(row["posted_at"]).astimezone(IST)
        entry = {
            "id": (existing[-1]["id"] + 1) if existing else 1,
            "date": posted.date().isoformat(), "ts": posted.isoformat(timespec="minutes"),
            "sub": row["sub"], "thread_url": row.get("thread_url"), "thread_title": row.get("thread_title") or "",
            "type": {"comment": "value", "reply": "reply", "lead_reply": "reply", "post": "post"}.get(row["kind"], "value")
            if not row.get("mention") else "mention",
            "mentions_yuvgro": bool(row.get("mention")), "yuvgro_link": row.get("yuvgro_link"),
            "text": row.get("text"), "permalink": row.get("permalink", ""), "status": "posted",
            "fit_score": row.get("fit_score"), "note": "posted by engine",
        }
        with path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — the mirror is a convenience; the DB is the source of truth
        pass
