"""Legal-page B.V. detection and legal-form provenance (KVK_VERIFIED vs WEBSITE_BV vs UNKNOWN).
No network: enrich.polite_get is replaced by a fake site in every test."""
from tests import _isolation  # noqa: F401  (must be first)

import unittest
from unittest import mock

from prospects import enrich, report
from prospects.score import KVK_CHECK, READY, legal_form_status, score
from tests.test_pipeline import base

SITE = "https://www.jansen-installatie.nl/"
NAME = "Installatiebedrijf Jansen"

HOME = f"""<html lang="nl"><head><title>{NAME}</title></head><body>
<h1>{NAME}</h1><p>Wij helpen particulieren met cv-ketel onderhoud, lekkage en storing aan huis.
Voor uw badkamer en boiler bent u bij ons aan het juiste adres. Bel ons of mail info@jansen-installatie.nl.
3511 AB Utrecht</p>
<a href="/contact">Contact</a> <a href="/over-ons">Over ons</a>
<footer><a href="/privacyverklaring">Privacyverklaring</a> <a href="/algemene-voorwaarden">Algemene voorwaarden</a>
<a href="/cookiebeleid">Cookies</a> <a href="https://www.webbureau.nl/privacy">Privacy webbureau</a>
<a href="/files/voorwaarden.pdf">Voorwaarden (pdf)</a></footer></body></html>"""
CONTACT = "<html><body><p>Neem contact op: info@jansen-installatie.nl, 3511 AB Utrecht</p></body></html>"
ABOUT = "<html><body><p>Familiebedrijf sinds 1990, actief voor particulieren.</p></body></html>"
TERMS = ("<html><body><h1>Algemene voorwaarden</h1><p>Deze voorwaarden zijn van toepassing op alle opdrachten van "
         "Installatiebedrijf Jansen B.V., gevestigd te Utrecht, ingeschreven bij de KvK onder nummer 87654321. "
         "Opdrachtgever: iedere natuurlijke persoon, eenmanszaak of rechtspersoon. Zakelijke klanten, bedrijven, "
         "kantoren en zakelijke klanten vallen ook onder deze voorwaarden.</p></body></html>")
PRIVACY = "<html><body><p>Wij gaan zorgvuldig om met uw gegevens. Website gemaakt door Pixelfabriek B.V.</p></body></html>"


def fake_site(pages: dict, log: list):
    def get(url):
        log.append(url)
        key = url.replace("https://www.jansen-installatie.nl", "").rstrip("/") or "/"
        body = pages.get(key)
        return (200, url, body) if body is not None else (404, url, "")
    return get


def run_enrich(pages: dict):
    log = []
    with mock.patch.object(enrich, "polite_get", fake_site(pages, log)):
        p = enrich.enrich({"website": SITE, "domain": "jansen-installatie.nl", "business_name": NAME,
                           "source": "test"})
    return p, log


BASE_PAGES = {"/": HOME, "/contact": CONTACT, "/over-ons": ABOUT}


class TestLegalPageDiscovery(unittest.TestCase):
    def test_finds_same_site_legal_pages_in_priority_order(self):
        links = enrich.legal_page_links(SITE, HOME, "jansen-installatie.nl")
        self.assertEqual(links, ["https://www.jansen-installatie.nl/algemene-voorwaarden",
                                 "https://www.jansen-installatie.nl/privacyverklaring"])

    def test_ignores_external_pdf_and_already_fetched_links(self):
        links = enrich.legal_page_links(SITE, HOME, "jansen-installatie.nl",
                                        exclude=["https://www.jansen-installatie.nl/algemene-voorwaarden/"])
        self.assertNotIn("https://www.webbureau.nl/privacy", links)
        self.assertFalse(any(l.endswith(".pdf") for l in links))
        self.assertNotIn("https://www.jansen-installatie.nl/algemene-voorwaarden", links)
        self.assertEqual(links[0], "https://www.jansen-installatie.nl/privacyverklaring")

    def test_all_requested_page_types_are_recognised(self):
        html = "".join(f'<a href="/{p}">x</a>' for p in (
            "colofon", "bedrijfsgegevens", "algemene-voorwaarden", "terms", "privacy-policy", "privacyverklaring",
            "disclaimer", "cookiebeleid", "cookie-policy"))
        with mock.patch.object(enrich.config, "MAX_LEGAL_PAGES", 20):
            links = enrich.legal_page_links("https://a.nl/", html, "a.nl")
        self.assertEqual(len(links), 9)
        self.assertEqual(links[:2], ["https://a.nl/colofon", "https://a.nl/bedrijfsgegevens"])

    def test_limit(self):
        self.assertLessEqual(len(enrich.legal_page_links(SITE, HOME, "jansen-installatie.nl")),
                             enrich.config.MAX_LEGAL_PAGES)


