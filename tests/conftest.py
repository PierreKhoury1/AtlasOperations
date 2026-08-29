import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Everything runs in demo mode against a throw-away database, with accounts on.
os.environ["DESK_MODE"] = "demo"
os.environ["DEMO_DELAY"] = "0"
os.environ.pop("DESK_OPEN", None)
os.environ["DESK_SECRET"] = "test-secret"
_TMP = Path(tempfile.mkdtemp(prefix="hermes-tests-"))
os.environ["HERMES_DATA_DIR"] = str(_TMP)


@pytest.fixture(scope="session")
def tmp_dir():
    return _TMP


@pytest.fixture()
def store(tmp_path):
    from hermes.store import Store
    return Store(tmp_path / "t.db")


@pytest.fixture(scope="session")
def app_client():
    import hermes.desk.app as A
    A.store.__init__(_TMP / "desk.db")           # fresh DB for the session
    A.app.config["TESTING"] = True
    return A.app.test_client()
