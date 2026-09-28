"""The Writer <-> Critic loop, shared by the daily run, replies and lead replies.

At most `max_revisions` revisions; anything still failing is killed and
logged with the Critic's notes (Coach learns from kills).
"""
from __future__ import annotations

from . import critic, writer


def write_and_review(ctx, brief: dict, thread, context: dict | None, kind: str) -> dict:
    """Returns {"status": "pass"|"kill", "draft", "review", "revisions"}."""
    max_rev = ctx.cfg.settings["drafting"]["max_revisions"]
    d = writer.draft(ctx, brief, thread, reply_context=context)
    r = None
    for rnd in range(max_rev + 1):
        r = critic.review(ctx, brief, thread, d, kind=kind, reply_context=context)
        if r["status"] == "pass":
            return {"status": "pass", "draft": d, "review": r, "revisions": rnd}
        if r["status"] == "kill" or rnd == max_rev:
            break
        d = writer.draft(ctx, brief, thread, reply_context=context, previous=d["text"], critic_notes=r["notes"])
    ctx.store.event("info", "critic", f"killed a {kind} draft in r/{thread.sub}",
                    {"thread": thread.id, "notes": r["notes"] if r else "", "gate1": r["gate1"] if r else [],
                     "last_draft": d["text"]})
    return {"status": "kill", "draft": d, "review": r, "revisions": max_rev}
