"""Runs last (unittest discovers modules alphabetically): the real data/state.sqlite must be byte-identical
to its state when the test package was imported, i.e. the complete suite never touched it."""
from tests import _isolation  # noqa: F401

import unittest

from prospects import state
from tests.test_state_isolation import fingerprint


class TestRealStateUntouchedBySuite(unittest.TestCase):
    def test_real_state_unchanged_for_whole_test_run(self):
        if _isolation.REAL_FINGERPRINT_AT_START is None:
            self.skipTest("no real data/state.sqlite on this machine")
        self.assertEqual(fingerprint(state.REAL_DB_PATH), _isolation.REAL_FINGERPRINT_AT_START)
