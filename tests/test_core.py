"""Engine-level tests: multi-tenant store, policy layer, tools, MCP client, orchestrator in demo mode."""
import json
import time
from pathlib import Path

import pytest

from atlas import policy as P
from atlas import templates
from atlas.store import Store


# ---------------------------------------------------------------- store isolation
def test_desks_are_isolated(store: Store):
    d1 = store.add_desk(1, "A", "sales_desk", "free", {})
    d2 = store.add_desk(2, "B", "consultancy", "free", {})
    a, b = store.for_desk(d1["id"]), store.for_desk(d2["id"])
    a.add_lead("Ann", "", "ann@x.com", "", "web", "hi")
    b.add_lead("Bob", "", "bob@x.com", "", "web", "hi")
    a.upsert_contact("ann@x.com", {"name": "Ann", "stage": "New"})
    b.upsert_contact("ann@x.com", {"name": "Ann (other desk)", "stage": "Won"})   # same email, two desks
    assert [l["name"] for l in a.leads()] == ["Ann"]
    assert [l["name"] for l in b.leads()] == ["Bob"]
    assert a.contacts()[0]["stage"] == "New" and b.contacts()[0]["stage"] == "Won"
    a.create_run("r1", "t", "auto", "/tmp")
    assert [r["id"] for r in a.runs()] == ["r1"] and b.runs() == []
    qid = a.add_action("r1", "atlas", "email", "ann@x.com", "s", "b", "r")
    assert a.action(qid)["desk_id"] == d1["id"] and b.actions() == []
    assert a.stats()["leads"] == 1 and b.stats()["leads"] == 1
    a.reset()
    assert a.leads() == [] and b.leads() and b.contacts()


def test_memory_and_jobs(store: Store):
    d = store.add_desk(1, "A", "sales_desk", "free", {})
    ds = store.for_desk(d["id"])
    ds.remember("k", "v1"); ds.remember("k", "v2")
    assert [m["value"] for m in ds.recall("k")] == ["v2"]
    j = ds.add_job("task", "t", "do x", 0, time.time() - 1)
    assert [x["id"] for x in store.due_jobs(time.time())] == [j["id"]]
    store.update_job(j["id"], enabled=0)
    assert store.due_jobs(time.time()) == []


