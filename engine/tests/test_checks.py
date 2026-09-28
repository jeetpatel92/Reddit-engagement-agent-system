import json
import unittest

from ygr import checks
from ygr.config import ROOT, load


def ctx(**kw):
    cfg = load(dry_run=True)
    base = dict(kind="comment", mention_allowed=False, allowed_yuvgro_links=set(),
                neutral_domains=set(cfg.settings["links"]["neutral_domains"]), neutral_links_ok=False, history=[],
                min_words=40, max_words=320, blocked_terms=["Acme Dental"], proof_numbers=cfg.proof_numbers(),
                disclosure_patterns=cfg.settings["disclosure_patterns"])
    base.update(kw)
    return checks.GateContext(**base)


LONG = " ".join(["word"] * 45)


class GateOne(unittest.TestCase):
    def codes(self, text, **kw):
        return {v.code for v in checks.gate1(text, ctx(**kw))}

    @unittest.skipUnless((ROOT / "log" / "comments.jsonl").exists(), "no local comment log (public checkout)")
    def test_human_approved_comments_pass(self):
        """Every comment actually posted from this checkout must pass the gate it would face today."""
        rows = [json.loads(l) for l in (ROOT / "log" / "comments.jsonl").read_text().splitlines() if l.strip()]
        for r in rows:
            self.assertEqual(self.codes(r["text"]), set(), r["thread_title"])

    def test_sample_drafts_pass(self):
        from helpers import GOOD_TEXT
        for tid, text in GOOD_TEXT.items():
            self.assertEqual(self.codes(text), set(), tid)

    def test_hyphens_and_dashes_blocked_but_urls_exempt(self):
        self.assertIn("hyphen", self.codes(LONG + " a follow-up"))
        self.assertIn("hyphen", self.codes(LONG + " this — that"))
        self.assertIn("hyphen", self.codes(LONG + " 5–10"))
        url_only = LONG + " https://yuvgro.com/local-seo-guide-for-multi-location-businesses/ I run YuvGro"
        link = "https://yuvgro.com/local-seo-guide-for-multi-location-businesses/"
        self.assertNotIn("hyphen", self.codes(url_only, mention_allowed=True, allowed_yuvgro_links={link}))

    def test_advice_numbers_pass_track_record_numbers_must_be_proof_points(self):
        self.assertNotIn("self_number", self.codes(LONG + " I'd give a creative 7 days before judging it."))
        self.assertIn("self_number", self.codes(LONG + " I've grown clients by 300% in a year."))
        self.assertNotIn("self_number", self.codes(LONG + " I've got 10+ years of experience in this."))

    def test_mentions_need_permission_and_disclosure(self):
        self.assertIn("mention_not_allowed", self.codes(LONG + " YuvGro can help."))
        self.assertIn("missing_disclosure", self.codes(LONG + " YuvGro can help.", mention_allowed=True))
        self.assertEqual(set(), self.codes(LONG + " I run a small agency (YuvGro) and this is what I check.", mention_allowed=True))

    def test_links(self):
        self.assertIn("link_not_allowed", self.codes(LONG + " see https://support.google.com/google-ads/answer/1"))
        self.assertNotIn("link_not_allowed", self.codes(LONG + " see https://support.google.com/googleads/answer/1", neutral_links_ok=True))
        self.assertIn("link_not_allowed", self.codes(LONG + " see https://example.com/x", neutral_links_ok=True))
        self.assertIn("link_not_allowed", self.codes(LONG + " I run YuvGro, see https://yuvgro.com/seo-checklist/", mention_allowed=True))

    def test_prices_dms_blocked_terms_similarity(self):
        self.assertIn("self_price", self.codes(LONG + " At YuvGro I charge $799 a month for this."))
        self.assertNotIn("self_price", self.codes(LONG + " Most retainers land around $500 to $2,000 a month."))
        self.assertIn("dm_solicit", self.codes(LONG + " DM me for details."))
        self.assertNotIn("dm_solicit", self.codes(LONG + " Happy to, send me a DM.", kind="lead_reply"))
        self.assertIn("blocked_term", self.codes(LONG + " like we did for acme dental."))
        self.assertIn("too_similar", self.codes(LONG, history=[LONG]))
        self.assertIn("length", self.codes("too short"))


if __name__ == "__main__":
    unittest.main()
