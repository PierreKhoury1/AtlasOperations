"""One thread per customer: inbound hooks and approved sends land in it, the owner can reply directly, and a
customer's stage survives repeat traffic (the audit's phantom-contact and stage-reset bugs stay fixed)."""
import json

from atlas.desk import app as A


def J(r):
    assert r.status_code < 300, r.get_data(as_text=True)
    return json.loads(r.data)


def _desk(c, email):
    c.post("/signup", json={"name": "M", "company": "Msg Bistro", "email": email, "password": "password1"})
    c.post("/login", json={"email": email, "password": "password1"})
    did = J(c.post("/api/desks", json={"name": "Bistro", "template": "sales_desk", "tier": "free"}))["id"]
    return did, A.store.ensure_hook_token(did)


def test_inbound_sms_threads_and_keeps_the_stage(app_client):
    c = app_client
    did, tok = _desk(c, "msg1@example.com")
    c.post(f"/hook/{tok}/sms", data={"From": "+44 7700 900123", "Body": "Do you have a table tonight?", "ProfileName": "Dana"})
    J(c.post("/api/contacts", json={"contact": "Dana", "fields": {"stage": "Qualified"}}))
    c.post(f"/hook/{tok}/sms", data={"From": "+447700900123", "Body": "For four people", "ProfileName": "Dana"})
    th = J(c.get("/api/messages/threads"))["threads"]
    assert len(th) == 1 and th[0]["key"] == "+447700900123" and th[0]["count"] == 2 and th[0]["name"] == "Dana"
    msgs = J(c.get("/api/messages?contact=%2B44%207700%20900123"))["messages"]
    assert [m["dir"] for m in msgs] == ["in", "in"] and msgs[0]["channel"] == "sms"
    dana = [x for x in J(c.get("/api/contacts")) if x["name"] == "Dana"]
    assert len(dana) == 1 and dana[0]["stage"] == "Qualified"     # a second message neither duplicates nor resets her


def test_approved_send_logs_out_and_never_knocks_a_stage_back(app_client, monkeypatch):
    c = app_client
    did, tok = _desk(c, "msg2@example.com")
    ds = A.store.for_desk(did)
    ds.upsert_contact("buyer@example.com", {"email": "buyer@example.com", "stage": "Proposal", "name": "Buyer"})
    aid = ds.add_action("r-x", "closer", "email", "buyer@example.com", "Your quote", "Here it is.", "")
    J(c.post(f"/api/actions/{aid}/decide", json={"status": "approved", "subject": "Your quote", "body": "Here it is."}))
    msgs = J(c.get("/api/messages?contact=buyer@example.com"))["messages"]
    assert len(msgs) == 1 and msgs[0]["dir"] == "out" and msgs[0]["action_id"] == aid and msgs[0]["channel"] == "email"
    buyer = ds.contact_for("buyer@example.com")
    assert buyer["stage"] == "Proposal" and buyer["next_action"]           # stage kept, follow-up noted
    # a brand-new recipient is created once, as Contacted
    aid2 = ds.add_action("r-y", "closer", "whatsapp", "+15550001111", "", "On our way", "")
    J(c.post(f"/api/actions/{aid2}/decide", json={"status": "approved", "subject": "", "body": "On our way"}))
    nc = ds.contact_for("+15550001111")
    assert nc and nc["stage"] == "Contacted" and A.store.message_key(nc["phone"]) == "+15550001111"


def test_owner_reply_sends_through_the_connector(app_client, monkeypatch):
    c = app_client
    did, tok = _desk(c, "msg3@example.com")
    r = c.post("/api/messages/send", json={"channel": "email", "to": "x@example.com", "body": "hi"})
    assert r.status_code == 400 and "connector" in J2(r)["error"]
    A.store.add_connector(did, "smtp", "mail", {"host": "smtp.example.com", "user": "u", "password": "p", "from": "bistro@example.com"}, False)
    sent = {}
    monkeypatch.setattr(A.I, "deliver", lambda conn, kind, to, subject, body: sent.update(kind=kind, to=to, body=body) or "sent via test")
    out = J(c.post("/api/messages/send", json={"channel": "email", "to": "Guest@Example.com", "subject": "Re: table", "body": "See you at 8."}))
    assert out["ok"] and sent == {"kind": "email", "to": "Guest@Example.com", "body": "See you at 8."}
    th = J(c.get("/api/messages/threads"))
    assert th["channels"]["email"] is True and th["threads"][0]["key"] == "guest@example.com"
    assert th["threads"][0]["last"]["actor"] == "M"                        # sent in the owner's name
    # failure is recorded, not hidden
    monkeypatch.setattr(A.I, "deliver", lambda *a: (_ for _ in ()).throw(RuntimeError("SMTP down")))
    bad = c.post("/api/messages/send", json={"channel": "email", "to": "guest@example.com", "body": "again"})
    assert bad.status_code == 502 and "SMTP down" in J2(bad)["error"]
    msgs = J(c.get("/api/messages?contact=guest@example.com"))["messages"]
    assert [m["status"] for m in msgs] == ["sent", "failed"]


def test_backfill_builds_threads_from_history(app_client):
    c = app_client
    did, tok = _desk(c, "msg4@example.com")
    ds = A.store.for_desk(did)
    aid = ds.add_action("r-1", "closer", "email", "old@example.com", "Old quote", "Body", "")
    ds.decide_action(aid, "sent", by="owner")
    ds.add_lead("Old Customer", "", "old@example.com", "", "email", "Subject: Old quote\n\nPlease quote me")
    th = J(c.get("/api/messages/threads"))["threads"]
    assert len(th) == 1 and th[0]["count"] == 2 and {"email"} == set(th[0]["channels"])


def J2(r):
    return json.loads(r.data)


def test_daily_report_send_now_and_daily_at_jobs(app_client, monkeypatch):
    c = app_client
    did, tok = _desk(c, "msg5@example.com")
    # no channel configured yet
    r = c.post("/api/report/day/send", json={})
    assert r.status_code == 400 and "channel" in J2(r)["error"]
    J(c.patch(f"/api/desks/{did}", json={"notify": {"channel": "email", "to": "owner@example.com", "report": True, "report_time": "06:45"}}))
    out = J(c.post("/api/report/day/send", json={}))
    assert out["ok"] and out["status"] == "pending"                      # queued for approval (auto is off)
    pend = J(c.get("/api/actions?status=pending"))
    assert any("Camera report" in (a["subject"] or "") for a in pend)
    jobs = J(c.get("/api/jobs"))["jobs"]
    rep = next(j for j in jobs if j["kind"] == "daily_report")           # saving notify scheduled the morning report
    assert rep["every_min"] == 1440
    import time as _t
    assert _t.localtime(rep["next_run"])[3:5] == (6, 45)
    # a plain task automation daily at 08:15, then edited to 09:30
    j = J(c.post("/api/jobs", json={"kind": "task", "name": "Morning brief", "task": "Prepare the brief", "at": "08:15"}))
    assert j["every_min"] == 1440 and _t.localtime(j["next_run"])[3:5] == (8, 15)
    j2 = J(c.patch(f"/api/jobs/{j['id']}", json={"at": "09:30", "task": "Prepare the brief, shorter"}))
    assert _t.localtime(J(c.get("/api/jobs"))["jobs"][-1]["next_run"] if False else j2.get("next_run") or next(x for x in J(c.get("/api/jobs"))["jobs"] if x["id"] == j["id"])["next_run"])[3:5] == (9, 30)
    assert c.post("/api/jobs", json={"kind": "task", "name": "x", "task": "y", "at": "25:99"}).status_code == 400
