"""SQLite state. Every agent reads and writes through here, so a crash or a
redeploy resumes from the same state. The `jobs.dedupe_key` UNIQUE index is
what guarantees one comment per thread and one reply per parent comment, ever.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
  agent TEXT NOT NULL, message TEXT NOT NULL, data TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL, summary TEXT, tokens TEXT);
CREATE TABLE IF NOT EXISTS seen_threads (
  thread_id TEXT PRIMARY KEY, sub TEXT, seen_at TEXT NOT NULL, disposition TEXT NOT NULL,
  reason TEXT, source TEXT, score INTEGER);
CREATE TABLE IF NOT EXISTS sub_policies (
  sub TEXT PRIMARY KEY COLLATE NOCASE, policy TEXT NOT NULL, rules_hash TEXT, fetched_at TEXT,
  source TEXT, paused_until TEXT, pause_reason TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  dedupe_key TEXT UNIQUE,
  thread_id TEXT, parent_id TEXT, sub TEXT, thread_title TEXT, thread_url TEXT,
  comment_type TEXT, mention INTEGER DEFAULT 0, link TEXT,
  fit_score INTEGER, topic TEXT, source TEXT,
  brief TEXT, critic TEXT, draft_text TEXT, final_text TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT, approved_at TEXT, not_before TEXT,
  posting_started_at TEXT, posted_at TEXT,
  attempts INTEGER DEFAULT 0, error TEXT, run_id INTEGER, comment_id TEXT, permalink TEXT,
  revisions INTEGER DEFAULT 0, inbox_id TEXT);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, comment_id TEXT UNIQUE, kind TEXT, sub TEXT, thread_id TEXT,
  thread_url TEXT, thread_title TEXT, text TEXT, draft_text TEXT,
  mention INTEGER DEFAULT 0, yuvgro_link TEXT, topic TEXT, fit_score INTEGER, source TEXT,
  posted_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'posted', removed_at TEXT,
  score_1h INTEGER, score_24h INTEGER, score_72h INTEGER, score_7d INTEGER,
  replies INTEGER DEFAULT 0, checkpoints TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS inbox (
  id TEXT PRIMARY KEY, kind TEXT, author TEXT, body TEXT, thread_id TEXT, parent_id TEXT,
  comment_id TEXT, subject TEXT, subreddit TEXT, received_at TEXT,
  classification TEXT, handled_at TEXT, job_id INTEGER);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT, thread_url TEXT, inbox_id TEXT UNIQUE,
  intent TEXT, details TEXT, notion_company TEXT, notion_contact TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT, title TEXT, detail TEXT,
  evidence TEXT, payload TEXT, status TEXT NOT NULL DEFAULT 'open', decided_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS tunable_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, applied_at TEXT, key TEXT, old TEXT, new TEXT,
  reason TEXT, baseline REAL, status TEXT DEFAULT 'active', evaluated_at TEXT, after REAL);
CREATE TABLE IF NOT EXISTS account_snapshots (
  ts TEXT PRIMARY KEY, total_karma INTEGER, comment_karma INTEGER, link_karma INTEGER,
  created_utc REAL, public_visible INTEGER);
CREATE TABLE IF NOT EXISTS candidate_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, thread_id TEXT, sub TEXT, source TEXT,
  stage TEXT, score INTEGER, picked INTEGER DEFAULT 0, ts TEXT);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS comments_posted ON comments(posted_at);
"""

JSON_FIELDS = {"brief", "critic", "policy", "summary", "tokens", "data", "classification",
               "details", "payload", "result", "checkpoints", "old", "new"}

