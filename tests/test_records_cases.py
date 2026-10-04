"""Records (the desk's live model of the business) and cases (work that lasts longer than one run)."""
import json
import time

import pytest

from atlas import cases as C
from atlas import records as R


def J(resp):
    return json.loads(resp.data)


# ------------------------------------------------------------------------------------------------ records
def test_upsert_merges_by_natural_key(store):
    d = store.add_desk(0, "D", "sales_desk", "free", {})["id"]
    a, ch = R.upsert(store, d, "person", {"name": "Jane Doe", "email": "Jane@Example.com"})
    assert a["key"] == "jane@example.com" and a["title"] == "Jane Doe" and set(ch) == {"name", "email"}
    b, ch2 = R.upsert(store, d, "person", {"email": "jane@example.com", "phone": "07700 900123", "name": ""})
    assert b["id"] == a["id"] and ch2 == ["phone"] and b["props"]["name"] == "Jane Doe"     # blanks never wipe a field
    assert R.by_key(store, d, "person", " JANE@example.com ")["id"] == a["id"]
    assert R.person_for(store, d, phone="+44 7700 900123")["id"] == a["id"]               # phone found inside the props
    o, _ = R.upsert(store, d, "order", {"ref": "A-1001", "total": 42.5})
    assert o["key"] == "a-1001" and o["title"] == "A-1001"
    with pytest.raises(ValueError):
        R.upsert(store, d, "order", {"total": 3})                                       # nothing identifies it
    assert {t["type"]: t["count"] for t in R.types_for(store, d)}["person"] == 1
    c, _ = R.upsert(store, d, "Supplier Account", {"name": "Brakes Ltd"})                # custom type, slugged
    assert c["type"] == "supplier_account" and any(t["type"] == "supplier_account" and not t["builtin"] for t in R.types_for(store, d))


def test_links_graph_timeline_and_describe(store):
    d = store.add_desk(0, "D", "sales_desk", "free", {})["id"]
    p, _ = R.upsert(store, d, "person", {"name": "Sam", "email": "sam@x.com"})
    org, _ = R.upsert(store, d, "organisation", {"name": "Northgate"})
    bk, _ = R.upsert(store, d, "booking", {"ref": "BK-7", "when": "Fri 10:00"})
    assert R.link(store, d, p["id"], "works at", org["id"], actor="atlas")
    assert not R.link(store, d, p["id"], "works_at", org["id"])                           # once only
    R.link(store, d, bk["id"], "booked_by", p["id"])
    ls = R.links(store, p["id"])
    assert {(l["rel"], l["direction"]) for l in ls} == {("works_at", "out"), ("booked_by", "in")}
    g = R.graph(store, org["id"], depth=2)
    assert {n["id"] for n in g["nodes"]} == {p["id"], org["id"], bk["id"]} and len(g["edges"]) == 2
    R.add_timeline(store, d, record_id=p["id"], kind="note", actor="owner", text="prefers mornings")
    tl = R.timeline_for(store, p["id"])
    assert tl[0]["text"] == "prefers mornings" and any(t["kind"] == "linked" for t in tl)
    txt = R.describe(store, p["id"])
    assert "works_at organisation" in txt and "booked_by booking" in txt and "prefers mornings" in txt
    assert R.resolve_ref(store, d, "#%d" % bk["id"])["id"] == bk["id"]
    assert R.resolve_ref(store, d, "person:SAM@x.com")["id"] == p["id"]
    R.delete(store, org["id"])
    assert not any(l["rel"] == "works_at" for l in R.links(store, p["id"]))


def test_crm_contacts_and_cameras_become_records(store):
    d = store.add_desk(0, "D", "sales_desk", "free", {})["id"]
    store.upsert_contact("lee@shop.com", {"name": "Lee", "company": "Lee's Shop", "phone": "+447700900111"}, desk_id=d)
    p = R.person_for(store, d, email="lee@shop.com")
    assert p and p["props"]["company"] == "Lee's Shop" and p["source"] == "crm"
    assert any(l["rel"] == "works_at" and l["record"]["title"] == "Lee's Shop" for l in R.links(store, p["id"]))
    store.upsert_contact("lee@shop.com", {"stage": "Contacted"}, desk_id=d)
    assert R.get(store, p["id"])["props"]["stage"] == "Contacted"                          # CRM edits flow through
    store.add_connector(d, "camera", "till", {"source": "rtsp://u:pw@cam/1", "notes": "front till"})
    out = R.sync_desk(store, d)
    cam = R.by_key(store, d, "camera", "till")
    assert out["cameras"] == 1 and cam["props"]["notes"] == "front till" and "rtsp" not in json.dumps(cam["props"])


