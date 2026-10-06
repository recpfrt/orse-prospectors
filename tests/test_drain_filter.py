"""Drain-domain selection filter (approved 2026-10-06): Common Crawl domains whose ONLY trade keyword is
riool / ontstop / lekkage are skipped at selection. Nothing else in the pipeline changes."""
from tests import _isolation  # noqa: F401  (must be first)

import random
import tempfile
import unittest
from pathlib import Path

from prospects import collect, state
from prospects.collect import DOMAIN_CATEGORY, collect_commoncrawl, is_drain_only
from tests.test_state_isolation import fingerprint


def write_tsv(path: Path, domains: list[str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i, d in enumerate(domains):
            host_rev = ".".join(reversed(d.split(".")))
            f.write(f"{i}\t0.1\t{i}\t0.1\t{host_rev}\t1\n")


class TestDrainFilter(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(state.REAL_DB_PATH)

    def tearDown(self):
        self.assertEqual(fingerprint(state.REAL_DB_PATH), self.real_before, "real data/state.sqlite changed")

    def test_drain_only_domains_are_excluded(self):           # real Batch 1 / network examples
        for d in ("rioolservicebommelerwaard.nl", "ontstoppen-dongen.nl", "besselinkrioolservice.nl",
                  "ontstoppingsbedrijfvelp.nl", "lekkagedetectie-nederland.nl", "rioolontstoppengoirle.nl"):
            self.assertTrue(is_drain_only(d), d)

    def test_domain_with_another_core_trade_signal_is_kept(self):
        for d in ("loodgieterrioolservice.nl", "installatiebedrijf-riool.nl", "airco-lekkage.nl",
                  "sanitair-ontstoppen.nl", "elektro-en-riool.nl", "verwarming-lekkage.nl"):
            self.assertFalse(is_drain_only(d), d)

    def test_plumbers_hvac_electricians_unaffected(self):
        for d in ("loodgieterutrecht.nl", "040loodgieter.nl", "aircoservicezwaan.nl", "installatiebedrijfhw.nl",
                  "elektricien-arnhem.nl", "vloedgraven-elektrotechniek.nl", "haringklimaattechniek.nl"):
            self.assertFalse(is_drain_only(d), d)

    def test_collection_drops_only_drain_domains_and_keeps_order_and_categories(self):
        domains = ["loodgieterutrecht.nl", "rioolservicebommelerwaard.nl", "aircoservicezwaan.nl",
                   "ontstoppen-dongen.nl", "elektricien-arnhem.nl", "loodgieterrioolservice.nl",
                   "lekkagedetectie-nederland.nl", "installatiebedrijfhw.nl"]
        with tempfile.TemporaryDirectory() as d:
            tsv = Path(d) / "pool.tsv"
            write_tsv(tsv, domains)
            got = collect_commoncrawl(str(tsv), sample=100, seed=20261006, exclude_domains=set())
        # previous behaviour: same seeded shuffle of the full list; the filter only removes drain-only domains
        expected_order = domains[:]
        random.Random(20261006).shuffle(expected_order)
        expected_order = [x for x in expected_order if not is_drain_only(x)]
        self.assertEqual([c["domain"] for c in got], expected_order)
        self.assertNotIn("rioolservicebommelerwaard.nl", [c["domain"] for c in got])
        self.assertIn("loodgieterrioolservice.nl", [c["domain"] for c in got])
        for c in got:                                         # category mapping unchanged
            expected_cat = next((cat for p, cat in DOMAIN_CATEGORY if p.search(c["domain"])), "hvac")
            self.assertEqual(c["service_category"], expected_cat)
        cats = {c["domain"]: c["service_category"] for c in got}
        self.assertEqual((cats["loodgieterutrecht.nl"], cats["aircoservicezwaan.nl"], cats["elektricien-arnhem.nl"]),
                         ("plumber", "hvac", "electrician"))

    def test_only_selection_changed(self):
        """The filter lives in collect.py only; scoring/enrichment modules do not reference it."""
        root = Path(collect.__file__).parent
        for module in ("score.py", "enrich.py", "pipeline.py", "customer_type.py", "state.py"):
            self.assertNotIn("is_drain_only", (root / module).read_text(encoding="utf-8"), module)
