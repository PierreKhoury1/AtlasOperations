"""Team architect: the validation contract (ids, hierarchy, tools, engines), the demo designer, the live designer
with a repair turn, engine configs (leads get delegate scoped to members), the portal API and the orchestrator's
enforcement of the structure at run time."""
import json

from atlas import team as TM
from atlas.providers import LLMResponse, ToolCall


def J(resp):
    return json.loads(resp.data)


AGENTS = [
    {"id": "research_lead", "name": "Research Lead", "role": "Runs the research pod", "reports_to": "atlas",
     "instructions": ["split the work", "merge with sources", "reject unsourced claims"], "tools": ["read_file", "save_deliverable"]},
    {"id": "analyst_uk", "name": "UK Analyst", "role": "UK market", "reports_to": "research_lead",
     "instructions": ["fetch primary sources", "bullets with links"], "tools": ["web_fetch", "read_file"]},
    {"id": "analyst_de", "name": "DE Analyst", "role": "German market", "reports_to": "research_lead",
     "instructions": ["fetch primary sources", "bullets with links"], "tools": ["web_fetch", "read_file", "finish", "delegate"]},
    {"id": "writer", "name": "Writer", "role": "Drafts the note", "reports_to": "atlas",
     "instructions": ["house tone", "queue outbound"], "tools": ["queue_action", "read_file"], "engine": "hermes_agent"},
]


# ------------------------------------------------------------------ contract

def test_validate_builds_hierarchy_and_scopes_tools():
    team, errors = TM.validate_team({"agents": AGENTS, "rationale": "pod + writer",
                                     "workflow": [{"agent": "research_lead", "task": "brief"}, {"agent": "writer"}],
                                     "deliverables": ["a note"], "checks": ["sources cited"]}, hermes_available=True)
    by = {a["id"]: a for a in team["agents"]}
    assert by["research_lead"]["members"] == ["analyst_uk", "analyst_de"] and by["research_lead"]["lead"] is True
    assert by["analyst_uk"]["reports_to"] == "research_lead" and by["analyst_uk"]["lead"] is False
    assert by["analyst_de"]["tools"] == ["web_fetch", "read_file"]            # finish / delegate never grantable
    assert any("analyst_de" in e and "disallowed" in e for e in errors)
    assert by["writer"]["engine"] == "hermes_agent"
    assert [s["agents"] for s in team["workflow"]] == [["research_lead"], ["writer"]]
    assert "research_lead" in TM.tree(team) and "analyst_uk" in TM.tree(team)
    # without Hermes the engine falls back and says so
    team2, errors2 = TM.validate_team({"agents": AGENTS}, hermes_available=False)
    assert {a["id"]: a["engine"] for a in team2["agents"]}["writer"] == "atlas"
    assert any("hermes_agent is not connected" in e for e in errors2)


def test_validate_rejects_bad_structure_but_stays_usable():
    raw = {"agents": [
        {"id": "a", "name": "A", "role": "r", "reports_to": "b", "instructions": ["x", "y"]},
        {"id": "b", "name": "B", "role": "r", "reports_to": "a", "instructions": ["x", "y"]},          # cycle
        {"id": "c", "name": "C", "role": "r", "reports_to": "ghost", "instructions": ["x", "y"]},      # unknown lead
        {"id": "d", "name": "D", "role": "r", "reports_to": "e", "instructions": ["x", "y"]},
        {"id": "e", "name": "E", "role": "r", "reports_to": "f", "instructions": ["x", "y"]},
        {"id": "f", "name": "F", "role": "r", "reports_to": "atlas", "instructions": ["x", "y"]},      # d -> e -> f -> atlas too deep
        {"id": "atlas", "name": "Atlas", "role": "root"},
        {"id": "f", "name": "dup", "role": "r"},
        {"id": "g", "name": "G", "role": "r", "instructions": "one line only"},
    ], "workflow": [{"agent": "nobody"}]}
    team, errors = TM.validate_team(raw)
    by = {a["id"]: a for a in team["agents"]}
    assert "atlas" not in by and len([a for a in team["agents"] if a["id"] == "f"]) == 1
    assert by["c"]["reports_to"] == "atlas"
    assert by["a"]["reports_to"] == "atlas" or by["b"]["reports_to"] == "atlas"     # cycle broken
    assert by["d"]["reports_to"] == "atlas"                                          # too deep -> flattened
    msgs = " | ".join(errors)
    assert "cycle" in msgs and "ghost" in msgs and "deeper" in msgs and "reserved" in msgs and "used twice" in msgs
    assert "g: needs at least 2 standing orders" in msgs and "workflow step #1 names no known agent" in msgs
    assert team["workflow"]                                                          # default plan from the top level
    big, errs = TM.validate_team({"agents": [{"id": f"a{i}", "name": "x", "role": "r", "instructions": ["a", "b"]} for i in range(12)]}, max_agents=8)
    assert len(big["agents"]) == 8 and any("too large" in e for e in errs)