# ------------------------------------------------------------------------------------------------ case engine
@pytest.fixture()
def engine(store, monkeypatch):
    """Cases with a fake run starter: runs are ids in a dict until the test finishes them."""
    runs: dict[str, dict] = {}
    told: list[str] = []

    def start(desk, task, mode, lead_id=None, case_id=None):
        rid = f"r{len(runs) + 1}"
        runs[rid] = {"task": task, "case": case_id, "status": "running"}
        return rid

    monkeypatch.setattr(C, "START_RUN", start)
    monkeypatch.setattr(C, "RUN_STATUS", lambda rid: runs.get(rid, {}).get("status", "lost"))
    monkeypatch.setattr(C, "NOTIFY", lambda did, text: told.append(text))
    desk = store.add_desk(0, "Clinic", "sales_desk", "free", {"business": {"name": "Clinic"}})
    return store, desk, runs, told


def _finish(store, runs, rid, status="done", summary="did it"):
    runs[rid]["status"] = status
    C.run_finished(store, runs[rid]["case"], rid, status, summary)


def test_enquiry_lifecycle_sent_wait_timeout_followup_reply(engine):
    store, desk, runs, told = engine
    p, _ = R.upsert(store, desk["id"], "person", {"name": "Jo", "email": "jo@x.com"})
    c = C.open_case(store, desk["id"], "enquiry", "Jo - whitening price?", p["id"], brief="Email: jo@x.com\nEnquiry:\nprice?")
    assert c["state"] == "new" and c["waiting_for"] == "atlas" and c["due_at"] > time.time()
    rid = C.kick(store, desk, c)
    c = C.get(store, c["id"])
    assert rid == "r1" and c["active_run"] == "r1" and c["runs"] == 1
    task = runs["r1"]["task"]
    assert task.startswith(f"Case #{c['id']} · Enquiry · step: New") and "YOUR STEP NOW" in task and "Email: jo@x.com" in task
    assert C.kick(store, desk, c) == ""                                                   # one run at a time
    # the run queued a reply (tagged to the case) -> waiting for approval, not moved on
    aid = store.add_action("r1", "atlas", "email", "jo@x.com", "Re: price", "Hi Jo", "", desk_id=desk["id"])
    store.tag_action_case(aid, c["id"])
    _finish(store, runs, "r1")
    c = C.get(store, c["id"])
    assert c["state"] == "new" and c["waiting_for"] == "approval" and C.lane(c) == "you" and not c["active_run"]
    # owner approves -> sent -> waiting for their reply with a 48 h timer
    store.decide_action(aid, "sent")
    assert C.fire(store, c["id"], "sent", "email → jo@x.com") == ""
    c = C.get(store, c["id"])
    assert c["state"] == "waiting_reply" and c["waiting_for"] == "reply" and C.lane(c) == "customer"
    assert 47 * 3600 < c["wake_at"] - time.time() <= 48 * 3600
    # nothing for 48 h -> the clock moves it to follow_up and starts that step
    out = C.tick(store, now=time.time() + 49 * 3600)
    c = C.get(store, c["id"])
    assert out["timeouts"] == 1 and c["state"] == "follow_up" and c["active_run"] == "r2"
    assert "No reply for two days" in runs["r2"]["task"]
    # they reply while Atlas is mid-step: held, then handed over when the step ends
    assert C.inbound_reply(store, desk, c, "sms", "Sorry, busy week - Thursday?") == ""
    assert C.get(store, c["id"])["data"]["pending_reply"] is True
    _finish(store, runs, "r2")
    c = C.get(store, c["id"])
    assert c["active_run"] == "r3" and "new reply arrived" in runs["r3"]["task"] and "Thursday" in runs["r3"]["task"]
    kinds = [t["kind"] for t in R.timeline_for(store, case_ids=[c["id"]], limit=50)]
    assert {"opened", "run", "run_done", "sent", "state", "reply"} <= set(kinds)
    assert any(t["kind"] == "reply" for t in R.timeline_for(store, p["id"]))               # also on Jo's own record


def test_reply_moves_waiting_case_and_booking_closes(engine):
    store, desk, runs, _ = engine
    c = C.open_case(store, desk["id"], "enquiry", "Visit", 0)
    C.enter(store, c["id"], "waiting_reply", kick_now=False)
    rid = C.inbound_reply(store, desk, C.get(store, c["id"]), "email", "Yes please, Tuesday")
    c = C.get(store, c["id"])
    assert rid and c["state"] == "replied" and "Tuesday" in runs[rid]["task"]
    _finish(store, runs, rid)
    C.fire(store, c["id"], "booked", "booking Tue 10:00")
    c = C.get(store, c["id"])
    assert c["state"] == "booked" and c["closed_at"] and c["outcome"] == "booked" and C.lane(c) == "closed"
    assert C.fire(store, c["id"], "reply", "thanks!") == ""                                # closed cases only log


