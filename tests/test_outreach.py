"""Outreach tracking: status transitions, contacted-company exclusion, prospect-list dedupe.
All tests use temporary databases; the real data/state.sqlite must stay byte-identical."""
from tests import _isolation  # noqa: F401  (must be first)

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prospects import outreach, pipeline, state
from prospects.score import score
from tests.test_pipeline import base
from tests.test_state_isolation import fingerprint


def ready(domain, **kw):
    kw.setdefault("public_email", f"info@{domain}")
    kw.setdefault("business_name", domain.split(".")[0].title())
    kw.setdefault("city", "Utrecht")
    return base(customer_type="homeowner", domain=domain, **kw)


class OutreachCase(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.con = state.connect(Path(self.tmp.name) / "o.sqlite")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before)

    def save(self, p):
        state.record_attempt(self.con, p["domain"], "test", 200)
        p = score(pipeline.apply_review(self.con, p))
        state.save_prospect(self.con, p)
        self.con.commit()
        return p

    def rescore(self, domain):
        import json
        row = self.con.execute("select data from prospects where domain = ?", (domain,)).fetchone()
        p = {k: v for k, v in json.loads(row["data"]).items() if k not in pipeline.STRIP}
        p = score(pipeline.apply_review(self.con, p))
        state.save_prospect(self.con, p)
        return p

    def contact(self, domain, email=None, **kw):
        return outreach.record_contact(self.con, domain, domain.title(), email or f"info@{domain}", "2026-10-06", **kw)


class TestTransitions(OutreachCase):
    def test_record_contact(self):
        r = self.contact("a.nl", subject="Onderwerp", message_version="draft-1", follow_up_at="2026-10-13")
        self.assertEqual((r["status"], r["contacted_at"], r["follow_up_at"]), ("CONTACTED", "2026-10-06", "2026-10-13"))
        events = self.con.execute("select to_status from outreach_events where domain='a.nl'").fetchall()
        self.assertEqual([e[0] for e in events], ["CONTACTED"])

    def test_record_contact_without_email(self):
        r = outreach.record_contact(self.con, "https://www.ventilatietotaal.nl/", "VentilatieTotaal", None,
                                    "2026-10-06", subject="Onderwerp", message_version="V1")
        self.assertEqual((r["domain"], r["email"], r["status"]), ("ventilatietotaal.nl", None, "CONTACTED"))
        self.assertIn("ventilatietotaal.nl", outreach.excluded_domains(self.con))
        self.assertIsNotNone(outreach.contact_match(self.con, ready("ventilatietotaal.nl")))
        self.assertIsNone(outreach.contact_match(self.con, ready("andere-installateur.nl")))

    def test_happy_path_to_customer(self):
        self.contact("a.nl")
        for s in ("REPLIED", "INTERESTED", "PILOT", "CUSTOMER"):
            self.assertEqual(outreach.set_status(self.con, "a.nl", s)["status"], s)
        self.assertEqual(self.con.execute("select count(*) from outreach_events").fetchone()[0], 5)

    def test_follow_up_loop_and_reply_metadata(self):
        self.contact("a.nl")
        outreach.set_status(self.con, "a.nl", "FOLLOW_UP", follow_up_at="2026-10-20", note="follow-up 1 sent")
        r = outreach.set_status(self.con, "a.nl", "REPLIED", reply_type="question", next_action="answer pricing")
        self.assertEqual((r["reply_type"], r["next_action"], r["follow_up_at"]), ("question", "answer pricing", "2026-10-20"))
        self.assertIn("follow-up 1 sent", r["notes"])

    def test_invalid_transitions_rejected(self):
        self.contact("a.nl")
        with self.assertRaises(outreach.OutreachError):
            outreach.set_status(self.con, "a.nl", "CUSTOMER")          # skipping the conversation
        with self.assertRaises(outreach.OutreachError):
            outreach.set_status(self.con, "a.nl", "BOGUS")
        with self.assertRaises(outreach.OutreachError):
            self.contact("a.nl")                                       # first contact recorded twice
        with self.assertRaises(outreach.OutreachError):
            outreach.set_status(self.con, "unknown.nl", "REPLIED")

    def test_do_not_contact_from_anywhere_and_final(self):
        self.contact("a.nl")
        outreach.set_status(self.con, "a.nl", "DO_NOT_CONTACT", reply_type="unsubscribe")
        for s in outreach.STATUSES:
            with self.assertRaises(outreach.OutreachError):
                outreach.set_status(self.con, "a.nl", s)

    def test_not_contacted_needs_record_contact(self):
        outreach.add_not_contacted(self.con, "b.nl", "B")
        with self.assertRaises(outreach.OutreachError):
            outreach.set_status(self.con, "b.nl", "CONTACTED")
        self.assertEqual(self.contact("b.nl")["status"], "CONTACTED")

    def test_input_validation(self):
        with self.assertRaises(outreach.OutreachError):
            outreach.record_contact(self.con, "a.nl", "A", "info@a.nl\r\nBcc: x@y.nl", "2026-10-06")
        with self.assertRaises(outreach.OutreachError):
            outreach.record_contact(self.con, "a.nl", "A", "info@a.nl", "06-10-2026")
        with self.assertRaises(Exception):
            outreach.record_contact(self.con, "a.nl", " ", "info@a.nl", "2026-10-06")

    def test_domain_normalised(self):
        outreach.record_contact(self.con, "https://www.A.nl/contact", "A", "info@a.nl", "2026-10-06")
        self.assertIsNotNone(outreach.get(self.con, "a.nl"))

    def test_no_credential_columns(self):
        cols = {r[1] for r in self.con.execute("pragma table_info(outreach)")}
        self.assertFalse(cols & {"password", "token", "smtp_password", "body", "message_body"})


