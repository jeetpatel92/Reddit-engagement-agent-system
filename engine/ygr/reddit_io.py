"""Reddit access through the official Data API (PRAW), behind a small interface
so every agent can run against FakeIO in tests and fixture demos.

Two clients: `self.r` is Jeet's authenticated session (read, post, inbox);
`self.public` is app-only and sees what a logged-out visitor sees, which is how
Watcher detects removed comments and a shadowbanned account.

All PRAW/prawcore exceptions are translated into the small set below so the
agents can route them without importing PRAW.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from functools import wraps
from pathlib import Path


class RedditError(Exception):
    pass


class RedditRateLimited(RedditError):
    def __init__(self, msg: str, retry_after: float = 600):
        super().__init__(msg)
        self.retry_after = retry_after


class RedditForbidden(RedditError):
    """403: banned from the sub, thread restricted, or the token lost a scope."""


class RedditAuthError(RedditError):
    """The OAuth credentials themselves are bad: stop everything."""


class RedditNotFound(RedditError):
    pass


class RedditUnavailable(RedditError):
    pass


class ThreadClosed(RedditError):
    """Locked, archived or deleted: this thread can't take a comment any more."""


@dataclass
class RComment:
    id: str
    author: str
    body: str
    score: int
    replies: list = field(default_factory=list)


