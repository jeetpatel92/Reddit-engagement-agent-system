import unittest
from datetime import timedelta

from helpers import inbox_item, make_ctx

from ygr.agents import coach, conductor, publisher, watcher
from ygr.core import halted, resume, account_state
from ygr.reddit_io import RedditForbidden, RedditRateLimited
from ygr.store import iso


def pending(ctx):
    return sorted(ctx.store.jobs(statuses=("pending_approval",)), key=lambda j: j["id"])


class DailyRun(unittest.TestCase):
    def test_queues_filters_and_respects_phase_one(self):
        ctx, *_ = make_ctx()
        r = conductor.daily_run(ctx)
        self.assertEqual(r["status"], "ok", r)
        jobs = pending(ctx)
        self.assertTrue(1 <= len(jobs) <= 5, len(jobs))
        self.assertTrue(all(j["comment_type"] == "value" and not j["mention"] and not j["link"] for j in jobs))
        per_sub = {}
        for j in jobs:
            per_sub[j["sub"]] = per_sub.get(j["sub"], 0) + 1
        self.assertTrue(all(n <= 2 for n in per_sub.values()), per_sub)
        seen = {r["thread_id"]: r["disposition"] for r in ctx.store._rows("SELECT * FROM seen_threads")}
        self.assertEqual(seen.get("fxG"), "blocked_sub")      # r/Entrepreneur held until 50 karma
        self.assertEqual(seen.get("fxI"), "filtered")         # locked
        self.assertEqual(seen.get("fxJ"), "screened_out")     # "looking for projects"
        self.assertEqual(conductor.daily_run(ctx)["status"], "already_ran")
        self.assertTrue(any("ready for your approval" in s["title"] for s in ctx.notify.sent))

    def test_budget_stops_drafting_but_keeps_run_recorded(self):
        ctx, *_ = make_ctx(budget=(0, 0))
        ctx.llm.usage.input_tokens = 1  # already over budget
        r = conductor.daily_run(ctx)
        self.assertIn(r["status"], ("error", "partial"))
        self.assertIn("budget", r.get("error", ""))

    def test_phase_two_allows_one_disclosed_mention_with_link(self):
        ctx, script, clock, *_ = make_ctx(karma=150, created_days_ago=30)
        for i in range(12):  # history so the 10% ratio has room
            ctx.store.record_comment(comment_id=f"h{i}", kind="comment", sub=f"sub{i}", text=f"history {i} " * 20,
                                     posted_at=iso(clock() - timedelta(days=2, hours=i)), status="posted", mention=0)
        script.op_asked = True
        script.promo_subs = {"socialmedia"}
        script.mention_text = ("I run a small agency (YuvGro) so take this with that in mind, but the first thing I check "
                               "is whether the basics are covered. Full checklist on my agency's blog: "
                               "https://yuvgro.com/seo-checklist/ and the short version is to fix crawl errors, titles and "
                               "internal links before anything clever.")
        r = conductor.daily_run(ctx)
        self.assertEqual(r["status"], "ok", r)
        mentions = [j for j in pending(ctx) if j["comment_type"] == "mention"]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["sub"], "socialmedia")  # the only sub whose rules allow a disclosed mention
        self.assertEqual(mentions[0]["link"], "https://yuvgro.com/seo-checklist/")
        others = {j["sub"]: j["comment_type"] for j in pending(ctx) if j["sub"] != "socialmedia"}
        self.assertTrue(all(not j["mention"] for j in pending(ctx) if j["sub"] != "socialmedia"))
        # With 150 karma r/Entrepreneur is no longer held, and its AI ban turns the job into talking points.
        if "Entrepreneur" in others:
            self.assertEqual(others.pop("Entrepreneur"), "talking_points")
        self.assertTrue(all(t == "value" for t in others.values()), others)
        self.assertEqual(account_state(ctx)["phase"], "established")


