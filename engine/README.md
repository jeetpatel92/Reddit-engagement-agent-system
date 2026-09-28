# YuvGro Reddit Engine

Nine agents that run Reddit engagement for your Reddit account end to end. Jeet's only job is **one tap per comment** on a phone-friendly dashboard. Everything else runs on its own: finding threads, reading subreddit rules, drafting, quality control, posting, monitoring, replies, CRM and learning.

Built 2026-09-29. Same rules as the manual `/reddit-daily` flow (`../playbook.md`, `../config.json`). The engine enforces them in code rather than relying on a prompt.

## The agents

| # | Agent | Kind | Receives | Decides | Passes on |
|---|---|---|---|---|---|
| 0 | **Conductor** `agents/conductor.py` | code | 22:00 IST trigger, state | go/no-go, phase, limits, today's topic, subs, queries, budget | run brief → Scout |
| 1 | **Scout** `agents/scout.py` | code + Claude (low effort) | run brief | drops seen/locked/NSFW/stale/crowded/non-English; screens for a real question | ≤15 threads with top comments |
| 2 | **Rulekeeper** `agents/rulekeeper.py` | code + Claude (low effort) | subs in play | reads rules (API, weekly); mode: normal / value_only / talking_points / blocked | threads + policy |
| 3 | **Analyst** `agents/analyst.py` | Claude + code | threads + limits | Claude scores expertise, gap, audience; **code** picks 3–5 and decides any YuvGro mention | one brief per pick |
| 4 | **Writer** `agents/writer.py` | Claude (high effort) | brief + house pack | wording; stories only from the experience bank | draft + claims list |
| 5 | **Critic** `agents/critic.py` + `checks.py` | code + independent Claude | draft, brief, policy | pass / revise (≤2) / kill | approved drafts → Publisher |
| 6 | **Publisher** `agents/publisher.py` | code | approved drafts | pacing (≥5 min, jitter, alternate subs); posts only after **Approve** | posting record → ledger + Watcher |
| 7 | **Watcher** `agents/watcher.py` | code + Claude (low effort) | ledger, inbox | removals, shadowban, karma trend; sorts replies | reply jobs, leads → Closer, mod mail → stop |
| 8 | **Closer** `agents/closer.py` | code + Claude | buying intent, DMs | intent, fit, region | Notion Company (Source = Reddit) + Contact; DM draft for Jeet |
| 9 | **Coach** `agents/coach.py` | code + Claude, weekly | outcomes, Jeet's edits, kills | bounded tuning; proposals | tunables.json, example banks, digest |

The shared Writer ⇄ Critic loop is in `agents/drafting.py`. Everything is persisted in SQLite (`store.py`), so a crash or redeploy resumes exactly where it stopped.

## What runs by itself vs. what's Jeet's

**By itself:** discovery, rules, drafting, quality control, scheduling, posting after approval, checking visibility at 1h, 24h, 72h and 7d, account health, reply drafting, Notion leads, weekly learning, safety stops.

**Jeet's, by design:**
1. **One tap per comment or reply** (Approve, Edit or Skip). Everything goes out under his name, so nothing posts without it. Unapproved drafts expire after 12 hours.
2. **Talking points**: in subs that ban AI writing (r/Entrepreneur rule 4), he writes the words himself.
3. **DMs and prices**: drafted for him, never sent automatically.
4. **Resume** after an emergency stop.
5. **Coach's proposals**: new subs, style notes, blog ideas.

## Safety guarantees (all tested)

