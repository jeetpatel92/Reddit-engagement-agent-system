"""Writer: turns a brief into a comment in Jeet's voice.

It sees the brief, the thread and its top comments, plus the cached house pack
(playbook, experience bank, learned notes, approved exemplars). On a revision
it also gets its previous draft and the Critic's notes. First person stories
may only come from the experience bank; there is no human editing live, so
when no story fits it writes without one instead of leaving a placeholder.
"""
from __future__ import annotations

from ..llm import S, arr, obj

DRAFT_SCHEMA = obj({"text": S, "story_id": S, "claims": arr(S), "link_used": S})


def draft(ctx, brief: dict, thread, *, reply_context: dict | None = None,
          previous: str | None = None, critic_notes: str | None = None) -> dict:
    payload = {
        "brief": brief,
        "thread": {"sub": thread.sub, "title": thread.title, "body": thread.body[:6000],
                   "top_comments": thread.to_dict()["comments"]},
    }
    if reply_context:
        payload["reply_context"] = reply_context
    if previous:
        payload["revision"] = {"previous_draft": previous, "critic_notes": critic_notes or ""}
    out = ctx.llm.structured("writer", payload, DRAFT_SCHEMA)
    out["text"] = out["text"].strip()
    return out
