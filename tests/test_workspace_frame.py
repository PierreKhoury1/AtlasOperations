"""The workspace inside the app frame: one desk per page (X-Desk / ?desk=), one honest camera status, one model-cost
figure for Home and the workspace, failed runs that say so, run events that keep their data for a reload, and
/desk/workspace meaning "my desk" once there is one."""
import json
import time

import pytest

from atlas import templates

J = lambda r: json.loads(r.get_data(as_text=True))


@pytest.fixture()
def fresh_limits(monkeypatch):
    """The suite signs up many owners from one address; give these tests their own login allowance. Accounts on:
    another test module switches the app into open mode and leaves it there."""
    from atlas import secure as SEC
    from atlas.desk import app as A
    monkeypatch.setattr(A, "_RL_AUTH", SEC.RateLimiter(10000, 600))
    monkeypatch.setattr(A, "OPEN", False)
    return A


def _owner(c, email):
    c.get("/logout")
    if c.post("/signup", json={"name": "W", "company": "W", "email": email, "password": "password1"}).status_code != 200:
        c.post("/login", json={"email": email, "password": "password1"})


def _desk(c, name):
    return J(c.post("/api/desks", json={"name": name, "template": "site_watch", "tier": "free"}))


def test_x_desk_pins_a_page_to_its_desk(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-pin@example.com")
    a, b = _desk(c, "Pin A"), _desk(c, "Pin B")
    assert J(c.get("/api/desks"))["current"] == b["id"]                       # the session follows the newest desk
    assert J(c.get("/api/config", headers={"X-Desk": str(a["id"])}))["desk"]["id"] == a["id"]
    assert J(c.get(f"/api/config?desk={a['id']}"))["desk"]["id"] == a["id"]   # images and event streams name it in the query
    assert J(c.get("/api/desks"))["current"] == b["id"]                       # reading another desk never switches the session
    _owner(c, "ws-pin-other@example.com")
    other = _desk(c, "Other")
    assert J(c.get("/api/config", headers={"X-Desk": str(a["id"])}))["desk"]["id"] == other["id"]   # not yours: ignored


def test_workspace_means_my_desk_once_there_is_one(app_client, fresh_limits):
    c = app_client
    c.get("/logout")
    r = c.get("/desk/workspace?desk=5")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login?next=/desk/workspace%3Fdesk%3D5")   # the link survives the login
    _owner(c, "ws-door@example.com")
    c.get("/logout")
    r = c.post("/login?next=/desk/workspace%3Fdesk%3D5", data={"email": "ws-door@example.com", "password": "password1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace?desk=5")
    assert c.get("/desk/workspace").status_code == 200                         # no desk yet: the conversation that builds one
    d = _desk(c, "Door")
    r = c.get("/desk/workspace")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/desk/workspace?desk={d['id']}")
    assert c.get("/desk/workspace?new=1").status_code == 200                   # a new desk, on purpose


def test_the_link_survives_a_google_sign_in_too(app_client, fresh_limits, monkeypatch):
    """The login page hands ?next= to both ways in: the password form (then the mailed code) and Continue with Google."""
    import re
    from urllib.parse import quote
    from atlas.desk import auth as AUTH
    c = app_client
    c.get("/logout")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    monkeypatch.setenv("TWO_FACTOR", "off")
    page = c.get("/login?next=/desk/workspace%3Fdesk%3D5").get_data(as_text=True)
    assert 'class="gbtn on"' in page and 'href="/auth/google"' in page
    assert "action='/login?next='+encodeURIComponent(n)" in page and "href='/auth/google?next='+encodeURIComponent(n)" in page
    monkeypatch.setattr(AUTH, "google_exchange", lambda code, redirect: {"sub": "g-ws-next", "email": "ws-gnext@example.com", "name": "G"})
    r = c.get("/auth/google?next=" + quote("/desk/workspace?desk=5", safe=""))
    state = re.search(r"state=([^&]+)", r.headers["Location"]).group(1)
    r = c.get(f"/auth/google/callback?state={state}&code=x")
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace?desk=5")


def test_stats_carry_the_one_cost_figure(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-cost@example.com")
    _desk(c, "Cost")
    s = J(c.get("/api/stats"))
    cost = s["cost"]
    assert cost["basis"] == "estimate" and cost["scope"] == "desk" and cost["window"] == "all time"
    assert cost["tokens"] == s["tokens_in"] + s["tokens_out"]
    assert cost["gbp"] == round((s["tokens_in"] * 5 + s["tokens_out"] * 25) / 1e6 * 0.78, 2)


def test_camera_status_is_live_only_while_watched_and_fresh():
    from atlas.desk.app import _camera_status
    now = 1_000_000.0
    cam = {"config": {"source": "rtsp://cam.local/1"}}
    task = json.dumps({"connector": "door", "every_s": 8})
    on, off = {"enabled": 1, "task": task}, {"enabled": 0, "task": task}
    assert _camera_status(cam, {"ts": now - 5}, None, on, now)["state"] == "live"
    assert _camera_status(cam, {}, {"ts": now - 15 * 86400}, on, now)["state"] == "waiting"      # watched, newest frame is old
    st = _camera_status(cam, {}, {"ts": now - 15 * 86400}, off, now)
    assert st["state"] == "off" and not st["watching"] and st["frame_ts"] == now - 15 * 86400 and st["every_s"] == 8
    assert _camera_status({"config": {"_decrypt_error": "x"}}, {}, None, on, now)["problem"].startswith("stored settings")
    assert _camera_status({"config": {}}, {}, None, None, now) == {"state": "off", "watching": False, "every_s": 30.0,
                                                                     "frame_ts": 0.0, "problem": "no stream address yet"}


def test_last_vision_notes_pick_each_cameras_newest_written_note(store):
    t0 = time.time() - 100
    store.add_vision_event(1, "door", {"person": 1}, answer="first note", source="journal", ts=t0)
    store.add_vision_event(1, "door", {"person": 2}, answer="", source="camera", ts=t0 + 1)            # a detection, not a note
    store.add_vision_event(1, "door", {"person": 1}, answer="second note", source="journal", ts=t0 + 2)
    store.add_vision_event(1, "door", {"person": 3}, answer="asked: 3 people", source="camera", ts=t0 + 3)   # an answer, not a note
    store.add_vision_event(1, "yard", {}, answer="the summary", source="digest", ts=t0 + 4)
    store.add_vision_event(2, "door", {}, answer="another desk", source="journal", ts=t0 + 5)
    notes = store.last_vision_notes(1, 0)
    assert set(notes) == {"door", "yard"}
    assert notes["door"]["text"] == "second note" and notes["door"]["ts"] == t0 + 2 and notes["yard"]["source"] == "digest"
    assert store.last_vision_notes(1, t0 + 3) == {"yard": {"text": "the summary", "ts": t0 + 4, "source": "digest"}}


def test_run_events_keep_their_data_for_a_reload(store):
    store.add_event("r1", "agent_start", "writer", "Writer <- task", json.dumps({"inst": "writer#2", "assignment": "draft it"}))
    store.add_event("r1", "log", "writer", "the text", "")
    ev = store.events("r1")
    assert ev[0]["data"] == {"inst": "writer#2", "assignment": "draft it"} and ev[1]["data"] == {}
    assert [e["kind"] for e in ev] == ["agent_start", "log"]


def _configs(template="sales_desk"):
    t = templates.get(template)
    for a in t["agents"]:
        a["provider"] = "demo"; a["model"] = ""
    return {"providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}},
            "orchestration": {"max_iterations": 12, "max_delegation_depth": 2, "specialist_max_iterations": 4},
            "business": t["business"], "agents": t["agents"], "workflows": t["workflows"], "ui": {}}


def test_a_run_whose_lead_never_ran_is_failed_not_done(store, monkeypatch):
    from atlas.orchestrator import Orchestrator
    d = store.add_desk(1, "Acme", "sales_desk", "free", templates.build_desk("sales_desk", {}))
    ds = store.for_desk(d["id"])
    evs = []
    orch = Orchestrator(_configs(), ds, evs.append)
    monkeypatch.setattr(orch, "run_agent", lambda *a, **k: "ERROR from Atlas: RuntimeError: HTTP 404: This model is unavailable for free")
    res = orch.run("Summarise the day", "auto")
    assert res.status == "error" and ds.runs()[0]["status"] == "error"
    assert [e.data.get("status") for e in evs if e.kind == "done"] == ["error"]
    assert "Verified by the desk" not in res.summary                             # no success footer on a failed run


def test_cost_leads_with_its_basis(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-cost-words@example.com")
    _desk(c, "Cost words")
    cost = J(c.get("/api/stats"))["cost"]
    assert cost["note"].startswith("at paid rates")                       # the qualifier first: a truncated line still says it
    assert cost["tier"] == "free" and "not a bill" in cost["title"] and "free models" in cost["title"]
    # a free-tier desk leads with what its models cost (nothing); the paid-rate figure stays as context, never as the headline
    assert cost["value"] == "Free" and cost["label"] == "Model cost" and f"£{cost['gbp']:.2f}" in cost["note"]


def test_sample_footage_needs_no_desk(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-samples@example.com")
    assert J(c.get("/api/desks"))["desks"] == []
    assert c.get("/api/cameras").status_code == 409                       # a desk's cameras need a desk...
    r = c.get("/api/cameras/samples")                                     # ...the server's sample clips do not
    assert r.status_code == 200 and isinstance(J(r)["samples"], list)
    c.get("/logout")
    assert c.get("/api/cameras/samples").status_code == 401


def test_an_unfinished_draft_follows_its_owner_and_nobody_else(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-draft-a@example.com")
    assert J(c.get("/api/design/mine")) == {}                             # nothing started yet
    sid = J(c.post("/api/design/start", json={"tier": "free"}))["sid"]
    assert J(c.get("/api/design/mine")) == {}                             # a greeting alone is not a draft
    c.post(f"/api/design/{sid}/say", json={"text": "We are a small dental clinic; enquiries wait too long"}).get_data()
    mine = J(c.get("/api/design/mine"))
    assert mine["sid"] == sid and mine["turn"] == 1                        # another tab or device resumes it
    _owner(c, "ws-draft-b@example.com")
    assert J(c.get("/api/design/mine")) == {}
    assert c.get(f"/api/design/{sid}").status_code == 404                 # someone else's draft, even with its id
    _owner(c, "ws-draft-a@example.com")
    assert J(c.get(f"/api/design/{sid}"))["sid"] == sid
    bp = J(c.get(f"/api/design/{sid}"))["blueprint"]
    J(c.post(f"/api/design/{sid}/build", json={"blueprint": bp, "tier": "free"}))
    assert J(c.get("/api/design/mine")) == {}                             # built: no longer a draft


def test_a_failed_run_keeps_the_desks_record_of_what_it_queued(store, monkeypatch):
    from atlas.orchestrator import Orchestrator
    d = store.add_desk(1, "Acme", "sales_desk", "free", templates.build_desk("sales_desk", {}))
    ds = store.for_desk(d["id"])
    orch = Orchestrator(_configs(), ds, lambda e: None)

    def lead_fails_late(*a, **k):                                         # a specialist queued a message, then the lead's model went away
        orch._counts["queued"] += 1
        return "ERROR from Atlas: RuntimeError: HTTP 503"
    monkeypatch.setattr(orch, "run_agent", lead_fails_late)
    res = orch.run("Reply to the newest enquiry", "auto")
    assert res.status == "error" and res.summary.startswith("ERROR from Atlas")
    assert "Verified by the desk (not model-claimed): 1 action(s) queued for approval" in res.summary