class Publishing(unittest.TestCase):
    def setUp(self):
        self.ctx, self.script, self.clock, self.reddit, _ = make_ctx()
        conductor.daily_run(self.ctx)
        self.jobs = pending(self.ctx)

    def test_fixture_posts_never_touch_the_real_manual_log(self):
        from ygr.config import ROOT
        log = ROOT / "log" / "comments.jsonl"
        before = log.read_text() if log.exists() else None
        for j in self.jobs:
            publisher.approve(self.ctx, j["id"])
            self.clock.advance(minutes=10)
            publisher.tick(self.ctx)
        self.assertTrue(self.reddit.posted)
        self.assertEqual(log.read_text() if log.exists() else None, before)

    def test_approval_enforces_rules_on_edited_text(self):
        j = self.jobs[0]
        errs = publisher.approve(self.ctx, j["id"], j["draft_text"] + " A follow-up thought.")
        self.assertTrue(any("hyphen" in e for e in errs), errs)
        self.assertEqual(publisher.approve(self.ctx, j["id"]), [])
        self.assertEqual(self.ctx.store.job(j["id"])["status"], "approved")

    def test_posts_are_spaced_and_alternate_subs(self):
        for j in self.jobs:
            self.assertEqual(publisher.approve(self.ctx, j["id"]), [])
        self.assertEqual(publisher.tick(self.ctx), "posted")
        self.assertIsNone(publisher.tick(self.ctx))           # gap not reached
        self.clock.advance(minutes=9)
        self.assertEqual(publisher.tick(self.ctx), "posted")
        posted = self.ctx.store.comments()
        self.assertEqual(len(posted), 2)
        if len({j["sub"] for j in self.jobs}) > 1:
            self.assertNotEqual(posted[0]["sub"], posted[1]["sub"])
        self.assertEqual(len(self.reddit.posted), 2)

    def test_nothing_posts_without_approval(self):
        for _ in range(3):
            self.clock.advance(minutes=10)
            self.assertIsNone(publisher.tick(self.ctx))
        self.assertEqual(self.reddit.posted, [])

    def test_unapproved_jobs_expire(self):
        self.clock.advance(hours=13)
        publisher.tick(self.ctx)
        self.assertTrue(all(self.ctx.store.job(j["id"])["status"] == "expired" for j in self.jobs))

    def test_rate_limit_reschedules(self):
        j = self.jobs[0]
        publisher.approve(self.ctx, j["id"])
        self.reddit.fail_next_post = RedditRateLimited("slow down", retry_after=540)
        self.assertEqual(publisher.tick(self.ctx), "rate_limited")
        job = self.ctx.store.job(j["id"])
        self.assertEqual(job["status"], "approved")
        self.assertIsNone(publisher.tick(self.ctx))
        self.clock.advance(minutes=10)
        self.assertEqual(publisher.tick(self.ctx), "posted")

    def test_forbidden_pauses_the_sub(self):
        j = self.jobs[0]
        publisher.approve(self.ctx, j["id"])
        self.reddit.fail_next_post = RedditForbidden("403")
        self.assertEqual(publisher.tick(self.ctx), "forbidden")
        pol = self.ctx.store.policy(j["sub"])
        self.assertTrue(pol["paused_until"])

    def test_crash_mid_post_never_double_posts(self):
        j = self.jobs[0]
        publisher.approve(self.ctx, j["id"])
        # Simulate: the comment landed on Reddit, then the process died before recording it.
        self.reddit.posted.append({"id": "landed1", "thread_id": j["thread_id"], "parent": None, "text": "x"})
        self.ctx.store.update_job(j["id"], status="posting", posting_started_at=iso(self.clock() - timedelta(minutes=10)))
        publisher.tick(self.ctx)
        self.assertEqual(self.ctx.store.job(j["id"])["status"], "posted")
        self.assertEqual(self.ctx.store.job(j["id"])["comment_id"], "landed1")
        self.assertEqual(len(self.reddit.posted), 1)

    def test_talking_points_need_jeets_own_words(self):
        j = self.jobs[0]
        self.ctx.store.update_job(j["id"], comment_type="talking_points")
        errs = publisher.approve(self.ctx, j["id"])
        self.assertTrue(errs and "own words" in errs[0])


