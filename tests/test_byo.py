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


def test_owner_endpoints_never_get_the_house_key(app_client, monkeypatch):
    """A custom server saved without a key, and a Hermes Agent connector without one, send no key at all. An empty key
    used to fall back to the platform's own OPENROUTER_API_KEY / OPENAI_API_KEY, which then went to the URL the owner
    typed. The operator's own providers still fall back to the env keys."""
    import socket

    import httpx
    import pytest

    from atlas import config as cfg
    from atlas import secure as SEC
    from atlas.desk import app as A
    from atlas.providers import make_provider
    for k, v in (("OPENROUTER_API_KEY", "sk-or-HOUSE-0001"), ("OPENAI_API_KEY", "sk-HOUSE-0002"), ("ANTHROPIC_API_KEY", "sk-ant-HOUSE-0003")):
        monkeypatch.setenv(k, v)
    real = socket.getaddrinfo
    monkeypatch.setattr(SEC.socket, "getaddrinfo", lambda h, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
                        if str(h).endswith(".test") else real(h, *a, **k))
    seen = []

    def handler(req):                                   # nothing leaves the machine: the guarded transport is stubbed
        seen.append({"url": str(req.url), "auth": req.headers.get("authorization")})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "m"})
    monkeypatch.setattr(SEC, "guarded_transport", lambda scope="public", **kw: httpx.MockTransport(handler))
    c = app_client
    did = _desk(c, "byo-housekey@example.com")
    J(c.post(f"/api/desks/{did}/providers", json={"preset": "custom", "api_key": "", "base_url": "https://gpu.owner.test/v1", "model": "m"}))
    r = J(c.post(f"/api/desks/{did}/providers/custom/test", json={}))
    assert r["ok"], r
    assert seen and seen[-1]["url"].startswith("https://gpu.owner.test/") and seen[-1]["auth"] is None
    # the same entry as the desk's engine
    monkeypatch.setenv("DESK_MODE", "live")
    monkeypatch.setenv("DESK_LEAD_ENGINE", "hermes_agent")
    monkeypatch.delenv("DESK_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_AGENT_URL", raising=False)
    A.store.add_connector(did, "hermes_agent", "hermes", {"base_url": "https://hermes.owner.test:8642", "api_key": ""}, False)
    provs = A.desk_configs(A.store.desk(did))["providers"]["providers"]
    seen.clear()
    p = make_provider(provs["desk:custom"])
    assert p.api_key == ""
    p.chat("", [{"role": "user", "content": "hi"}], [])
    assert seen and seen[-1]["auth"] is None
    # a Hermes Agent connector without a key
    seen.clear()
    h = make_provider(provs["hermes_agent_atlas"])
    assert h.api_key == ""
    h.chat("", [{"role": "user", "content": "hi"}], [])
    assert seen and seen[-1]["url"].startswith("https://hermes.owner.test:8642/v1/") and seen[-1]["auth"] is None
    # any config marked as an owner's, however it was built
    assert make_provider({"type": "openai", "base_url": "https://x.owner.test/v1", "api_key": "", "url_guard": True}).api_key == ""
    assert make_provider({"type": "hermes_agent", "base_url": "https://x.owner.test", "api_key": ""}).api_key == ""
    with pytest.raises(RuntimeError, match="no API key"):           # the Anthropic SDK would read ANTHROPIC_API_KEY itself
        make_provider({"type": "anthropic", "api_key": "", "env_key": False})
    # an owner's own key still goes where they pointed it; the operator's providers keep the env fallback
    assert make_provider({"type": "openai", "base_url": "https://x.owner.test/v1", "api_key": "own-1", "url_guard": True}).api_key == "own-1"
    assert cfg.resolve_api_key({"type": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key": ""}) == "sk-or-HOUSE-0001"
    assert cfg.resolve_api_key({"type": "anthropic", "api_key": ""}) == "sk-ant-HOUSE-0003"


def test_landscape_override_cannot_pick_other_house_providers(app_client, monkeypatch):
    """The per-agent model picker may name the catalogue provider (openrouter), the desk default or the desk's own
    accounts. The house's local presets (ollama / openai_compatible on localhost) are not the owner's to choose."""
    from atlas.desk import app as A
    c = app_client
    did = _desk(c, "byo-landscape@example.com")
    monkeypatch.setenv("DESK_MODE", "live")
    monkeypatch.delenv("DESK_PROVIDER", raising=False)
    J(c.patch(f"/api/desks/{did}", json={"models": {"atlas": {"model": "llama3.2:3b", "provider": "ollama"}}}))
    agents = {a["id"]: a for a in A.desk_configs(A.store.desk(did))["agents"]}
    assert agents["atlas"]["provider"] != "ollama"
    J(c.patch(f"/api/desks/{did}", json={"models": {"atlas": {"model": "anthropic/claude-haiku-4.5", "provider": "openrouter"}}}))
    agents = {a["id"]: a for a in A.desk_configs(A.store.desk(did))["agents"]}
    assert agents["atlas"]["provider"] == "openrouter" and agents["atlas"]["model"] == "anthropic/claude-haiku-4.5"
