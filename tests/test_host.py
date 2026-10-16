"""Hosted sessions: Claude Code on the house agent is the desk's lead; the desk's tools reach it over /mcp."""
import json
import time
from types import SimpleNamespace

import pytest

import atlas.desk.app as A
import atlas.host as HOST
import atlas.usage as U
from atlas import templates


def _account(client):
    client.get("/try")
    return client.get("/api/me").get_json()


def _connect(client, agent="claude"):
    tok = client.post("/api/bridges", json={"agent": agent, "name": "ops box"}).get_json()["token"]
    h = {"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.3"}
    A.app.test_client().get("/api/bridge/jobs?wait=0", headers=h)
    return h


@pytest.fixture
def house(monkeypatch):
    x, y = A.app.test_client(), A.app.test_client()
    mx, my = _account(x), _account(y)
    monkeypatch.setattr(A, "HOUSE_BRIDGE_EMAIL", mx["email"].lower())
    A._HB_CACHE.update(ts=0.0, id=-1, email="")
    monkeypatch.setenv("PUBLIC_URL", "https://desk.test")
    h = _connect(x)
    d = A.store.add_desk(my["id"], "Salon", templates.DESK_TYPES[0]["id"], "free", {})
    assert y.post(f"/api/desks/{d['id']}/select", json={}).status_code == 200
    return SimpleNamespace(x=x, y=y, mx=mx, my=my, h=h, desk_id=d["id"])


def _take_job(h, tries=60):
    runner = A.app.test_client()
    for _ in range(tries):
        job = runner.get("/api/bridge/jobs?wait=1", headers=h).get_json()["job"]
        if job:
            return runner, job
    raise AssertionError("no job offered")


def _line(runner, h, jid, ev):
    return runner.post(f"/api/bridge/jobs/{jid}/chunk", json={"text": json.dumps(ev)}, headers=h).get_json()


def test_sync_session_marks_the_desk_hosted(house):
    r = house.y.post("/api/host/sync", json={"desk_id": house.desk_id}).get_json()
    assert r["ok"] and r["synced"] and r["online"] and r["label"] == "Atlas agent (Claude Code)" and not r["pending"]
    st = house.y.get("/api/host").get_json()
    assert st["synced"] and st["session_id"] == "" and st["turns"] == 0
    cfg = A.desk_configs(A.store.desk(house.desk_id))
    assert cfg["providers"]["default_provider"] == "agent:house"
    assert cfg["providers"]["host"] == {"session_id": "", "agent": "claude"}
    assert all(a["engine"] == "host" and a["model"].endswith("session") for a in cfg["agents"])
    with A.app.test_request_context():
        assert A._live_reason(A.store.desk(house.desk_id)) == ""
    pv = house.y.get(f"/api/desks/{house.desk_id}/providers").get_json()
    assert pv["default"] == "host" and pv["host"]["synced"] is True
    # back to the house models
    assert house.y.post(f"/api/desks/{house.desk_id}/providers/default", json={"name": "house"}).status_code == 200
    assert house.y.get("/api/host").get_json()["synced"] is False


def test_sync_before_a_desk_is_remembered(house):
    fresh = A.app.test_client()
    _account(fresh)
    r = fresh.post("/api/host/sync", json={}).get_json()
    assert r["ok"] and r["pending"] is True
    with fresh.session_transaction() as sess:
        assert sess.get("host_pref") is True