class TestExclusion(OutreachCase):
    def test_contacted_prospect_leaves_ready_and_prospect_list(self):
        self.save(ready("a.nl"))
        self.assertEqual([p["domain"] for p in pipeline.prospect_list(self.con)], ["a.nl"])
        self.contact("a.nl")
        p = self.rescore("a.nl")
        self.assertEqual(p["queue"], "CONTACTED")
        self.assertIn("would otherwise be READY", p["queue_reason"])
        self.assertEqual(pipeline.prospect_list(self.con), [])

    def test_match_by_email_domain_kvk_and_business_identity(self):
        self.save(ready("feiken.nl", kvk_number="11111111", business_name="Feiken Verwarming", city="Leusden"))
        self.contact("feiken.nl")
        cases = {
            "feiken-cv.nl": dict(public_email="planning@feiken.nl"),                       # email domain
            "other-site.nl": dict(kvk_number="11111111", public_email="info@other-site.nl"),  # KvK number
            "feikenverwarming.nl": dict(business_name="Feiken Verwarming", city="Leusden",
                                        kvk_number=None, public_email="info@feikenverwarming.nl"),  # name + town
        }
        for domain, kw in cases.items():
            p = self.save(ready(domain, **kw))
            self.assertEqual(p["queue"], "CONTACTED", (domain, p["queue_reason"]))

    def test_unrelated_prospect_not_excluded(self):
        self.save(ready("feiken.nl", kvk_number="11111111"))
        self.contact("feiken.nl")
        p = self.save(ready("jansen.nl", kvk_number="22222222"))
        self.assertEqual(p["queue"], "READY")

    def test_not_contacted_status_does_not_exclude(self):
        outreach.add_not_contacted(self.con, "a.nl", "A")
        self.assertEqual(self.save(ready("a.nl"))["queue"], "READY")

    def test_do_not_contact_queue(self):
        self.save(ready("a.nl"))
        self.contact("a.nl")
        outreach.set_status(self.con, "a.nl", "DO_NOT_CONTACT")
        self.assertEqual(self.rescore("a.nl")["queue"], "DO_NOT_CONTACT")

    def test_contacted_domain_without_prospect_row_is_excluded_from_daily_pool(self):
        self.contact("ventilatietotaal.nl", email="service@ventilatietotaal.nl")
        self.assertIn("ventilatietotaal.nl", outreach.excluded_domains(self.con))
        pool = [{"domain": "ventilatietotaal.nl", "website": "https://ventilatietotaal.nl/", "source": "commoncrawl"},
                {"domain": "jansen.nl", "website": "https://jansen.nl/", "source": "commoncrawl"}]
        processed = []

        def fake_process(con, c):
            processed.append(c["domain"])
            p = score(pipeline.apply_review(con, ready(c["domain"])))
            state.record_attempt(con, c["domain"], "commoncrawl", 200)
            state.save_prospect(con, p)
            return p

        with mock.patch.object(pipeline.collect, "collect_commoncrawl", return_value=pool), \
                mock.patch.object(pipeline, "process", fake_process), \
                mock.patch.object(pipeline, "_red_list", return_value=(set(), set())):
            pipeline.run_daily(self.con, "unused.tsv", batch=10, seed=1)
        self.assertEqual(processed, ["jansen.nl"])


class TestProspectList(OutreachCase):
    def test_dedupe_by_email_kvk_and_business(self):
        self.save(ready("a.nl", public_email="info@a.nl", kvk_number="1"))
        self.save(ready("a-cv.nl", public_email="info@a.nl", kvk_number="2"))          # same email
        self.save(ready("b.nl", kvk_number="3"))
        self.save(ready("b-site.nl", kvk_number="3"))                                  # same KvK number
        self.save(ready("c.nl", business_name="Cee Installatie", city="Ede", kvk_number="4"))
        self.save(ready("c-other.nl", business_name="Cee Installatie", city="Ede", kvk_number="5"))
        domains = [p["domain"] for p in pipeline.prospect_list(self.con)]
        self.assertEqual(len(domains), 3, domains)
        self.assertEqual(len(set(domains)), 3)

    def test_kvk_verified_before_website_bv(self):
        self.save(ready("web.nl", kvk_number="1", qualification_score=100))
        state.record_kvk_review(self.con, "kvk.nl", "bv", "2", None, entity_match="yes", active="yes")
        self.save(ready("kvk.nl", kvk_number="2", legal_form="unknown", legal_form_confidence=None))
        rows = pipeline.prospect_list(self.con)
        self.assertEqual([(p["domain"], p["legal_form_status"]) for p in rows],
                         [("kvk.nl", "KVK_VERIFIED"), ("web.nl", "WEBSITE_BV")])


if __name__ == "__main__":
    unittest.main()