def test_team_to_agents_gives_leads_delegate_and_members_reports_to():
    team, _ = TM.validate_team({"agents": AGENTS}, hermes_available=True)
    agents = TM.team_to_agents(team, {"name": "Acme", "tone": "warm"}, tier="free")
    by = {a["id"]: a for a in agents}
    assert agents[0]["id"] == "atlas" and "research_lead" in agents[0]["system_prompt"] and "leads: analyst_uk, analyst_de" in agents[0]["system_prompt"]
    top_lines = [l for l in agents[0]["system_prompt"].split("Team designed")[1].split("Plan:")[0].splitlines() if l.startswith("- ")]
    assert [l.split(":")[0] for l in top_lines] == ["- research_lead", "- writer"]   # members nested under their lead, not top-level
    assert by["research_lead"]["tools"][:2] == ["delegate", "list_agents"] and by["research_lead"]["members"] == ["analyst_uk", "analyst_de"]
    assert "You lead a sub-team" in by["research_lead"]["system_prompt"]
    assert by["analyst_uk"]["reports_to"] == "research_lead" and "report to research_lead" in by["analyst_uk"]["system_prompt"]
    assert "delegate" not in by["analyst_uk"]["tools"]
    assert by["writer"]["engine"] == "hermes_agent" and by["writer"]["model"].endswith(":free")
    wf = TM.workflow_for(team)
    assert wf and wf["id"] == "designed_plan" and [s["agent"] for s in wf["steps"]] == ["research_lead", "writer"]


def test_demo_team_shapes_follow_the_task():
    biz = {"name": "Northgate Plumbing"}
    t = TM.demo_team(biz, "Research our 3 competitors and draft a comparison email")
    ids = [a["id"] for a in t["agents"]]
    assert "research_lead" in ids and "analyst_1" in ids and "analyst_3" in ids and "writer" in ids and "qa" in ids
    lead = next(a for a in t["agents"] if a["id"] == "research_lead")
    assert lead["members"] == ["analyst_1", "analyst_2", "analyst_3"]
    assert [s["agents"] for s in t["workflow"]] == [["research_lead"], ["writer"], ["qa"]]
    t2 = TM.demo_team(biz, "Reconcile last month's invoices against the bank export")
    assert [a["id"] for a in t2["agents"]] == ["analyst_data", "qa"]
    t3 = TM.demo_team(biz, "Tell me what the cameras saw after hours", cameras=["yard"])
    assert "watcher" in [a["id"] for a in t3["agents"]]


class ScriptedProvider:
    """Replies in order; records what it was asked."""
    default_model = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def user_message(self, text):
        return {"role": "user", "content": text}

    def chat(self, system, messages, tools, model="", on_token=None):
        self.calls.append((system, messages))
        text = self.replies.pop(0)
        return LLMResponse(text, [], {"role": "assistant", "content": text}, "end_turn", 10, 10, "scripted")


