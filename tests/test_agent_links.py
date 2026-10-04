"""Agents <-> cameras and connectors: who watches what, who sends through what, and the gaps every tab shows."""
from atlas import agent_links as AL

CAMS = [{"name": "kitchen-cam"}, {"name": "bar-cam"}, {"name": "door"}]


def _agent(aid, tools, ins=(), goal=""):
    return {"id": aid, "name": aid, "tools": list(tools), "instructions": list(ins), "goal": goal}


def test_cameras_come_from_the_agents_own_instructions():
    kitchen = _agent("kitchen_watch", AL.CAMERA_TOOLS, [AL.CAMERA_LINE.format(cam="kitchen-cam")])
    roamer = _agent("floor", ["camera_ask"], goal="answer questions about any camera")
    L = AL.links([kitchen, roamer], CAMS, [])
    assert L["agents"]["kitchen_watch"]["cameras"] == ["kitchen-cam"] and not L["agents"]["kitchen_watch"]["any_camera"]
    assert L["agents"]["floor"]["any_camera"] and L["any_camera"] == ["floor"]
    assert L["cameras"] == {"kitchen-cam": ["kitchen_watch"], "bar-cam": [], "door": []}
    # "door" must not match inside "back-door" or "doorway"
    assert AL.agent_cameras(_agent("x", [], goal="watch the back-door and the doorway"), ["door"]) == []


def test_channels_need_a_connector_and_a_way_to_send():
    reporter = _agent("daily_reporter", ["camera_ask", "save_deliverable"], goal="compile the daily report and email it")
    sender = _agent("outreach", ["queue_action"], goal="reply to enquiries by email and WhatsApp")
    smtp = {"name": "mail", "kind": "smtp", "config": {}}
    L = AL.links([reporter, sender], CAMS, [smtp])
    assert "cannot send" in " ".join(L["agents"]["daily_reporter"]["gaps"])        # told to email, no queue_action
    assert L["agents"]["outreach"]["channels"] == {"email": "mail", "whatsapp": None}
    assert any("no whatsapp connector" in g for g in L["agents"]["outreach"]["gaps"])
    assert L["connectors"]["mail"] == ["outreach"]


def test_tools_that_need_a_connector_and_disabled_agents():
    booker = _agent("booker", ["calendar_book"])
    off = dict(_agent("off", ["calendar_book"]), enabled=False)
    L = AL.links([booker, off], [], [])
    assert L["agents"]["booker"]["gaps"] == ["calendar_book needs a gcal connector"] and "off" not in L["agents"]
    L = AL.links([booker], [], [{"name": "cal", "kind": "gcal", "config": {}}])
    assert L["agents"]["booker"]["gaps"] == [] and L["connectors"]["cal"] == ["booker"]


def test_assign_camera_adds_and_removes_the_binding():
    a = _agent("bar_watch", ["save_deliverable"], ["Be brief."])
    on = AL.assign_camera(a, "bar-cam")
    assert set(AL.CAMERA_TOOLS) <= set(on["tools"]) and AL.agent_cameras(on, ["bar-cam"]) == ["bar-cam"]
    assert AL.assign_camera(on, "bar-cam")["instructions"].count(AL.CAMERA_LINE.format(cam="bar-cam")) == 1   # no duplicates
    off = AL.assign_camera(on, "bar-cam", on=False)
    assert AL.agent_cameras(off, ["bar-cam"]) == [] and off["instructions"] == ["Be brief."]


def test_links_and_assign_endpoints(app_client):
    import json
    c = app_client
    c.post("/signup", json={"name": "L", "company": "Links Bistro", "email": "links@example.com", "password": "password1"})
    c.post("/login", json={"email": "links@example.com", "password": "password1"})
    c.post("/api/desks", json={"name": "Bistro", "template": "site_watch", "tier": "free"})
    cam = json.loads(c.post("/api/connectors", json={"kind": "camera", "name": "pass-cam", "config": {"source": "0"}}).data)
    cfg = json.loads(c.get("/api/config").data)
    spec = next(a["id"] for a in cfg["agents"] if a["id"] != "atlas")
    L = json.loads(c.get("/api/agents/links").data)
    assert L["camera_ids"] == {"pass-cam": cam["id"]} and "pass-cam" in L["cameras"]
    r = json.loads(c.post(f"/api/cameras/{cam['id']}/assign", json={"agent": spec}).data)
    assert r["ok"] and json.loads(c.get("/api/agents/links").data)["cameras"]["pass-cam"] == [spec]
    c.post(f"/api/cameras/{cam['id']}/assign", json={"agent": spec, "on": False})
    assert json.loads(c.get("/api/agents/links").data)["cameras"]["pass-cam"] == []
    assert c.post(f"/api/cameras/{cam['id']}/assign", json={"agent": "atlas"}).status_code == 400
