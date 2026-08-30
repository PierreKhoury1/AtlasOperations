"""Design Studio: designer parsing/normalising, demo conversation, blueprint -> desk, and the HTTP flow."""
import json

from hermes import designer as D


def J(r):
    return json.loads(r.data)


def test_split_reply_variants():
    prose, data = D.split_reply('Hello there.\n<hermes-design>{"suggestions": ["a"], "ready": false, "blueprint": null}</hermes-design>')
    assert prose == "Hello there." and data["suggestions"] == ["a"]
    # fenced json fallback
    prose, data = D.split_reply('Hi.\n```json\n{"suggestions": ["b"], "blueprint": null}\n```')
    assert prose == "Hi." and data["suggestions"] == ["b"]
    # cut-off block -> prose only, no crash
    prose, data = D.split_reply('Hi.\n<hermes-design>{"suggestions": ["c"], "blueprint": {"agents": [')
    assert prose == "Hi."
    # truncated but repairable json
    prose, data = D.split_reply('Ok.\n<hermes-design>{"suggestions": ["d"], "ready": true, "blueprint": {"agents": []}</hermes-design>')
    assert data and data["suggestions"] == ["d"]


def test_normalise_coerces_shapes():
    bp = D.normalise({
        "business": {"name": "Acme", "services": "A, B"},
        "agents": [{"id": "Hermes", "name": "Hermes"}, {"id": "Lead Researcher!", "role": "x", "tools": ["web_fetch", "delegate", "bogus"]},
                   {"name": "Writer", "strong": True}, "junk"],
        "workflows": [{"name": "Inbound", "trigger": "form", "steps": ["lead_researcher", "writer", "hermes", "nope"]}],
        "connectors": ["email", {"kind": "gmail", "name": "Inbox"}, {"kind": "carrier_pigeon"}],
        "policy": {"banned_phrases": "guaranteed, cheapest", "max_words": "150"},
    })
    ids = [a["id"] for a in bp["agents"]]
    assert ids == ["lead_researcher", "writer"]                      # hermes stripped, ids slugged
    assert bp["agents"][0]["tools"] == ["web_fetch"]                # orchestrator/unknown tools dropped
    assert bp["agents"][1]["strong"] is True and bp["agents"][1]["reports_to"] == "hermes"
    wf = bp["workflows"][0]
    assert wf["trigger"]["kind"] == "webhook" and wf["steps"] == ["lead_researcher", "writer"]
    assert [c["kind"] for c in bp["connectors"]] == ["smtp", "imap"]
    assert bp["policy"] == {"no_money_figures": True, "max_words": 150, "banned_phrases": ["guaranteed", "cheapest"]}
    assert bp["business"]["services"] == ["A", "B"]
    # previous sections survive when a turn omits them
    bp2 = D.normalise({"business": {"name": "Acme 2"}}, bp)
    assert [a["id"] for a in bp2["agents"]] == ids and bp2["business"]["name"] == "Acme 2"


def test_demo_conversation_grows_blueprint_and_builds_desk():
    s = D.new_session("demo")
    r1 = D.reply(s, "We are Acme Estates and our enquiries get answered too slowly")
    assert len(r1["blueprint"]["agents"]) == 2 and r1["ready"] is False and r1["suggestions"]
    assert r1["blueprint"]["business"]["name"] == "Acme Estates"
    r2 = D.reply(s, "Mostly through our website form")
    assert len(r2["blueprint"]["agents"]) == 3
    r3 = D.reply(s, "Warm tone, never quote prices")
    assert r3["ready"] is True and len(r3["blueprint"]["agents"]) == 4
    assert "<hermes-design>" not in r3["text"]
    conf = D.blueprint_to_desk(r3["blueprint"], "best")
    agent_ids = [a["id"] for a in conf["agents"]]
    assert agent_ids[0] == "hermes" and set(agent_ids) >= {"research", "writer", "crm", "qa"}
    strong = {a["id"] for a in conf["agents"] if a["model"].startswith("anthropic/")}
    assert {"hermes", "writer", "qa"} <= strong and "research" not in strong
    assert conf["business"]["policy"]["no_money_figures"] is True and conf["business"]["model"] == "custom"
    wf = conf["workflows"][0]
    assert [st["agent"] for st in wf["steps"]] == ["research", "writer", "qa", "crm"] and wf["trigger"]["kind"] == "webhook"
    # engine accepts the config end to end (demo provider)
    from hermes.orchestrator import Orchestrator
    from hermes import config as cfg
    configs = {"providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}},
               "orchestration": dict(cfg.DEFAULT_ORCHESTRATION), "business": conf["business"], "agents": conf["agents"],
               "workflows": conf["workflows"], "ui": {}, "mode": "demo"}
    for a in configs["agents"]:
        a["provider"] = "demo"; a["model"] = ""
    events = []
    res = Orchestrator(configs, None, lambda e: events.append(e)).run("New lead: Jane Doe, wants a valuation.", "auto")
    assert res.status == "done" and any(e.kind == "delegate" for e in events) or res.status == "done"


