"""Targeted improvements approved after Batch 1 (2026-10-06):
1. case-insensitive legal-form detection, 2. redirect / email-domain guard, 3. dual-audience -> mixed.
Cases are modelled on real Batch 1 / KvK-review findings. All tests use temporary databases."""
from tests import _isolation  # noqa: F401  (must be first)

import tempfile
import types
import unittest
from pathlib import Path

import run
from prospects import pipeline, state
from prospects.customer_type import dual_audience
from prospects.enrich import bv_evidence_strength, detect_legal_form_with_evidence as legal
from prospects.score import domain_guard, score
from tests.test_pipeline import base
from tests.test_state_isolation import fingerprint


class RealDbGuard(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)

    def tearDown(self):
        run.close_connections()
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before, "real data/state.sqlite changed")


class TestLegalFormCaseInsensitive(RealDbGuard):
    def test_lowercase_bv_in_business_name(self):            # Rullens Installatie b.v. (Batch 1 KVK_CHECK)
        form, ev = legal("", "Rullens Installatie b.v.", "rullensinstallatie.nl")
        self.assertEqual(form, "bv")
        self.assertEqual(bv_evidence_strength(ev, "Rullens Installatie b.v.", "rullensinstallatie.nl"), "strong")

    def test_lowercase_vof_with_spaced_name(self):           # 'FOEK installatie vof' on foekinstallatie.nl
        form, _ = legal("Totaal installateur voor uw woning. FOEK installatie vof MAIL: info@x",
                        "foekinstallatie.nl", "foekinstallatie.nl")
        self.assertEqual(form, "vof")

    def test_lowercase_bv_attached_to_own_name(self):
        form, ev = legal("© 2026 Jansen Installaties bv, Dorpsstraat 1", "Jansen Installaties", "jansen.nl")
        self.assertEqual(form, "bv")
        self.assertEqual(bv_evidence_strength(ev, "Jansen Installaties", "jansen.nl"), "strong")

    def test_supplier_bv_in_any_case_does_not_count(self):
        self.assertEqual(legal("Wij werken met Bosch b.v. en Remeha bv.", "Jansen Installaties", "jansen.nl")[0],
                         "unknown")

    def test_conflicting_forms_stay_unknown(self):
        self.assertEqual(legal("Jansen b.v. en Jansen vof", "Jansen", "jansen.nl")[0], "unknown")

    def test_uppercase_still_works(self):
        self.assertEqual(legal("© 2025 Jansen Installatietechniek B.V.", "Jansen Installatietechniek", "jansen.nl")[0],
                         "bv")


class TestDomainGuard(RealDbGuard):
    H = dict(customer_type="homeowner")

    def test_redirect_to_other_domain_blocks_automatic_ready(self):   # thtinstallatietechniek.nl -> breman.nl
        p = base(**self.H, domain="thtinstallatietechniek.nl", final_url="https://www.breman.nl/zakelijk",
                 public_email="info@breman.nl")
        r = score(p)
        self.assertEqual(r["queue"], "KVK_CHECK")
        self.assertIn("redirects from thtinstallatietechniek.nl to breman.nl", r["queue_reason"])

    def test_www_or_https_redirect_on_same_domain_is_fine(self):
        r = score(base(**self.H, domain="jansen.nl", final_url="https://www.jansen.nl/home", public_email="info@jansen.nl"))
        self.assertIsNone(r["domain_guard"])
        self.assertEqual(r["queue"], "READY")

    def test_email_on_other_domain_blocks_automatic_ready(self):      # bolverwarming.nl / planning@bol-vermeulen.nl
        r = score(base(**self.H, domain="bolverwarming.nl", final_url="https://bolverwarming.nl/",
                       public_email="planning@bol-vermeulen.nl"))
        self.assertEqual(r["queue"], "KVK_CHECK")
        self.assertIn("email domain bol-vermeulen.nl differs", r["queue_reason"])

    def test_ceo_verified_entity_clears_the_guard(self):              # vandalinstallaties.nl -> divo.nl (merger)
        common = dict(**self.H, domain="vandalinstallaties.nl", final_url="https://divo.nl/", public_email="info@divo.nl",
                      legal_form="unknown", kvk_review="bv", kvk_active="yes")
        self.assertEqual(score(base(**common, kvk_entity_match="yes"))["queue"], "READY")
        self.assertEqual(score(base(**common, kvk_entity_match="unknown"))["queue"], "KVK_CHECK")

    def test_guard_never_upgrades_a_queue(self):
        redirect = dict(domain="a.nl", final_url="https://b.nl/", public_email="info@b.nl")
        self.assertEqual(score(base(**self.H, **redirect, fetch_status=0))["queue"], "REJECTED")
        self.assertEqual(score(base(**self.H, **redirect, legal_form="vof"))["queue"], "MANUAL_REVIEW")
        self.assertEqual(score(base(**self.H, **redirect, legal_form="unknown"))["queue"], "KVK_CHECK")

    def test_no_domain_means_nothing_to_compare(self):
        self.assertIsNone(domain_guard({"public_email": "info@x.nl"}))