def test_rejections_redraft_then_owner(engine):
    store, desk, runs, _ = engine
    c = C.open_case(store, desk["id"], "enquiry", "Quote", 0)
    rid = C.kick(store, desk, c)
    _finish(store, runs, rid)
    assert C.get(store, c["id"])["state"] == "needs_owner"                                 # finished without sending
    rid = C.enter(store, c["id"], "new")

    def draft_then_reject(rid, n):                       # the step queues a draft; the owner rejects it
        aid = store.add_action(rid, "atlas", "email", "q@x.com", f"Quote v{n}", "...", "", desk_id=desk["id"])
        store.tag_action_case(aid, c["id"])
        _finish(store, runs, rid)
        assert C.get(store, c["id"])["waiting_for"] == "approval"
        store.decide_action(aid, "rejected")
        return C.fire(store, c["id"], "rejected", f"rejected draft {n} - too pushy")

    for n in (1, 2):
        rid = draft_then_reject(rid, n)
        c2 = C.get(store, c["id"])
        assert rid and c2["state"] == "revise" and c2["data"]["rejections"] == n and c2["active_run"] == rid
        assert "too pushy" in runs[rid]["task"]                                             # the owner's note reaches the redraft
    assert draft_then_reject(rid, 3) == ""
    c2 = C.get(store, c["id"])
    assert c2["state"] == "needs_owner" and c2["waiting_for"] == "owner" and c2["data"]["rejections"] == 3


def test_sla_breach_is_flagged_once_and_owner_told(engine):
    store, desk, runs, told = engine
    c = C.open_case(store, desk["id"], "enquiry", "Late one", 0)
    C.enter(store, c["id"], "needs_owner", kick_now=False)
    out = C.tick(store, now=time.time() + 25 * 3600)
    c = C.get(store, c["id"])
    assert out["breached"] == 1 and c["breached"] == 1 and c["priority"] == "high" and "deadline missed" in told[0]
    assert C.tick(store, now=time.time() + 26 * 3600)["breached"] == 0
    C.enter(store, c["id"], "new", kick_now=False)
    assert C.get(store, c["id"])["breached"] == 0                                           # a new state, a new clock


def test_incident_cases_from_camera_alerts(engine):
    store, desk, runs, _ = engine
    rid = C.camera_alert(store, desk, "back-door", "CAMERA ALERT — back-door\nSeen: 1 person", "person after hours", 11)
    inc = C.list_cases(store, desk["id"], ctype="incident")[0]
    assert rid and inc["priority"] == "high" and R.get(store, inc["record_id"])["type"] == "camera"
    assert "CAMERA ALERT" in runs[rid]["task"]
    assert C.camera_alert(store, desk, "back-door", "again", "person still there", 12) == ""   # same episode, Atlas busy
    assert len(C.list_cases(store, desk["id"], ctype="incident")) == 1
    _finish(store, runs, rid)
    inc = C.get(store, inc["id"])
    assert inc["state"] == "monitoring" and inc["waiting_for"] == "time"
    rid2 = C.camera_alert(store, desk, "back-door", "CAMERA ALERT — back-door again", "person returned", 13)
    assert rid2 and C.get(store, inc["id"])["state"] == "open"                               # fired again -> re-assess
    _finish(store, runs, rid2)
    C.tick(store, now=time.time() + 31 * 60)                                                # 30 min quiet -> re-check
    inc = C.get(store, inc["id"])
    assert inc["state"] == "review" and inc["active_run"]
    _finish(store, runs, inc["active_run"])
    inc = C.get(store, inc["id"])
    assert inc["state"] == "resolved" and inc["closed_at"]


