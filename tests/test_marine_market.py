"""Marine / shipping B2B false positives (rullensinstallatie.nl, 2026-10-07): an electrical installer for inland
shipping was READY because 'thuis' and 'vloerverwarming' (a ship supplier) looked residential.
Strong marine evidence + weak homeowner fit -> REJECTED; strong marine + clear homeowner work -> MANUAL_REVIEW;
an isolated marine word never changes anything. All in memory or on temporary databases."""
from tests import _isolation  # noqa: F401  (must be first)

import tempfile
import unittest
from pathlib import Path

from prospects import pipeline, state
from prospects.customer_type import marine_b2b, marine_evidence
from prospects.score import marine_market, score
from tests.test_pipeline import base
from tests.test_state_isolation import fingerprint

# Homepage text of rullensinstallatie.nl as fetched on 2026-10-07 (abridged, verbatim phrases).
RULLENS_TEXT = (
    "Rullens Installatiebedrijf b.v. Nieuwbouw binnenvaart Een complete installatie volgens de laatste normen. "
    "Wij zijn Rullens Installatiebedrijf b.v. Strategisch gevestigd, tussen Rotterdam en Antwerpen. "
    "We bieden complete elektrische installaties aan voor diverse takken van de binnenvaart. Dit doen we in "
    "samenwerking met diverse nautische, airco, vloerverwarming, sanitaire en tankalarmering leveranciers. "
    "we hebben al decennia ervaring in de scheepvaart. Als het schip in de vaart is genomen blijven we met "
    "plezier het onderhoud en service doen. We zorgen dat het schip up-to-date blijft. Zorgen dat de schepen "
    "aan de huidige regels voldoen. Aarding tankdek. Nieuwe stuurhuizen sluiten we opnieuw aan.")

RULLENS_STORED = {  # what the pipeline stored for rullensinstallatie.nl (customer type 'homeowner', fit 100)
    "domain": "rullensinstallatie.nl", "business_name": "Rullens Installatie b.v.", "legal_form": "bv",
    "legal_form_confidence": "strong", "legal_form_evidence": "business name: Rullens Installatie b.v.",
    "public_email": "info@rullensinstallatie.nl", "kvk_number": "18105826", "customer_type": "homeowner",
    "homeowner_signal": 12, "business_signal": 0, "dual_audience_evidence": None,
    "customer_type_evidence": {"homeowner": ["thuis×2", "spoed×1", "vloerverwarming×1", "airco×1"], "business": []},
}

RESIDENTIAL = {"homeowner": ["cv-ketel×3", "lekkage×3", "badkamer×2", "thuis×2"], "business": []}


def rullens(**kw):
    return base(**{**RULLENS_STORED, "homepage_excerpt": RULLENS_TEXT, **kw})


class MarineCase(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)

    def tearDown(self):
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before, "real data/state.sqlite changed")


class TestMarineEvidence(MarineCase):
    def test_rullens_text_is_strong_marine(self):
        ev = marine_evidence(RULLENS_TEXT.lower())
        self.assertIn("binnenvaart×2", ev)
        self.assertIn("scheepvaart×1", ev)
        strong, score_ = marine_b2b(ev)
        self.assertTrue(strong)
        self.assertGreaterEqual(score_, 6)

    def test_one_isolated_word_is_not_enough(self):
        for text in ("wij installeren ook airco's op uw jacht", "onze monteur werkte vroeger in de scheepvaart",
                     "marine marine marine offshore offshore"):     # repeated weak words, no sector term
            self.assertFalse(marine_b2b(marine_evidence(text))[0], text)

    def test_weak_words_alone_never_strong(self):
        self.assertFalse(marine_b2b(["schip×3", "schepen×3", "jacht×3", "aan boord×3"])[0])

    def test_two_sector_terms_are_strong(self):
        self.assertTrue(marine_b2b(["binnenvaart×1", "scheepswerven×1"])[0])

    def test_english_marine_terms(self):
        self.assertTrue(marine_b2b(marine_evidence("marine electrical systems for shipbuilding and yachts"))[0])

    def test_word_boundaries(self):
        self.assertEqual(marine_evidence("ultramarine verf en een jachthaven"), [])


class TestRullensRegression(MarineCase):
    def test_rullens_stored_record_is_rejected(self):
        r = score(rullens())
        self.assertEqual(r["queue"], "REJECTED")
        self.assertIn("marine/shipping B2B market", r["queue_reason"])
        self.assertIn("binnenvaart", r["queue_reason"])
        self.assertFalse(r["strong_prospect"])

    def test_rullens_rejected_from_stored_evidence_without_excerpt(self):
        r = score(rullens(homepage_excerpt=None, marine_evidence=marine_evidence(RULLENS_TEXT.lower())))
        self.assertEqual(r["queue"], "REJECTED")

    def test_without_marine_rule_rullens_would_be_ready(self):
        # Guards the fixture: the same record minus the marine text is the original false positive.
        self.assertEqual(score(rullens(homepage_excerpt="", marine_evidence=[]))["queue"], "READY")

    def test_rullens_rejected_regardless_of_kvk_verification(self):
        r = score(rullens(kvk_review="bv", kvk_entity_match="yes", kvk_active="yes"))
        self.assertEqual(r["queue"], "REJECTED")

    def test_rullens_type_never_in_prospect_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = state.connect(Path(tmp) / "m.sqlite")
            try:
                for p in (rullens(), base(domain="woonwarm.nl", business_name="Woonwarm", city="Breda",
                                          public_email="info@woonwarm.nl", customer_type="homeowner",
                                          customer_type_evidence=RESIDENTIAL)):
                    state.record_attempt(con, p["domain"], "test", 200)
                    state.save_prospect(con, score(pipeline.apply_review(con, p)))
                con.commit()
                self.assertEqual([p["domain"] for p in pipeline.prospect_list(con)], ["woonwarm.nl"])
            finally:
                con.close()

    def test_strong_marine_with_clear_homeowner_work_goes_to_manual_review(self):
        # A homeowner installer that also does boats: not auto-rejected, not auto-READY.
        p = rullens(customer_type_evidence=RESIDENTIAL, homeowner_signal=30)
        self.assertFalse(marine_market(p)["weak_homeowner"])
        r = score(p)
        self.assertEqual(r["queue"], "MANUAL_REVIEW")
        self.assertIn("marine/shipping work alongside homeowner work", r["queue_reason"])

    def test_ceo_homeowner_confirmation_overrides_mixed_marine(self):
        p = rullens(customer_type_evidence=RESIDENTIAL, homeowner_signal=30, kvk_homeowner_ok="yes")
        self.assertEqual(score(p)["queue"], "READY")

    def test_marine_score_above_homeowner_signal_is_weak_fit(self):
        p = rullens(customer_type_evidence=RESIDENTIAL, homeowner_signal=10)
        self.assertTrue(marine_market(p)["weak_homeowner"])
        self.assertEqual(score(p)["queue"], "REJECTED")

    def test_homeowner_installer_with_isolated_marine_word_unchanged(self):
        p = base(customer_type="homeowner", customer_type_evidence=RESIDENTIAL,
                 homepage_excerpt="Cv-ketel storing? Wij helpen u thuis. Ook airco op uw jacht.")
        self.assertIsNone(marine_market(p))
        self.assertEqual(score(p)["queue"], "READY")


if __name__ == "__main__":
    unittest.main()
