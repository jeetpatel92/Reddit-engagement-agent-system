# Reddit Engagement Agent System

A multi agent pipeline that runs a founder's Reddit engagement end to end: finding threads, reading each community's rules, drafting, quality control, posting, monitoring, handling replies and leads, and learning every week. There is exactly one human step: approving each comment before it goes out. Built for YuvGro, an AI search and marketing consultancy, on Claude.

## Why this exists

Reddit is the most cited source in AI answers, and the threads that rank for "[keyword] reddit" feed those answers directly. Taking part well means reading a lot of threads, answering only where you genuinely add something, following every community's rules, and keeping self promotion rare and disclosed. That work is repetitive, and one careless comment can get an account banned. This system does the repetitive part, enforces the rules in code, and leaves the one judgement that matters with a person: whether a comment goes out under their name.

## Two ways to run it

1. **Manual session** (`skills/reddit-daily/`, `scripts/reddit.py`). Claude finds and drafts inside Claude Code; the person pastes threads in and posts approved comments. This is in daily use, because browser agents can't reach Reddit.
2. **Engine** (`engine/`). Nine agents on a schedule, with a phone friendly dashboard for the approval tap. Built and tested; waiting on Reddit Data API access to go live.

Both follow the same `playbook.md` and `config.json`.

## Pipeline

```
 22:00 cron
      │
  CONDUCTOR ── go / no go, today's topic, limits, token budget
      │
   SCOUT ─────── finds up to 15 candidate threads
      │
 RULEKEEPER ──── attaches each subreddit's current rules
      │
  ANALYST ────── scores, picks 3 to 5, writes a brief per thread
      │
   WRITER ◄───┐  drafts the comment
      │       │  revise (at most 2 rounds)
   CRITIC ────┘  code checks + independent review: pass / revise / kill
      │
  PUBLISHER ──── paces posts, sends the approval card, posts on Approve
      │
   WATCHER ───── checks each comment at 1h, 24h, 72h, 7d: score, replies, removals
      │  └──► replies go back to ANALYST as reply jobs
      │  └──► buying intent ─► CLOSER ─► CRM + alert
      ▼
   COACH (weekly) ─ learns from outcomes and edits, tunes, proposes
```

## The agents

| Agent | Kind | Receives | Decides | Hands on |
|---|---|---|---|---|
| Conductor | code | trigger and state | whether to run, phase, limits, topic, budget | run brief |
| Scout | code + Claude (low effort) | run brief | drops seen, locked, stale, crowded and non English threads; screens for a real question | up to 15 threads with top comments |
| Rulekeeper | code + Claude (low effort) | subreddits in play | reads rules weekly; posting mode per subreddit | threads with policy |
| Analyst | Claude + code | threads and limits | Claude scores expertise, gap and audience; **code** picks and decides any brand mention | a brief per pick |
| Writer | Claude | brief and house pack | the wording; first person stories only from an approved bank | draft and its factual claims |
| Critic | code + independent Claude | draft, brief, policy | pass, revise or kill | approved drafts |
| Publisher | code | approved drafts | pacing; posts only after Approve | posting record |
| Watcher | code + Claude (low effort) | ledger and inbox | removals, shadowban, karma trend; sorts replies | reply jobs, leads, emergency stop |
| Closer | code + Claude | buying intent and DMs | intent, fit, region | CRM records and a DM draft the person sends |
| Coach | code + Claude, weekly | outcomes, edits, kills | bounded tuning and proposals | tunables, example banks, digest |

Each module's docstring states its role, inputs, decisions and handoff. [`docs/architecture.md`](docs/architecture.md) has the full contracts.

## Design decisions

* **One approval tap, not zero.** Every comment is posted under a real person's name. Reddit expects automated accounts to be labelled, several communities ban AI written comments, and a new account posting on a machine schedule gets flagged. So the system does everything except the decision to post. Unapproved drafts expire after 12 hours, and nothing is ever posted late.
* **Code owns every limit.** The model scores threads, but code decides how many comments go out, in which communities, and whether a comment may mention the brand at all: never while the account is warming up, then at most 10% of recent comments, one a day, one per community every 14 days. The model never sets its own promotion budget.
* **Two gates, one of them unarguable.** Gate 1 is plain code (`engine/ygr/checks.py`): no hyphens or dashes, a disclosure whenever the brand appears, links only from an allow list, track record figures only from approved proof points, no self pricing, no DM requests, no blocked names, nothing too similar to an earlier comment. It runs on every draft and again on the person's edited text before posting. Gate 2 is a separate Claude call that never sees the Writer's reasoning.
* **A workflow, not an agent framework.** The order of steps never changes, so a fixed pipeline in Python is cheaper, testable and predictable. Claude is called only where judgement is needed, always with a JSON schema, so every handoff is validated.
* **One model, one cache.** Every role uses the same model at different effort levels. A large, stable house pack (playbook, experience bank, approved examples) is the cached system prefix for every call.
* **State you can resume.** Everything lives in SQLite. A unique key guarantees one comment per thread, and a post interrupted by a crash is reconciled against Reddit before any retry.

