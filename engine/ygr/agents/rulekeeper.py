"""Rulekeeper: attaches each subreddit's current policy to its candidates.

Rules are fetched through the API at most every `rules_refresh_days`. When the
rule text's hash changes, Claude re-reads it into policy fields; a change in a
sub we've already posted in is flagged to Jeet. The hand-verified seed policies
are a floor: a live read can make a sub stricter, never looser.

The posting mode is decided in code from the policy:
  blocked         paused, below the sub's karma/age minimum, or held by the seed
  talking_points  the sub bans AI-written content: Jeet writes the words
  value_only      self promotion banned: never mention YuvGro here
  normal
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from ..core import sub_pause_state
from ..llm import B, I, S, arr, enum, obj
from ..reddit_io import RedditError
from ..store import parse

POLICY_SCHEMA = obj({
    "self_promotion": enum("banned", "designated_threads_only", "allowed_with_disclosure", "not_mentioned"),
    "links": enum("banned", "restricted", "allowed", "not_mentioned"),
    "ai_content_banned": B,
    "min_karma": I,
    "min_account_age_days": I,
    "promo_threads": arr(S),
    "summary": S,
})

STRICTNESS = {"self_promotion": ["not_mentioned", "allowed_with_disclosure", "designated_threads_only", "banned"],
              "links": ["not_mentioned", "allowed", "restricted", "banned"]}


def _merge_with_seed(live: dict, seed: dict | None) -> dict:
    if not seed:
        return live
    out = dict(live)
    for k, order in STRICTNESS.items():
        a, b = live.get(k, "not_mentioned"), seed.get(k, "not_mentioned")
        out[k] = a if order.index(a) >= order.index(b) else b
    out["ai_content_banned"] = bool(live.get("ai_content_banned")) or bool(seed.get("ai_content_banned"))
    for k in ("min_karma", "min_account_age_days"):
        out[k] = max(int(live.get(k, -1)), int(seed.get(k, -1)))
    if seed.get("hold_until_karma"):
        out["hold_until_karma"] = seed["hold_until_karma"]
    return out


def mode_for(policy: dict, karma: int | None, age_days: int, paused: bool) -> tuple[str, str]:
    k = karma or 0
    if paused:
        return "blocked", "paused"
    if policy.get("min_karma", -1) > 0 and k < policy["min_karma"]:
        return "blocked", f"needs {policy['min_karma']} karma"
    if policy.get("min_account_age_days", -1) > 0 and age_days < policy["min_account_age_days"]:
        return "blocked", f"needs account age {policy['min_account_age_days']} days"
    if policy.get("hold_until_karma") and k < policy["hold_until_karma"]:
        return "blocked", f"held until {policy['hold_until_karma']} karma (heavy anti bot tooling)"
    if policy.get("ai_content_banned"):
        return "talking_points", "sub bans AI written content"
    if policy.get("self_promotion") in ("banned", "designated_threads_only"):
        return "value_only", "self promotion banned"
    return "normal", ""


def policy_for(ctx, sub: str) -> dict:
    cfg, store = ctx.cfg, ctx.store
    seed = next((v for k, v in cfg.seed_policies.items() if k.lower() == sub.lower()), None)
    cached = store.policy(sub)
    fresh = cached and cached.get("fetched_at") and cached.get("policy") and \
        parse(cached["fetched_at"]) > ctx.now() - timedelta(days=cfg.settings["rules_refresh_days"]) and \
        cached.get("source") != "seed"
    if fresh:
        return cached["policy"]
    try:
        raw = ctx.reddit.rules(sub)
    except RedditError as e:
        store.event("warn", "rulekeeper", f"could not read rules for r/{sub}: {e}")
        if cached and cached.get("policy"):
            return cached["policy"]
        if seed:
            store.set_policy(sub, seed, None, "seed")
            return seed
        # Unknown and unreadable: treat as strict.
        return {"self_promotion": "banned", "links": "banned", "ai_content_banned": False,
                "min_karma": -1, "min_account_age_days": -1, "promo_threads": [], "summary": "rules unreadable: strict"}
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    if cached and cached.get("rules_hash") == digest and cached.get("policy"):
        store.set_policy(sub, cached["policy"], digest, "api")  # refresh fetched_at only
        return cached["policy"]
    live = ctx.llm.structured("rules", {"subreddit": sub, "rules": raw["rules"], "sidebar": raw["sidebar"][:5000]}, POLICY_SCHEMA)
    policy = _merge_with_seed(live, seed)
    changed = bool(cached and cached.get("rules_hash") and cached["rules_hash"] != digest)
    store.set_policy(sub, policy, digest, "api")
    if changed:
        store.event("warn", "rulekeeper", f"r/{sub} changed its rules", {"old": cached.get("policy"), "new": policy})
        if any(c["sub"].lower() == sub.lower() for c in store.comments()):
            ctx.notify.send(f"r/{sub} changed its rules", policy.get("summary", ""), "/status", level="warn")
    return policy


def annotate(ctx, threads: list, account: dict) -> list[tuple]:
    """Returns [(thread, policy, mode, mode_reason)], dropping blocked subs."""
    out = []
    cache: dict[str, tuple] = {}
    for t in threads:
        key = t.sub.lower()
        if key not in cache:
            policy = policy_for(ctx, t.sub)
            paused, why = sub_pause_state(ctx.store, t.sub, ctx.now())
            mode, reason = mode_for(policy, account["karma"], account["age_days"], paused)
            cache[key] = (policy, mode, why or reason)
        policy, mode, reason = cache[key]
        if mode == "blocked":
            ctx.store.mark_seen(t.id, t.sub, "blocked_sub", reason, t.source)
            continue
        out.append((t, policy, mode, reason))
    return out