class TestBvOnLegalPages(unittest.TestCase):
    def test_bv_found_on_terms_page_with_provenance(self):
        p, log = run_enrich({**BASE_PAGES, "/algemene-voorwaarden": TERMS, "/privacyverklaring": PRIVACY})
        self.assertEqual(p["legal_form"], "bv")
        self.assertEqual(p["legal_form_confidence"], "strong")
        self.assertEqual(p["legal_form_source"], "legal_page")
        self.assertEqual(p["legal_form_source_url"], "https://www.jansen-installatie.nl/algemene-voorwaarden")
        self.assertIn("Jansen B.V.", p["legal_form_evidence"])
        self.assertEqual(p["kvk_number"], "87654321")
        self.assertIn("https://www.jansen-installatie.nl/algemene-voorwaarden", log)

    def test_generic_eenmanszaak_in_terms_does_not_make_it_not_bv(self):
        p, _ = run_enrich({**BASE_PAGES, "/algemene-voorwaarden": TERMS})
        self.assertEqual(p["legal_form"], "bv")

    def test_web_agency_bv_is_not_attributed_to_the_business(self):
        p, _ = run_enrich({**BASE_PAGES, "/privacyverklaring": PRIVACY})
        self.assertEqual(p["legal_form"], "unknown")
        self.assertIsNone(p["legal_form_source_url"])

    def test_legal_page_text_does_not_change_customer_type_signals(self):
        without, _ = run_enrich(BASE_PAGES)
        with_legal, _ = run_enrich({**BASE_PAGES, "/algemene-voorwaarden": TERMS})
        for key in ("customer_type", "homeowner_signal", "business_signal", "trade_signal", "public_email",
                    "webshop_markers", "broker_markers"):
            self.assertEqual(without.get(key), with_legal.get(key), key)

    def test_no_legal_fetch_when_homepage_already_shows_strong_bv(self):
        home = HOME.replace("<h1>Installatiebedrijf Jansen</h1>", "<h1>Installatiebedrijf Jansen B.V.</h1>")
        p, log = run_enrich({**BASE_PAGES, "/": home, "/algemene-voorwaarden": TERMS})
        self.assertEqual(p["legal_form"], "bv")
        self.assertEqual(p["legal_form_source"], "website_pages")
        self.assertFalse(any("voorwaarden" in u or "privacy" in u for u in log))

    def test_legal_kvk_pattern(self):
        r = enrich.LEGAL_KVK_RE
        self.assertEqual(r.search("Kamer van Koophandel onder nummer 12345678").group(1), "12345678")
        self.assertEqual(r.search("KvK-nr: 12345678").group(1), "12345678")
        self.assertIsNone(r.search("KvK, bel 0612345678"))           # 10-digit phone number
        self.assertIsNone(r.search("nummer 12345678 zonder kvk-vermelding ervoor"))

    def test_merge_rules(self):
        m = enrich.merge_legal_evidence
        bv = ("bv", "Jansen B.V.", "strong")
        self.assertEqual(m(("unknown", None, None), bv), ("bv", "Jansen B.V.", "strong", True))
        self.assertEqual(m(("unknown", "conflicting: a | b", None), bv)[0], "unknown")       # conflict stays
        self.assertEqual(m(("bv", "x BV", "weak"), bv), ("bv", "Jansen B.V.", "strong", True))  # weak -> strong
        self.assertEqual(m(("vof", "Jansen vof", None), bv)[0], "unknown")                   # disagreement
        self.assertEqual(m(("bv", "Jansen BV", "strong"), ("unknown", None, None)), ("bv", "Jansen BV", "strong", False))


class TestProvenance(unittest.TestCase):
    def test_website_bv_is_ready_but_labelled_not_kvk_verified(self):
        p = score(base(customer_type="homeowner", legal_form="bv", legal_form_confidence="strong",
                       legal_form_evidence="Jansen B.V."))
        self.assertEqual(p["queue"], READY)
        self.assertEqual(p["legal_form_status"], "WEBSITE_BV")
        self.assertIn("not verified on KvK", p["queue_reason"])
        self.assertIn("WEBSITE_BV", report.bv_evidence_text(p))
        self.assertNotIn("KVK_VERIFIED", report.bv_evidence_text(p))

    def test_kvk_review_gives_kvk_verified(self):
        p = score(base(customer_type="homeowner", legal_form="unknown", legal_form_confidence=None,
                       kvk_review="bv", kvk_entity_match="yes", kvk_active="yes"))
        self.assertEqual(p["legal_form_status"], "KVK_VERIFIED")
        self.assertIn("KVK_VERIFIED", report.bv_evidence_text(p))

    def test_kvk_review_with_entity_mismatch_is_not_verified(self):
        p = score(base(customer_type="homeowner", kvk_review="bv", kvk_entity_match="no"))
        self.assertEqual(p["legal_form_status"], "UNKNOWN")

    def test_weak_website_mention_is_unknown(self):
        p = score(base(customer_type="homeowner", legal_form="bv", legal_form_confidence="weak"))
        self.assertEqual(p["legal_form_status"], "UNKNOWN")
        self.assertEqual(p["queue"], KVK_CHECK)

    def test_not_bv(self):
        self.assertEqual(legal_form_status(base(legal_form="vof", legal_form_confidence=None)), "NOT_BV")
        self.assertEqual(legal_form_status(base(kvk_review="not_bv")), "NOT_BV")

    def test_legal_page_evidence_never_produces_kvk_verified(self):
        legal = {"pages": ["u"], "legal_form": "bv", "legal_form_evidence": "Jansen B.V.",
                 "legal_form_confidence": "strong", "source_url": "u", "kvk_number": "87654321",
                 "kvk_source_url": "u"}
        p = enrich.apply_legal_pages(base(legal_form="unknown", legal_form_confidence=None, kvk_number=None), legal)
        self.assertNotIn("kvk_review", p)
        self.assertEqual(legal_form_status(score({**p, "customer_type": "homeowner"})), "WEBSITE_BV")


if __name__ == "__main__":
    unittest.main()