class TestDualAudience(RealDbGuard):
    H = dict(customer_type="homeowner")

    def test_explicit_particulieren_en_bedrijven_is_mixed(self):     # MC Elektraservice
        r = score(base(**self.H, homepage_excerpt="Elektraservice voor particulieren en bedrijven. 24/7 storing."))
        self.assertEqual(r["queue"], "MANUAL_REVIEW")
        self.assertIn("mixed audience", r["queue_reason"])

    def test_separate_business_vve_aannemer_pages_are_mixed(self):  # Feenstra Elektrotechniek
        text = "groepenkast elektricien voor bedrijven elektricien voor vve elektricien voor aannemers"
        self.assertTrue(dual_audience(text))
        self.assertEqual(score(base(**self.H, dual_audience_evidence=dual_audience(text)))["queue"], "MANUAL_REVIEW")

    def test_single_business_signal_is_not_enough(self):
        self.assertEqual(dual_audience("wij werken ook voor vve's in de regio"), [])

    def test_pure_homeowner_site_unchanged(self):
        r = score(base(**self.H, homepage_excerpt="Lekkage of een kapotte cv-ketel in uw woning? Wij helpen u snel."))
        self.assertEqual(r["queue"], "READY")

    def test_stored_evidence_takes_precedence_over_excerpt(self):
        r = score(base(**self.H, dual_audience_evidence=[], homepage_excerpt="voor particulieren en bedrijven"))
        self.assertEqual(r["queue"], "READY")


class TestPipelinePathsUseNewRules(RealDbGuard):
    def test_import_and_rescore_apply_guard_and_dual_audience(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.sqlite"
            con = state.connect(db)
            # is_installer marks the rows as current-format for import_previous (see report: legacy-detection issue)
            installer = dict(trade_signal=10, is_installer=True,
                             installer_evidence={"trade": ["installateur×5", "cv-ketel×5"], "unrelated": []})
            rows = [
                base(customer_type="homeowner", domain="redirect.nl", final_url="https://other.nl/",
                     public_email="info@other.nl", kvk_number="11111111", **installer),
                base(customer_type="homeowner", domain="mixed.nl", public_email="info@mixed.nl", kvk_number="22222222",
                     homepage_excerpt="installateur voor particulieren en bedrijven", **installer),
                base(customer_type="homeowner", domain="clean.nl", public_email="info@clean.nl", kvk_number="33333333",
                     homepage_excerpt="lekkage en cv-ketel in uw woning", **installer),
            ]
            pipeline.import_previous(con, rows)
            con.close()
            run.cmd_rescore(types.SimpleNamespace(db=str(db)))
            run.close_connections()
            con = state.connect(db)
            got = {r["domain"]: r["queue"] for r in con.execute("select domain, queue from prospects")}
            con.close()
        self.assertEqual(got, {"redirect.nl": "KVK_CHECK", "mixed.nl": "MANUAL_REVIEW", "clean.nl": "READY"})
