"""Closer: buying intent and DMs.

Public replies showing buying intent get a short "happy to, send me a DM"
reply job (through Critic and Jeet's approval). DMs the other person started
get a drafted answer that is NEVER sent automatically: it waits on the
dashboard for Jeet to send himself. Pricing only ever comes from Jeet.
Leads land in Notion (Company.Source = Reddit) and ping Jeet.
"""
from __future__ import annotations

from ..llm import S, enum, obj
from ..reddit_io import RedditError
from ..store import iso
from . import drafting, publisher

LEAD_SCHEMA = obj({
    "intent_strength": enum("low", "medium", "high"),
    "business_name": S,
    "website": S,
    "country": S,
    "role": S,
    "need_summary": S,
    "icp_segment": S,
    "suggested_dm_reply": S,
})


def handle(ctx, item, verdict: dict, dm_only: bool = False) -> None:
    cfg, store = ctx.cfg, ctx.store
    thread = None
    if item.thread_id:
        try:
            thread = ctx.reddit.thread(item.thread_id, top_n=3)
        except RedditError:
            thread = None
    our = store.comment_by_reddit_id(item.parent_id[3:]) if item.parent_id.startswith("t1_") else None
    payload = {
        "kind": item.kind, "author": item.author, "subject": item.subject, "message": item.body[:3000],
        "our_comment": our["text"] if our else "", "thread_title": thread.title if thread else "",
        "icp_segment_options": cfg.base["notion"]["icp_segment_options"],
        "countries_we_map": sorted(cfg.base["notion"]["region_map"].keys()),
        "booking_link": cfg.base.get("booking_link", ""),
    }
    lead = ctx.llm.structured("closer", payload, LEAD_SCHEMA)
    thread_url = thread.url if thread else f"https://www.reddit.com/message/messages/{item.id[3:]}"

    notion = {}
    if not dm_only or lead["intent_strength"] != "low":
        try:
            notion = ctx.notion.write_lead(
                handle=item.author, thread_url=thread_url, asked=verdict.get("summary") or lead["need_summary"],
                company=lead["business_name"], website=lead["website"], country=lead["country"],
                segment=lead["icp_segment"], pain=lead["need_summary"])
        except Exception as e:  # noqa: BLE001 — a CRM failure must never lose the lead
            store.event("warn", "closer", f"Notion write failed for u/{item.author}: {e}")
        store.add_lead(handle=item.author, thread_url=thread_url, inbox_id=item.id, intent=lead["intent_strength"],
                       details=lead, notion_company=notion.get("company_id"), notion_contact=notion.get("contact_id"),
                       created_at=iso(ctx.now()))

    # DM drafts: parked for Jeet, never posted.
    if item.kind == "message":
        store.create_job(kind="dm_draft", dedupe_key=f"dm:{item.id}", thread_id=item.thread_id or None,
                         sub=item.subreddit or "", thread_title=item.subject, thread_url=thread_url,
                         comment_type="dm", draft_text=lead["suggested_dm_reply"], status="manual",
                         brief={"from": item.author, "message": item.body[:3000], "lead": lead}, inbox_id=item.id)
    elif thread and item.comment_id:
        brief = {"thread_id": thread.id, "sub": thread.sub, "title": thread.title, "url": thread.url,
                 "question": "They asked to talk or to hire help. Reply briefly and invite them to send a DM. "
                             "No pitch, no prices, no links.",
                 "already_covered": [], "gap_angle": "", "comment_type": "lead_reply", "yuvgro_link": None,
                 "posting_mode": "value_only", "sub_policy": "", "neutral_links_allowed": False,
                 "target_words": 25, "fit_score": None, "phase": "", "topic": "lead", "source": "inbox"}
        context = {"our_comment": our["text"] if our else "", "their_reply": item.body, "their_username": item.author}
        res = drafting.write_and_review(ctx, brief, thread, context, "lead_reply")
        # A lead is worth a reply even if the Critic still has notes: Jeet sees them on the card,
        # and his final text goes through the code checks before anything posts.
        publisher.enqueue(ctx, kind="lead_reply", dedupe_key=f"reply:{item.comment_id}", brief=brief,
                          draft=res["draft"], review=res["review"], thread=thread, parent_id=item.comment_id,
                          inbox_id=item.id, revisions=res["revisions"])

    ctx.notify.send(f"Possible lead on Reddit: u/{item.author} ({lead['intent_strength']} intent)",
                    f"{lead['need_summary']}\n{('Business: ' + lead['business_name']) if lead['business_name'] else ''}".strip(),
                    "/", level="lead")
