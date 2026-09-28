"""CLI and server entry point.

  python -m ygr serve                 dashboard + scheduler (what Railway runs)
  python -m ygr daily [--force]       one daily run now
  python -m ygr tick | inbox | checkpoints | health | coach
  python -m ygr eval [--update-baseline]
  python -m ygr status | pending | resume
  python -m ygr approve ID [--text-file F] | skip ID
  python -m ygr reddit-auth           one-time: get REDDIT_REFRESH_TOKEN

Global flags:
  --dry-run         never post, never write Notion (also YGR_DRY_RUN=1)
  --fixture PATH    use a fixture file instead of the Reddit API (implies --dry-run,
                    separate demo database)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

from .config import load, load_dotenv
from .core import Ctx, account_state, bootstrap, halted, headroom, resume
from .notify import Notifier
from .notion import NotionIO
from .reddit_io import make_reddit
from .store import Store


def build_ctx(dry_run: bool = False, fixture: str | None = None, llm=None) -> Ctx:
    load_dotenv()
    cfg = load(dry_run=True if fixture else (dry_run or None))
    db = cfg.data_dir / ("ygr-demo.sqlite" if fixture else "ygr.sqlite")
    store = Store(db)
    if llm is None:
        from .llm import ClaudeLLM
        llm = ClaudeLLM(cfg)
    ctx = Ctx(cfg=cfg, store=store, reddit=make_reddit(fixture), llm=llm,
              notify=Notifier(store), notion=NotionIO(cfg, store))
    bootstrap(ctx)
    return ctx


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


SCOPES = ["identity", "read", "submit", "history", "privatemessages"]


def reddit_auth() -> int:
    """One-time OAuth: prints a Reddit consent link, catches the redirect on
    localhost:8080, and prints the refresh token to put in REDDIT_REFRESH_TOKEN.
    Your Reddit password never touches this system."""
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    import praw
    load_dotenv()
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not cid:
        print("Set REDDIT_CLIENT_ID (and REDDIT_CLIENT_SECRET) first. See engine/README.md step 1.")
        return 1
    r = praw.Reddit(client_id=cid, client_secret=secret, redirect_uri="http://localhost:8080",
                    user_agent="python:yuvgro-engagement:v1.0 (setup)")
    state = secrets.token_urlsafe(16)
    print("\n1. Open this link signed in as the Reddit account the engine will post from, and press Allow:\n")
    print(r.auth.url(scopes=SCOPES, state=state, duration="permanent"))
    print("\n2. Waiting for Reddit to redirect back to http://localhost:8080 …")
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            got.update({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

    HTTPServer(("127.0.0.1", 8080), H).handle_request()
    if got.get("state") != state or "code" not in got:
        print(f"\nReddit did not return a valid code ({got.get('error', 'state mismatch')}).")
        return 1
    token = r.auth.authorize(got["code"])
    print("\n3. Put this in your environment (Railway variables, or engine/.env):\n")
    print(f"REDDIT_REFRESH_TOKEN={token}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ygr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixture")
    ap.add_argument("--fake-llm", action="store_true",
                    help="with --fixture: scripted model answers from tests/helpers.py (free UI preview, no API calls)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    p = sub.add_parser("daily")
    p.add_argument("--force", action="store_true")
    for c in ("tick", "inbox", "checkpoints", "health", "coach", "status", "pending", "resume"):
        sub.add_parser(c)
    p = sub.add_parser("eval")
    p.add_argument("--update-baseline", action="store_true")
    p = sub.add_parser("approve")
    p.add_argument("id", type=int)
    p.add_argument("--text-file")
    p = sub.add_parser("skip")
    p.add_argument("id", type=int)
    sub.add_parser("reddit-auth", help="one-time: get REDDIT_REFRESH_TOKEN through Reddit's consent page")
    a = ap.parse_args(argv)
    if a.cmd == "reddit-auth":
        return reddit_auth()

    llm = None
    if a.fake_llm:
        if not a.fixture:
            ap.error("--fake-llm only works with --fixture")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
        from helpers import Script  # noqa: E402 — dev-only scripted answers
        from .llm import FakeLLM
        llm = FakeLLM(Script())
    ctx = build_ctx(a.dry_run, a.fixture, llm=llm)
    from .agents import coach, conductor, publisher, watcher

    if a.cmd == "serve":
        from .scheduler import Scheduler
        from .web import App
        app = App(ctx, os.environ.get("APPROVAL_PASSWORD", ""), os.environ.get("APPROVAL_USER", "jeet"))
        srv = app.serve(port=int(os.environ.get("PORT", "8080")))
        stop = threading.Event()
        threading.Thread(target=Scheduler(ctx).run_forever, args=(stop,), daemon=True).start()
        print(f"dashboard on :{srv.server_address[1]}  dry_run={ctx.cfg.dry_run}", flush=True)
        try:
            srv.serve_forever()
        finally:
            stop.set()
        return 0
    if a.cmd == "daily":
        _print(conductor.daily_run(ctx, force=a.force))
    elif a.cmd == "tick":
        print(publisher.tick(ctx))
    elif a.cmd == "inbox":
        _print(watcher.inbox(ctx))
    elif a.cmd == "checkpoints":
        print(watcher.checkpoints(ctx))
    elif a.cmd == "health":
        _print(watcher.health(ctx))
    elif a.cmd == "coach":
        _print(coach.weekly(ctx))
    elif a.cmd == "eval":
        from .evalrun import run_eval
        r = run_eval(ctx, update_baseline=a.update_baseline)
        _print({k: v for k, v in r.items() if k != "cases"})
        for c in r.get("cases", []):
            print(f"\n--- {c.get('sub')} {c['id']} [{c['status']}] score {c.get('score')} revisions {c.get('revisions')}\n{c.get('text', c.get('error', ''))}")
    elif a.cmd == "status":
        acc = account_state(ctx)
        _print({"account": acc, "headroom": headroom(ctx, acc["phase"]), "halted": halted(ctx.store),
                "dry_run": ctx.cfg.dry_run, "pending": len(ctx.store.jobs(statuses=("pending_approval",))),
                "recent_runs": [{k: r[k] for k in ("kind", "started_at", "status", "summary")} for r in ctx.store.runs(5)]})
    elif a.cmd == "pending":
        for j in sorted(ctx.store.jobs(statuses=("pending_approval", "manual")), key=lambda j: j["id"]):
            print(f"\n#{j['id']} [{j['kind']}/{j['comment_type']}] r/{j['sub']}  {j['thread_title']}\n{j['thread_url']}\n\n{j['draft_text']}\n")
    elif a.cmd == "resume":
        resume(ctx, "cli")
        print("resumed")
    elif a.cmd == "approve":
        text = Path(a.text_file).read_text() if a.text_file else None
        errs = publisher.approve(ctx, a.id, text)
        print("\n".join(errs) if errs else "approved")
        return 1 if errs else 0
    elif a.cmd == "skip":
        publisher.skip(ctx, a.id)
        print("skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