def test_design_http_flow(app_client):
    c = app_client
    c.post("/signup", json={"name": "Des", "company": "Bright Smile", "email": "des@example.com", "password": "password1"})
    S = J(c.post("/api/design/start", json={"tier": "free"}))
    assert S["sid"] and S["transcript"][0]["role"] == "assistant" and "Bright Smile" in S["transcript"][0]["text"]
    # streamed turn: tokens then done
    r = c.post(f"/api/design/{S['sid']}/say", json={"text": "We are Bright Smile Dental, enquiries answered too slowly"})
    body = r.data.decode()
    evs = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    kinds = [e["t"] for e in evs]
    assert kinds[-1] == "done" and "tok" in kinds and evs[-1]["blueprint"]["agents"]
    state = J(c.get(f"/api/design/{S['sid']}"))
    assert state["turn"] == 1 and len(state["transcript"]) == 3
    # owner edits on the canvas
    bp = state["blueprint"]
    bp["agents"][0]["name"] = "Lead Researcher"
    bp["agents"].append({"id": "qa", "name": "QA", "role": "Review", "tools": ["read_file"]})
    e = J(c.post(f"/api/design/{S['sid']}/blueprint", json={"blueprint": bp}))
    assert [a["name"] for a in e["blueprint"]["agents"]][0] == "Lead Researcher" and e["blueprint"]["agents"][-1]["id"] == "qa"
    # build -> desk selected, connect plan, jobs from triggers
    b = J(c.post(f"/api/design/{S['sid']}/build", json={"tier": "free"}))
    assert b["desk"]["template"] == "custom" and b["desk"]["business_name"] == "Bright Smile Dental"
    kinds_needed = [x["kind"] for x in b["connect"]["connectors"]]
    assert "webhook" in kinds_needed and b["connect"]["hook_url"].endswith(b["connect"]["hook_url"].rsplit("/", 1)[-1])
    cfgd = J(c.get("/api/config"))
    assert cfgd["template"] == "custom" and {a["id"] for a in cfgd["agents"]} >= {"hermes", "qa"}
    assert cfgd["business"]["policy"]["no_money_figures"] is True
    # rebuild updates the same desk instead of creating another
    before = len(J(c.get("/api/desks"))["desks"])
    b2 = J(c.post(f"/api/design/{S['sid']}/build", json={"tier": "balanced"}))
    assert b2["desk"]["id"] == b["desk"]["id"] and b2["desk"]["tier"] == "balanced"
    assert len(J(c.get("/api/desks"))["desks"]) == before
    # webhook lead runs on the custom desk
    token = b["connect"]["hook_url"].rsplit("/", 1)[-1]
    h = J(c.post(f"/hook/{token}", json={"name": "Test", "email": "t@x.example.com", "notes": "hello"}))
    assert h["ok"] and h["run_id"]
    assert J(c.get(f"/api/design/{S['sid']}/connect"))["connectors"]
    assert c.get("/api/design/nope").status_code == 404