@dataclass
class Thread:
    id: str
    sub: str
    title: str
    body: str
    author: str
    created_utc: float
    num_comments: int
    score: int
    url: str
    archived: bool = False
    locked: bool = False
    over_18: bool = False
    removed: bool = False
    comments: list = field(default_factory=list)
    source: str = ""

    def age_hours(self, now: float | None = None) -> float:
        return ((now or time.time()) - self.created_utc) / 3600

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Thread":
        d = dict(d)
        d["comments"] = [c if isinstance(c, dict) else asdict(c) for c in d.get("comments", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class InboxItem:
    id: str                 # fullname, e.g. t1_abc / t4_abc
    kind: str               # comment_reply | post_reply | mention | message | mod | admin
    author: str
    body: str
    created_utc: float
    thread_id: str = ""
    parent_id: str = ""     # fullname of what they replied to (our comment for comment_reply)
    comment_id: str = ""    # their comment id (for replying back)
    subject: str = ""
    subreddit: str = ""


RATE_RE = re.compile(r"(\d+)\s*(minute|second)", re.I)


def _translate(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        import praw
        import prawcore
        try:
            return fn(*a, **kw)
        except prawcore.exceptions.TooManyRequests as e:
            raise RedditRateLimited(str(e), float(e.retry_after or 600)) from e
        except (prawcore.exceptions.InvalidToken, prawcore.exceptions.OAuthException) as e:
            raise RedditAuthError(str(e)) from e
        except (prawcore.exceptions.Forbidden, prawcore.exceptions.InsufficientScope) as e:
            raise RedditForbidden(str(e)) from e
        except (prawcore.exceptions.NotFound, prawcore.exceptions.Redirect) as e:
            raise RedditNotFound(str(e)) from e
        except (prawcore.exceptions.ServerError, prawcore.exceptions.RequestException,
                prawcore.exceptions.ResponseException) as e:
            raise RedditUnavailable(str(e)) from e
        except praw.exceptions.RedditAPIException as e:
            for item in e.items:
                et = (item.error_type or "").upper()
                if et == "RATELIMIT":
                    m = RATE_RE.search(item.message or "")
                    wait = 600.0
                    if m:
                        wait = float(m.group(1)) * (60 if m.group(2).lower().startswith("minute") else 1) + 30
                    raise RedditRateLimited(item.message or "RATELIMIT", wait) from e
                if et in ("THREAD_LOCKED", "TOO_OLD", "DELETED_LINK", "DELETED_COMMENT", "SUBREDDIT_NOTALLOWED",
                          "COMMENT_UNREPLIABLE", "USER_BLOCKED"):
                    raise ThreadClosed(f"{et}: {item.message}") from e
            raise RedditError(str(e)) from e
        except praw.exceptions.ClientException as e:
            raise RedditNotFound(str(e)) from e
    return wrapper


class PrawIO:
    is_live = True

    def __init__(self, env: dict | None = None):
        import praw
        env = env or os.environ
        ua = env.get("REDDIT_USER_AGENT") or f"python:yuvgro-engagement:v1.0 (by /u/{env.get('REDDIT_USERNAME', 'unknown')})"
        common = dict(client_id=env["REDDIT_CLIENT_ID"], client_secret=env.get("REDDIT_CLIENT_SECRET", ""), user_agent=ua)
        if env.get("REDDIT_REFRESH_TOKEN"):
            self.r = praw.Reddit(**common, refresh_token=env["REDDIT_REFRESH_TOKEN"])
        else:
            self.r = praw.Reddit(**common, username=env["REDDIT_USERNAME"], password=env["REDDIT_PASSWORD"])
        self.public = praw.Reddit(**common)
        self.public.read_only = True
        self._pending_read: dict[str, object] = {}

    # -- conversions ----------------------------------------------------------
    @staticmethod
    def _thread(s, source: str = "") -> Thread:
        body = s.selftext or ""
        return Thread(
            id=s.id, sub=s.subreddit.display_name, title=s.title, body=body,
            author=str(s.author) if s.author else "[deleted]", created_utc=float(s.created_utc),
            num_comments=int(s.num_comments), score=int(s.score), url=f"https://www.reddit.com{s.permalink}",
            archived=bool(getattr(s, "archived", False)), locked=bool(getattr(s, "locked", False)),
            over_18=bool(getattr(s, "over_18", False)),
            removed=body in ("[removed]", "[deleted]") or bool(getattr(s, "removed_by_category", None)),
            source=source,
        )

    # -- account ------------------------------------------------------------------
    @_translate
    def me(self) -> dict:
        u = self.r.user.me()
        return {"name": u.name, "total_karma": int(u.link_karma) + int(u.comment_karma),
                "comment_karma": int(u.comment_karma), "link_karma": int(u.link_karma), "created_utc": float(u.created_utc)}

    @_translate
    def account_public(self, username: str) -> dict:
        try:
            r = self.public.redditor(username)
            _ = r.id  # forces the fetch
            return {"visible": True, "suspended": bool(getattr(r, "is_suspended", False))}
        except Exception as e:  # NotFound for shadowbanned/deleted accounts
            import prawcore
            if isinstance(e, prawcore.exceptions.NotFound):
                return {"visible": False, "suspended": False}
            raise

    # -- reading ----------------------------------------------------------------------
    @_translate
    def new_posts(self, sub: str, limit: int = 25) -> list[Thread]:
        return [self._thread(s, f"new:{sub}") for s in self.r.subreddit(sub).new(limit=limit)]

    @_translate
    def search(self, query: str, subs: list[str] | None = None, sort: str = "new", time_filter: str = "day", limit: int = 25) -> list[Thread]:
        target = "+".join(subs) if subs else "all"
        return [self._thread(s, f"search:{query}") for s in
                self.r.subreddit(target).search(query, sort=sort, time_filter=time_filter, limit=limit)]

    @_translate
    def thread(self, thread_id: str, top_n: int = 8) -> Thread:
        s = self.r.submission(id=thread_id)
        s.comment_sort = "top"
        s.comments.replace_more(limit=0)
        t = self._thread(s)
        for c in list(s.comments)[:top_n]:
            replies = [RComment(r.id, str(r.author) if r.author else "[deleted]", r.body, int(r.score))
                       for r in list(c.replies)[:2]]
            t.comments.append(RComment(c.id, str(c.author) if c.author else "[deleted]", c.body, int(c.score), replies))
        return t

    @_translate
    def comment_text(self, comment_id: str) -> dict:
        c = self.r.comment(id=comment_id)
        return {"id": c.id, "author": str(c.author) if c.author else "[deleted]", "body": c.body,
                "thread_id": c.link_id[3:], "parent_id": c.parent_id}

    @_translate
    def rules(self, sub: str) -> dict:
        sr = self.r.subreddit(sub)
        rules = [{"name": r.short_name, "description": r.description or ""} for r in sr.rules]
        sidebar = (sr.public_description or "") + "\n\n" + (sr.description or "")[:6000]
        return {"rules": rules, "sidebar": sidebar.strip()}

    # -- writing --------------------------------------------------------------------------
    @_translate
    def reply_to_thread(self, thread_id: str, text: str) -> dict:
        c = self.r.submission(id=thread_id).reply(body=text)
        if c is None:
            raise RedditError("reply returned nothing (comment may have been filtered)")
        return {"id": c.id, "permalink": f"https://www.reddit.com{c.permalink}"}

    @_translate
    def reply_to_comment(self, comment_id: str, text: str) -> dict:
        c = self.r.comment(id=comment_id).reply(body=text)
        if c is None:
            raise RedditError("reply returned nothing (comment may have been filtered)")
        return {"id": c.id, "permalink": f"https://www.reddit.com{c.permalink}"}

    @_translate
    def find_my_comment(self, thread_id: str, parent_fullname: str | None = None) -> dict | None:
        """Crash recovery: did a post actually land before we lost track of it?"""
        for c in self.r.user.me().comments.new(limit=50):
            if c.link_id == f"t3_{thread_id}" and (parent_fullname is None or c.parent_id == parent_fullname):
                return {"id": c.id, "permalink": f"https://www.reddit.com{c.permalink}"}
        return None

    # -- watching ----------------------------------------------------------------------------
    @_translate
    def public_comment_state(self, comment_id: str) -> dict:
        """What a logged-out visitor sees. Removed, deleted and shadowbanned all read as not visible."""
        import praw
        c = self.public.comment(id=comment_id)
        try:
            c.refresh()
        except praw.exceptions.ClientException:
            return {"visible": False, "score": None, "replies": 0}
        visible = c.author is not None and c.body not in ("[removed]", "[deleted]")
        return {"visible": visible, "score": int(c.score), "replies": len(c.replies)}

    @_translate
    def inbox_unread(self, limit: int = 50) -> list[InboxItem]:
        import praw
        out = []
        for item in self.r.inbox.unread(limit=limit):
            self._pending_read[item.fullname] = item
            author = str(item.author) if item.author else ""
            if isinstance(item, praw.models.Comment):
                subject = (item.subject or "").lower()
                kind = "comment_reply" if "comment" in subject else "post_reply" if "post" in subject else "mention"
                out.append(InboxItem(item.fullname, kind, author, item.body, float(item.created_utc),
                                     thread_id=item.link_id[3:], parent_id=item.parent_id, comment_id=item.id,
                                     subject=item.subject or "", subreddit=str(item.subreddit)))
            else:
                if isinstance(item, praw.models.SubredditMessage) or getattr(item, "distinguished", None) == "moderator":
                    kind = "mod"
                elif author.lower() == "reddit" or getattr(item, "distinguished", None) == "admin":
                    kind = "admin"
                else:
                    kind = "message"
                out.append(InboxItem(item.fullname, kind, author, item.body, float(item.created_utc),
                                     subject=item.subject or "", subreddit=str(getattr(item, "subreddit", "") or "")))
        return out

    @_translate
    def mark_read(self, ids: list[str]) -> None:
        items = [self._pending_read.pop(i) for i in ids if i in self._pending_read]
        if items:
            self.r.inbox.mark_read(items)


class FakeIO:
    """Fixture-backed Reddit for tests and demos. Nothing leaves the machine."""

    is_live = False

    def __init__(self, fixture: dict | str | Path):
        if not isinstance(fixture, dict):
            fixture = json.loads(Path(fixture).read_text())
        self.data = fixture
        threads = []
        for t in fixture.get("threads", []):
            t = dict(t)
            if "age_hours" in t:  # fixtures store relative ages so they never go stale
                t["created_utc"] = time.time() - float(t.pop("age_hours")) * 3600
            threads.append(Thread.from_dict(t))
        self.threads = {t.id: t for t in threads}
        self.posted: list[dict] = []
        self.inbox = [InboxItem(**i) for i in fixture.get("inbox", [])]
        self.hidden_comments: set[str] = set()
        self.account_visible = True
        self.fail_next_post: Exception | None = None
        self._n = 0

    def me(self) -> dict:
        return dict(self.data.get("me", {"name": "test_user", "total_karma": 1, "comment_karma": 1,
                                         "link_karma": 0, "created_utc": time.time() - 3 * 86400}))

    def account_public(self, username: str) -> dict:
        return {"visible": self.account_visible, "suspended": False}

    def new_posts(self, sub: str, limit: int = 25) -> list[Thread]:
        return [Thread.from_dict({**t.to_dict(), "comments": [], "source": f"new:{sub}"})
                for t in self.threads.values() if t.sub.lower() == sub.lower()][:limit]

    def search(self, query: str, subs=None, sort="new", time_filter="day", limit=25) -> list[Thread]:
        words = {w.strip('"').lower() for w in query.split() if len(w) > 3}
        allowed = {s.lower() for s in subs} if subs else None
        out = []
        for t in self.threads.values():
            if allowed and t.sub.lower() not in allowed:
                continue
            hay = f"{t.title} {t.body}".lower()
            if words and any(w in hay for w in words):
                out.append(Thread.from_dict({**t.to_dict(), "comments": [], "source": f"search:{query}"}))
        return out[:limit]

    def thread(self, thread_id: str, top_n: int = 8) -> Thread:
        if thread_id not in self.threads:
            raise RedditNotFound(thread_id)
        return self.threads[thread_id]

    def comment_text(self, comment_id: str) -> dict:
        for p in self.posted:
            if p["id"] == comment_id:
                return {"id": comment_id, "author": self.me()["name"], "body": p["text"], "thread_id": p["thread_id"], "parent_id": ""}
        raise RedditNotFound(comment_id)

    def rules(self, sub: str) -> dict:
        return self.data.get("rules", {}).get(sub, {"rules": [{"name": "Be civil", "description": ""}], "sidebar": ""})

    def _post(self, thread_id: str, parent: str | None, text: str) -> dict:
        if self.fail_next_post:
            e, self.fail_next_post = self.fail_next_post, None
            raise e
        self._n += 1
        cid = f"fake{self._n}"
        self.posted.append({"id": cid, "thread_id": thread_id, "parent": parent, "text": text})
        return {"id": cid, "permalink": f"https://www.reddit.com/r/x/comments/{thread_id}/_/{cid}/"}

    def reply_to_thread(self, thread_id: str, text: str) -> dict:
        return self._post(thread_id, None, text)

    def reply_to_comment(self, comment_id: str, text: str) -> dict:
        return self._post("", comment_id, text)

    def find_my_comment(self, thread_id: str, parent_fullname=None) -> dict | None:
        for p in self.posted:
            if p["thread_id"] == thread_id:
                return {"id": p["id"], "permalink": ""}
        return None

    def public_comment_state(self, comment_id: str) -> dict:
        return {"visible": comment_id not in self.hidden_comments, "score": 3, "replies": 0}

    def inbox_unread(self, limit: int = 50) -> list[InboxItem]:
        return list(self.inbox)

    def mark_read(self, ids: list[str]) -> None:
        self.inbox = [i for i in self.inbox if i.id not in ids]


def make_reddit(fixture: str | None = None):
    if fixture:
        return FakeIO(fixture)
    if not os.environ.get("REDDIT_CLIENT_ID"):
        raise RedditAuthError("REDDIT_CLIENT_ID is not set: Reddit API access is required (see engine/README.md)")
    return PrawIO()
