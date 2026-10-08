"""A follow-up design turn edits the sketch instead of redrawing it: ids, colours and canvas positions survive,
and agents the owner never asked to drop are carried over (atlas/designer.py normalise + reply)."""
import atlas.designer as DS


def _bp(*agents, layout=None):
    return {"business": {"name": "Acme"}, "agents": list(agents), "workflows": [], "connectors": [], "policy": {},
            **({"layout": layout} if layout else {})}


def _a(id_, name, role="Specialist", tools=("record_find",), **kw):
    return {"id": id_, "name": name, "role": role, "goal": "do the thing", "tools": list(tools), "reports_to": "atlas", **kw}


def test_rename_keeps_the_id_and_colour():
    prev = DS.normalise(_bp(_a("enquiry_writer", "Enquiry writer"), _a("research", "Research")))
    colours = {a["id"]: a["color"] for a in prev["agents"]}
    # the model renamed the writer and re-slugged its id from the new name
    nxt = DS.normalise(_bp(_a("enquiry_reply_writer", "Enquiry reply writer"), _a("research", "Research")), prev)
    ids = [a["id"] for a in nxt["agents"]]
    assert ids == ["enquiry_writer", "research"]
    assert nxt["agents"][0]["name"] == "Enquiry reply writer"
    assert {a["id"]: a["color"] for a in nxt["agents"]} == colours


def test_reorder_does_not_recolour():
    prev = DS.normalise(_bp(_a("a1", "Alpha"), _a("b2", "Beta"), _a("c3", "Gamma")))
    before = {a["id"]: a["color"] for a in prev["agents"]}
    nxt = DS.normalise(_bp(_a("c3", "Gamma"), _a("a1", "Alpha"), _a("b2", "Beta")), prev)
    assert {a["id"]: a["color"] for a in nxt["agents"]} == before
    new = DS.normalise(_bp(_a("c3", "Gamma"), _a("a1", "Alpha"), _a("b2", "Beta"), _a("d4", "Delta")), nxt)
    assert new["agents"][-1]["color"] not in before.values()


def test_silently_dropped_agents_are_carried_over_only_when_no_removal_was_asked():
    prev = DS.normalise(_bp(_a("writer", "Writer"), _a("research", "Research"), _a("qa", "QA")))
    lazy = _bp(_a("writer", "Writer"))
    kept = DS.normalise(lazy, prev, keep_missing=True)
    assert [a["id"] for a in kept["agents"]] == ["writer", "research", "qa"]
    assert kept["carried_over"] == ["research", "qa"]
    dropped = DS.normalise(lazy, prev, keep_missing=False)
    assert [a["id"] for a in dropped["agents"]] == ["writer"]
    assert DS.REMOVAL_WORDS.search("remove the QA agent") and DS.REMOVAL_WORDS.search("make the team simpler")
    assert not DS.REMOVAL_WORDS.search("rename the writer to Enquiry desk")


def test_layout_rides_along_and_follows_renamed_ids():
    prev = DS.normalise(_bp(_a("writer", "Writer"), _a("research", "Research"), layout={"writer": {"x": 410, "y": 120}}))
    assert prev["layout"] == {"writer": {"x": 410, "y": 120}}
    nxt = DS.normalise(_bp(_a("reply_writer", "Reply writer"), _a("research", "Research")), prev)
    assert nxt["layout"] == {"writer": {"x": 410, "y": 120}}
    moved = DS.normalise({**nxt, "layout": {"writer": {"x": 1, "y": 2}, "research": {"x": 3.0, "y": 4.0}, "ghost": "no"}}, nxt)
    assert moved["layout"] == {"writer": {"x": 1, "y": 2}, "research": {"x": 3, "y": 4}}


def test_demo_follow_up_turn_keeps_structure(app_client):
    """End to end through the demo designer: a second message must not replace the ids the canvas drew."""
    c = app_client
    c.post("/signup", json={"name": "Owner", "company": "Acme Lettings", "email": "stable@example.com", "password": "password1"})
    sid = c.post("/api/design/start", json={}).get_json()["sid"]

    def say(text):
        r = c.post(f"/api/design/{sid}/say", json={"text": text})
        body = r.get_data(as_text=True)
        return [l for l in body.split("\n") if l.startswith("data: ") and '"done"' in l][-1]

    say("We are a lettings agency in Clapham and reply to Rightmove enquiries far too slowly; we need replies drafted and viewings booked.")
    first = c.get(f"/api/design/{sid}").get_json()["blueprint"]
    ids1 = [a["id"] for a in first["agents"]]
    assert ids1
    c.post(f"/api/design/{sid}/layout", json={"positions": {ids1[0]: {"x": 500, "y": 60}}})
    say("Please also add an agent that chases unanswered enquiries after three days.")
    second = c.get(f"/api/design/{sid}").get_json()["blueprint"]
    ids2 = [a["id"] for a in second["agents"]]
    assert ids2[:len(ids1)] == ids1, (ids1, ids2)
    assert second["layout"] == {ids1[0]: {"x": 500, "y": 60}}
    c.get("/logout")
