"""The owner's own agent: scripts/atlas_bridge.py talks to /api/bridge/*, providers.BridgeProvider waits on it."""
import threading
import time

import atlas.desk.app as A
import atlas.providers as PV


def _owner(client):
    client.get("/try")
    return client.get("/api/me").get_json()


def test_connect_poll_answer_roundtrip(app_client):
    c = A.app.test_client()
    me = _owner(c)
    r = c.post("/api/bridges", json={"agent": "claude", "name": "laptop"}).get_json()
    assert r["ok"] and r["token"].startswith("atb_") and "--agent claude" in r["command"]
    tok = r["token"]
    assert A.bridge_online(me["id"]) is None
    runner = A.app.test_client()                                 # no session: the token alone
    h = {"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.1"}
    assert runner.get("/api/bridge/jobs?wait=0", headers=h).get_json() == {"job": None}
    assert A.bridge_online(me["id"])["name"] == "laptop"
    assert c.get("/api/bridges").get_json()["online"] is True

    got, seen = {}, []
    prov = PV.make_provider({"type": "bridge", "owner_id": me["id"], "default_model": "your claude", "call_max_s": 10})

    def call():
        try:
            got["r"] = prov.chat("Be brief.", [prov.user_message("ping")], [], on_token=lambda t, **k: seen.append(t))
        except Exception as e:                                    # noqa: BLE001 - reported by the assert below
            got["e"] = e
    t = threading.Thread(target=call)
    t.start()
    job = None
    for _ in range(40):
        job = runner.get("/api/bridge/jobs?wait=1", headers=h).get_json()["job"]
        if job:
            break
    assert job and job["system"] == "Be brief." and job["messages"][-1]["content"] == "ping"
    assert runner.post(f"/api/bridge/jobs/{job['id']}/chunk", json={"text": "pon"}, headers=h).status_code == 200
    assert runner.post(f"/api/bridge/jobs/{job['id']}", json={"text": "pong"}, headers=h).status_code == 200
    t.join(10)
    assert "e" not in got, got.get("e")
    assert got["r"].text == "pong" and seen == ["pon"]
    assert runner.get("/api/bridge/jobs?wait=0", headers={"Authorization": "Bearer atb_wrong"}).status_code == 401
    assert c.delete(f"/api/bridges/{r['bridge']['id']}").get_json()["ok"]
    assert runner.get("/api/bridge/jobs?wait=0", headers=h).status_code == 401       # removed = token dead


def test_offline_runner_fails_fast(app_client):
    c = A.app.test_client()
    me = _owner(c)
    prov = PV.make_provider({"type": "bridge", "owner_id": me["id"], "call_max_s": 2})
    t0 = time.time()
    try:
        prov.chat("x", [prov.user_message("y")], [])
        raise AssertionError("expected an error")
    except RuntimeError as e:
        assert "not connected" in str(e) and time.time() - t0 < 2
    assert c.post("/api/desks/999999/providers/default", json={"name": "agent"}).status_code == 404


def test_desk_runs_on_the_agent_when_connected(app_client):
    c = A.app.test_client()
    _owner(c)
    from atlas import templates
    me = c.get("/api/me").get_json()
    d = A.store.add_desk(me["id"], "Bridge Test", templates.DESK_TYPES[0]["id"], "free", {})
    did = d["id"]
    assert c.post(f"/api/desks/{did}/select", json={}).status_code == 200
    assert c.post(f"/api/desks/{did}/providers/default", json={"name": "agent"}).status_code == 400   # nothing connected yet
    tok = c.post("/api/bridges", json={"agent": "codex"}).get_json()["token"]
    assert c.post(f"/api/desks/{did}/providers/default", json={"name": "agent"}).get_json()["default"] == "agent"
    cfg = c.get("/api/config").get_json()
    assert all("not connected" in (a.get("engine_note") or "") for a in cfg["agents"])
    A.app.test_client().get("/api/bridge/jobs?wait=0", headers={"Authorization": "Bearer " + tok})
    cfg = c.get("/api/config").get_json()
    assert all(a["engine"] == "agent" and a["model"] == "your codex" and a.get("provider") == f"agent:{me['id']}" for a in cfg["agents"])
    full = A.desk_configs(A.store.desk(did))                       # the loop itself sees the text protocol switched on
    assert all(a.get("text_tools") for a in full["agents"])


def test_start_page_then_design_chat_on_the_owners_agent(app_client):
    """Step 1 of the chain: /try lands on /desk/start; once the runner has checked in, the design chat's provider is
    the owner's agent (no house key needed), not the house models."""
    from types import SimpleNamespace
    c = A.app.test_client()
    assert c.get("/try").headers["Location"].endswith("/desk/start")
    page = c.get("/desk/start")
    assert page.status_code == 200 and b"Which AI runs your desk" in page.data
    me = c.get("/api/me").get_json()
    tok = c.post("/api/bridges", json={"agent": "codex"}).get_json()["token"]
    with c.session_transaction() as sess:
        uid = sess["uid"]
    draft = SimpleNamespace(mode="live", tier="free")
    with A.app.test_request_context():
        from flask import session
        session["uid"] = uid
        if A._mode() != "demo":
            try:
                A._design_providers(draft)                 # no runner yet: the house rule applies (a key, or 503)
            except Exception:
                pass
    runner = A.app.test_client()
    runner.get("/api/bridge/jobs?wait=0", headers={"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.1"})
    assert A.bridge_online(me["id"]) is not None
    with A.app.test_request_context():
        from flask import session
        session["uid"] = uid
        providers, model = A._design_providers(draft)
    assert providers["default_provider"] == f"agent:{me['id']}" and model == ""
    assert providers["providers"][f"agent:{me['id']}"]["type"] == "bridge"
    assert "agent:" not in str(A._providers_cfg().get("providers", {}).keys())   # the shared config was not touched


