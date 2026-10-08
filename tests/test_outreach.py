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
        self.assertEqual((r["reply_type"], r["next_action"], r["follow_up_at"]), ("question", "answer pricing", None))
        self.assertIn("follow-up 1 sent", r["notes"])

    def test_reply_stop_and_final_outcomes_clear_follow_up_date(self):
        for i, s in enumerate(("REPLIED", "INTERESTED", "NOT_INTERESTED", "DO_NOT_CONTACT")):
            d = f"c{i}.nl"
            self.contact(d)
            outreach.update_details(self.con, d, follow_up_at="2026-10-13", note="policy")
            r = outreach.set_status(self.con, d, s, note="outcome")
            self.assertIsNone(r["follow_up_at"], s)
            ev = self.con.execute("select note from outreach_events where domain = ? order by id desc", (d,)).fetchone()
            self.assertIn("follow_up_at cleared (was 2026-10-13)", ev[0])
        self.contact("x.nl")
        with self.assertRaises(outreach.OutreachError):                    # a final outcome cannot carry a date
            outreach.set_status(self.con, "x.nl", "REPLIED", follow_up_at="2026-10-20")

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


class TestUpdateDetails(OutreachCase):
    """Follow-up date / recipient / next action change without a status change, always with an audit event."""

    def unknown_recipient(self):
        return outreach.record_contact(self.con, "ventilatietotaal.nl", "VentilatieTotaal", None, "2026-10-06",
                                       subject="Onderwerp", message_version="V1")

    def events(self, domain):
        return self.con.execute("select from_status, to_status, note from outreach_events where domain = ? "
                                "order by id", (domain,)).fetchall()

    def test_sets_follow_up_without_changing_status(self):
        self.contact("a.nl")
        r = outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="policy: 5 working days")
        self.assertEqual((r["status"], r["follow_up_at"], r["email"]), ("CONTACTED", "2026-10-13", "info@a.nl"))
        last = self.events("a.nl")[-1]
        self.assertEqual((last["from_status"], last["to_status"]), ("CONTACTED", "CONTACTED"))
        self.assertIn("follow_up_at: - -> 2026-10-13", last["note"])
        self.assertIn("policy: 5 working days", last["note"])

    def test_unknown_recipient_stays_unknown_when_only_the_date_changes(self):
        self.unknown_recipient()
        r = outreach.update_details(self.con, "ventilatietotaal.nl", follow_up_at="2026-10-13", note="policy")
        self.assertIsNone(r["email"])
        self.assertNotIn("email", self.events("ventilatietotaal.nl")[-1]["note"])

    def test_email_change_is_logged_with_old_and_new_value(self):
        self.unknown_recipient()
        r = outreach.update_details(self.con, "ventilatietotaal.nl", email=" Info@VentilatieTotaal.NL ",
                                    note="confirmed from Sent folder")
        self.assertEqual(r["email"], "info@ventilatietotaal.nl")
        self.assertIn("email: - -> info@ventilatietotaal.nl", self.events("ventilatietotaal.nl")[-1]["note"])

    def test_reschedule_keeps_history(self):
        self.contact("a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="first")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-22", note="second")
        notes = [e["note"] for e in self.events("a.nl")]
        self.assertIn("follow_up_at: 2026-10-13 -> 2026-10-22", notes[-1])
        self.assertEqual(len(notes), 3)                       # contact + 2 updates

    def test_same_values_are_a_noop(self):
        self.contact("a.nl")
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", note="x")
        before = len(self.events("a.nl"))
        outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-13", email="info@a.nl", note="again")
        self.assertEqual(len(self.events("a.nl")), before)

    def test_validation(self):
        self.contact("a.nl")
        bad = [dict(follow_up_at="13-10-2026", note="n"), dict(follow_up_at="2026-02-30", note="n"),
               dict(email="a@b.nl, c@d.nl", note="n"), dict(email="not-an-email", note="n"),
               dict(follow_up_at="2026-10-13", note=" "), dict(note="nothing to change")]
        for kw in bad:
            with self.assertRaises((outreach.OutreachError, ValueError), msg=str(kw)):
                outreach.update_details(self.con, "a.nl", **kw)
        with self.assertRaises(outreach.OutreachError):
            outreach.update_details(self.con, "untracked.nl", follow_up_at="2026-10-13", note="n")
        self.assertIsNone(outreach.get(self.con, "a.nl")["follow_up_at"])

    def test_closed_or_uncontacted_records_are_locked(self):
        outreach.add_not_contacted(self.con, "new.nl", "New")
        self.contact("dnc.nl")
        outreach.set_status(self.con, "dnc.nl", "DO_NOT_CONTACT", note="opt-out")
        self.contact("ni.nl")
        outreach.set_status(self.con, "ni.nl", "NOT_INTERESTED")
        for d in ("new.nl", "dnc.nl", "ni.nl"):
            with self.assertRaises(outreach.OutreachError, msg=d):
                outreach.update_details(self.con, d, follow_up_at="2026-10-13", note="n")

    def test_follow_up_status_record_can_be_rescheduled(self):
        self.contact("a.nl")
        outreach.set_status(self.con, "a.nl", "FOLLOW_UP", follow_up_at="2026-10-13", note="follow-up 1 sent")
        r = outreach.update_details(self.con, "a.nl", follow_up_at="2026-10-22", note="final follow-up")
        self.assertEqual((r["status"], r["follow_up_at"]), ("FOLLOW_UP", "2026-10-22"))


class TestUpdateCli(OutreachCase):
    def cli(self, *argv):
        import run
        db = str(Path(self.tmp.name) / "o.sqlite")
        with mock.patch("sys.argv", ["run.py", "--db", db, *argv]):
            try:
                run.main()
            finally:
                run.close_connections()

    def test_follow_up_only_does_not_touch_prospect_rows(self):
        self.save(ready("p.nl"))
        self.contact("c.nl")
        self.con.commit()
        before = [tuple(r) for r in self.con.execute("select * from prospects")]
        self.cli("outreach", "update", "c.nl", "--follow-up", "2026-10-13", "--note", "policy")
        self.assertEqual([tuple(r) for r in self.con.execute("select * from prospects")], before)
        self.assertEqual(outreach.get(self.con, "c.nl")["follow_up_at"], "2026-10-13")

    def test_email_change_rescores_contact_matching(self):
        self.save(ready("other-site.nl", public_email="info@vt-mail.nl"))
        outreach.record_contact(self.con, "vt.nl", "VT", None, "2026-10-06")
        self.con.commit()
        self.cli("outreach", "update", "vt.nl", "--email", "info@vt-mail.nl", "--note", "confirmed")
        q = self.con.execute("select queue from prospects where domain = 'other-site.nl'").fetchone()[0]
        self.assertEqual(q, "CONTACTED")                     # same email as a contacted company -> excluded

    def test_invalid_input_exits_without_change(self):
        self.contact("c.nl")
        self.con.commit()
        for bad in ("2026/10/13", "2026-02-30"):
            with self.assertRaises(SystemExit):
                self.cli("outreach", "update", "c.nl", "--follow-up", bad, "--note", "x")
        self.assertIsNone(outreach.get(self.con, "c.nl")["follow_up_at"])


if __name__ == "__main__":
    unittest.main()