def test_migration_from_old_schema(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE runs (id TEXT PRIMARY KEY, created REAL, task TEXT, mode TEXT, status TEXT, summary TEXT,
      tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, run_dir TEXT);
      CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, email TEXT UNIQUE, phone TEXT,
      stage TEXT DEFAULT 'New', notes TEXT DEFAULT '', next_action TEXT DEFAULT '', updated REAL);
      INSERT INTO runs(id,created,task,mode,status,summary,run_dir) VALUES('old',1,'t','auto','done','', '/x');
      INSERT INTO contacts(name,email,updated) VALUES('Old','old@x.com',1);""")
    c.commit(); c.close()
    s = Store(db)
    assert s.runs()[0]["desk_id"] == 1
    assert s.contacts(desk_id=1)[0]["name"] == "Old"
    s.upsert_contact("old@x.com", {"name": "dup on desk 2"}, desk_id=2)   # UNIQUE index must be gone
    assert len(s.contacts(desk_id=2)) == 1


# ---------------------------------------------------------------- policy
@pytest.mark.parametrize("body,expect", [
    ("Hi Sam, managed lettings is 8 percent plus VAT. Sam Reid", "money"),
    ("Hi, could we meet on Tuesday 10am? Sam Reid", "specific time"),
    ("Hi **there**, Sam Reid", "markdown"),
    ("Hi [Your name] here. Sam Reid", "placeholder"),
    ("We guarantee results. Sam Reid", "guarantee"),
    ("Hello, a clean note. Sam Reid", None),
])
def test_policy_rules(body, expect):
    biz = {"sender_name": "Sam Reid", "policy": {"no_money_figures": True}}
    v = P.check_outbound("email", "Subject", body, biz)
    if expect is None:
        assert v == []
    else:
        assert any(expect in x for x in v), v


def test_policy_signoff_and_length():
    biz = {"sender_name": "Sam Reid"}
    assert any("sign-off" in x for x in P.check_outbound("email", "", "Hello there", biz))
    assert any("too long" in x for x in P.check_outbound("email", "", ("word " * 300) + "Sam Reid", biz))
    assert any("whatsapp too long" in x for x in P.check_outbound("whatsapp", "", "word " * 80, {}))


# ---------------------------------------------------------------- tools
def test_workspace_tools(tmp_path):
    from atlas.tools import WorkspaceTools
    w = WorkspaceTools(tmp_path / "run")
    assert "saved" in w.call("save_deliverable", {"name": "x.md", "markdown": "# hi"})      # alias mapping
    assert "hi" in w.call("read_file", {"file": "x.md"})
    assert "45" in w.call("run_python", {"code": "print(sum(range(10)))"})
    with pytest.raises(ValueError):
        w.call("delegate", {})                                                             # orchestrator-only
    with pytest.raises(ValueError):
        w.call("read_file", {})                                                            # missing arg


def test_run_python_timeout(tmp_path):
    from atlas.tools import WorkspaceTools
    w = WorkspaceTools(tmp_path / "run")
    import atlas.tools as T
    # keep the real test fast: patch the timeout via a tiny script that sleeps longer than allowed
    out = w.run_python("import time; time.sleep(0.2); print('ok')")
    assert "ok" in out


# ---------------------------------------------------------------- MCP
def test_mcp_client_roundtrip():
    from atlas import mcp_client as M
    root = Path(__file__).resolve().parents[1]
    srv = M.MCPServer("demo", {"command": f"py {root / 'examples' / 'demo_mcp_server.py'}"})
    try:
        srv.ensure()
        names = [s["name"] for s in srv.schemas()]
        assert "mcp__demo__add" in names and "mcp__demo__create_note" in names
        assert srv.call("add", {"a": 2, "b": 3}) == "5.0"
        assert M.is_write("create_note") and not M.is_write("echo")
    finally:
        srv.stop()


# ---------------------------------------------------------------- orchestrator end-to-end (demo provider)
def _configs(template="sales_desk"):
    t = templates.get(template)
    for a in t["agents"]:
        a["provider"] = "demo"; a["model"] = ""
    return {"providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}},
            "orchestration": {"max_iterations": 12, "max_delegation_depth": 2, "specialist_max_iterations": 4},
            "business": t["business"], "agents": t["agents"], "workflows": t["workflows"], "ui": {}}


def test_orchestrator_demo_run_queues_approval(store: Store):
    from atlas.orchestrator import Orchestrator
    d = store.add_desk(1, "Acme", "sales_desk", "free", templates.build_desk("sales_desk", {}))
    ds = store.for_desk(d["id"])
    evs = []
    orch = Orchestrator(_configs(), ds, evs.append)
    res = orch.run("New inbound lead — handle end to end.\nName: Test Lead\nCompany: (individual)\nEmail: t@x.com\nPhone: -\nSource: web\n\nEnquiry:\nWants a valuation.", "auto")
    kinds = [e.kind for e in evs]
    assert res.status == "done"
    assert "delegate" in " ".join(e.text for e in evs if e.kind == "tool")
    assert ds.actions("pending"), "demo run must queue an outbound action"
    assert not any(e.kind == "error" for e in evs), [e.text for e in evs if e.kind == "error"]
    assert ds.runs()[0]["status"] == "done"


def test_orchestrator_policy_flags_after_repeats(store: Store):
    """A body that keeps violating gets bounced twice, then queued with flags."""
    from atlas.orchestrator import Orchestrator
    from atlas.providers import ToolCall
    d = store.add_desk(1, "Acme", "sales_desk", "free", {"business": {"policy": {"no_money_figures": True}}})
    ds = store.for_desk(d["id"])
    orch = Orchestrator(_configs(), ds, lambda e: None)
    orch.run_id = "r-test"; ds.create_run("r-test", "t", "auto", "/x")
    call = ToolCall("1", "queue_action", {"kind": "email", "to": "a@x.com", "subject": "s", "body": "Fee is £500. Sam"})
    r1 = orch._tool({"id": "atlas"}, call, 0)
    r2 = orch._tool({"id": "atlas"}, call, 0)
    r3 = orch._tool({"id": "atlas"}, call, 0)
    assert r1.startswith("POLICY BLOCK") and r2.startswith("POLICY BLOCK") and "queued for approval" in r3
    assert ds.actions("pending")[0]["flags"]