def test_design_turn_answered_by_the_owners_agent(app_client, monkeypatch):
    """Step 2 of the chain on step 1's agent: a design turn streams through /api/bridge/* and the runner's answer
    comes back as the designer's reply."""
    monkeypatch.setenv("DESK_MODE", "live")                     # the real designer, on the owner's agent: no house key needed
    c = A.app.test_client()
    c.get("/try")
    me = c.get("/api/me").get_json()
    tok = c.post("/api/bridges", json={"agent": "claude"}).get_json()["token"]
    h = {"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.1"}
    runner = A.app.test_client()
    assert runner.get("/api/bridge/jobs?wait=0", headers=h).get_json() == {"job": None}
    seen = {}

    def answer():
        for _ in range(40):
            j = runner.get("/api/bridge/jobs?wait=2", headers=h).get_json().get("job")
            if j:
                seen["job"] = j
                runner.post(f"/api/bridge/jobs/{j['id']}", headers=h, json={"text": "Two plumbers, Brighton: I would start with an enquiries desk.", "seconds": 0.1})
                return
    t = threading.Thread(target=answer, daemon=True)
    t.start()
    sid = c.post("/api/design/start", json={}).get_json().get("sid")
    assert sid, "design start refused"
    r = c.post(f"/api/design/{sid}/say", json={"text": "A two-person plumbing firm in Brighton."})
    body = r.get_data(as_text=True)
    t.join(20)
    assert "job" in seen, "the runner never received the design turn: " + body[:300]
    assert "plumbing" in str(seen["job"]["messages"]).lower()
    assert "enquiries desk" in body, body[-400:]
    assert "your claude is on it" in body                                   # the wait is announced before anything streams


def test_lost_handoff_is_offered_again(app_client, monkeypatch):
    """A job the runner took but never acknowledged (dropped response, runner restart) is handed out again after
    BRIDGE_ACK_S instead of waiting out the whole call timeout."""
    monkeypatch.setattr(A, "BRIDGE_ACK_S", 0.2)
    c = A.app.test_client()
    c.get("/try")
    me = c.get("/api/me").get_json()
    tok = c.post("/api/bridges", json={"agent": "claude"}).get_json()["token"]
    h = {"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.1"}
    runner = A.app.test_client()
    runner.get("/api/bridge/jobs?wait=0", headers=h)
    out = {}

    def call():
        try:
            out["text"] = A._bridge_run(me["id"], {"system": "s", "messages": [{"role": "user", "content": "hi"}]}, None, 20)
        except Exception as e:
            out["err"] = str(e)
    t = threading.Thread(target=call, daemon=True)
    t.start()
    j1 = None
    for _ in range(50):
        j1 = runner.get("/api/bridge/jobs?wait=1", headers=h).get_json().get("job")
        if j1:
            break
    assert j1, "first hand-off"
    assert runner.get("/api/bridge/jobs?wait=0", headers=h).get_json() == {"job": None}     # just taken: not offered twice
    time.sleep(0.3)
    j2 = runner.get("/api/bridge/jobs?wait=1", headers=h).get_json().get("job")               # never acked: offered again
    assert j2 and j2["id"] == j1["id"]
    runner.post(f"/api/bridge/jobs/{j2['id']}/chunk", headers=h, json={"text": ""})            # acked now
    time.sleep(0.3)
    assert runner.get("/api/bridge/jobs?wait=0", headers=h).get_json() == {"job": None}      # acked: stays with the runner
    runner.post(f"/api/bridge/jobs/{j2['id']}", headers=h, json={"text": "answer", "seconds": 0.1})
    t.join(5)
    assert out.get("text") == "answer", out


def test_connected_agent_is_the_default_everywhere_unless_the_desk_opts_out(app_client):
    """A desk that never chose runs on the owner's agent while it is connected (every call of the desk: runs, the team
    canvas, camera questions' text); "house" opts out; off-line, the default case just uses the house models quietly."""
    c = A.app.test_client()
    _owner(c)
    from atlas import templates
    me = c.get("/api/me").get_json()
    d = A.store.add_desk(me["id"], "Nails", templates.DESK_TYPES[0]["id"], "free", {})
    did = d["id"]
    assert c.post(f"/api/desks/{did}/select", json={}).status_code == 200
    tok = c.post("/api/bridges", json={"agent": "claude"}).get_json()["token"]
    cfg = A.desk_configs(A.store.desk(did))                        # a runner exists but is off: house models, no nagging
    assert all(a.get("engine") != "agent" and not a.get("engine_note") for a in cfg["agents"])
    runner = A.app.test_client()
    runner.get("/api/bridge/jobs?wait=0", headers={"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.1"})
    cfg = A.desk_configs(A.store.desk(did))                        # connected: the agent, without any setting
    assert all(a["engine"] == "agent" and a.get("provider") == f"agent:{me['id']}" for a in cfg["agents"])
    assert cfg["providers"]["default_provider"] == f"agent:{me['id']}"
    with A.app.test_request_context():
        assert A._live_reason(A.store.desk(did)) == ""
    assert c.post(f"/api/desks/{did}/providers/default", json={"name": "house"}).get_json()["default"] == "house"
    cfg = A.desk_configs(A.store.desk(did))                        # opted out: house models even though the runner is on
    assert all(a.get("engine") != "agent" for a in cfg["agents"])
    assert c.post(f"/api/desks/{did}/providers/default", json={"name": ""}).status_code == 200
    assert A.desk_configs(A.store.desk(did))["providers"]["default_provider"] == f"agent:{me['id']}"
