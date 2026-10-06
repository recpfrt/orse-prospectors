"""Per-business manual hold (CEO decision, 2026-10-06): forces MANUAL_REVIEW for one domain only.
All tests use temporary databases; the real data/state.sqlite must stay byte-identical."""
from tests import _isolation  # noqa: F401  (must be first)

import csv
import tempfile
import types
import unittest
from pathlib import Path

import run
from prospects import pipeline, state
from prospects.score import score
from tests.test_pipeline import base
from tests.test_state_isolation import fingerprint

REASON = "installer onzeker: drain-network pattern"
HOME = dict(customer_type="homeowner")


def save(con, domain: str, **kw) -> dict:
    p = score(base(**HOME, domain=domain, public_email=f"info@{domain}", **kw))
    state.record_attempt(con, domain, "test", 200)
    state.save_prospect(con, p)
    return p


def queue_of(con, domain: str) -> dict:
    import json
    return json.loads(con.execute("select data from prospects where domain = ?", (domain,)).fetchone()["data"])


class HoldTestCase(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "hold.sqlite"
        self.con = state.connect(self.db)

    def tearDown(self):
        self.con.close()
        run.close_connections()
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before, "real data/state.sqlite changed")
        self.tmp.cleanup()

    def args(self, **kw):
        return types.SimpleNamespace(db=str(self.db), **kw)


class TestManualHold(HoldTestCase):
    def test_prospects_without_hold_are_unchanged(self):
        state.add_hold(self.con, "held.nl", REASON)            # a hold on some OTHER domain
        variants = [dict(), dict(legal_form="unknown"), dict(customer_type="mixed"), dict(fetch_status=0)]
        for kw in variants:
            p = base(**{**HOME, **kw}, domain="free.nl")
            self.assertEqual(score(pipeline.apply_review(self.con, p)), score(p), kw)

    def test_hold_forces_manual_review_with_readable_reason(self):
        self.assertEqual(save(self.con, "riool.nl")["queue"], "READY")
        state.add_hold(self.con, "riool.nl", REASON)
        p = score(pipeline.apply_review(self.con, queue_of(self.con, "riool.nl")))
        self.assertEqual(p["queue"], "MANUAL_REVIEW")
        self.assertIn(f"manual hold (CEO): {REASON}", p["queue_reason"])
        self.assertIn("would otherwise be READY", p["queue_reason"])
        self.assertEqual(state.active_hold(self.con, "riool.nl")["reason"], REASON)

    def test_hold_lowers_kvk_check_too(self):
        save(self.con, "kvk.nl", legal_form="unknown")
        state.add_hold(self.con, "kvk.nl", REASON)
        p = score(pipeline.apply_review(self.con, queue_of(self.con, "kvk.nl")))
        self.assertEqual(p["queue"], "MANUAL_REVIEW")
        self.assertIn("would otherwise be KVK_CHECK", p["queue_reason"])

    def test_rejected_prospect_stays_rejected(self):
        save(self.con, "dead.nl", fetch_status=0)
        state.add_hold(self.con, "dead.nl", REASON)
        p = score(pipeline.apply_review(self.con, queue_of(self.con, "dead.nl")))
        self.assertEqual(p["queue"], "REJECTED")
        self.assertIn(REASON, p["queue_reason"])

    def test_empty_reason_is_refused(self):
        with self.assertRaises(ValueError):
            state.add_hold(self.con, "riool.nl", "   ")


class TestHoldCli(HoldTestCase):
    def test_add_release_and_audit_trail(self):
        save(self.con, "riool.nl")
        self.con.commit()
        run.cmd_hold_add(self.args(domain="riool.nl", reason=REASON))
        self.assertEqual(queue_of(self.con, "riool.nl")["queue"], "MANUAL_REVIEW")
        run.cmd_hold_release(self.args(domain="riool.nl", note="CEO verified installer"))
        p = queue_of(self.con, "riool.nl")
        self.assertEqual(p["queue"], "READY")
        self.assertNotIn("manual_hold_reason", p)                 # released hold does not linger
        audit = state.list_holds(self.con, include_released=True)
        self.assertEqual((audit[0]["reason"], audit[0]["release_note"]), (REASON, "CEO verified installer"))
        self.assertIsNotNone(audit[0]["released_at"])
        self.assertEqual(state.list_holds(self.con), [])

    def test_unknown_domain_is_refused(self):
        with self.assertRaises(SystemExit):
            run.cmd_hold_add(self.args(domain="nope.nl", reason=REASON))

    def test_hold_survives_rescore_and_network_sweep(self):
        save(self.con, "riool.nl", kvk_number="11111111")
        save(self.con, "other.nl", kvk_number="22222222")      # distinct KvK numbers: no network match
        self.con.commit()
        run.cmd_hold_add(self.args(domain="riool.nl", reason=REASON))
        run.cmd_rescore(self.args())
        self.assertEqual(queue_of(self.con, "riool.nl")["queue"], "MANUAL_REVIEW")
        self.assertEqual(queue_of(self.con, "other.nl")["queue"], "READY")


class TestHoldWithKvkImport(HoldTestCase):
    def _sheet(self, domains):
        path = Path(self.tmp.name) / "kvk_review_test.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(run.KVK_SHEET_COLUMNS)
            for d in domains:
                row = {c: "" for c in run.KVK_SHEET_COLUMNS}
                row.update({"domain": d, "is_bv (yes/no/uncertain)": "yes", "kvk_number": "12345678",
                            "entity_matches (yes/no)": "yes", "active (yes/no)": "yes",
                            "genuine_local_installer (yes/no)": "onzeker" if d == "riool.nl" else "ja",
                            "homeowner_fit_reasonable (yes/no)": "ja"})
                w.writerow([row[c] for c in run.KVK_SHEET_COLUMNS])
        return path

    def _import(self, with_hold: bool) -> dict:
        self.con.close()
        run.close_connections()
        self.db = Path(self.tmp.name) / f"import-{with_hold}.sqlite"
        self.con = state.connect(self.db)
        for d, kvk in (("riool.nl", "11111111"), ("kennemer.nl", "22222222")):
            save(self.con, d, legal_form="unknown", kvk_number=kvk)
        if with_hold:
            state.add_hold(self.con, "riool.nl", REASON)
        self.con.commit()
        run.cmd_kvk_import(self.args(file=str(self._sheet(["riool.nl", "kennemer.nl"]))))
        return {d: queue_of(self.con, d) for d in ("riool.nl", "kennemer.nl")}

    def test_import_unchanged_without_hold_and_respects_hold(self):
        plain = self._import(with_hold=False)
        self.assertEqual(plain["riool.nl"]["queue"], "READY")       # general 'onzeker' rule unchanged
        self.assertEqual(plain["kennemer.nl"]["queue"], "READY")
        held = self._import(with_hold=True)
        self.assertEqual(held["riool.nl"]["queue"], "MANUAL_REVIEW")
        self.assertIn(REASON, held["riool.nl"]["queue_reason"])
        self.assertEqual(held["kennemer.nl"]["queue"], plain["kennemer.nl"]["queue"])
        self.assertEqual(held["kennemer.nl"]["queue_reason"], plain["kennemer.nl"]["queue_reason"])
        self.assertEqual(self.con.execute("select count(*) from kvk_reviews").fetchone()[0], 2)