class Watching(unittest.TestCase):
    def setUp(self):
        self.ctx, self.script, self.clock, self.reddit, _ = make_ctx()

    def _post(self, sub, cid, hours_ago=2):
        return self.ctx.store.record_comment(comment_id=cid, kind="comment", sub=sub, text=f"text {cid} " * 20,
                                             thread_id="fxC", posted_at=iso(self.clock() - timedelta(hours=hours_ago)),
                                             status="posted")

    def test_removals_pause_sub_then_trip_global(self):
        for cid in ("r1", "r2"):
            self._post("googleads", cid)
            self.reddit.hidden_comments.add(cid)
        watcher.checkpoints(self.ctx)
        self.assertTrue(self.ctx.store.policy("googleads")["paused_until"])
        self.assertIsNone(halted(self.ctx.store))
        self._post("FacebookAds", "r3")
        self.reddit.hidden_comments.add("r3")
        watcher.checkpoints(self.ctx)
        self.assertIsNotNone(halted(self.ctx.store))

    def test_checkpoint_records_score(self):
        self._post("googleads", "ok1", hours_ago=25)
        watcher.checkpoints(self.ctx)
        c = self.ctx.store.comment_by_reddit_id("ok1")
        self.assertEqual(c["score_24h"], 3)
        self.assertIn("24h", c["checkpoints"])

    def test_shadowban_trips_global(self):
        self.reddit.account_visible = False
        watcher.health(self.ctx)
        self.assertIn("not visible", halted(self.ctx.store)["reason"])

    def test_mod_mail_halts_and_resume_restores(self):
        conductor.daily_run(self.ctx)
        j = pending(self.ctx)[0]
        publisher.approve(self.ctx, j["id"])
        self.reddit.inbox = [inbox_item(id="t4_mod", kind="mod", author="", subject="Rule 1", body="Please stop.")]
        watcher.inbox(self.ctx)
        self.assertIsNotNone(halted(self.ctx.store))
        self.assertIsNone(publisher.tick(self.ctx))
        resume(self.ctx)
        self.assertEqual(publisher.tick(self.ctx), "posted")

    def test_reply_and_lead_routing(self):
        self._post("googleads", "fake1")
        self.reddit.inbox = [
            inbox_item(id="t1_q", comment_id="q", parent_id="t1_fake1", body="Would you still do this with a small budget?"),
            inbox_item(id="t1_l", comment_id="l", parent_id="t1_fake1", body="Could I hire you to look at ours? What's the price?"),
            inbox_item(id="t4_dm", kind="message", author="owner", subject="hi", body="Can I hire you for our dental clinic?",
                       thread_id="", parent_id="", comment_id=""),
        ]
        stats = watcher.inbox(self.ctx)
        self.assertEqual(stats["replies_drafted"], 1)
        kinds = sorted(j["kind"] for j in self.ctx.store.jobs())
        self.assertEqual(kinds, ["dm_draft", "lead_reply", "reply"])
        self.assertEqual(len(self.ctx.store.leads()), 2)
        dm = next(j for j in self.ctx.store.jobs() if j["kind"] == "dm_draft")
        self.assertEqual(dm["status"], "manual")
        self.assertTrue(publisher.approve(self.ctx, dm["id"]))  # DMs can never be approved for posting
        self.assertTrue(any(e["message"].startswith("dry run: would write lead") for e in self.ctx.store.events(50)))
        self.assertEqual(self.reddit.inbox, [])