def test_case_tools_bind_move_and_schedule(engine):
    store, desk, runs, _ = engine
    ds = store.for_desk(desk["id"])
    out, bound = C.tool(ds, "case_open", {"type": "task", "title": "Chase the supplier", "record": {"type": "organisation", "key": "Brakes"},
                                          "note": "invoice 77 overdue"}, "atlas", "run-x", None)
    assert bound and "bound it to this run" in out
    c = C.get(store, bound)
    assert c["active_run"] == "run-x" and c["record_id"] and R.get(store, c["record_id"])["type"] == "organisation"
    out, _ = C.tool(ds, "case_update", {"note": "emailed accounts", "fact": "contact is Dee", "wait_hours": 4}, "atlas", "run-x", bound)
    c = C.get(store, bound)
    assert "will look again in 4 h" in out and c["waiting_for"] == "time" and c["data"]["facts"] == ["contact is Dee"]
    C.run_finished(store, bound, "run-x", "done", "")
    c = C.get(store, bound)
    assert c["state"] == "todo" and c["waiting_for"] == "time" and not c["active_run"]        # the agent's choice stands
    C.tick(store, now=time.time() + 5 * 3600)
    c = C.get(store, bound)
    assert c["active_run"] and "Scheduled check" in runs[c["active_run"]]["task"] and "contact is Dee" in runs[c["active_run"]]["task"]
    rid = c["active_run"]
    out, _ = C.tool(ds, "case_update", {"state": "nonsense"}, "atlas", rid, bound)
    assert out.startswith("ERROR") and "todo" in out
    out, _ = C.tool(ds, "case_update", {"close": True, "outcome": "paid"}, "atlas", rid, bound)
    c = C.get(store, bound)
    assert c["closed_at"] and c["outcome"] == "paid"
    assert "Chase the supplier" in C.tool(ds, "case_list", {"include_closed": True}, "atlas", "", None)[0]


def test_runaway_case_pauses_and_refused_start_retries(engine, monkeypatch):
    store, desk, runs, _ = engine
    c = C.open_case(store, desk["id"], "task", "Loop", 0)
    for _ in range(C.MAX_RUNS_PER_DAY):
        rid = C.kick(store, desk, C.get(store, c["id"]))
        assert rid
        runs[rid]["status"] = "done"
        C._set(store, c["id"], active_run="")
    assert C.kick(store, desk, C.get(store, c["id"])) == ""
    c = C.get(store, c["id"])
    assert c["waiting_for"] == "owner" and any(t["kind"] == "paused" for t in R.timeline_for(store, case_ids=[c["id"]]))

    def refuse(*a, **k):
        raise RuntimeError("spend cap reached")
    monkeypatch.setattr(C, "START_RUN", refuse)
    c2 = C.open_case(store, desk["id"], "task", "Blocked", 0)
    assert C.kick(store, desk, c2) == ""
    c2 = C.get(store, c2["id"])
    assert c2["active_run"] == "" and c2["wake_at"] > time.time() + 10 * 60 and "spend cap" in R.timeline_for(store, case_ids=[c2["id"]])[0]["text"]


def test_playbook_validation_and_desk_playbooks(store):
    ok, errs = C.validate_playbook({"states": [{"id": "a", "kind": "work", "on": {"sent": "zzz"}}]})
    assert not ok and "unknown target state 'zzz'" in errs[0]
    pb = {"label": "Return", "initial": "check", "states": [
        {"id": "check", "kind": "work", "task": "Check the return.", "after_run": "refund"},
        {"id": "refund", "kind": "wait", "wait": "owner", "on": {"sent": "done"}},
        {"id": "done", "kind": "done", "outcome": "refunded"}]}
    assert C.validate_playbook(pb)[0]
    desk = store.add_desk(0, "Shop", "sales_desk", "free", {"ops": {"playbooks": {"return": pb, "broken": {"states": []}}}})
    pbs = C.playbooks(desk)
    assert "return" in pbs and "broken" not in pbs and "enquiry" in pbs
    c = C.open_case(store, desk["id"], "return", "Order A-1 return", 0, desk=desk)
    assert c["type"] == "return" and c["state"] == "check"
    assert C.open_case(store, desk["id"], "unknown-type", "x", 0)["type"] == "task"
    assert C.duration("15m") == 900 and C.duration("2d") == 172800 and C.duration("bad") == 0


