"""The approval dashboard: the one tap.

Stdlib HTTP server, password login (APPROVAL_PASSWORD) with a signed 30-day
session cookie (Basic auth breaks inside Slack/Telegram in-app browsers, which
is where approval links get opened), failed-login throttling, a CSRF token on
every POST, phone-first layout. Pages:
  /           comments and replies waiting for approval, DM drafts to send yourself
  /status     phase, limits, emergency stop + Resume, paused subs, runs, warnings, leads
  /proposals  Coach's proposals, automatic changes, latest digest
  /health     unauthenticated liveness probe for the host
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from .core import account_state, halted, headroom, resume
from .store import parse
from .agents import coach, publisher

E = html.escape

CSS = """
:root{--brand:#1444DC;--ink:#101010;--muted:#5b5f6b;--line:#e3e5ea;--bg:#f5f5f5;--card:#fff;--ok:#127a3a;--warn:#a15c00;--bad:#b3261e}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 Barlow,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header b{color:var(--brand);font-size:18px}header a{color:var(--ink);text-decoration:none}header a.on{color:var(--brand);font-weight:600}
main{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:0 0 14px}
.meta{color:var(--muted);font-size:14px}.pill{display:inline-block;border-radius:99px;padding:1px 9px;font-size:13px;border:1px solid var(--line);margin-right:6px}
.pill.mention{border-color:var(--brand);color:var(--brand)}.pill.tp{border-color:var(--warn);color:var(--warn)}
textarea{width:100%;min-height:180px;font-family:inherit;font-size:15px;line-height:1.45;padding:10px;border:1px solid var(--line);border-radius:8px}
form{display:inline}form:has(textarea){display:block}
button{font:inherit;border:0;border-radius:8px;padding:10px 16px;cursor:pointer;margin:8px 8px 0 0}
.go{background:var(--brand);color:#fff}.no{background:#eceef2;color:var(--ink)}.danger{background:var(--bad);color:#fff}
.flash{background:#eef3ff;border:1px solid #c9d6ff;padding:10px;border-radius:8px;margin-bottom:14px}
.flash.err{background:#fdeceb;border-color:#f3c3bf}.stop{background:#fdeceb;border:1px solid #f3c3bf}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
details{margin-top:8px}summary{cursor:pointer;color:var(--muted);font-size:14px}pre{white-space:pre-wrap;font-size:13px;margin:6px 0}
a{color:var(--brand)}
"""


SESSION_DAYS = 30
COOKIE = "ygr_session"


class App:
    def __init__(self, ctx, password: str, user: str = "jeet"):
        if not password:
            raise ValueError("APPROVAL_PASSWORD must be set before the dashboard can run")
        self.ctx, self.user, self.password = ctx, user, password
        self.csrf = hmac.new(password.encode(), b"ygr-csrf", hashlib.sha256).hexdigest()[:32]
        self._failures: list[float] = []

    # -- sessions -------------------------------------------------------------------------
    def _sign(self, exp: int) -> str:
        return hmac.new(self.password.encode(), f"session:{exp}".encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        exp = int(time.time()) + SESSION_DAYS * 86400
        return f"{exp}.{self._sign(exp)}"

    def valid_session(self, token: str) -> bool:
        exp, _, sig = (token or "").partition(".")
        return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, self._sign(int(exp)))

    def check_password(self, attempt: str) -> bool:
        now = time.time()
        self._failures = [t for t in self._failures if now - t < 600]
        if len(self._failures) >= 5:
            return False  # throttled: 5 wrong passwords in 10 minutes
        if hmac.compare_digest(attempt.encode(), self.password.encode()):
            return True
        self._failures.append(now)
        self.ctx.store.event("warn", "web", "failed dashboard login")
        return False

    def login_page(self, nxt: str, failed: bool) -> str:
        msg = '<div class="flash err">Wrong password, or too many attempts. Wait a few minutes.</div>' if failed else ""
        return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>Sign in · YuvGro Reddit</title><style>{CSS}</style></head><body><header><b>YuvGro · Reddit</b></header>'
                f'<main><div class="card">{msg}<form method="post" action="/login"><input type="hidden" name="next" value="{E(nxt)}">'
                f'<p><label>Password<br><input type="password" name="password" autocomplete="current-password" autofocus '
                f'style="width:100%;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px"></label></p>'
                f'<button class="go">Sign in</button></form></div></main></body></html>')

    # -- pages --------------------------------------------------------------------------
    def page(self, title: str, active: str, body: str, flash: str = "", err: bool = False) -> str:
        nav = "".join(f'<a href="{p}" class="{"on" if p == active else ""}">{n}</a>'
                      for p, n in (("/", "Approve"), ("/status", "Status"), ("/proposals", "Coach")))
        f = f'<div class="flash{" err" if err else ""}">{E(flash)}</div>' if flash else ""
        stop = halted(self.ctx.store)
        banner = (f'<div class="card stop"><b>Emergency stop is on.</b> {E(stop["reason"])}<br>'
                  f'<span class="meta">Nothing posts until you resume on the Status page.</span></div>') if stop else ""
        return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{E(title)} · YuvGro Reddit</title><link rel="preconnect" href="https://fonts.googleapis.com">'
                f'<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&display=swap" rel="stylesheet">'
                f'<style>{CSS}</style></head><body><header><b>YuvGro · Reddit</b>{nav}</header><main>{banner}{f}{body}</main></body></html>')

    def form(self, action: str, inner: str) -> str:
        return f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{self.csrf}">{inner}</form>'

    def queue(self) -> str:
        s = self.ctx.store
        pending = sorted(s.jobs(statuses=("pending_approval",)), key=lambda j: j["id"])
        manual = s.jobs(statuses=("manual",), kinds=("dm_draft",))
        approved = s.jobs(statuses=("approved", "posting"))
        out = []
        if not pending and not manual:
            out.append('<div class="card">Nothing waiting. The next batch arrives after the daily run.</div>')
        for j in pending:
            b = j.get("brief") or {}
            c = j.get("critic") or {}
            kind = {"comment": "comment", "reply": "reply to a reply", "lead_reply": "lead reply", "post": "post"}.get(j["kind"], j["kind"])
            pills = f'<span class="pill">r/{E(j["sub"] or "")}</span><span class="pill">{kind}</span>'
            if j["comment_type"] == "mention":
                pills += '<span class="pill mention">mentions YuvGro</span>'
            if j["comment_type"] == "talking_points":
                pills += '<span class="pill tp">write it yourself</span>'
            if j["fit_score"] is not None:
                pills += f'<span class="pill">fit {j["fit_score"]}/10</span>'
            warn = ('<p class="flash err">This subreddit bans AI written comments. Replace the talking points below with your own words, then approve.</p>'
                    if j["comment_type"] == "talking_points" else "")
            notes = ""
            if c.get("notes") and j["kind"] == "lead_reply":
                notes = f'<details open><summary>Critic notes</summary><pre>{E(c["notes"])}</pre></details>'
            gap = f'<details><summary>Why this thread</summary><pre>{E(b.get("question", ""))}\n\nAdds: {E(b.get("gap_angle", ""))}\nAlready said: {E("; ".join(b.get("already_covered", [])))}</pre></details>' if b.get("question") else ""
            expires = parse(j["expires_at"]).astimezone(self.ctx.ist().tzinfo).strftime("%d %b %H:%M IST") if j["expires_at"] else ""
            out.append(
                f'<div class="card">{pills}<h3 style="margin:8px 0"><a href="{E(j["thread_url"] or "#")}" target="_blank" rel="noopener">{E(j["thread_title"] or "")}</a></h3>'
                f'<div class="meta">Expires {expires}</div>{warn}{gap}{notes}'
                + self.form("/approve", f'<input type="hidden" name="id" value="{j["id"]}"><textarea name="text">{E(j["draft_text"] or "")}</textarea>'
                            f'<button class="go">Approve and post</button>')
                + self.form("/skip", f'<input type="hidden" name="id" value="{j["id"]}"><button class="no">Skip</button>')
                + "</div>")
        for j in manual:
            b = j.get("brief") or {}
            out.append(
                f'<div class="card"><span class="pill tp">DM: send it yourself</span><span class="pill">from u/{E(b.get("from", ""))}</span>'
                f'<details open><summary>Their message</summary><pre>{E(b.get("message", ""))}</pre></details>'
                f'<p class="meta">Draft (never sent automatically; pricing is yours):</p><textarea readonly>{E(j["draft_text"] or "")}</textarea>'
                f'<p><a href="https://www.reddit.com/message/inbox/" target="_blank" rel="noopener">Open Reddit messages</a></p>'
                + self.form("/dm-done", f'<input type="hidden" name="id" value="{j["id"]}"><button class="no">Done</button>') + "</div>")
        if approved:
            rows = "".join(f'<tr><td>r/{E(j["sub"] or "")}</td><td>{E(j["thread_title"] or "")}</td><td>{E(j["status"])}</td></tr>' for j in approved)
            out.append(f'<div class="card"><b>Approved, posting shortly</b><table>{rows}</table>'
                       f'<p class="meta">Posts go out at least {self.ctx.cfg.settings["publisher"]["min_gap_minutes"]} minutes apart.</p></div>')
        return "".join(out)

    def status(self) -> str:
        ctx, s = self.ctx, self.ctx.store
        acc = account_state(ctx)
        hr = headroom(ctx, acc["phase"])
        stop = halted(s)
        parts = []
        if stop:
            parts.append('<div class="card stop"><b>Emergency stop</b><p>' + E(stop["reason"]) + '</p><p class="meta">Resolve the cause first (read any mod mail, check the account in a private window).</p>'
                         + self.form("/resume", '<button class="danger">Resume posting</button>') + "</div>")
        phase = "Phase 2: established" if acc["phase"] == "established" else "Phase 1: warm up"
        gate = "" if acc["phase"] == "established" else f' · gate in {acc["days_to_gate"]} days and {acc["karma_to_gate"] if acc["karma_to_gate"] is not None else "?"} karma'
        parts.append(f'<div class="card"><b>{phase}</b><div class="meta">u/{E(ctx.cfg.username)} · {acc["age_days"]} days old · karma {acc["karma"]}{gate}</div>'
                     f'<p>Today: {hr["today_count"]} of {ctx.cfg.base["session"]["comments_per_day"]["max"]} · replies left {hr["replies_remaining"]} · '
                     f'YuvGro mention now: {"yes" if hr["mention_ok"] else "no"}</p>'
                     + self.form("/run-now", '<button class="no">Run the daily search now</button>') + "</div>")
        paused = [p for p in s.policies() if p.get("paused_until") and parse(p["paused_until"]) > ctx.now()]
        if paused:
            rows = "".join(f'<tr><td>r/{E(p["sub"])}</td><td>until {parse(p["paused_until"]).date()}</td><td>{E(p.get("pause_reason") or "")}</td></tr>' for p in paused)
            parts.append(f'<div class="card"><b>Paused subreddits</b><table>{rows}</table></div>')
        runs = "".join(f'<tr><td>{E(r["kind"])}</td><td>{E((r["started_at"] or "")[:16])}</td><td>{E(r["status"])}</td>'
                       f'<td>{E(json.dumps(r.get("summary") or {}, ensure_ascii=False)[:160])}</td></tr>' for r in s.runs(12))
        parts.append(f'<div class="card"><b>Recent runs</b><table>{runs}</table></div>')
        posted = s.comments()[-10:][::-1]
        rows = "".join(f'<tr><td>r/{E(c["sub"])}</td><td>{E((c["thread_title"] or "")[:70])}</td><td>{E(c["status"])}</td>'
                       f'<td>{c["score_72h"] if c["score_72h"] is not None else (c["score_24h"] if c["score_24h"] is not None else "")}</td></tr>' for c in posted)
        parts.append(f'<div class="card"><b>Recent comments</b><table><tr><th>Sub</th><th>Thread</th><th>State</th><th>Score</th></tr>{rows}</table></div>')
        leads = s.leads(10)
        if leads:
            rows = "".join(f'<tr><td>u/{E(l["handle"] or "")}</td><td>{E(l["intent"] or "")}</td><td><a href="{E(l["thread_url"] or "#")}">thread</a></td>'
                           f'<td>{"in Notion" if l.get("notion_contact") else "not in Notion"}</td></tr>' for l in leads)
            parts.append(f'<div class="card"><b>Leads</b><table>{rows}</table></div>')
        warns = s.events(15, min_level="warn")
        if warns:
            rows = "".join(f'<tr><td>{E(e["ts"][:16])}</td><td>{E(e["level"])}</td><td>{E(e["agent"])}</td><td>{E(e["message"])}</td></tr>' for e in warns)
            parts.append(f'<div class="card"><b>Warnings</b><table>{rows}</table></div>')
        return "".join(parts)

    def proposals(self) -> str:
        s = self.ctx.store
        out = []
        for p in s.proposals():
            out.append(f'<div class="card"><span class="pill">{E(p["kind"].replace("_", " "))}</span><h3 style="margin:8px 0">{E(p["title"])}</h3>'
                       f'<pre>{E(p["detail"] or "")}</pre><div class="meta">Evidence: {E(p["evidence"] or "")}</div>'
                       + self.form("/proposal", f'<input type="hidden" name="id" value="{p["id"]}"><button class="go" name="action" value="approve">Approve</button>'
                                   f'<button class="no" name="action" value="reject">Reject</button>') + "</div>")
        if not out:
            out.append('<div class="card">No open proposals. Coach reviews every Sunday morning.</div>')
        ch = s.tunable_changes()
        if ch:
            rows = "".join(f'<tr><td>{E((c["applied_at"] or "")[:10])}</td><td>{E(c["key"])}</td><td>{E(str(c["old"]))} → {E(str(c["new"]))}</td>'
                           f'<td>{E(c["status"])}</td><td>{E(c["reason"] or "")}</td></tr>' for c in ch[:20])
            out.append(f'<div class="card"><b>Automatic changes</b><table>{rows}</table></div>')
        digests = sorted((self.ctx.cfg.data_dir / "digests").glob("*.md")) if (self.ctx.cfg.data_dir / "digests").exists() else []
        if digests:
            out.append(f'<div class="card"><b>Latest digest ({E(digests[-1].stem)})</b><pre>{E(digests[-1].read_text())}</pre></div>')
        return "".join(out)

    # -- actions -------------------------------------------------------------------------------
    def action(self, path: str, form: dict) -> tuple[str, str, bool]:
        if not hmac.compare_digest(form.get("csrf", ""), self.csrf):
            return "/", "Form expired, try again.", True
        ctx = self.ctx
        if path == "/approve":
            errs = publisher.approve(ctx, int(form["id"]), form.get("text", ""))
            return ("/", "Can't approve yet: " + " · ".join(errs), True) if errs else ("/", "Approved. It will post within a few minutes.", False)
        if path == "/skip":
            publisher.skip(ctx, int(form["id"]))
            return "/", "Skipped.", False
        if path == "/dm-done":
            ctx.store.update_job(int(form["id"]), status="sent_by_jeet")
            return "/", "Marked as sent.", False
        if path == "/resume":
            resume(ctx, "dashboard")
            return "/status", "Resumed. Posting continues on the next tick.", False
        if path == "/proposal":
            msg = coach.decide(ctx, int(form["id"]), form.get("action") == "approve")
            return "/proposals", msg, False
        if path == "/run-now":
            # PRAW isn't thread safe: the scheduler thread picks this up within ~15 seconds.
            ctx.store.kv_set("run_now_requested", True)
            return "/status", "Daily search requested. New comments appear on Approve in a few minutes.", False
        return "/", "Unknown action.", True

    # -- server ---------------------------------------------------------------------------------
    def handler(self):
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _session(self) -> str:
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE:
                        return v
                return ""

            def _authed(self) -> bool:
                if app.valid_session(self._session()):
                    return True
                if self.command == "GET":
                    self.send_response(303)
                    self.send_header("Location", f"/login?next={quote(self.path)}")
                    self.end_headers()
                else:
                    self._send(401, "Signed out. Reload the page and sign in.", "text/plain")
                return False

            def _read_form(self) -> dict:
                n = int(self.headers.get("Content-Length", "0") or 0)
                return {k: v[0] for k, v in parse_qs(self.rfile.read(min(n, 200_000)).decode(), keep_blank_values=True).items()}

            def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/health":
                    return self._send(200, "ok", "text/plain")
                if u.path == "/login":
                    q = parse_qs(u.query)
                    return self._send(200, app.login_page(q.get("next", ["/"])[0], q.get("failed", ["0"])[0] == "1"))
                if not self._authed():
                    return
                q = parse_qs(u.query)
                flash, err = q.get("m", [""])[0], q.get("e", ["0"])[0] == "1"
                try:
                    if u.path == "/":
                        return self._send(200, app.page("Approve", "/", app.queue(), flash, err))
                    if u.path == "/status":
                        return self._send(200, app.page("Status", "/status", app.status(), flash, err))
                    if u.path == "/proposals":
                        return self._send(200, app.page("Coach", "/proposals", app.proposals(), flash, err))
                except Exception as e:  # noqa: BLE001
                    app.ctx.store.event("error", "web", f"page error: {e}")
                    return self._send(500, app.page("Error", "", f'<div class="card">Something broke: {E(str(e))}</div>'))
                self._send(404, app.page("Not found", "", '<div class="card">Not found.</div>'))

            def do_POST(self):
                if urlparse(self.path).path == "/login":
                    form = self._read_form()
                    nxt = form.get("next", "/")
                    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"  # no open redirects
                    if not app.check_password(form.get("password", "")):
                        self.send_response(303)
                        self.send_header("Location", f"/login?failed=1&next={quote(nxt)}")
                        self.end_headers()
                        return
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"{COOKIE}={app.new_session()}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
                                                   f"HttpOnly; SameSite=Lax{secure}")
                    self.send_header("Location", nxt)
                    self.end_headers()
                    return
                if not self._authed():
                    return
                form = self._read_form()
                try:
                    dest, msg, err = app.action(urlparse(self.path).path, form)
                except Exception as e:  # noqa: BLE001
                    app.ctx.store.event("error", "web", f"action error: {e}")
                    dest, msg, err = "/", f"Error: {e}", True
                self.send_response(303)
                self.send_header("Location", f"{dest}?m={quote(msg)}&e={1 if err else 0}")
                self.end_headers()

        return H

    def serve(self, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, port), self.handler())
        self.ctx.store.event("info", "web", f"dashboard listening on {host}:{port}")
        return srv
