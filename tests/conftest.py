import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Everything runs in demo mode against a throw-away database, with accounts on.
os.environ["DESK_MODE"] = "demo"
os.environ["DEMO_DELAY"] = "0"
os.environ.pop("DESK_OPEN", None)
os.environ["DESK_SECRET"] = "test-secret"
os.environ["VISION_EMBED"] = "hash"          # no CLIP / API in the suite: deterministic hashed vectors
os.environ["VISION_INDEX_SYNC"] = "1"        # embed inline so retrieval tests see the index immediately
# The uploads' disk floor (usage spec §1.3 L9) reads this machine's real free space: a nearly full dev disk would refuse
# every upload test. The floor's own tests (test_usage.py T19) set it back and fake the disk.
os.environ["USAGE_DISK_FLOOR_PCT"] = "0"


def _prune_stale(prefix: str, max_age_s: float = 6 * 3600) -> None:
    """Each run's throw-away database folder is ~36 MB. Windows keeps the SQLite files open until exit, so a run
    can't always delete its own; the next run drops any older than six hours (no live run is that old)."""
    now = time.time()
    for p in Path(tempfile.gettempdir()).glob(prefix + "*"):
        try:
            if p.is_dir() and now - p.stat().st_mtime > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


_prune_stale("atlas-tests-")
_TMP = Path(tempfile.mkdtemp(prefix="atlas-tests-"))
os.environ["ATLAS_DATA_DIR"] = str(_TMP)
atexit.register(shutil.rmtree, _TMP, True)   # ignore_errors: whatever is still open is pruned by a later run


@pytest.fixture(scope="session")
def tmp_dir():
    return _TMP


@pytest.fixture(autouse=True)
def _fresh_sign_in_limit():
    """Every test client is 127.0.0.1, so the per-IP sign-in limit (20 per 10 minutes) is shared by the whole suite and
    used to run out halfway, failing unrelated tests with 429. Each test starts with a clean count; the tests about
    the limit install their own limiter."""
    A = sys.modules.get("atlas.desk.app")
    if A is not None:
        A._RL_AUTH._hits.clear()
    yield


TWILIO_TOKEN = "twilio-test-auth-token"


@pytest.fixture()
def twilio_sms():
    """POST to a desk's Twilio hook the way Twilio does: the form fields signed (X-Twilio-Signature) with the auth
    token of the desk's Twilio connector, over the URL the app sees (http://localhost/... in the test client).
    twilio_sms.connect(did) adds that connector."""
    from atlas import integrations as I

    def post(client, token, data, auth_token=TWILIO_TOKEN, sign=True):
        sig = I.twilio_signature(auth_token, f"http://localhost/hook/{token}/sms", data) if sign else ""
        return client.post(f"/hook/{token}/sms", data=data, headers={"X-Twilio-Signature": sig} if sig else {})

    def connect(did):
        import atlas.desk.app as A
        A.store.add_connector(did, "twilio", "sms", {"account_sid": "AC0000", "auth_token": TWILIO_TOKEN,
                                                     "from_number": "+15550000000"}, False)
    post.connect, post.token = connect, TWILIO_TOKEN
    return post


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