# ------------------------------------------------------------------------------------------------ portal flow (demo models)
def _wait_idle(c, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not J(c.get("/api/live"))["runs"]:
            return True
        time.sleep(0.2)
    return False


def _wait(pred, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    return None


def test_portal_lead_becomes_case_and_conversation_continues(app_client):
    import atlas.desk.app as A
    c = app_client
    A._RL_AUTH._hits.clear()
    if c.post("/login", json={"email": "ops@example.com", "password": "password1"}).status_code != 200:
        assert J(c.post("/signup", json={"name": "Ops", "email": "ops@example.com", "password": "password1"}))["ok"]
    A._RL_AUTH._hits.clear()
    d = J(c.post("/api/desks", json={"name": "Ops Clinic", "template": "sales_desk", "tier": "free", "sender_name": "Rana"}))
    c.post(f"/api/desks/{d['id']}/select")
    assert c.get("/desk/ops").status_code == 200
    cfg = J(c.get("/api/config"))
    atlas = next(a for a in cfg["agents"] if a["id"] == "atlas")
    assert {"record_find", "record_save", "case_update"} <= set(atlas["runtime_tools"])
    # a lead arrives -> an enquiry case is opened and worked
    r = J(c.post("/api/leads", json={"name": "Dana Levi", "email": "dana@example.com", "phone": "+447700900321",
                                     "notes": "Do you do Invisalign? Price?"}))
    assert r["run_id"] and _wait_idle(c)
    cases = J(c.get("/api/cases"))["cases"]
    case = next(x for x in cases if x["subject"] and x["subject"]["title"] == "Dana Levi")
    assert case["type"] == "enquiry" and case["state"] == "new" and case["lane"] == "you"   # waiting for the owner's approval
    detail = J(c.get(f"/api/cases/{case['id']}"))
    pend = [a for a in detail["actions"] if a["status"] == "pending"]
    assert pend and pend[0]["case_id"] == case["id"]
    assert any(t["kind"] == "note" and t["actor"] == "atlas" for t in detail["timeline"])     # the demo lead noted its step
    # approve -> sent -> the case now waits for Dana
    J(c.post(f"/api/actions/{pend[0]['id']}/decide", json={"status": "approved"}))
    case = J(c.get(f"/api/cases/{case['id']}"))
    assert case["state"] == "waiting_reply" and case["lane"] == "customer"
    person = J(c.get(f"/api/records/{case['subject']['id']}"))
    assert any(t["kind"] == "sent" for t in person["timeline"]) and person["props"]["phone"] == "+447700900321"
    # she texts back -> same case, Atlas answers it (no second lead/case)
    token = J(c.get("/api/connectors"))["hook_url"].rsplit("/", 1)[1]
    n_cases = len(J(c.get("/api/cases"))["cases"])
    r2 = c.post(f"/hook/{token}/sms", data={"From": "+447700900321", "Body": "Great - is Thursday ok?", "ProfileName": "Dana"})
    assert r2.status_code == 200 and r2.headers["X-Atlas-Run"]
    assert _wait_idle(c)
    assert len(J(c.get("/api/cases"))["cases"]) == n_cases
    case = J(c.get(f"/api/cases/{case['id']}"))
    assert case["state"] == "replied" and any(t["kind"] == "reply" and "Thursday" in t["text"] for t in case["timeline"])
    run = J(c.get(f"/api/runs/{r2.headers['X-Atlas-Run']}"))
    assert "Thursday" in run["task"] and "kind=sms" in run["task"]
    # the owner rejects the new draft -> Atlas redrafts on its own
    pend = [a for a in case["actions"] if a["status"] == "pending"]
    J(c.post(f"/api/actions/{pend[0]['id']}/decide", json={"status": "rejected", "note": "offer Friday instead"}))
    case = _wait(lambda: (lambda x: x if x["state"] == "revise" else None)(J(c.get(f"/api/cases/{case['id']}"))))
    assert case and case["rejections"] == 1
    assert _wait_idle(c)
    # owner moves and closes it by hand; records + types API
    J(c.post(f"/api/cases/{case['id']}/move", json={"state": "closed", "outcome": "went elsewhere"}))
    case = J(c.get(f"/api/cases/{case['id']}"))
    assert case["closed_at"] and case["outcome"] == "went elsewhere" and case["lane"] == "closed"
    types = {t["type"]: t["count"] for t in J(c.get("/api/records/types"))["types"]}
    assert types["person"] >= 1
    found = J(c.get("/api/records?q=dana"))
    assert found and found[0]["cases"] >= 1
    assert J(c.get("/api/stats"))["cases"]["open"] >= 0
    # owner-made case without a run, then a note and a snooze
    oc = J(c.post("/api/cases", json={"type": "task", "title": "Order more aligners", "run": False}))["case"]
    assert oc["state"] == "todo" and oc["lane"] == "atlas"
    J(c.post(f"/api/cases/{oc['id']}/note", json={"text": "supplier is Align UK"}))
    oc = J(c.patch(f"/api/cases/{oc['id']}", json={"snooze_hours": 2, "priority": "low"}))
    assert oc["priority"] == "low" and oc["lane"] == "scheduled" and 7000 < oc["wake_in_s"] <= 7200
    assert c.get("/api/cases/999999").status_code == 404
    A._RL_AUTH._hits.clear()