## Error handling

| Failure | Response |
|---|---|
| Reddit rate limits or outages | back off as instructed; continue with what was collected |
| Reddit rejects the credentials | emergency stop |
| Claude unavailable, out of credit, or the run budget reached | stop drafting, keep what is queued, alert |
| A thread the model declines | skip that thread |
| Malformed output between agents | schema validation; one retry, then drop |
| Writer and Critic disagree | killed after 2 revisions, logged for the Coach |
| Nothing good enough today | post nothing; the daily number is a ceiling, not a target |
| A comment removed | 2 in a community within 14 days pause it for 30 days |
| Mod mail, shadowban, suspension, falling karma, 3 removals in 24h | emergency stop until the person resumes |
| Silent failure | alert after 48 hours without a successful run |

## How it improves

* **Bounded automatic tuning.** Weekly, the Coach adjusts topic weights, community priority, target length, the freshness window and search queries, with at most a 20% change a week and at least 10 comments of evidence per arm. Any change is reverted automatically if results drop.
* **Example banks.** The top 10% of comments become the Writer's style references; removed or downvoted ones become the Critic's warnings.
* **Human edits as the strongest signal.** Every difference between a draft and what the person actually posted is fed to the Coach.
* **Proposals, not silent changes.** New communities, style notes and blog ideas wait for approval, and an approved style note must pass the regression eval (`engine/eval/`) before the Writer sees it.
* **Rules it can't unlearn.** The Coach can write only the tunables, the example banks and proposals. The code checks, settings and prompts are out of its reach.

## What's real code vs. specs

* `engine/ygr/`: working Python (about 3,800 lines). The agents, SQLite store, Reddit adapter (PRAW), Claude client, scheduler, dashboard and CLI. It calls real APIs with credentials from environment variables; none are committed.
* `engine/tests/`: 30 tests against a fixture Reddit and a scripted model.
* `engine/prompts/`: one prompt per model role.
* `playbook.md`, `subreddits.md`, `experience-bank.md`: the judgement rules every agent reads.
* `skills/reddit-daily/`, `scripts/reddit.py`: the manual session for Claude Code.

## Stack

| Layer | Choice |
|---|---|
| Model | Claude through the Anthropic SDK, structured outputs, prompt caching, server side refusal fallback |
| Reddit | official Data API through PRAW, OAuth refresh token |
| State | SQLite |
| Dashboard | Python standard library HTTP server, password login with a signed session cookie |
| Notifications | Slack webhook or Telegram bot |
| CRM | Notion API |
| Hosting | Railway with a persistent volume |

## Status

The manual session has been in daily use since September 2026. The engine is built: all 30 tests pass, and the full loop (daily run, approval on the dashboard, paced posting) was verified in a browser in dry run mode. It has **not yet run against the live Reddit or Claude APIs**. The first live run is still the real test.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd engine && PYTHONPATH=.:tests ../.venv/bin/python -m unittest discover -s tests
../.venv/bin/python -m ygr --fixture tests/fixtures/reddit_fixture.json --fake-llm daily --force
```

The last command is a free demo on synthetic threads with a scripted model. For live use, fill in `config.json` (your username, CRM IDs and booking link), copy `engine/.env.example`, and follow [`engine/README.md`](engine/README.md).

## Layout

```
playbook.md, subreddits.md, experience-bank.md   judgement rules
config.json                                      limits, topics, links (placeholders)
engine/ygr/                                      the agents and infrastructure
engine/prompts/                                  one prompt per model role
engine/tests/, engine/eval/                      tests and the regression eval
skills/reddit-daily/, scripts/reddit.py          the manual session
docs/                                            architecture and the manual workflow
```

## Licence

GNU Affero General Public License v3.0, see [LICENSE](LICENSE).