def test_design_team_repairs_once_from_validation_errors():
    bad = json.dumps({"agents": [{"id": "lead", "name": "Lead", "role": "leads", "reports_to": "atlas", "instructions": ["a", "b"]},
                                 {"id": "m1", "name": "M1", "role": "member", "reports_to": "nobody", "instructions": ["a", "b"], "tools": ["hack"]}]})
    good = json.dumps({"rationale": "fixed", "agents": [
        {"id": "lead", "name": "Lead", "role": "leads", "reports_to": "atlas", "instructions": ["a", "b", "c"], "tools": ["read_file"]},
        {"id": "m1", "name": "M1", "role": "member", "reports_to": "lead", "instructions": ["a", "b"], "tools": ["web_fetch"]},
        {"id": "m2", "name": "M2", "role": "member", "reports_to": "lead", "instructions": ["a", "b"], "tools": ["web_fetch"]}],
        "workflow": [{"agent": "lead", "task": "run the pod"}], "deliverables": ["brief"], "checks": ["sources"]})
    prov = ScriptedProvider([f"Here you go\n<atlas-team>{bad}</atlas-team>", f"<atlas-team>{good}</atlas-team>"])
    res = TM.design_team({"name": "Acme"}, "compare two suppliers", prov, "m", hermes_available=False, cameras=["yard"])
    assert res["turns"] == 2 and res["errors"] == []
    assert any("nobody" in w for w in res["warnings"]) and any("hack" in w for w in res["warnings"])
    assert prov.calls[1][1][-1]["content"].startswith("Your team broke these rules")
    assert "Cameras: yard" in prov.calls[0][0] and "NOT connected" in prov.calls[0][0]
    by = {a["id"]: a for a in res["team"]["agents"]}
    assert by["lead"]["members"] == ["m1", "m2"] and res["team"]["source"]["turns"] == 2
    # a reply with no JSON at all still yields a (empty) team with a clear error
    res2 = TM.design_team({"name": "Acme"}, "x", ScriptedProvider(["no json", "still none"]), "m")
    assert res2["team"]["agents"] == [] and "no <atlas-team>" in res2["errors"][0]


# ------------------------------------------------------------------ portal API

def test_team_design_and_apply_via_api(app_client):
    c = app_client
    c.post("/signup", json={"name": "Team Designer", "company": "Pod Ltd", "email": "pod@example.com", "password": "password1"})
    c.post("/login", json={"email": "pod@example.com", "password": "password1"})
    d = J(c.post("/api/desks", json={"name": "Pod Ltd", "template": "blank", "tier": "free"}))
    assert c.post(f"/api/desks/{d['id']}/team/design", json={}).status_code == 400
    r = J(c.post(f"/api/desks/{d['id']}/team/design", json={"task": "Research our 3 competitors and draft a comparison email"}))
    assert r["mode"] == "demo" and "research_lead" in r["tree"] and r["errors"] == []
    assert next(a for a in r["team"]["agents"] if a["id"] == "research_lead")["members"] == ["analyst_1", "analyst_2", "analyst_3"]
    a = J(c.post(f"/api/desks/{d['id']}/team/apply", json={"team": r["team"]}))
    assert a["agents"][0] == "atlas" and "analyst_2" in a["agents"] and a["workflow"] == "designed_plan"
    cfg = J(c.get("/api/config"))
    by = {x["id"]: x for x in cfg["agents"]}
    assert cfg["custom_roster"] is True and "research_lead" in cfg["team_tree"]
    assert by["research_lead"]["members"] == ["analyst_1", "analyst_2", "analyst_3"] and "delegate" in by["research_lead"]["tools"]
    assert by["analyst_1"]["reports_to"] == "research_lead" and by["analyst_1"]["instructions"]
    assert by["writer"]["reports_to"] == "atlas"
    # editing the roster on the Team page keeps the structure; moving a member to Atlas dissolves its link
    roster = [{"id": x["id"], "name": x["name"], "role": x["role"], "system_prompt": "", "tools": x["tools"], "enabled": True,
               "reports_to": ("atlas" if x["id"] == "analyst_3" else x["reports_to"])} for x in cfg["agents"]]
    assert c.patch(f"/api/desks/{d['id']}", json={"agents": roster}).status_code == 200
    by = {x["id"]: x for x in J(c.get("/api/config"))["agents"]}
    assert by["research_lead"]["members"] == ["analyst_1", "analyst_2"] and by["analyst_3"]["reports_to"] == "atlas"
    # one-shot: design for the job, apply, run
    run = J(c.post("/api/runs", json={"task": "Reconcile last month's invoices against the bank export", "design_team": True}))
    assert run["run_id"] and "analyst_data" in run["tree"]
    by = {x["id"]: x for x in J(c.get("/api/config"))["agents"]}
    assert "analyst_data" in by and "research_lead" not in by


