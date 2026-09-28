"""Critic: gate 1 in code (checks.py), gate 2 an independent Claude review.

Gate 2 starts fresh: it never sees the Writer's reasoning, only the brief, the
thread and the draft. A draft passes only when gate 1 is clean AND gate 2
passes with no factual concerns, no client identification, the question
answered without the link, and mod removal risk below high.
"""
from __future__ import annotations

from .. import checks
from ..llm import B, S, arr, enum, obj

REVIEW_SCHEMA = obj({
    "answers_without_link": B,
    "fills_gap": B,
    "ai_tells": arr(S),
    "factual_concerns": arr(S),
    "identifies_client": B,
    "mod_removal_risk": enum("low", "medium", "high"),
    "verdict": enum("pass", "revise", "kill"),
    "notes": S,
})


def gate_context(ctx, brief: dict, kind: str) -> checks.GateContext:
    cfg, s = ctx.cfg, ctx.cfg.settings
    reply = kind in ("reply", "lead_reply")
    talking = brief.get("comment_type") == "talking_points"
    history = [c["text"] for c in ctx.store.comments() if c.get("text")]
    return checks.GateContext(
        kind=kind,
        mention_allowed=brief.get("comment_type") == "mention",
        allowed_yuvgro_links={brief["yuvgro_link"]} if brief.get("yuvgro_link") else set(),
        neutral_domains=set(s["links"]["neutral_domains"]),
        neutral_links_ok=bool(brief.get("neutral_links_allowed")),
        history=history,
        min_words=s["drafting"]["reply_min_words"] if (reply or talking) else s["drafting"]["min_words"],
        max_words=s["drafting"]["reply_max_words"] if reply else s["drafting"]["max_words"],
        blocked_terms=s.get("blocked_terms", []),
        proof_numbers=cfg.proof_numbers(),
        disclosure_patterns=s["disclosure_patterns"],
    )


def review(ctx, brief: dict, thread, draft: dict, kind: str = "comment", reply_context: dict | None = None) -> dict:
    gctx = gate_context(ctx, brief, kind)
    violations = checks.gate1(draft["text"], gctx)
    if any(v.code == "empty" for v in violations):
        return {"status": "kill", "gate1": [str(v) for v in violations], "review": None, "notes": "empty draft"}

    payload = {
        "brief": brief,
        "thread": {"sub": thread.sub, "title": thread.title, "body": thread.body[:6000],
                   "top_comments": thread.to_dict()["comments"]},
        "draft": draft["text"],
        "claims_made": draft.get("claims", []),
        "code_check_findings": [str(v) for v in violations],
    }
    if reply_context:
        payload["reply_context"] = reply_context
    r = ctx.llm.structured("critic", payload, REVIEW_SCHEMA)

    problems = [str(v) for v in violations]
    if not r["answers_without_link"] and brief.get("comment_type") != "talking_points":
        problems.append("does not fully answer the question without the link")
    if not r["fills_gap"]:
        problems.append("repeats what the thread already says instead of adding the gap")
    problems += [f"AI tell: {x}" for x in r["ai_tells"]]
    problems += [f"factual concern: {x}" for x in r["factual_concerns"]]
    if r["identifies_client"]:
        problems.append("could identify a client, employer or project")
    if r["mod_removal_risk"] == "high":
        problems.append("high risk that moderators remove it")

    if r["verdict"] == "kill":
        status = "kill"
    elif problems or r["verdict"] == "revise":
        status = "revise"
    else:
        status = "pass"
    notes = "\n".join(f"- {p}" for p in problems)
    if r["notes"]:
        notes += ("\n" if notes else "") + r["notes"]
    return {"status": status, "gate1": [str(v) for v in violations], "review": r, "notes": notes}


def final_check(ctx, job: dict, text: str) -> list[str]:
    """Run gate 1 on the exact text about to be posted (Jeet's edit included).
    Length is advisory for Jeet's own words; every other rule is enforced."""
    brief = job.get("brief") or {}
    gctx = gate_context(ctx, brief, job["kind"])
    gctx.history = [h for h in gctx.history if h != text]
    return [str(v) for v in checks.gate1(text, gctx) if v.code != "length"]
