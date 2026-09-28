# Architecture: agent contracts

Every agent is a Python module in `engine/ygr/agents/`. Agents that use Claude call it through `engine/ygr/llm.py` with a JSON schema, so each handoff below is validated before the next agent sees it. The house pack (playbook, experience bank, learned notes, approved examples) is the cached system prefix of every call; each role adds its own prompt from `engine/prompts/`.

## 0. Conductor (`conductor.py`, code)

**Receives** the 22:00 trigger (or a dashboard "run now"), the store, and the account snapshot.
**Decides** whether to run at all: emergency stop, account health, phase (warm up or established), and whether today's cap is already used. Then the topic of the day (weekday rotation), a secondary topic if the Coach has learned it performs well, the subreddits (warm up subreddits only in phase 1, minus paused ones and ones the Coach ranked low), the search queries (rotated weekly), and the token budget.
**Hands on** a run brief to Scout, then drives every later stage and routes failures: rejected Reddit credentials stop everything, an unavailable model or an exhausted budget stops drafting but keeps what is queued, a declined thread is skipped.

## 1. Scout (`scout.py`, code + Claude at low effort)

**Receives** the run brief.
**Decides** in code: drop threads already seen, archived, locked, NSFW, removed, our own, older than the freshness window, too crowded, or not English enough. Then one batched model call per run asks only whether each thread holds a real question inside the expertise.
**Hands on** up to 15 threads, each refetched with its top comments and one level of replies.

## 2. Rulekeeper (`rulekeeper.py`, code + Claude at low effort)

**Receives** the subreddits of today's threads.
**Decides** each subreddit's policy: rules are read through the API at most weekly and re-classified by the model only when their text changes. Hand verified seed policies are a floor: a live read can make a subreddit stricter, never looser. Code turns the policy into a posting mode: `blocked` (paused, below a karma or age minimum, or held), `talking_points` (AI written content banned, so the person writes the words), `value_only` (self promotion banned) or `normal`. A rule change in a community already posted in alerts the person.
**Hands on** each thread with its policy and mode; blocked ones are dropped with the reason recorded.

## 3. Analyst (`analyst.py`, Claude + code)

**Receives** the threads with policies, the remaining limits, and a summary of recent comments.
**Decides** with the model: for each thread, the real question, expertise (0 to 3), gap (0 to 3, after reading the existing replies), audience (0 to 2), what's already covered, the angle to add, whether the poster asked for a recommendation, and which blog topic would go deeper. With code: visibility from age, the total score, the pick order, every cap, and whether a pick may mention the brand and carry a link.
**Hands on** a brief per pick: question, what's covered, the gap, comment type (`value`, `mention` or `talking_points`), any approved link, and a target length.

## 4. Writer (`writer.py`, Claude)

**Receives** one brief, the thread with its top comments, and on a revision its previous draft plus the Critic's notes.
**Decides** the wording, structure and length. First person stories come only from the experience bank; with no human editing in the loop, it writes without a story rather than leaving a placeholder.
**Hands on** the draft, the list of factual claims it makes about platforms, and any story or link used.

## 5. Critic (`critic.py` + `checks.py`)

**Receives** the draft, the brief, the thread, and the subreddit policy.
**Decides** in two gates. Gate 1 is code and cannot be argued with (see the README). Gate 2 is a fresh model call that never sees the Writer's reasoning: does it answer without the link, add the gap, read as machine written, make wrong or outdated claims, risk identifying a client, or risk removal? Pass needs both gates clean.
**Hands on** approved drafts to the Publisher's queue. Revisions go back to the Writer (at most 2). Kills are logged with the notes for the Coach. The shared loop is `drafting.py`.

## 6. Publisher (`publisher.py`, code)

**Receives** approved drafts, and the person's Approve, Edit or Skip from the dashboard.
**Decides** nothing about content. On approval it runs gate 1 again on the exact text to be posted, then paces: at least 5 minutes apart with jitter, never the same community twice in a row while another is waiting. A job is marked `posting` before the API call; after a crash it is reconciled against the account's recent comments before any retry. Rate limits reschedule, a closed thread fails the job, and a 403 pauses the community.
**Hands on** a posting record (comment ID, link, time and final text) to the ledger and the Watcher.

## 7. Watcher (`watcher.py`, code + Claude at low effort)

**Receives** the ledger, the inbox, and the account.
**Decides** at 1h, 24h, 72h and 7d what a logged out visitor sees for each comment (score, replies, still visible); account health (profile visible logged out, suspension, karma trend); and a category for every inbox item.
**Hands on** removals to the breakers, follow up questions and disagreements back through Writer and Critic as reply jobs, buying intent and DMs to the Closer, and mod or admin mail straight to an emergency stop with the message forwarded verbatim.

## 8. Closer (`closer.py`, code + Claude)

**Receives** buying intent replies and DMs the other person started.
**Decides** intent strength, business, country, need and ideal customer segment.
**Hands on** a Company and a Contact in the CRM (Notion), a short public "happy to, send me a DM" reply for approval, and a DM draft that is never sent automatically. Pricing always comes from the person.

## 9. Coach (`coach.py`, weekly, code + Claude)

**Receives** four weeks of outcomes (72 hour score, replies, removals), the person's edits, killed drafts, skipped and expired jobs, and last week's automatic changes.
**Decides** in code: keep or revert last week's changes, then bounded adjustments with evidence. With the model: a digest, proposals, and up to 2 new search queries.
**Hands on** `tunables.json`, refreshed example banks, proposals on the dashboard, and the digest by notification. Approved style notes pass the regression eval before they reach the Writer.

## Safety stops

| Trigger | Scope |
|---|---|
| 2 removals in a community within 14 days, or a 403 on posting | that community paused 30 days |
| 3 removals in 24 hours, mod or admin mail, profile invisible logged out, suspension, karma down 10 in 3 days, 403s in 2 communities within 24 hours, rejected credentials | everything stops until the person resumes |

## Scheduling

One loop (`scheduler.py`) with timers stored in the database: publisher every minute, inbox every 15 minutes, checkpoints hourly, health every 6 hours, the daily run at 22:00 (at most 3 attempts, an hour apart), the Coach on Sunday morning, and a dead man's switch after 48 hours without a successful run. All Reddit and model calls happen on this one thread, because PRAW is not thread safe; the dashboard only reads and writes the database.