# ------------------------------------------------------------------ orchestrator enforcement

def _orch_with_pod():
    from atlas import config as cfg
    from atlas.orchestrator import Orchestrator
    team, _ = TM.validate_team({"agents": AGENTS}, hermes_available=False)
    agents = TM.team_to_agents(team, {"name": "Acme"}, tier="free")
    for a in agents:
        a["provider"] = "demo"
        a["model"] = ""
    configs = {"business": {"name": "Acme", "model": "custom"}, "agents": agents, "workflows": [],
               "orchestration": {"max_delegation_depth": 2},
               "providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}}}
    events = []
    return Orchestrator(configs, None, events.append), events


def test_orchestrator_scopes_rosters_and_delegation_to_the_team():
    orch, events = _orch_with_pod()
    top = orch.roster_text("atlas")
    assert "research_lead" in top and "lead of a sub-team: analyst_uk, analyst_de" in top
    assert "· analyst_uk" in top and top.index("research_lead") < top.index("· analyst_uk")
    lead_view = orch.roster_text("research_lead")
    assert "analyst_uk" in lead_view and "writer" not in lead_view
    lead = orch.agents["research_lead"]
    assert "You lead a sub-team" in orch.system_prompt(lead) and "writer" not in orch.system_prompt(lead).split("delegate(agent_id")[1]
    out = orch._tool(lead, ToolCall(id="1", name="delegate", args={"agent_id": "writer", "task": "x"}), 1)
    assert out.startswith("ERROR: writer is not in your team")
    # a whole run in demo mode: Atlas -> lead -> members, and the lead's delegations really happen
    res = orch.run("Compare the UK and German markets for our widget", "auto")
    assert res.status == "done"
    starts = [(e.agent, e.data.get("parent")) for e in events if e.kind == "agent_start"]
    assert ("research_lead", "atlas") in starts
    assert any(a == "analyst_uk" and p == "research_lead" for a, p in starts)


def test_assemble_team_builds_a_pod_at_run_time():
    from atlas.orchestrator import Orchestrator
    configs = {"business": {"name": "Acme", "model": "custom"},
               "agents": [{"id": "atlas", "name": "Atlas", "role": "lead", "enabled": True, "provider": "demo", "model": "",
                           "tools": ["delegate", "assemble_team", "finish"], "system_prompt": "lead"}],
               "workflows": [], "orchestration": {"max_team_agents": 6},
               "providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}}}
    events = []
    orch = Orchestrator(configs, None, events.append)
    specs = [{"id": "pod", "name": "Pod Lead", "role": "leads", "system_prompt": "lead the pod", "tools": ["read_file"]},
             {"id": "m1", "name": "M1", "role": "member", "system_prompt": "do", "reports_to": "pod"},
             {"id": "m2", "name": "M2", "role": "member", "system_prompt": "do", "reports_to": "pod"}]
    out = orch._tool(orch.agents["atlas"], ToolCall(id="1", name="assemble_team", args={"agents": specs, "reason": "split"}), 0)
    assert out.startswith("Team ready") and "leads=m1,m2" in out
    assert orch.agents["pod"]["members"] == ["m1", "m2"] and orch.agents["pod"]["tools"][:2] == ["delegate", "list_agents"]
    assert orch.agents["m1"]["reports_to"] == "pod" and "delegate" not in orch.agents["m1"]["tools"]
    team_ev = next(e for e in events if e.kind == "team")
    assert "pod" in team_ev.data["tree"] and "· m1" in team_ev.data["tree"]
    bad = orch._tool(orch.agents["atlas"], ToolCall(id="2", name="assemble_team",
                                                    args={"agents": [{"id": "x", "name": "X", "role": "r", "system_prompt": "s", "reports_to": "ghost"}]}), 0)
    assert bad.startswith("ERROR: fix the team structure") and "ghost" in bad
