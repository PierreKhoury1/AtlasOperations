"""The house agent: HOUSE_BRIDGE_OWNER's connected runner (Claude Code / Codex on the operator's machine) answers for
every account without a runner of its own, counted on each account's allowance and on USAGE_HOUSE_AGENT_DAY."""
import threading
from types import SimpleNamespace

import pytest

import atlas.desk.app as A
import atlas.providers as PV
import atlas.usage as U
from atlas import templates


def _account(client):
    client.get("/try")
    return client.get("/api/me").get_json()


def _connect(client, agent="claude"):
    tok = client.post("/api/bridges", json={"agent": agent, "name": "ops box"}).get_json()["token"]
    h = {"Authorization": "Bearer " + tok, "X-Atlas-Bridge": "0.2"}
    A.app.test_client().get("/api/bridge/jobs?wait=0", headers=h)
    return h


def _desk(uid, name="Shop"):
    return A.store.add_desk(uid, name, templates.DESK_TYPES[0]["id"], "free", {})


@pytest.fixture
def house(monkeypatch):
    """Account X is the house agent's owner, its runner connected; Y is any other account."""
    x, y = A.app.test_client(), A.app.test_client()
    mx, my = _account(x), _account(y)
    monkeypatch.setattr(A, "HOUSE_BRIDGE_EMAIL", mx["email"].lower())
    A._HB_CACHE.update(ts=0.0, id=-1, email="")
    h = _connect(x)
    assert A.house_bridge_owner() == mx["id"] and A.house_bridge_online()["name"] == "ops box"
    return SimpleNamespace(x=x, y=y, mx=mx, my=my, h=h)


def test_other_accounts_run_on_the_house_agent(house):
    d = _desk(house.my["id"])
    cfg = A.desk_configs(A.store.desk(d["id"]))
    pc = cfg["providers"]["providers"]["agent:house"]
    assert cfg["providers"]["default_provider"] == "agent:house"
    assert pc["type"] == "bridge" and pc["house"] is True and pc["owner_id"] == house.mx["id"]
    assert all(a["engine"] == "agent" and a.get("text_tools") and a["model"] == "Atlas agent (Claude Code)" for a in cfg["agents"])
    with A.app.test_request_context():
        assert A._live_reason(A.store.desk(d["id"])) == ""
    # the bridges API tells the start page
    b = house.y.get("/api/bridges").get_json()
    assert b["house"] == {"online": True, "agent": "claude", "label": "Atlas agent (Claude Code)"} and b["bridges"] == []
    # opting out: "house" = the house models, even while the house agent is on
    assert house.y.post(f"/api/desks/{d['id']}/providers/default", json={"name": "house"}).status_code == 200
    cfg = A.desk_configs(A.store.desk(d["id"]))
    assert cfg["providers"]["default_provider"] != "agent:house" and all(a.get("engine") != "agent" for a in cfg["agents"])
    # the owner's own desks: their own runner (BYO), not the house agent
    dx = _desk(house.mx["id"], "Ops")
    cfg = A.desk_configs(A.store.desk(dx["id"]))
    assert cfg["providers"]["default_provider"] == f"agent:{house.mx['id']}"
    assert not cfg["providers"]["providers"][f"agent:{house.mx['id']}"].get("house")


def test_own_runner_beats_the_house_agent(house):
    d = _desk(house.my["id"])
    _connect(house.y, "codex")
    cfg = A.desk_configs(A.store.desk(d["id"]))
    assert cfg["providers"]["default_provider"] == f"agent:{house.my['id']}"
    assert all(a["model"] == "your codex" for a in cfg["agents"])


