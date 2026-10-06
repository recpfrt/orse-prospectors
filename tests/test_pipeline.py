import unittest

from prospects.collect import dedupe, domain_of, normalize_website
from prospects.enrich import detect_legal_form, detect_team, extract_role_emails
from prospects.score import score


def base(**kw):
    p = {"fetch_status": 200, "robots_blocked": False, "is_dutch": True, "probable_chain": False,
         "branches": None, "team_signal": "small", "booking_tools": [], "booking_phrases": [],
         "online_booking": False, "emergency_service": True, "legal_form": "bv",
         "website_quality": 2, "public_email": "info@bedrijf.nl", "kvk_number": "12345678"}
    p.update(kw)
    return p


class TestCollect(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_website("www.Bedrijf.nl"), "https://www.bedrijf.nl/")
        self.assertEqual(domain_of("https://www.bedrijf.nl/x"), "bedrijf.nl")
        self.assertIsNone(normalize_website("geen"))

    def test_dedupe_and_red_list(self):
        c = lambda d, e=None: {"domain": d, "osm_email": e}
        kept, stats = dedupe([c("a.nl"), c("a.nl"), c(None), c("b.nl"), c("c.nl", "info@c.nl")],
                             {"b.nl"}, {"info@c.nl"})
        self.assertEqual([k["domain"] for k in kept], ["a.nl"])
        self.assertEqual(stats["duplicate_domain"], 1)
        self.assertEqual(stats["red_listed_or_contacted"], 2)


class TestEnrich(unittest.TestCase):
    def test_only_role_emails_on_own_domain(self):
        html = ('<a href="mailto:info@bedrijf.nl">x</a> jan.jansen@bedrijf.nl '
                'info@gmail.com planning@www.bedrijf.nl')
        self.assertEqual(extract_role_emails(html, "bedrijf.nl"), ["info@bedrijf.nl", "planning@www.bedrijf.nl"])  # kept exactly as published

    def test_legal_form_requires_own_name(self):
        self.assertEqual(detect_legal_form("© 2025 Jansen Installatietechniek B.V.", "Jansen Installatietechniek", "jansen.nl"), "bv")
        self.assertEqual(detect_legal_form("Wij werken met Bosch B.V. en Remeha B.V.", "Jansen Installaties", "jansen.nl"), "unknown")
        self.assertEqual(detect_legal_form("Pietersen V.O.F. - KvK 12345678", "Pietersen Loodgieters", "pietersen.nl"), "vof")
        self.assertEqual(detect_legal_form("Jansen B.V. en Jansen V.O.F.", "Jansen", "jansen.nl"), "unknown")

    def test_team(self):
        self.assertEqual(detect_team("Met 8 vakmensen staan wij klaar"), ("small", 8))
        self.assertEqual(detect_team("ruim 40 medewerkers"), ("large", 40))


class TestScore(unittest.TestCase):
    def test_bv_with_email_is_email_candidate(self):
        r = score(base())
        self.assertEqual((r["status"], r["outreach_channel"]), ("qualified", "email"))
        self.assertGreaterEqual(r["qualification_score"], 60)

    def test_non_bv_never_email_even_with_high_score(self):
        for form in ("vof", "eenmanszaak", "unknown"):
            r = score(base(legal_form=form))
            self.assertEqual(r["outreach_channel"], "phone_or_contact_form", form)
            self.assertEqual(r["status"], "manual_review", form)

    def test_booking_tool_excludes(self):
        r = score(base(booking_tools=["calendly.com"], online_booking=True))
        self.assertEqual(r["status"], "disqualified")

    def test_robots_blocked_excludes(self):
        self.assertEqual(score(base(robots_blocked=True, fetch_status=-1))["status"], "disqualified")


if __name__ == "__main__":
    unittest.main()


class TestCustomerType(unittest.TestCase):
    def test_homeowner(self):
        from prospects.customer_type import classify
        r = classify("wij helpen particulieren met lekkage, verstopping en uw cv-ketel. ook voor uw badkamer.")
        self.assertEqual(r["customer_type"], "homeowner")
        self.assertTrue(any(e.startswith("lekkage") for e in r["customer_type_evidence"]["homeowner"]))

    def test_business(self):
        from prospects.customer_type import classify
        r = classify("installaties voor utiliteitsbouw, industrie, zorginstellingen en kantoorgebouwen.")
        self.assertEqual(r["customer_type"], "business")

    def test_unknown_and_word_boundaries(self):
        from prospects.customer_type import classify
        self.assertEqual(classify("welkom op onze website")["customer_type"], "unknown")
        # 'thuisbasis' must not count as 'thuis'
        self.assertEqual(classify("thuisbasis thuisbasis thuisbasis")["homeowner_signal"], 0)

    def test_business_customer_type_is_excluded(self):
        r = score(base(customer_type="business", customer_type_evidence={"business": ["utiliteit×3"]}))
        self.assertEqual(r["status"], "disqualified")

    def test_strong_prospect_requires_homeowner_and_email_channel(self):
        self.assertTrue(score(base(customer_type="homeowner"))["strong_prospect"])
        self.assertFalse(score(base(customer_type="mixed"))["strong_prospect"])
        self.assertFalse(score(base(customer_type="homeowner", legal_form="vof"))["strong_prospect"])


class TestNewExclusions(unittest.TestCase):
    def test_broker_network_excluded(self):
        r = score(base(broker_markers=["in heel nederland", "aangesloten vakmensen"]))
        self.assertEqual(r["status"], "disqualified")

    def test_webshop_excluded(self):
        self.assertEqual(score(base(webshop_markers=["winkelwagen", "afrekenen"]))["status"], "disqualified")

    def test_commoncrawl_requires_dutch_address(self):
        r = score(base(source="commoncrawl", nl_address_found=False))
        self.assertEqual(r["status"], "disqualified")


class TestPlaces(unittest.TestCase):
    def test_town_parsing_drops_trailing_junk(self):
        from prospects.enrich import postcode_city
        self.assertEqual(postcode_city("Dorpsstraat 1, 5321 AB Hedel Info info@x.nl"), "Hedel")
        self.assertEqual(postcode_city("Kerkplein 2 4611 AA Bergen op Zoom Tel 0164"), "Bergen op Zoom")

    def test_landing_page_mismatch(self):
        from prospects.places import landing_page_mismatch
        self.assertIsNotNone(landing_page_mismatch("ontstoppingsbedrijfvelp.nl", "Hoofddorp"))
        self.assertIsNone(landing_page_mismatch("rioolontstoppengoirle.nl", "Goirle"))
        self.assertIsNone(landing_page_mismatch("feenstra-elektro.nl", "Den Dungen"))
        self.assertIsNone(landing_page_mismatch("loodgietersbedrijfutrecht.nl", "Utrecht"))
        self.assertIsNotNone(landing_page_mismatch("electricien-voorburg.nl", "Warmond"))
        # surnames that happen to be place names
        self.assertIsNone(landing_page_mismatch("vandongeninstallatie.nl", "Hank"))
        self.assertIsNone(landing_page_mismatch("installatiebedrijfvanbenschop.nl", "Kerkdriel"))
        self.assertIsNone(landing_page_mismatch("nijbroekinstallatiebedrijf.nl", "Dordrecht"))
