# YuvGro Reddit Engagement

Built 2026-09-25. Daily, human-posted Reddit engagement from **a founder's personal Reddit account**. The aim is to be genuinely useful in the threads YuvGro's buyers read. Most comments never mention YuvGro. The rare ones that do say plainly that Jeet runs it.

Why bother: Reddit is the most-cited domain in ChatGPT answers, and the threads that rank for "[keyword] reddit" feed AI answers directly (`Internal/SOP_Reddit_Modifier_Interception_GEO.pdf`). A credible presence there is both a lead source and GEO work.

**Start a session:** say **"reddit daily"** (runs `.claude/skills/reddit-daily/SKILL.md`). Best time is 22:00–23:30 IST.

**Automated engine (built 2026-09-29):** `engine/` runs the same rules end to end with nine agents, leaving Jeet one tap per comment on a dashboard. It needs Reddit API access and Claude API credit first; setup is in [engine/README.md](engine/README.md). Until then, the manual session above is the way to post.

---

## How a session works

```
Claude                                   Jeet (on Reddit)
──────                                   ────────────────
status + karma check          ◄────────  karma number from profile
drafts replies to inbox       ◄────────  pastes new replies / DMs
today's search links          ────────►  skims, pastes ≤10 threads back
asks for unverified sub rules ◄────────  pastes the Rules panel
scores, drafts 3–5 comments   ────────►  approves / edits
posting order, 5 min apart    ────────►  posts each one himself
logs what was posted          ◄────────  "posted 1, 3, 4" (+ links)
report
```

**Why Jeet posts and Claude doesn't** (verified 2026-09-25): reddit.com is blocked for both Claude in Chrome and the app's built-in browser ("not allowed due to safety restrictions"). Reddit also serves a bot challenge to direct requests and blocks Claude's web search. The agent never tries to get around any of this. The upside is that every comment is typed by a person, which is what Reddit wants anyway.

---

## One-time account setup (Jeet, ~15 min, before the first session)

Claude can't change Reddit settings for you, so these are yours:

1. **The username.** Reddit's auto generated usernames read as throwaways; if yours is one, fix it while the account is new. Check Settings → Account for a one-time username change (Reddit has offered this for auto-generated names). If it isn't offered, a new account with a real handle costs nothing today. If you switch, update `config.json → account`.
2. **Verify the email** (Settings → Account). Unverified new accounts get filtered more.
3. **Profile = disclosure.** Keep it within Reddit's 200 characters:
   - Display name: your real name
   - About: one line saying who you are and what you run, and that you are there to help rather than sell
   - Social link: Custom URL `https://yuvgro.com`, label `YuvGro`

   Anyone who clicks your name then knows who you are, which is what keeps the warm-up comments honest too.
4. **Avatar:** your logo, square and sized to sit inside Reddit's circular crop. A real headshot of you works even better on a personal account if you have one.
5. **Two-factor auth:** recommended.
6. **Join the warm-up subs** (`config.json → phases.warmup.prefer_subs`) and spend a day or two reading each one's top posts of the month. That's how you learn what gets upvoted there.
7. **Never:** use a second account to upvote yourself, ask anyone to upvote, or hop between VPN locations. Any of these can get an account suspended.

---

## The rules in one screen

| | Phase 1 — Warm-up | Phase 2 — Established |
|---|---|---|
| When | From 2026-09-25 | ≥ 14 days old **and** ≥ 100 karma (earliest: day 14) |
| YuvGro mentions | None | ≤ 10% of last 30 comments · ≤ 1/day · ≤ 1 per sub per 14 days |
| yuvgro.com links | None | ≤ 1 a week, blog posts only, always disclosed |
| Original posts | No | ≤ 1 a week, no link in body |

Always, in every phase: Jeet's own voice ("I"). No invented stories. No client or project names. Only the four yuvgro.com/about figures. No YuvGro prices in public. Recommend competitors when they're the better fit, never criticise them. No cold DMs. Sub rules win. Full detail in `playbook.md`.

---

## Files

| File | What it is |
|---|---|
| `config.json` | Every number and list: phases, caps, topic rotation, search queries, blog-link map, Notion IDs |
| `playbook.md` | Judgement: non-negotiables, thread scoring, voice, disclosure, replies, leads |
| `subreddits.md` | 30-sub roster + rules cache. **All unverified** until Jeet pastes each sub's rules |
| `experience-bank.md` | The only source of first-person stories. Grows as Jeet adds real ones |
| `scripts/reddit.py` | `pack` · `status` · `karma` · `dupcheck` · `add` · `mark` (stdlib only, no network) |
| `log/comments.jsonl` | Every posted and removed comment: the source of truth for the promo ratio |
| `log/account.jsonl` | Karma snapshots |

**Notion:** `Reddit` was added to Companies → Source on 2026-09-25, with the four existing options kept. Contacts has no Source field, so Reddit leads carry `Source: Reddit — <thread> — u/<handle>` in Response Notes. Only people showing buying intent are written to the CRM.

---

## Decisions (Jeet, 2026-09-25)

Posting: Claude drafts, Jeet approves and posts (originally chose "Claude posts via Chrome", changed because Reddit is blocked for Claude) · Cadence: daily, 3–5 comments, 22:00–23:30 IST · Voice: "I", natural founder disclosure · Mix: mostly value-only · Warm-up gate: 2 weeks AND ~100 karma · Scope: all services, all audiences (incl. mid-market, e-commerce, enterprise owners), global English-language subs only · Proof: experience first, numbers only when needed · Links: most relevant blog post, never homepage · Posts: comments only during warm-up, then ≤1 a week · Replies: in-thread drafted, DMs only if they start it · Logging: local log + Notion for leads · Pricing: market ranges, never ours · Competitors: may recommend, never trash · Off-limits: client and project names.

## Known gaps

- Every subreddit rule is unverified until the first time a thread comes from that sub.
- No blog post yet covers CRM/ERP, automation, paid ads or e-commerce platforms, so those threads can't carry a link. The session report collects these as blog ideas.
- Discovery depends on Jeet skimming ~7 links a day. If that becomes the bottleneck, a keyword-alert service that emails new Reddit matches to Gmail (e.g. F5Bot) could feed the session through the Gmail connector. Not set up or tested.
