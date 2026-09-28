"""Critic gate 1: deterministic checks no prompt can argue with.

These encode playbook §1 (the non-negotiables). They run on every draft, and
again on Jeet's final text when he approves, so an edit can't slip a rule
either. Coach has no write access to this file or settings.json.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

HYPHENS = re.compile(r"[-‐-―−]")
URL = re.compile(r"https?://[^\s)\]>\"']+")
YUVGRO = re.compile(r"yuvgro", re.I)
SELF_REF = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|my|me|we|we've|our|YuvGro)\b", re.I)
CREDENTIAL = re.compile(
    r"\b(?:years?|clients?|customers?|accounts?|brands?|businesses|companies|rankings?|page[ ]one|page[ ]1|"
    r"growth|grew|revenue|results?|campaigns?|projects?|team|people|increase[sd]?|leads|ROI|ROAS|sales|experience)\b", re.I)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[+%]?")
CURRENCY = re.compile(r"(?:[$₹€£]|\b(?:A|C|CA|NZ|AU|US)\$)\s?\d|\d\s?(?:USD|INR|AUD|CAD|NZD|dollars|rupees)\b", re.I)
SELF_PRICE = re.compile(r"\b(?:YuvGro|I charge|my (?:rate|price|fee|pricing|retainer)|we charge|our (?:rate|price|fee|pricing|retainer)|I quote)\b", re.I)
DM_SOLICIT = re.compile(r"\b(?:DM me|send me a DM|message me|PM me|inbox me|DMs? (?:are|is) open|hit me up|reach out to me)\b", re.I)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass
class Violation:
    code: str
    detail: str
    hard: bool = True  # hard = must be fixed; soft = reported, writer may still pass

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class GateContext:
    kind: str                       # comment | reply | lead_reply | post
    mention_allowed: bool
    allowed_yuvgro_links: set[str]  # exact URLs this draft may carry (empty unless mention)
    neutral_domains: set[str]
    neutral_links_ok: bool
    history: list[str] = field(default_factory=list)
    min_words: int = 40
    max_words: int = 320
    blocked_terms: list[str] = field(default_factory=list)
    proof_numbers: set[str] = field(default_factory=set)
    disclosure_patterns: list[str] = field(default_factory=list)
    similarity_limit: float = 0.6


def strip_urls(text: str) -> str:
    return URL.sub("", text)


def hyphen_hits(text: str) -> list[str]:
    s = strip_urls(text)
    return [s[max(0, m.start() - 15):m.end() + 15].replace("\n", " ") for m in HYPHENS.finditer(s)]


def word_count(text: str) -> int:
    return len(strip_urls(text).split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().split(), b.lower().split()).ratio()


def max_similarity(text: str, history: list[str]) -> float:
    return max((similarity(text, h) for h in history if h), default=0.0)


def urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in URL.findall(text)]


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(text) if s.strip()]


def has_disclosure(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def self_claim_numbers(text: str, allowed: set[str]) -> list[str]:
    """Numbers in sentences that make claims about Jeet or YuvGro's track record.
    Advice numbers ("give it 7 days") pass: they sit in sentences without a
    credential word. Track-record numbers must be one of the approved proof points."""
    bad = []
    for s in sentences(strip_urls(text)):
        if SELF_REF.search(s) and CREDENTIAL.search(s):
            for m in NUMBER.findall(s):
                norm = m.replace(",", "").replace(" ", "")
                if norm and norm not in allowed:
                    bad.append(f"'{m.strip()}' in: {s.strip()[:120]}")
    return bad


def self_prices(text: str) -> list[str]:
    return [s.strip()[:120] for s in sentences(text) if SELF_PRICE.search(s) and CURRENCY.search(s)]


def gate1(text: str, ctx: GateContext) -> list[Violation]:
    v: list[Violation] = []
    t = (text or "").strip()
    if not t:
        return [Violation("empty", "draft is empty")]

    for h in hyphen_hits(t):
        v.append(Violation("hyphen", f"hyphen or dash near: …{h}…"))

    n = word_count(t)
    if n < ctx.min_words or n > ctx.max_words:
        v.append(Violation("length", f"{n} words, allowed {ctx.min_words} to {ctx.max_words}"))

    sim = max_similarity(t, ctx.history)
    if sim >= ctx.similarity_limit:
        v.append(Violation("too_similar", f"{sim:.2f} similarity to an earlier comment"))

    mentions = bool(YUVGRO.search(t))
    found = urls(t)
    yuvgro_links = [u for u in found if "yuvgro.com" in u.lower()]
    if (mentions or yuvgro_links) and not ctx.mention_allowed:
        v.append(Violation("mention_not_allowed", "YuvGro or yuvgro.com appears but this comment may not mention it"))
    if (mentions or yuvgro_links) and ctx.mention_allowed and not has_disclosure(t, ctx.disclosure_patterns):
        v.append(Violation("missing_disclosure", "names or links YuvGro without saying Jeet runs it"))

    for u in found:
        host = (urlparse(u).hostname or "").lower()
        if "yuvgro.com" in host:
            if u.rstrip("/") + "/" not in {x.rstrip("/") + "/" for x in ctx.allowed_yuvgro_links}:
                v.append(Violation("link_not_allowed", f"{u} is not the approved link for this comment"))
        elif not (ctx.neutral_links_ok and host in ctx.neutral_domains):
            v.append(Violation("link_not_allowed", f"{u} (links allowed now: {'neutral sources only' if ctx.neutral_links_ok else 'none'})"))

    for s in self_claim_numbers(t, ctx.proof_numbers):
        v.append(Violation("self_number", f"track record number that is not an approved proof point: {s}"))
    for s in self_prices(t):
        v.append(Violation("self_price", f"quotes YuvGro's own price: {s}"))
    if ctx.kind != "lead_reply" and DM_SOLICIT.search(t):
        v.append(Violation("dm_solicit", "asks people to DM; only allowed when they asked to talk"))
    low = t.lower()
    for term in ctx.blocked_terms:
        if term and term.lower() in low:
            v.append(Violation("blocked_term", f"contains a blocked name: {term}"))
    return v


def describe(violations: list[Violation]) -> str:
    return "\n".join(f"- {x}" for x in violations)