# Statuses that still count against today's and this week's caps.
ACTIVE_JOB_STATUSES = ("pending_approval", "approved", "posting")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _enc(k: str, v):
    return json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None and not isinstance(v, str) else v


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock, self._conn() as c:
            out = []
            for r in c.execute(sql, args).fetchall():
                d = dict(r)
                for k in JSON_FIELDS & d.keys():
                    if isinstance(d[k], str) and d[k][:1] in "[{\"":
                        try:
                            d[k] = json.loads(d[k])
                        except json.JSONDecodeError:
                            pass
                out.append(d)
            return out

    def _one(self, sql: str, args=()) -> dict | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args=()) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid

    def _insert(self, table: str, fields: dict) -> int:
        keys = list(fields)
        vals = [_enc(k, fields[k]) for k in keys]
        return self._exec(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", vals)

    def _update(self, table: str, key: str, key_val, fields: dict) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE {table} SET {sets} WHERE {key}=?", [_enc(k, v) for k, v in fields.items()] + [key_val])

    # -- kv ---------------------------------------------------------------------
    def kv_get(self, key: str, default=None):
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))

    def kv_del(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    # -- events & runs ------------------------------------------------------------
    def event(self, level: str, agent: str, message: str, data=None) -> None:
        self._insert("events", {"ts": iso(utcnow()), "level": level, "agent": agent, "message": message, "data": data})

    def events(self, limit: int = 50, min_level: str | None = None) -> list[dict]:
        if min_level == "warn":
            return self._rows("SELECT * FROM events WHERE level IN ('warn','error','critical') ORDER BY id DESC LIMIT ?", (limit,))
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def run_start(self, kind: str) -> int:
        return self._insert("runs", {"kind": kind, "started_at": iso(utcnow()), "status": "running"})

    def run_finish(self, run_id: int, status: str, summary=None, tokens=None) -> None:
        self._update("runs", "id", run_id, {"finished_at": iso(utcnow()), "status": status, "summary": summary, "tokens": tokens})

    def last_run(self, kind: str, status: str | None = None) -> dict | None:
        if status:
            return self._one("SELECT * FROM runs WHERE kind=? AND status=? ORDER BY id DESC LIMIT 1", (kind, status))
        return self._one("SELECT * FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))

    def runs(self, limit: int = 20) -> list[dict]:
        return self._rows("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    # -- threads -----------------------------------------------------------------
    def seen(self, thread_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM seen_threads WHERE thread_id=?", (thread_id,)) is not None

    def mark_seen(self, thread_id: str, sub: str, disposition: str, reason: str = "", source: str = "", score: int | None = None) -> None:
        self._exec("""INSERT INTO seen_threads(thread_id,sub,seen_at,disposition,reason,source,score) VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(thread_id) DO UPDATE SET disposition=excluded.disposition, reason=excluded.reason,
                      score=COALESCE(excluded.score, seen_threads.score)""",
                   (thread_id, sub, iso(utcnow()), disposition, reason, source, score))

    def log_candidate(self, run_id: int, thread_id: str, sub: str, source: str, stage: str, score: int | None = None, picked: bool = False) -> None:
        self._insert("candidate_log", {"run_id": run_id, "thread_id": thread_id, "sub": sub, "source": source,
                                       "stage": stage, "score": score, "picked": int(picked), "ts": iso(utcnow())})

    def candidate_stats(self, since: datetime) -> list[dict]:
        return self._rows("SELECT source, COUNT(*) AS seen, SUM(picked) AS picked FROM candidate_log WHERE ts>=? GROUP BY source",
                          (iso(since),))

    # -- sub policies --------------------------------------------------------------
    def policy(self, sub: str) -> dict | None:
        return self._one("SELECT * FROM sub_policies WHERE sub=?", (sub,))

    def policies(self) -> list[dict]:
        return self._rows("SELECT * FROM sub_policies ORDER BY sub")

    def set_policy(self, sub: str, policy: dict, rules_hash: str | None, source: str) -> None:
        self._exec("""INSERT INTO sub_policies(sub,policy,rules_hash,fetched_at,source) VALUES(?,?,?,?,?)
                      ON CONFLICT(sub) DO UPDATE SET policy=excluded.policy, rules_hash=excluded.rules_hash,
                      fetched_at=excluded.fetched_at, source=excluded.source""",
                   (sub, json.dumps(policy, ensure_ascii=False), rules_hash, iso(utcnow()), source))

    def pause_sub(self, sub: str, until: datetime, reason: str) -> None:
        if not self.policy(sub):
            self.set_policy(sub, {}, None, "pause")
        self._exec("UPDATE sub_policies SET paused_until=?, pause_reason=? WHERE sub=?", (iso(until), reason, sub))

    # -- jobs ------------------------------------------------------------------------
    def create_job(self, **fields) -> int | None:
        fields.setdefault("created_at", iso(utcnow()))
        try:
            return self._insert("jobs", fields)
        except sqlite3.IntegrityError:
            return None  # dedupe_key already exists: this thread/parent was handled before

    def update_job(self, job_id: int, **fields) -> None:
        self._update("jobs", "id", job_id, fields)

    def job(self, job_id: int) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def jobs(self, statuses=None, kinds=None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        return self._rows(sql + " ORDER BY id DESC LIMIT ?", args + [limit])

    def job_by_key(self, key: str) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE dedupe_key=?", (key,))

    # -- posted comments (the ledger) ---------------------------------------------------
    def record_comment(self, **fields) -> int:
        return self._insert("comments", fields)

    def update_comment(self, cid: int, **fields) -> None:
        self._update("comments", "id", cid, fields)

    def comments(self, since: datetime | None = None, statuses=("posted", "removed")) -> list[dict]:
        sql = f"SELECT * FROM comments WHERE status IN ({','.join('?' * len(statuses))})"
        args = list(statuses)
        if since:
            sql += " AND posted_at>=?"
            args.append(iso(since))
        return self._rows(sql + " ORDER BY posted_at ASC", args)

    def comment_by_reddit_id(self, comment_id: str) -> dict | None:
        return self._one("SELECT * FROM comments WHERE comment_id=?", (comment_id,))

    def last_post(self) -> dict | None:
        return self._one("SELECT * FROM comments ORDER BY posted_at DESC LIMIT 1")

    def our_comment_ids(self) -> set[str]:
        return {r["comment_id"] for r in self._rows("SELECT comment_id FROM comments")}

    # -- inbox & leads ---------------------------------------------------------------------
    def inbox_known(self, item_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM inbox WHERE id=?", (item_id,)) is not None

    def add_inbox(self, **fields) -> None:
        self._insert("inbox", fields)

    def update_inbox(self, item_id: str, **fields) -> None:
        self._update("inbox", "id", item_id, fields)

    def add_lead(self, **fields) -> int | None:
        try:
            return self._insert("leads", fields)
        except sqlite3.IntegrityError:
            return None

    def leads(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))

    # -- proposals & tunables -----------------------------------------------------------------
    def add_proposal(self, **fields) -> int:
        fields.setdefault("created_at", iso(utcnow()))
        return self._insert("proposals", fields)

    def proposals(self, statuses=("open",)) -> list[dict]:
        return self._rows(f"SELECT * FROM proposals WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id DESC",
                          list(statuses))

    def proposal(self, pid: int) -> dict | None:
        return self._one("SELECT * FROM proposals WHERE id=?", (pid,))

    def update_proposal(self, pid: int, **fields) -> None:
        self._update("proposals", "id", pid, fields)

    def add_tunable_change(self, **fields) -> int:
        fields.setdefault("applied_at", iso(utcnow()))
        return self._insert("tunable_changes", fields)

    def tunable_changes(self, status: str | None = None) -> list[dict]:
        if status:
            return self._rows("SELECT * FROM tunable_changes WHERE status=? ORDER BY id", (status,))
        return self._rows("SELECT * FROM tunable_changes ORDER BY id DESC LIMIT 50")

    def update_tunable_change(self, tid: int, **fields) -> None:
        self._update("tunable_changes", "id", tid, fields)

    # -- account ---------------------------------------------------------------------------
    def add_snapshot(self, **fields) -> None:
        fields.setdefault("ts", iso(utcnow()))
        self._exec("INSERT OR REPLACE INTO account_snapshots(ts,total_karma,comment_karma,link_karma,created_utc,public_visible) "
                   "VALUES(:ts,:total_karma,:comment_karma,:link_karma,:created_utc,:public_visible)",
                   {"comment_karma": None, "link_karma": None, "created_utc": None, "public_visible": None, **fields})

    def latest_snapshot(self) -> dict | None:
        return self._one("SELECT * FROM account_snapshots ORDER BY ts DESC LIMIT 1")

    def snapshot_before(self, when: datetime) -> dict | None:
        return self._one("SELECT * FROM account_snapshots WHERE ts<=? ORDER BY ts DESC LIMIT 1", (iso(when),))


def days_ago(n: float) -> datetime:
    return utcnow() - timedelta(days=n)
