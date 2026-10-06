"""Regression tests for the 2026-10-06 test-isolation bug.

Root cause: `def connect(path=DB_PATH)` captured the real database path when the module was imported.
A test that reassigned `state.DB_PATH` to a temporary file still wrote to data/state.sqlite, because
`connect()` and every CLI command never re-read that attribute.
"""
from tests import _isolation  # noqa: F401  (must be first)

import hashlib
import json
import os
import tempfile
import types
import unittest
from pathlib import Path

from prospects import state


def fingerprint(path: Path):
    if not path.exists():
        return None
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), path.stat().st_mtime_ns, len(data)


REAL = state.REAL_DB_PATH


class TestStateIsolation(unittest.TestCase):
    def setUp(self):
        self.real_before = fingerprint(REAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self.tmp.name) / "isolated.sqlite"

    def tearDown(self):
        import run
        run.close_connections()
        self.assertEqual(fingerprint(REAL), self.real_before, "real data/state.sqlite changed during a test")
        self.tmp.cleanup()

    def test_temporary_database_receives_all_writes(self):
        con = state.connect(self.tmp_db)
        state.record_attempt(con, "isolated.nl", "test", 200)
        state.save_prospect(con, {"domain": "isolated.nl", "queue": "KVK_CHECK"})
        state.record_kvk_review(con, "isolated.nl", "bv", review_batch="isolation-test")
        con.commit()
        con.close()
        check = state.connect(self.tmp_db)
        self.assertEqual(check.execute("select count(*) from kvk_reviews").fetchone()[0], 1)
        self.assertEqual(check.execute("select count(*) from prospects").fetchone()[0], 1)
        check.close()

    def test_default_connect_in_tests_uses_isolated_database(self):
        resolved = state.resolve_db_path()
        self.assertNotEqual(resolved, REAL.resolve())
        self.assertEqual(resolved, Path(os.environ[state.DB_ENV]).resolve())

    def test_real_database_is_refused_in_test_mode(self):
        with self.assertRaises(state.RealStateAccessError):
            state.connect(REAL)

    def test_missing_isolation_fails_loudly_instead_of_using_real_db(self):
        saved = os.environ.pop(state.DB_ENV)
        try:
            with self.assertRaises(state.RealStateAccessError):
                state.connect()
        finally:
            os.environ[state.DB_ENV] = saved

    def test_previous_failure_mode_cannot_reach_real_db(self):
        """Exact old pattern: reassign a module attribute, then call CLI commands without an explicit path."""
        import run
        state.DB_PATH = self.tmp_db            # the old (ineffective) override
        try:
            saved = os.environ.pop(state.DB_ENV)
            try:
                with self.assertRaises(state.RealStateAccessError):
                    run.cmd_kvk_stats(types.SimpleNamespace(db=None))
            finally:
                os.environ[state.DB_ENV] = saved
        finally:
            del state.DB_PATH

    def test_cli_db_option_writes_only_to_that_database(self):
        import run
        rows = [{"domain": "cli-isolated.nl", "fetch_status": 0, "source": "test"}]
        src = Path(self.tmp.name) / "rows.json"
        src.write_text(json.dumps(rows), encoding="utf-8")
        run.cmd_import(types.SimpleNamespace(db=str(self.tmp_db), file=str(src)))
        con = state.connect(self.tmp_db)
        self.assertEqual([r[0] for r in con.execute("select domain from prospects")], ["cli-isolated.nl"])
        con.close()

    def test_real_state_integration_requires_explicit_opt_in(self):
        os.environ[state.ALLOW_REAL_DB_ENV] = "1"
        try:
            self.assertEqual(state.resolve_db_path(REAL), REAL.resolve())   # resolves, but opens nothing
        finally:
            os.environ.pop(state.ALLOW_REAL_DB_ENV)
        with self.assertRaises(state.RealStateAccessError):
            state.resolve_db_path(REAL)
