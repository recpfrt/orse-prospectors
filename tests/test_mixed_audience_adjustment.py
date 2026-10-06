"""Recommendation B (Batch 2 diagnosis): a mixed-audience business passes the homeowner gate only when
homeowner_signal >= 3 x business_signal AND it has >= 3 distinct residential terms. All in memory."""
from tests import _isolation  # noqa: F401  (must be first)

import unittest

from prospects import state
from prospects.score import clear_homeowner, score
from tests.test_pipeline import base
from tests.test_state_isolation import fingerprint

DUAL = ["particulieren en bedrijven"]
THREE = {"homeowner": ["cv-ketel×5", "lekkage×3", "badkamer×2"], "business": []}
TWO = {"homeowner": ["cv-ketel×5", "lekkage×3"], "business": []}


def prospect(home, biz, evidence, dual=DUAL, **kw):
    return base(customer_type="homeowner", homeowner_signal=home, business_signal=biz,
                customer_type_evidence=evidence, dual_audience_evidence=dual, **kw)


class TestMixedAudienceAdjustment(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)

    def tearDown(self):
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before, "real data/state.sqlite changed")

    def test_homeowner_dominant_mixed_audience_passes(self):
        ok, why = clear_homeowner(prospect(55, 3, THREE))
        self.assertTrue(ok)
        self.assertIn("also serves business customers (particulieren en bedrijven)", why)
        r = score(prospect(55, 3, THREE))
        self.assertEqual(r["queue"], "READY")
        self.assertIn("also serves business customers", r["queue_reason"])

    def test_homeowner_dominant_but_fewer_than_3_residential_terms_stays_blocked(self):
        ok, why = clear_homeowner(prospect(55, 3, TWO))
        self.assertFalse(ok)
        self.assertIn("mixed audience", why)
        self.assertEqual(score(prospect(55, 3, TWO))["queue"], "MANUAL_REVIEW")

    def test_3x_threshold_is_inclusive(self):
        self.assertTrue(clear_homeowner(prospect(30, 10, THREE))[0])      # exactly 3x

    def test_below_3x_stays_blocked(self):
        ok, why = clear_homeowner(prospect(29, 10, THREE))                 # 2.9x
        self.assertFalse(ok)
        self.assertIn("mixed audience", why)

    def test_business_dominant_mixed_audience_stays_blocked(self):
        ok, why = clear_homeowner(prospect(15, 15, THREE))
        self.assertFalse(ok)
        self.assertIn("commercial signals too strong", why)                 # earlier rule, unchanged
        self.assertEqual(score(prospect(15, 15, THREE))["queue"], "MANUAL_REVIEW")

    def test_zero_business_signal_counts_as_dominant(self):
        self.assertTrue(clear_homeowner(prospect(14, 0, THREE))[0])

    def test_non_mixed_homeowner_logic_unchanged(self):
        for evidence in (TWO, THREE):
            ok, why = clear_homeowner(prospect(20, 0, evidence, dual=[]))
            self.assertTrue(ok)
            self.assertNotIn("business customers", why)
        thin = {"homeowner": ["airco×5"], "business": []}
        self.assertIn("too thin", clear_homeowner(prospect(20, 0, thin, dual=[]))[1])
        self.assertIn("commercial signals", clear_homeowner(prospect(20, 12, THREE, dual=[]))[1])
        self.assertIn("not clearly homeowner", clear_homeowner(base(customer_type="mixed"))[1])

    def test_ceo_homeowner_review_still_wins(self):
        self.assertFalse(clear_homeowner(prospect(55, 3, THREE, kvk_homeowner_ok="no"))[0])

    def test_other_rules_still_apply_to_passing_mixed_business(self):
        common = dict(home=55, biz=3, evidence=THREE)
        self.assertEqual(score(prospect(**common, legal_form="unknown"))["queue"], "KVK_CHECK")
        self.assertEqual(score(prospect(**common, public_email=None))["queue"], "MANUAL_REVIEW")
        self.assertEqual(score(prospect(**common, fetch_status=0))["queue"], "REJECTED")
        self.assertEqual(score(prospect(**common, domain="a.nl", final_url="https://b.nl/",
                                        public_email="info@b.nl"))["queue"], "KVK_CHECK")
