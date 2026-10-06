"""Test isolation for the local state database. Imported before any test module.

Every test run gets its own temporary default database, and test mode makes
prospects.state refuse the real data/state.sqlite unless a test explicitly opts in.
"""
import os
import tempfile
from pathlib import Path

TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="orse-prospects-tests-"))
os.environ["ORSE_PROSPECTS_TESTING"] = "1"
os.environ["ORSE_PROSPECTS_DB"] = str(TEST_DB_DIR / "default-test.sqlite")
os.environ.pop("ORSE_PROSPECTS_ALLOW_REAL_DB", None)

# Fingerprint of the real database when the test run starts; checked again by the last test module.
_real = Path(__file__).resolve().parent.parent / "data" / "state.sqlite"
if _real.exists():
    import hashlib
    _data = _real.read_bytes()
    REAL_FINGERPRINT_AT_START = (hashlib.sha256(_data).hexdigest(), _real.stat().st_mtime_ns, len(_data))
else:
    REAL_FINGERPRINT_AT_START = None
