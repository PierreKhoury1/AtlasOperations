"""A follow-up design turn edits the sketch instead of redrawing it: ids, colours and canvas positions survive, and
agents the owner never asked to drop stay (atlas/designer.py normalise, atlas/design_ops.py apply_block)."""
import atlas.design_ops as OPS
import atlas.design_state as DST
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


def test_an_omission_is_kept_on_a_loose_parse_proposed_on_a_strict_one_and_ops_never_drop_silently():
    """A follow-up whose full blueprint leaves agents out (a truncated or lazy reply) never shrinks the team (round 2
    designer spec 5.5): on a parse that is not strict they stay silently; on a strict parse their removal waits as a
    proposal for the owner's OK; an op naming no agent of the sketch removes nothing."""
    prev = DS.normalise(_bp(_a("writer", "Writer"), _a("research", "Research"), _a("qa", "QA")))
    lazy = {"intent": "design", "blueprint": _bp(_a("writer", "Writer"))}
    loose = OPS.apply_block(prev, DST.empty(), lazy, owner_text="make the writer warmer", how="loose")
    assert [a["id"] for a in loose.blueprint["agents"]] == ["writer", "research", "qa"]
    assert DST.pending_proposal(loose.design) is None and not loose.changes["converted"]
    strict = OPS.apply_block(prev, DST.empty(), lazy, owner_text="make the writer warmer", how="strict")
    x = DST.pending_proposal(strict.design)
    assert [a["id"] for a in strict.blueprint["agents"]] == ["writer", "research", "qa"]
    assert (x["kind"], x["ids"], strict.changes["converted"]) == ("park", ["research", "qa"], ["Research", "QA"])
    ops = OPS.apply_block(prev, DST.empty(), {"intent": "design", "changes": [{"op": "remove_agent", "id": "nobody"}]},
                          owner_text="remove nobody")
    assert [a["id"] for a in ops.blueprint["agents"]] == ["writer", "research", "qa"] and ops.changes["skipped"] == 1
    assert DS.REMOVAL_WORDS.search("remove the QA agent") and DS.REMOVAL_WORDS.search("make the team simpler")
    assert not DS.REMOVAL_WORDS.search("rename the writer to Enquiry desk")


def test_layout_rides_along_and_follows_renamed_ids():
    prev = DS.normalise(_bp(_a("writer", "Writer"), _a("research", "Research"), layout={"writer": {"x": 410, "y": 120}}))
    assert prev["layout"] == {"writer": {"x": 410, "y": 120}}
    nxt = DS.normalise(_bp(_a("reply_writer", "Reply writer"), _a("research", "Research")), prev)
    assert nxt["layout"] == {"writer": {"x": 410, "y": 120}}
    moved = DS.normalise({**nxt, "layout": {"writer": {"x": 1, "y": 2}, "research": {"x": 3.0, "y": 4.0}, "ghost": "no"}}, nxt)
    assert moved["layout"] == {"writer": {"x": 1, "y": 2}, "research": {"x": 3, "y": 4}}


def test_canvas_saves_keep_a_bound_watchers_rules(app_client):
    """Every /layout and /blueprint call re-normalises the whole sketch. A bound watcher's third rule used to drop out
    on each one: the 3-rule clamp ran before the server's "Your camera is ..." line was stripped and re-added."""
    c = app_client
    c.post("/signup", json={"name": "Owner", "company": "Corner Shop", "email": "rules@example.com", "password": "password1"})
    sid = c.post("/api/design/start", json={}).get_json()["sid"]
    rules = ["Note who enters", "Never guess faces", "Flag bags left behind"]
    watcher = lambda i, n: _a(i, n, role="Watches one camera", tools=("camera_ask", "camera_events"), instructions=rules)
    bp = {"business": {"name": "Corner Shop"}, "agents": [watcher("door_watcher", "Door Watcher"), watcher("till_watcher", "Till Watcher")],
          "cameras": [{"name": "door"}, {"name": "till"}]}
    first = c.post(f"/api/design/{sid}/blueprint", json={"blueprint": bp}).get_json()["blueprint"]
    for _ in range(3):
        c.post(f"/api/design/{sid}/layout", json={"positions": {"door_watcher": {"x": 640, "y": 80}}})
    stored = c.get(f"/api/design/{sid}").get_json()["blueprint"]
    again = c.post(f"/api/design/{sid}/blueprint", json={"blueprint": stored}).get_json()["blueprint"]
    assert {a["id"]: a["camera"] for a in again["agents"]} == {"door_watcher": "door", "till_watcher": "till"}
    for a in again["agents"]:
        assert a["instructions"][1:] == rules and a["instructions"][0].startswith(f"Your camera is '{a['camera']}'")
    assert again["agents"] == first["agents"] and again["layout"] == {"door_watcher": {"x": 640, "y": 80}}
    c.get("/logout")


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
