"""One loop that runs every agent on its own clock.

Timers live in the kv table, so a restart doesn't double-run anything. Every
job is wrapped: an exception is logged, and three consecutive failures of the
same job alert Jeet. Reddit rejecting the credentials trips the emergency stop.
The daily run is retried at most 3 times a day, an hour apart. A dead man's
switch alerts if no daily run has succeeded for `deadman_hours`.
"""
from __future__ import annotations

import threading
import traceback
from datetime import time as dtime, timedelta

from .config import WEEKDAYS
from .core import trip
from .evalrun import run_eval
from .reddit_io import RedditAuthError
from .store import iso, parse
from .agents import coach, conductor, publisher, watcher

GOOD_DAILY = ("ok", "partial", "no_candidates", "skipped", "halted")


class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.failures: dict[str, int] = {}

    # -- plumbing ---------------------------------------------------------------
    def _due(self, name: str, seconds: float) -> bool:
        last = self.ctx.store.kv_get(f"sched:{name}")
        return not last or (self.ctx.now() - parse(last)).total_seconds() >= seconds

    def _safe(self, name: str, fn):
        self.ctx.store.kv_set(f"sched:{name}", iso(self.ctx.now()))
        try:
            result = fn()
            self.failures[name] = 0
            return result
        except RedditAuthError as e:
            trip(self.ctx, f"Reddit rejected the credentials during {name}: {e}")
        except Exception as e:  # noqa: BLE001
            n = self.failures.get(name, 0) + 1
            self.failures[name] = n
            self.ctx.store.event("error", "scheduler", f"{name} failed ({n} in a row): {e}",
                                 {"traceback": traceback.format_exc()[-3000:]})
            if n == 3:
                self.ctx.notify.send(f"Reddit engine: '{name}' has failed 3 times in a row", str(e), "/status", level="warn")
        return None

    # -- jobs ---------------------------------------------------------------------
    def _maybe_daily(self) -> None:
        ctx = self.ctx
        ist = ctx.ist()
        hh, mm = map(int, ctx.cfg.settings["schedule"]["daily_run"].split(":"))
        if ist.time() < dtime(hh, mm):
            return
        date = ist.date().isoformat()
        if ctx.store.kv_get(f"daily_run:{date}"):
            return
        attempts = ctx.store.kv_get(f"daily_attempts:{date}", [])
        if len(attempts) >= 3 or (attempts and ctx.now() - parse(attempts[-1]) < timedelta(hours=1)):
            return
        ctx.store.kv_set(f"daily_attempts:{date}", attempts + [iso(ctx.now())])
        self._safe("daily", lambda: conductor.daily_run(ctx))

    def _maybe_coach(self) -> None:
        ctx = self.ctx
        s = ctx.cfg.settings["schedule"]
        ist = ctx.ist()
        hh, mm = map(int, s["coach_time"].split(":"))
        if WEEKDAYS[ist.weekday()] != s["coach_weekday"] or ist.time() < dtime(hh, mm):
            return
        y, w, _ = ist.isocalendar()
        key = f"coach:{y}-W{w}"
        if ctx.store.kv_get(key):
            return
        ctx.store.kv_set(key, iso(ctx.now()))
        self._safe("coach", lambda: coach.weekly(ctx))

    def _deadman(self) -> None:
        ctx = self.ctx
        hours = ctx.cfg.settings["schedule"]["deadman_hours"]
        runs = [r for r in ctx.store.runs(50) if r["kind"] == "daily"]
        good = next((r for r in runs if r["status"] in GOOD_DAILY), None)
        started = parse(good["started_at"]) if good else parse(ctx.store.kv_get("engine:first_start") or iso(ctx.now()))
        if ctx.now() - started < timedelta(hours=hours):
            return
        key = f"deadman:{ctx.ist().date().isoformat()}"
        if ctx.store.kv_get(key):
            return
        ctx.store.kv_set(key, True)
        ctx.notify.send(f"No successful Reddit daily run in {hours} hours", "Check the status page for errors.", "/status", level="warn")

    def once(self) -> None:
        ctx, s = self.ctx, self.ctx.cfg.settings["schedule"]
        if not ctx.store.kv_get("engine:first_start"):
            ctx.store.kv_set("engine:first_start", iso(ctx.now()))
        if self._due("publisher", s["publisher_tick_seconds"]):
            self._safe("publisher", lambda: publisher.tick(ctx))
        if self._due("inbox", s["inbox_every_minutes"] * 60):
            self._safe("inbox", lambda: watcher.inbox(ctx))
        if self._due("checkpoints", s["checkpoints_every_minutes"] * 60):
            self._safe("checkpoints", lambda: watcher.checkpoints(ctx))
        if self._due("health", s["health_every_hours"] * 3600):
            self._safe("health", lambda: watcher.health(ctx))
        if ctx.store.kv_get("run_now_requested"):
            ctx.store.kv_del("run_now_requested")
            self._safe("daily", lambda: conductor.daily_run(ctx, force=True))
        self._maybe_daily()
        self._maybe_coach()
        if self._due("notes", 3600) and ctx.store.proposals(statuses=("approved_pending_eval",)):
            self._safe("notes", lambda: coach.apply_pending_notes(ctx, run_eval))
        self._deadman()

    def run_forever(self, stop: threading.Event | None = None, every: float = 15) -> None:
        stop = stop or threading.Event()
        self.ctx.store.event("info", "scheduler", "started")
        while not stop.is_set():
            self.once()
            stop.wait(every)