def test_house_agent_requests_are_counted_and_capped(house, monkeypatch):
    assert U.classify("bridge", "Atlas agent (Claude Code)", "house") == "free:bridge"
    assert U.classify("bridge", "", "byo") == "byo"
    d = _desk(house.my["id"])
    o = house.my["id"]
    before_o = U.counter_n(f"o:{o}", "req:free", "D")
    before_h = U.counter_n("house", "req:bridge", "D")
    meter = U.desk_meter(A.store.desk(d["id"]), "run.you", lane="you")
    t = U.before(meter, provider="bridge", model="Atlas agent (Claude Code)", key_kind="house")
    assert t.pool == "free:bridge" and t.owner_taken
    U.after(t, http_status=200, usage={"input_tokens": 10, "output_tokens": 5})
    assert U.counter_n(f"o:{o}", "req:free", "D") == before_o + 1          # the account's daily allowance
    assert U.counter_n("house", "req:bridge", "D") == before_h + 1         # everyone together (L14)
    monkeypatch.setattr(U.CFG, "house_agent_day", before_h + 1)
    assert U.limit_value(0, "house_agent_day") == before_h + 1
    with pytest.raises(U.UsageLimit) as ei:
        U.before(meter, provider="bridge", model="x", key_kind="house")
    assert ei.value.limit_id == "house_agent_day" and ei.value.scope == "house"
    with pytest.raises(U.UsageLimit):
        U.precheck(meter, pool="free:bridge")
    monkeypatch.setattr(U.CFG, "house_agent_day", None)                    # USAGE_HOUSE_AGENT_DAY=0: no cap
    assert U.limit_value(0, "house_agent_day") is None
    U.precheck(meter, pool="free:bridge")


def test_house_provider_roundtrip_through_the_owners_runner(house):
    d = _desk(house.my["id"])
    o = house.my["id"]
    n0 = U.counter_n(f"o:{o}", "req:free", "D")
    meter = U.desk_meter(A.store.desk(d["id"]), "run.you", lane="you")
    prov = PV.make_provider({"type": "bridge", "owner_id": house.mx["id"], "house": True,
                             "default_model": "Atlas agent (Claude Code)", "call_max_s": 10}, meter=meter)
    assert prov.key_kind == "house"
    got = {}

    def call():
        try:
            got["r"] = prov.chat("Be brief.", [prov.user_message("ping")], [], model="Atlas agent (Claude Code)")
        except Exception as e:                                    # noqa: BLE001
            got["e"] = e
    th = threading.Thread(target=call)
    th.start()
    runner = A.app.test_client()
    job = None
    for _ in range(40):
        job = runner.get("/api/bridge/jobs?wait=1", headers=house.h).get_json()["job"]
        if job:
            break
    assert job and job["model"] == "" and job["messages"][-1]["content"] == "ping"   # the label is not a CLI model name
    runner.post(f"/api/bridge/jobs/{job['id']}", json={"text": "pong"}, headers=house.h)
    th.join(10)
    assert "e" not in got, got.get("e")
    assert got["r"].text == "pong"
    assert U.counter_n(f"o:{o}", "req:free", "D") == n0 + 1


def test_design_chat_on_the_house_agent(house):
    draft = SimpleNamespace(mode="live", tier="free")
    with house.y.session_transaction() as sess:
        uid = sess["uid"]
    with A.app.test_request_context():
        from flask import session
        session["uid"] = uid
        assert A._design_pool(draft) == "free:bridge"
        providers, model, on_agent = A._design_providers(draft)
    assert on_agent is False and model == ""
    assert providers["default_provider"] == "agent:house" and providers["providers"]["agent:house"]["house"] is True
    assert "agent:" not in str(A._providers_cfg().get("providers", {}).keys())


def test_no_house_agent_without_the_env(monkeypatch):
    monkeypatch.setattr(A, "HOUSE_BRIDGE_EMAIL", "")
    A._HB_CACHE.update(ts=0.0, id=-1, email="")
    assert A.house_bridge_owner() == -1 and A.house_bridge_online() is None
    c = A.app.test_client()
    _account(c)
    assert c.get("/api/bridges").get_json()["house"] == {"online": False, "agent": "", "label": ""}