class Learning(unittest.TestCase):
    def test_coach_changes_are_bounded_and_proposals_wait(self):
        ctx, _s, clock, _r, notes = make_ctx()
        for i in range(24):
            topic = "ai_search" if i < 12 else "local_seo_reviews"
            score = 12 if topic == "ai_search" else 1
            ctx.store.record_comment(comment_id=f"c{i}", kind="comment", sub="SEO" if i % 2 else "smallbusiness",
                                     topic=topic, text=f"comment number {i} " * 25, draft_text=f"comment number {i} " * 25,
                                     posted_at=iso(clock() - timedelta(days=5)), status="posted", score_72h=score)
        r = coach.weekly(ctx)
        self.assertEqual(r["status"], "ok", r)
        w = ctx.cfg.tunables["topic_weights"]
        self.assertLessEqual(w["ai_search"], 1.2)
        self.assertGreaterEqual(w["local_seo_reviews"], round(1 / 1.2, 2))
        self.assertGreater(w["ai_search"], w["local_seo_reviews"])
        props = ctx.store.proposals()
        self.assertEqual(len(props), 2)
        self.assertIn("chatgpt citations", ctx.cfg.tunables["queries_added"]["ai_search"])
        # A learned note only lands after the eval; a new sub lands on approval.
        note = next(p for p in props if p["kind"] == "learned_note")
        coach.decide(ctx, note["id"], True)
        coach.apply_pending_notes(ctx, lambda c: {"regression": False, "metrics": {"pass_rate": 1.0}})
        self.assertEqual(notes, [("Open with the fix", "Open with the fix.")])
        sub = next(p for p in props if p["kind"] == "new_sub")
        coach.decide(ctx, sub["id"], True)
        self.assertEqual(ctx.cfg.tunables["extra_subs"]["PPC"], "paid_social_orm")

    def test_regressing_note_is_blocked(self):
        ctx, *_rest, notes = make_ctx()
        pid = ctx.store.add_proposal(kind="learned_note", title="Bad idea", detail="x", evidence="", payload={})
        coach.decide(ctx, pid, True)
        coach.apply_pending_notes(ctx, lambda c: {"regression": True, "metrics": {"pass_rate": 0.2}})
        self.assertEqual(notes, [])
        self.assertEqual(ctx.store.proposal(pid)["status"], "blocked_by_eval")

    def test_bad_change_is_reverted(self):
        ctx, _s, clock, *_ = make_ctx()
        ctx.cfg.tunables["target_words"] = 200
        ctx.store.add_tunable_change(key="target_words", old="170", new="200", reason="t", baseline=6.0,
                                     applied_at=iso(clock() - timedelta(days=8)))
        for i in range(6):
            ctx.store.record_comment(comment_id=f"a{i}", kind="comment", sub="SEO", text=f"after {i} " * 20,
                                     posted_at=iso(clock() - timedelta(days=3)), status="posted", score_72h=1)
        coach.weekly(ctx)
        self.assertEqual(ctx.cfg.tunables["target_words"], 170)


class Scheduling(unittest.TestCase):
    def test_scheduler_runs_daily_once_at_22_ist(self):
        from ygr.scheduler import Scheduler
        ctx, _s, clock, *_ = make_ctx()
        clock.t = clock.t.replace(hour=16, minute=0)  # 21:30 IST: too early
        sch = Scheduler(ctx)
        sch.once()
        self.assertIsNone(ctx.store.last_run("daily"))
        clock.advance(minutes=40)  # 22:10 IST
        sch.once()
        self.assertEqual(ctx.store.last_run("daily")["status"], "ok")
        clock.advance(minutes=30)
        sch.once()
        self.assertEqual(len([r for r in ctx.store.runs() if r["kind"] == "daily"]), 1)

    def test_dashboard_approve_flow(self):
        from ygr.web import App
        ctx, *_ = make_ctx()
        conductor.daily_run(ctx)
        app = App(ctx, "secret")
        html = app.queue()
        self.assertIn("Approve and post", html)
        j = pending(ctx)[0]
        dest, msg, err = app.action("/approve", {"csrf": app.csrf, "id": str(j["id"]), "text": j["draft_text"]})
        self.assertFalse(err, msg)
        dest, msg, err = app.action("/approve", {"csrf": "wrong", "id": str(j["id"])})
        self.assertTrue(err)
        self.assertIn("Phase 1", app.status())

    def test_dashboard_sessions_and_login_throttle(self):
        from ygr.web import App
        ctx, *_ = make_ctx()
        app = App(ctx, "secret")
        tok = app.new_session()
        self.assertTrue(app.valid_session(tok))
        self.assertFalse(app.valid_session(tok[:-1] + ("0" if tok[-1] != "0" else "1")))
        self.assertFalse(app.valid_session("9999999999.forged"))
        self.assertFalse(App(ctx, "other").valid_session(tok))
        for _ in range(5):
            self.assertFalse(app.check_password("nope"))
        self.assertFalse(app.check_password("secret"))  # throttled after 5 failures


if __name__ == "__main__":
    unittest.main()
