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
_TMP = Path(tempfile.mkdtemp(prefix="atlas-tests-"))
os.environ["ATLAS_DATA_DIR"] = str(_TMP)


@pytest.fixture(scope="session")
def tmp_dir():
    return _TMP


# Set ATLAS_TEST_DATABASE_URL=postgres://... to run the whole suite against PostgreSQL instead of SQLite
# (scripts/test_pg.py starts an embedded server and does exactly that).
PG_URL = os.environ.get("ATLAS_TEST_DATABASE_URL", "").strip() or None


def _fresh_pg(url: str, schema: str = "public") -> None:
    """Recreate a schema so each Store(...) starts empty, like a fresh SQLite file. The app's store lives in
    `public`; the unit-level `store` fixture gets its own schema so the two never trample each other."""
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.execute(f"CREATE SCHEMA {schema}")


def _pg_url(schema: str) -> str | None:
    if not PG_URL:
        return None
    return PG_URL + ("&" if "?" in PG_URL else "?") + f"options=-csearch_path%3D{schema}"


@pytest.fixture()
def store(tmp_path):
    from atlas.store import Store
    if PG_URL:
        _fresh_pg(PG_URL, "unit")
    return Store(tmp_path / "t.db", url=_pg_url("unit"))


@pytest.fixture(scope="session")
def app_client():
    import atlas.desk.app as A
    if PG_URL:
        _fresh_pg(PG_URL)
    A.store.__init__(_TMP / "desk.db", url=PG_URL)           # fresh DB for the session
    A.app.config["TESTING"] = True
    return A.app.test_client()
