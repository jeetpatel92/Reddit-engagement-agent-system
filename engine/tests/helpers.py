"""Test harness: fresh temp store, FakeIO on the fixture, a scripted FakeLLM,
a movable clock, and no writes to the real tunables/exemplars/notes files."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ygr.config import load
from ygr.core import Ctx
from ygr.llm import FakeLLM
from ygr.notify import Notifier
from ygr.notion import NotionIO
from ygr.reddit_io import FakeIO, InboxItem
from ygr.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "reddit_fixture.json"

GOOD_TEXT = {
    "fxA": "Leave it running for now. One good day and one flat day at this budget is not enough data to judge anything. Decide in advance how much each creative may spend without a sale, write that number down, and only act when a creative crosses it. Until sales pick up, compare the creatives on cost per add to cart rather than on purchases.",
    "fxB": "Stop judging every ad on the same day three clock. Look at your past trials and work out what share of eventual sales usually shows up by day 3, 7 and 14, then compare each ad against that curve instead of against zero. If you also let a small random slice of the ads you would have killed keep running on a tiny budget, you get a real measure of how often the rule is wrong.",
    "fxC": "Before changing bids, check whether calls are actually being answered. Pull the call log by location and by hour; a few locations missing calls at busy times will make any campaign look broken. Then make sure the ads and call extensions only run in the hours each location picks up, and judge every location on cost per booked job rather than cost per call.",
    "fxD": "Before splitting anything, check two numbers in the current setup. First, whether brand terms are excluded from Performance Max, because brand searches flatter its return. Second, how much of its revenue comes from existing customers, which matters a lot for refills and repeat buyers. Those answers usually tell you whether a split would add anything.",
    "fxE": "Start with the free stuff on your Google profile. Set the exact hours for each night, add photos of the menu board, post your location every evening, and ask regulars for a review while they wait. Once that is in place, a tiny search campaign that only runs from 8pm to 2am near your spot is worth testing, because that is when the big chains are closed.",
    "fxF": "Usually yes for a new plumber, because you pay for leads rather than clicks and you can dispute ones that are clearly not real jobs. Keep the weekly budget small, make sure your hours and service area are accurate, and answer every call quickly, since how fast you respond and your reviews both affect where you show up. Ask every early customer for a review.",
}


class Script:
    """Scripted FakeLLM behaviour. Tests tweak attributes to steer it."""

    def __init__(self):
        self.op_asked = False
        self.critic_verdict = "pass"
        self.writer_override: dict[str, str] = {}
        self.mention_text = None
        self.promo_subs: set[str] = set()

    def __call__(self, role: str, payload):
        if role == "screen":
            return {"items": [{"id": t["id"], "keep": not any(x in t["title"] for x in ("Looking for projects", "Cambiare")),
                               "reason": "test"} for t in payload["threads"]]}
        if role == "rules":
            sub = payload["subreddit"]
            ai = any("human" in r["name"].lower() for r in payload["rules"])
            promo = "allowed_with_disclosure" if sub in self.promo_subs else "banned"
            return {"self_promotion": promo, "links": "not_mentioned", "ai_content_banned": ai, "min_karma": -1,
                    "min_account_age_days": -1, "promo_threads": [], "summary": f"rules for {sub}"}
        if role == "analyst":
            return {"assessments": [{
                "id": c["id"], "question": f"What should they do about {c['title'][:40]}?", "expertise": 3, "gap": 2,
                "audience": 2, "already_covered": [], "gap_angle": "the missing angle",
                "op_asked_for_recommendation": self.op_asked, "link_topic": "seo_general", "skip_reason": ""}
                for c in payload["candidates"]]}
        if role == "writer":
            b = payload["brief"]
            tid = b["thread_id"]
            if b["comment_type"] == "mention" and self.mention_text:
                text = self.mention_text
            elif b["comment_type"] == "talking_points":
                text = "1. Price by meeting quality, not company size.\n2. Add a show rate guarantee.\n3. Move enterprise to retainer plus success fee."
            elif b["comment_type"] in ("reply", "lead_reply"):
                text = ("Happy to, send me a DM with your site and what you have tried so far."
                        if b["comment_type"] == "lead_reply" else
                        "Good question. Check the search terms report first, then add the obvious negatives before touching bids.")
            else:
                text = self.writer_override.get(tid) or GOOD_TEXT.get(tid) or f"Specific help for thread {tid}. " * 12
            return {"text": text, "story_id": "", "claims": [], "link_used": b.get("yuvgro_link") or ""}
        if role == "critic":
            return {"answers_without_link": True, "fills_gap": True, "ai_tells": [], "factual_concerns": [],
                    "identifies_client": False, "mod_removal_risk": "low", "verdict": self.critic_verdict, "notes": ""}
        if role == "triage":
            out = []
            for i in payload["items"]:
                body = i["body"].lower()
                cat = "buying_intent" if ("hire" in body or "price" in body) else "question" if "?" in body else "thanks"
                out.append({"id": i["id"], "category": cat, "needs_reply": cat != "thanks", "summary": i["body"][:60]})
            return {"items": out}
        if role == "closer":
            return {"intent_strength": "high", "business_name": "Test Plumbing", "website": "", "country": "United States",
                    "role": "owner", "need_summary": "wants more calls", "icp_segment": "📍 Local Business",
                    "suggested_dm_reply": "Thanks for reaching out. Before I suggest anything, a couple of questions."}
        if role == "coach":
            return {"digest": "Test digest.", "proposals": [
                {"kind": "learned_note", "title": "Open with the fix", "detail": "Open with the fix.", "evidence": "edits",
                 "sub": "", "topic": ""},
                {"kind": "new_sub", "title": "Add r/PPC", "detail": "Practitioner questions", "evidence": "x", "sub": "r/PPC",
                 "topic": "paid_social_orm"}], "new_queries": [{"topic": "ai_search", "query": "chatgpt citations"}]}
        raise AssertionError(f"unexpected role {role}")


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def make_ctx(start: datetime | None = None, karma: int = 1, created_days_ago: int = 3, budget=(10**9, 10**9)):
    tmp = Path(tempfile.mkdtemp(prefix="ygr-test-"))
    os.environ["YGR_DATA_DIR"] = str(tmp)
    for k in ("SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "NOTION_TOKEN", "PUBLIC_URL"):
        os.environ.pop(k, None)
    cfg = load(dry_run=False)
    cfg.save_tunables = lambda: None
    cfg.save_exemplars = lambda: None
    notes = []
    cfg.append_learned_note = lambda t, d: notes.append((t, d))
    store = Store(tmp / "t.sqlite")
    fixture = json.loads(FIXTURE.read_text())
    reddit = FakeIO(fixture)
    clock = Clock(start or datetime(2026, 10, 2, 16, 35, tzinfo=timezone.utc))  # Friday 22:05 IST: paid, social & ORM day
    created = clock() - timedelta(days=created_days_ago)
    reddit.data["me"] = {"name": "test_user", "total_karma": karma, "comment_karma": karma, "link_karma": 0,
                         "created_utc": created.timestamp()}
    script = Script()
    llm = FakeLLM(script, budget)
    notifier = Notifier(store, env={})
    notifier.send = lambda *a, **k: (notifier.sent.append({"title": a[0], "level": k.get("level", "info")}),
                                     store.event("info", "notify", a[0]))
    ctx = Ctx(cfg=cfg, store=store, reddit=reddit, llm=llm, notify=notifier, notion=NotionIO(cfg, store, env={}), clock=clock)
    # Fixture ages are relative to real time; make them relative to the test clock.
    now_ts = clock().timestamp()
    import time as _t
    shift = now_ts - _t.time()
    for t in reddit.threads.values():
        t.created_utc += shift
    return ctx, script, clock, reddit, notes


def inbox_item(**kw) -> InboxItem:
    base = dict(id="t1_in1", kind="comment_reply", author="someone", body="?", created_utc=0.0,
                thread_id="fxC", parent_id="t1_fake1", comment_id="in1", subject="comment reply", subreddit="googleads")
    base.update(kw)
    return InboxItem(**base)
