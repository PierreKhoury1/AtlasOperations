"""Blank desk, portal-edited roster and simple/full view level."""
import json

from atlas import templates as T


def J(resp):
    return json.loads(resp.data)


def test_blank_template_is_atlas_only():
    t = T.get("blank")
    assert [a["id"] for a in t["agents"]] == ["atlas"]
    assert t["workflows"] == []
    assert any(d["id"] == "blank" for d in T.DESK_TYPES)


def test_blank_desk_start_simple_and_expand(app_client):
    c = app_client
    c.post("/signup", json={"name": "Team Tester", "company": "Scratch Ltd", "email": "team@example.com", "password": "password1"})
    c.post("/login", json={"email": "team@example.com", "password": "password1"})
    d = J(c.post("/api/desks", json={"name": "Scratch Ltd", "template": "blank", "tier": "free"}))
    assert d["template"] == "blank" and d["ui_level"] == "simple"
    cfg = J(c.get("/api/config"))
    assert [a["id"] for a in cfg["agents"]] == ["atlas"]
    assert cfg["ui_level"] == "simple" and cfg["custom_roster"] is False
    tool_ids = {t["id"] for t in cfg["tools"]}
    assert "delegate" in tool_ids and "finish" not in tool_ids and "assemble_team" not in tool_ids
    r = J(c.patch(f"/api/desks/{d['id']}", json={"ui_level": "full"}))
    assert r["ui_level"] == "full"
    assert J(c.get("/api/config"))["ui_level"] == "full"
    assert c.patch(f"/api/desks/{d['id']}", json={"ui_level": "bogus"}).status_code == 200      # ignored, unchanged
    assert J(c.get("/api/config"))["ui_level"] == "full"


def test_roster_edit_validation_and_reset(app_client):
    c = app_client
    c.post("/login", json={"email": "team@example.com", "password": "password1"})
    d = J(c.get("/api/desks"))
    did = d["current"]
    cfg = J(c.get("/api/config"))
    atlas = next(a for a in cfg["agents"] if a["id"] == "atlas")
    roster = [
        atlas,
        {"name": "Research Analyst", "role": "Finds facts", "system_prompt": "You research things.",
         "tools": ["web_fetch", "read_file", "finish", "delegate", "crm_update", "nonsense"], "enabled": True, "color": "#7c3aed"},
        {"id": "Writer!!", "name": "Writer", "role": "Drafts replies", "tools": ["queue_action"], "enabled": False},
    ]
    r = c.patch(f"/api/desks/{did}", json={"agents": roster})
    assert r.status_code == 200, r.data
    cfg = J(c.get("/api/config"))
    assert cfg["custom_roster"] is True
    ids = [a["id"] for a in cfg["agents"]]
    assert ids[0] == "atlas" and "research_analyst" in ids and "writer" in ids
    ra = next(a for a in cfg["agents"] if a["id"] == "research_analyst")
    assert set(ra["tools"]) == {"web_fetch", "read_file", "crm_update"}       # finish/delegate/unknown stripped
    assert ra["system_prompt"] == "You research things."                        # suffix not echoed back
    wr = next(a for a in cfg["agents"] if a["id"] == "writer")
    assert wr["enabled"] is False and wr["color"]                              # colour auto-assigned
    # the engine sees the suffix and the disabled flag
    from atlas.desk import app as A
    eng = A.desk_configs(A.store.desk(did))
    eng_ra = next(a for a in eng["agents"] if a["id"] == "research_analyst")
    assert eng_ra["system_prompt"].endswith(T._SPECIALIST_SUFFIX)
    assert next(a for a in eng["agents"] if a["id"] == "writer")["enabled"] is False
    # invalid rosters
    assert c.patch(f"/api/desks/{did}", json={"agents": []}).status_code == 400
    assert c.patch(f"/api/desks/{did}", json={"agents": [{"name": "solo", "tools": []}]}).status_code == 400   # no atlas
    dup = [atlas, {"name": "x"}, {"name": "X"}]
    assert "share" in J(c.patch(f"/api/desks/{did}", json={"agents": dup}))["error"]
    # atlas always keeps delegate/finish path even if the client strips them
    stripped = [{"id": "atlas", "name": "Atlas", "tools": ["web_fetch"]}, {"name": "helper"}]
    assert c.patch(f"/api/desks/{did}", json={"agents": stripped}).status_code == 200
    at = next(a for a in J(c.get("/api/config"))["agents"] if a["id"] == "atlas")
    assert {"delegate", "list_agents", "web_fetch"} <= set(at["tools"])
    # reset
    assert c.patch(f"/api/desks/{did}", json={"reset_agents": True}).status_code == 200
    cfg = J(c.get("/api/config"))
    assert cfg["custom_roster"] is False and [a["id"] for a in cfg["agents"]] == ["atlas"]


def test_blank_desk_runs_a_task(app_client):
    c = app_client
    c.post("/login", json={"email": "team@example.com", "password": "password1"})
    r = J(c.post("/api/runs", json={"task": "Draft a two-line hello to a new customer", "mode": "auto"}))
    assert r.get("run_id")
    import time
    for _ in range(200):
        if not J(c.get("/api/live"))["runs"]:
            break
        time.sleep(0.2)
    run = J(c.get(f"/api/runs/{r['run_id']}"))
    assert run["status"] in ("done", "error")
