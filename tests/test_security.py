"""Opsec: secrets encrypted at rest, SSRF guard on agent fetches, rate limiting, security headers."""
import json
import sqlite3

import pytest

from atlas import secure as SEC


def J(r):
    return json.loads(r.data)


def test_connector_secrets_encrypted_at_rest(store):
    c = store.add_connector(1, "smtp", "Mail", {"host": "smtp.example.com", "password": "hunter2", "user": "me@example.com"})
    assert c["config"]["password"] == "hunter2"                      # decrypts transparently through the API
    raw = store._conn.execute("SELECT config FROM connectors WHERE id=?", (c["id"],)).fetchone()[0]
    assert raw.startswith("enc1:") and "hunter2" not in raw and "smtp.example.com" not in raw
    store.update_connector(c["id"], config={"host": "smtp.example.com", "password": "hunter3"})
    raw2 = store._conn.execute("SELECT config FROM connectors WHERE id=?", (c["id"],)).fetchone()[0]
    assert raw2.startswith("enc1:") and "hunter3" not in raw2
    assert store.connector(c["id"])["config"]["password"] == "hunter3"
    # legacy plaintext rows migrate on boot
    store._conn.execute("UPDATE connectors SET config=? WHERE id=?", (json.dumps({"password": "legacy-pw"}), c["id"]))
    store._conn.commit()
    assert store.encrypt_legacy_connectors() == 1
    raw3 = store._conn.execute("SELECT config FROM connectors WHERE id=?", (c["id"],)).fetchone()[0]
    assert raw3.startswith("enc1:") and "legacy-pw" not in raw3
    assert store.connector(c["id"])["config"]["password"] == "legacy-pw"


def test_decrypt_error_is_flagged_not_fatal():
    bad = "enc1:not-a-valid-token"
    out = SEC.decrypt_config(bad)
    assert "_decrypt_error" in out


def test_ssrf_guard():
    assert SEC.private_url_reason("http://127.0.0.1:8094/api/health")
    assert SEC.private_url_reason("http://localhost/admin")
    assert SEC.private_url_reason("http://169.254.169.254/latest/meta-data/")
    assert SEC.private_url_reason("http://10.0.0.5/")
    assert SEC.private_url_reason("http://192.168.1.1/router")
    assert SEC.private_url_reason("https://example.com/") is None
    from atlas.tools import WorkspaceTools
    wt = WorkspaceTools.__new__(WorkspaceTools)
    assert "refused" in wt.web_fetch("http://127.0.0.1:9/") and "public" in wt.web_fetch("http://127.0.0.1:9/")
    from atlas import study as ST
    code, _url, body = ST._fetch("http://127.0.0.1:9/")
    assert code == 403 and body == ""


def test_rate_limiter_window():
    rl = SEC.RateLimiter(3, 60)
    assert all(rl.allow("k") for _ in range(3))
    assert rl.allow("k") is False
    assert rl.allow("other") is True                                  # keys are independent


def test_security_headers_and_auth_rate_limit(app_client):
    r = app_client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "Referrer-Policy" in r.headers
    import atlas.desk.app as A
    old = A._RL_AUTH
    A._RL_AUTH = SEC.RateLimiter(2, 600)
    try:
        for _ in range(2):
            app_client.post("/login", json={"email": "x@example.com", "password": "nope"})
        r = app_client.post("/login", json={"email": "x@example.com", "password": "nope"})
        assert r.status_code == 429
    finally:
        A._RL_AUTH = old