- **No hyphens, disclosure, allowed links, proof points, no self pricing, no DM asks, blocked names**: `checks.py` enforces these on every draft *and* on Jeet's edited text before it posts.
- **Phase 1**: code refuses any YuvGro mention or link. In phase 2, mentions stay at ≤10% of the last 30 comments, ≤1 a day, and ≤1 per sub every 14 days. Code decides this, not the model.
- **One comment per thread, ever**: a unique database key. A crash in the middle of a post is checked against Reddit before any retry.
- **Limits count drafts that are waiting too**, so approving a batch can't push past a cap.
- **Emergency stop** (manual resume) on: mod or admin mail, the profile invisible when logged out, suspension, karma falling ≥10 in 3 days, ≥3 removals in 24h, Reddit refusing posts in 2 subs within 24h, rejected credentials.
- **Per-sub pause for 30 days** after 2 removals in 14 days, or a 403 on posting.
- **Spending cap per run**, a **"no successful run in 48 hours" alert**, 3 consecutive failures of a job → alert.
- **Coach can't touch** `checks.py`, `settings.json` or the prompts. It writes only `tunables.json` (bounded ±20%/week, reverted automatically if results drop), the example banks, and proposals. Approved style notes must pass the eval before the Writer sees them.

## Setup (one time)

1. **Reddit API app.** Signed in as your Reddit account, create an app at reddit.com/prefs/apps: type *web app*, redirect URI `http://localhost:8080`. Reddit reviews new Data API use, so follow its prompts. Describe the use honestly: one personal account, every comment human-approved, low volume. Copy the client id and secret.
2. **Refresh token** (so the engine never holds your password). Put the id and secret in `engine/.env`, then run:
   ```bash
   cd reddit-engagement/engine && ../.venv/bin/python -m ygr reddit-auth
   ```
   Open the link, press Allow, and copy the printed `REDDIT_REFRESH_TOKEN`.
3. **Claude credit.** The daily run stops cleanly with a "credit balance is too low" alert until it's topped up.
4. **Notifications.** A Slack incoming webhook and/or a Telegram bot token plus chat id.
5. **Notion.** Create an integration, share the Companies and Contacts databases with it, and set `NOTION_TOKEN`. Without it, leads are logged and the dashboard shows them "not in Notion".
6. **Railway.** New service from this folder (`reddit-engagement/` as root). Add a **volume mounted at `/data`**, set the variables from `engine/.env.example` (`YGR_DATA_DIR=/data`, `APPROVAL_PASSWORD`, `PUBLIC_URL`), and deploy. `railway.json` sets the start command and `/health` check.
7. **First week:** set `YGR_DRY_RUN=1`, watch a few batches on the dashboard, then remove it.

## Running locally

```bash
cd reddit-engagement/engine
../.venv/bin/python -m ygr status
```

```bash
../.venv/bin/python -m ygr --fixture tests/fixtures/reddit_fixture.json --fake-llm daily --force
```

The second command is a free end to end demo on synthetic threads, using scripted model answers. Preview the dashboard with the `reddit-dashboard-demo` launch config (password `demo`).

Other commands: `daily`, `tick`, `inbox`, `checkpoints`, `health`, `coach`, `eval [--update-baseline]`, `pending`, `approve ID`, `skip ID`, `resume`.

## Tests

```bash
cd reddit-engagement/engine && PYTHONPATH=.:tests ../.venv/bin/python -m unittest discover -s tests
```

30 tests cover the code checks, a full daily run, phase 1 and phase 2 mention rules, approval (including edits that break a rule), pacing, expiry, rate limits, 403s, crash recovery, removals and both kinds of stop, mod mail, shadowban, reply and lead routing, DMs never posting, Coach's bounds, reverts and the eval gate, the scheduler, and dashboard sessions. The fake Reddit never touches the real `log/comments.jsonl`, and a test guards that.

## Eval

`python -m ygr eval` runs Analyst → Writer → Critic over `eval/threads/` (6 sample threads; replace them with real threads from your own niche) and compares against `eval/baseline.json`. The first run sets the baseline. It costs real tokens. Add threads over time; 30 is a good target.

## Known limits

- **Not yet run against the live Claude API or Reddit API**: there was no API credit and no Reddit app yet. The SDK calls were checked against the installed `anthropic` 1.9 and PRAW 8.0.3 signatures, and the failure path for missing credit was exercised live.
- The Notion writes use API version `2025-09-03` (`data_sources/{id}/query`, `parent.data_source_id`) and are untested until `NOTION_TOKEN` exists.
- There's no Google search for older threads that still rank; phase 2 uses Reddit's own relevance search limited to the past year instead.
- The dashboard is single-user by design.
