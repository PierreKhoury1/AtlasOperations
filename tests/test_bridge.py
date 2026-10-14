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