def test_hosted_turn_end_to_end(house):
    """A task on a hosted desk: the job reaches the house runner with an MCP token, Claude Code's tool calls come back
    through /mcp (finish included), its stream lines become feed events, the session id is kept, usage is counted."""
    house.y.post("/api/host/sync", json={"desk_id": house.desk_id})
    o = house.my["id"]
    n0 = U.counter_n(f"o:{o}", "req:free", "D")
    desk = A.store.desk(house.desk_id)
    rid = A._start_run(desk, "Check today's bookings and tell me what needs me.", "auto")
    runner, job = _take_job(house.h)
    assert job["kind"] == "host" and job["desk_id"] == house.desk_id and job["resume"] is False and job["session_id"]
    assert job["mcp"]["url"] == "https://desk.test/mcp" and job["mcp"]["token"].startswith("ath_")
    assert "finish" in job["tools"] and "You are Atlas" in job["system"] and "<atlas>" not in job["system"]
    assert job["task"].startswith("Check today")
    tok = job["mcp"]["token"]
    mh = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    # Claude Code starts: the init line carries the session id
    sid = job["session_id"]
    assert _line(runner, house.h, job["id"], {"type": "system", "subtype": "init", "session_id": sid, "tools": []})["stop"] is False
    # MCP handshake + tools
    init = runner.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}), headers=mh)
    assert init.status_code == 200 and init.get_json()["result"]["serverInfo"]["name"] == "atlas"
    assert runner.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), headers=mh).status_code == 202
    tl = runner.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), headers=mh).get_json()
    names = [t["name"] for t in tl["result"]["tools"]]
    assert "finish" in names and names == job["tools"]
    assert all("inputSchema" in t and t["inputSchema"]["type"] == "object" for t in tl["result"]["tools"])
    bad = runner.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                               "params": {"name": "delete_everything", "arguments": {}}}), headers=mh).get_json()
    assert bad["result"]["isError"] is True and "NOT ALLOWED" in bad["result"]["content"][0]["text"]
    # an assistant message: metered, logged
    assert _line(runner, house.h, job["id"], {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 50, "output_tokens": 9},
                                                                              "content": [{"type": "text", "text": "Looking at the bookings now."}]}})["stop"] is False
    fin = runner.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                               "params": {"name": "finish", "arguments": {"summary": "Two bookings today; one needs a deposit reminder (queued)."}}}), headers=mh).get_json()
    assert fin["result"]["isError"] is False
    _line(runner, house.h, job["id"], {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "total_cost_usd": 0.01, "session_id": sid})
    runner.post(f"/api/bridge/jobs/{job['id']}", json={"text": "Done.", "seconds": 1.2}, headers=house.h)
    A._runs[rid]["thread"].join(15)
    run = A.store.for_desk(house.desk_id).run(rid) if hasattr(A.store.for_desk(house.desk_id), "run") else None
    kinds = [e["kind"] for e in A._runs[rid]["events"]]
    texts = " | ".join(e["text"] for e in A._runs[rid]["events"])
    assert "agent_start" in kinds and "tool" in kinds and "done" in kinds, kinds
    assert "finish" in texts and "Looking at the bookings now." in texts
    done = [e for e in A._runs[rid]["events"] if e["kind"] == "done"][-1]
    assert done["data"]["status"] == "done" and "deposit reminder" in done["text"], done
    if run:
        assert run["status"] == "done"
    assert U.counter_n(f"o:{o}", "req:free", "D") == n0 + 1
    conf = A.store.desk(house.desk_id)["config"]
    assert conf["host"]["session_id"] == sid and conf["host"]["turns"] == 1
    assert house.y.get("/api/host").get_json()["session_id"] == sid[:8]
    # the token died with the run
    assert runner.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}), headers=mh).status_code == 401
    assert runner.get("/mcp").status_code == 405
    # the next turn resumes the same session
    rid2 = A._start_run(A.store.desk(house.desk_id), "And tomorrow?", "auto")
    runner, job2 = _take_job(house.h)
    assert job2["resume"] is True and job2["session_id"] == sid
    _line(runner, house.h, job2["id"], {"type": "result", "subtype": "success", "result": "Nothing tomorrow."})
    runner.post(f"/api/bridge/jobs/{job2['id']}", json={"text": "Nothing tomorrow."}, headers=house.h)
    A._runs[rid2]["thread"].join(15)
    done2 = [e for e in A._runs[rid2]["events"] if e["kind"] == "done"][-1]
    assert done2["data"]["status"] == "done" and done2["text"].startswith("Nothing tomorrow.")
    assert A.store.desk(house.desk_id)["config"]["host"]["turns"] == 1     # no init line this time: the count moves on the init


def test_hosted_turn_stops_at_the_house_cap(house, monkeypatch):
    house.y.post("/api/host/sync", json={"desk_id": house.desk_id})
    used = U.counter_n("house", "req:bridge", "D")
    monkeypatch.setattr(U.CFG, "house_agent_day", used + 1)
    rid = A._start_run(A.store.desk(house.desk_id), "Anything new?", "auto")
    runner, job = _take_job(house.h)
    msg = {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "Hi"}]}}
    assert _line(runner, house.h, job["id"], msg)["stop"] is False
    assert _line(runner, house.h, job["id"], msg)["stop"] is True           # the cap: the runner kills the process
    runner.post(f"/api/bridge/jobs/{job['id']}", json={"error": "stopped by the desk"}, headers=house.h)
    A._runs[rid]["thread"].join(15)
    done = [e for e in A._runs[rid]["events"] if e["kind"] == "done"][-1]
    assert done["data"]["status"] == "limit" and done["data"]["limit"]["limit_id"] == "house_agent_day", done


def test_mcp_rejects_unknown_tokens():
    c = A.app.test_client()
    r = c.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
               headers={"Authorization": "Bearer ath_nope", "Content-Type": "application/json"})
    assert r.status_code == 401
    assert c.post("/mcp", data="{}", headers={"Content-Type": "application/json"}).status_code == 401
    assert HOST.live("ath_nope") is None and HOST.live_count() == 0
