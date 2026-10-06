"""Bring-your-own model accounts: a desk owner pastes their OpenAI / Anthropic / OpenRouter key (or points at any
OpenAI-compatible server) and that desk's agents run on it — key encrypted at rest, never echoed, house key not needed."""
import json

from atlas import store as ST


def J(r):
    assert r.status_code < 300, r.get_data(as_text=True)
    return json.loads(r.data)


def _desk(c, email="byo@example.com"):
    c.post("/signup", json={"name": "B", "company": "Byo Bistro", "email": email, "password": "password1"})
    c.post("/login", json={"email": email, "password": "password1"})
    return J(c.post("/api/desks", json={"name": "Bistro", "template": "sales_desk", "tier": "free"}))["id"]


def test_key_is_stored_encrypted_and_never_echoed(app_client):
    c = app_client
    did = _desk(c)
    r = J(c.post(f"/api/desks/{did}/providers", json={"preset": "openai", "api_key": "sk-test-abcdef123456", "model": ""}))
    assert r["default"] == "openai" and r["entry"]["key_hint"] == "…3456" and r["entry"]["model"] == "gpt-5.4-mini"
    assert "sk-test" not in json.dumps(J(c.get(f"/api/desks/{did}/providers")))
    row = J(c.get("/api/desks"))  # sanity: nothing in the desk listing either
    assert "sk-test" not in json.dumps(row)
    # the stored blob on disk is ciphertext
    from atlas.desk.app import store
    blob = (store.desk(did)["config"]["byo"]["entries"]["openai"])
    assert blob.startswith("enc1:") and "sk-test" not in blob


def test_desk_runs_on_its_own_account(app_client, monkeypatch):
    c = app_client
    did = _desk(c, "byo-live@example.com")
    J(c.post(f"/api/desks/{did}/providers", json={"preset": "anthropic", "api_key": "sk-ant-own-key-9999"}))
    monkeypatch.setenv("DESK_MODE", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DESK_PROVIDER", raising=False)
    from atlas.desk import app as A
    desk = A.store.desk(did)
    assert A._live_reason(desk) == "" and A._live_reason() != ""     # their key makes the desk live; the house still has none
    cfgs = A.desk_configs(desk)
    assert cfgs["providers"]["default_provider"] == "desk:anthropic"
    pc = cfgs["providers"]["providers"]["desk:anthropic"]
    assert pc["api_key"] == "sk-ant-own-key-9999" and pc["type"] == "anthropic" and pc["default_model"] == "claude-sonnet-4-5"
    assert all(a["provider"] == "desk:anthropic" and not a["model"] for a in cfgs["agents"])
    # /api/config agrees
    live = J(c.get("/api/config"))
    assert live["live_ready"] and not live["live_reason"]
    # back to house models: the reason returns
    J(c.post(f"/api/desks/{did}/providers/default", json={"name": ""}))
    assert A._live_reason(A.store.desk(did)) != ""


def test_update_keeps_key_custom_needs_public_url(app_client):
    c = app_client
    did = _desk(c, "byo2@example.com")
    J(c.post(f"/api/desks/{did}/providers", json={"preset": "openrouter", "api_key": "sk-or-zzzz-7777"}))
    r = J(c.post(f"/api/desks/{did}/providers", json={"preset": "openrouter", "api_key": "", "model": "anthropic/claude-haiku-4.5"}))
    assert r["entry"]["key_hint"] == "…7777" and r["entry"]["model"] == "anthropic/claude-haiku-4.5"
    assert c.post(f"/api/desks/{did}/providers", json={"preset": "openai", "api_key": ""}).status_code == 400
    for bad in ("ftp://x/v1", "http://localhost:11434/v1", "http://169.254.169.254/v1"):
        assert c.post(f"/api/desks/{did}/providers", json={"preset": "custom", "api_key": "", "base_url": bad}).status_code == 400, bad
    # removing the entry clears the default
    r = J(c.delete(f"/api/desks/{did}/providers/openrouter"))
    assert r["default"] == ""
    assert c.post(f"/api/desks/{did}/providers/nope/test", json={}).status_code == 404


def test_other_owners_cannot_touch_my_keys(app_client):
    c = app_client
    did = _desk(c, "byo3@example.com")
    J(c.post(f"/api/desks/{did}/providers", json={"preset": "openai", "api_key": "sk-mine-1234-5678"}))
    c.get("/logout")
    c.post("/signup", json={"name": "X", "company": "X", "email": "intruder@example.com", "password": "password1"})
    c.post("/login", json={"email": "intruder@example.com", "password": "password1"})
    assert c.get(f"/api/desks/{did}/providers").status_code == 404
    assert c.post(f"/api/desks/{did}/providers", json={"preset": "openai", "api_key": "sk-evil"}).status_code == 404
