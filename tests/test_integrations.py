"""Connector tests: every provider against an httpx mock transport, plus the portal flow
approve → real send → CRM mirror → Slack ping, and inbound WhatsApp / Twilio hooks."""
import json
import time

import httpx
import pytest

from hermes import integrations as I


def J(resp):
    return json.loads(resp.data)


class Fake:
    """Routes provider calls to canned responses and records what was sent."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.hubspot_hits: list[dict] = []          # search results to return
        self.pd_hits: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url, m = str(req.url), req.method
        body = {}
        if req.content:
            try:
                body = json.loads(req.content)
            except Exception:
                body = {"_form": req.content.decode()}
        self.calls.append((m, url, body))
        if "api.resend.com/emails" in url:
            return httpx.Response(200, json={"id": "re_123"})
        if "api.resend.com/domains" in url:
            return httpx.Response(200, json={"data": [{"name": "hermesops.co"}]})
        if "graph.facebook.com" in url and url.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})
        if "graph.facebook.com" in url:
            return httpx.Response(200, json={"display_phone_number": "+44 20 7946 0000", "verified_name": "Acme", "quality_rating": "GREEN"})
        if "api.twilio.com" in url and url.endswith("Messages.json"):
            return httpx.Response(201, json={"sid": "SM123"})
        if "api.twilio.com" in url:
            return httpx.Response(200, json={"friendly_name": "Acme", "status": "active"})
        if "api.hubapi.com/crm/v3/objects/contacts/search" in url:
            return httpx.Response(200, json={"results": self.hubspot_hits})
        if "api.hubapi.com/crm/v3/objects/contacts/" in url:      # PATCH
            return httpx.Response(200, json={"id": url.rsplit("/", 1)[1]})
        if "api.hubapi.com/crm/v3/objects/contacts" in url:
            return httpx.Response(201 if m == "POST" else 200, json={"id": "901", "results": []})
        if "api.hubapi.com/crm/v3/objects/notes" in url:
            return httpx.Response(201, json={"id": "n1"})
        if "pipedrive.com/api/v1/persons/search" in url:
            return httpx.Response(200, json={"data": {"items": self.pd_hits}})
        if "pipedrive.com/api/v1/persons" in url:
            return httpx.Response(201, json={"data": {"id": 77}})
        if "pipedrive.com/api/v1/notes" in url:
            return httpx.Response(201, json={"data": {"id": 5}})
        if "pipedrive.com/api/v1/users/me" in url:
            return httpx.Response(200, json={"data": {"name": "Pierre"}})
        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(200, json={"access_token": "ya29.x"})
        if "calendar/v3/freeBusy" in url:
            return httpx.Response(200, json={"calendars": {"primary": {"busy": [{"start": "2030-01-07T10:00:00+00:00", "end": "2030-01-07T11:00:00+00:00"}]}}})
        if "calendar/v3/calendars/primary/events" in url:
            return httpx.Response(200, json={"id": "evt1", "htmlLink": "https://cal/evt1"})
        if "calendar/v3/users/me/calendarList" in url:
            return httpx.Response(200, json={"summary": "Clinic"})
        if "hooks.slack.com" in url:
            return httpx.Response(200, text="ok")
        if "fail.example" in url:
            return httpx.Response(401, json={"error": "bad token"})
        return httpx.Response(404, json={"error": "unrouted " + url})

    def sent(self, frag):
        return [c for c in self.calls if frag in c[1]]


@pytest.fixture()
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(I, "_TRANSPORT", httpx.MockTransport(f))
    return f


# ----------------------------------------------------------------------------- unit: providers
def test_phone_and_mask():
    assert I._digits("07700 900123") == "447700900123"
    assert I._digits("+44 (0)7700 900123") != ""            # tolerant
    assert I._digits("0044 20 7946 0000") == "442079460000"
    m = I.mask({"access_token": "EAAG1234567890", "webhook_url": "https://hooks.slack.com/services/T/B/xyz9", "from_email": "a@b.co"})
    assert m["access_token"].startswith("••••") and m["webhook_url"].startswith("••••") and m["from_email"] == "a@b.co"
    assert I.merge_secrets({"api_key": "real"}, {"api_key": "••••real", "from_email": "x@y.z"}) == {"api_key": "real", "from_email": "x@y.z"}


def test_resend_send_and_test(fake):
    out = I.send_resend({"api_key": "re_k", "from_email": "desk@hermesops.co", "from_name": "Hermes"}, "lead@x.com", "Hi", "Body")
    assert "re_123" in out
    m, url, body = fake.sent("emails")[0]
    assert body["from"] == "Hermes <desk@hermesops.co>" and body["to"] == ["lead@x.com"] and body["text"] == "Body"
    assert "hermesops.co" in I.test_resend({"api_key": "re_k"})
    with pytest.raises(RuntimeError):
        I.send_resend({"api_key": ""}, "a@b.c", "s", "b")


def test_whatsapp_send_test_parse(fake):
    cfg = {"phone_number_id": "111", "access_token": "EAAG", "verify_token": "v"}
    assert "wamid.ABC" in I.send_whatsapp(cfg, "07700 900123", "hello")
    body = fake.sent("/111/messages")[0][2]
    assert body["to"] == "447700900123" and body["text"]["body"] == "hello" and body["messaging_product"] == "whatsapp"
    assert "Acme" in I.test_whatsapp(cfg)
    payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "447700900123", "profile": {"name": "Hannah"}}],
                                                  "messages": [{"from": "447700900123", "id": "wamid.1", "type": "text", "text": {"body": "Is Thursday free?"}},
                                                               {"from": "447700900123", "id": "wamid.2", "type": "image"}]}}]}]}
    msgs = I.parse_whatsapp_webhook(payload)
    assert msgs[0] == {"from": "447700900123", "name": "Hannah", "text": "Is Thursday free?", "id": "wamid.1"}
    assert msgs[1]["text"] == "[image message]"


def test_twilio_sms_and_whatsapp(fake):
    cfg = {"account_sid": "AC1", "auth_token": "tok", "from_number": "+447000000000", "whatsapp_from": "+14155238886"}
    assert "SM123" in I.send_twilio(cfg, "07700 900123", "hi", "sms")
    form = fake.sent("Messages.json")[0][2]["_form"]
    assert "To=%2B447700900123" in form and "From=%2B447000000000" in form
    I.send_twilio(cfg, "+447700900123", "hi", "whatsapp")
    form = fake.sent("Messages.json")[1][2]["_form"]
    assert "To=whatsapp%3A%2B447700900123" in form and "From=whatsapp%3A%2B14155238886" in form
    assert "Acme" in I.test_twilio(cfg)
    with pytest.raises(RuntimeError):
        I.send_twilio({"account_sid": "AC1", "auth_token": "t"}, "1", "x", "whatsapp")


def test_hubspot_create_then_update(fake):
    cfg = {"access_token": "pat"}
    contact = {"email": "hannah@x.com", "name": "Hannah Weiss", "company": "Weiss Ltd", "phone": "0770", "stage": "Contacted", "notes": "warm", "next_action": "call"}
    assert I.hubspot_upsert(cfg, contact) == "HubSpot: created contact 901"
    create = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/objects/contacts")][0][2]["properties"]
    assert create == {"email": "hannah@x.com", "firstname": "Hannah", "lastname": "Weiss", "company": "Weiss Ltd", "phone": "0770", "hs_lead_status": "ATTEMPTED_TO_CONTACT"}
    note = fake.sent("/objects/notes")[0][2]
    assert note["properties"]["hs_note_body"] == "[Hermes] warm · next: call" and note["associations"][0]["to"]["id"] == "901"
    fake.hubspot_hits = [{"id": "555"}]
    assert I.hubspot_upsert(cfg, contact) == "HubSpot: updated contact 555"
    assert [c for c in fake.calls if c[0] == "PATCH"][0][1].endswith("/contacts/555")
    assert "OK" in I.test_hubspot(cfg)


def test_pipedrive_upsert(fake):
    cfg = {"company_domain": "acme", "api_token": "pdt"}
    contact = {"email": "tom@x.com", "name": "Tom Okafor", "phone": "0771", "stage": "New", "company": "Okafor & Co", "notes": "n"}
    assert I.pipedrive_upsert(cfg, contact) == "Pipedrive: created person 77"
    post = [c for c in fake.calls if c[0] == "POST" and c[1].split("?")[0].endswith("/persons")][0]
    assert "api_token=pdt" in post[1] and post[2]["email"][0]["value"] == "tom@x.com"
    assert fake.sent("/notes")[0][2]["content"].startswith("[Hermes] stage=New · company=Okafor & Co · n")
    fake.pd_hits = [{"item": {"id": 12}}]
    assert I.pipedrive_upsert(cfg, contact) == "Pipedrive: updated person 12"
    assert "Pierre" in I.test_pipedrive(cfg)


def test_crm_sync_never_raises(fake):
    conns = [{"kind": "hubspot", "config": {"access_token": "pat"}}, {"kind": "pipedrive", "config": {"company_domain": "acme", "api_token": "x"}},
             {"kind": "hubspot", "config": {}}, {"kind": "slack", "config": {"webhook_url": "https://hooks.slack.com/a"}}]
    out = I.crm_sync(conns, {"email": "a@b.co", "name": "A B"})
    assert out[0].startswith("HubSpot: created") and out[1].startswith("Pipedrive: created") and "sync failed" in out[2] and len(out) == 3


def test_gcal_slots_and_booking(fake):
    import datetime as dt
    from zoneinfo import ZoneInfo
    cfg = {"client_id": "id", "client_secret": "sec", "refresh_token": "rt", "timezone": "Europe/London", "day_start": "09:00", "day_end": "12:00"}
    now = dt.datetime(2030, 1, 7, 8, 0, tzinfo=ZoneInfo("Europe/London"))        # a Monday
    slots = I.gcal_free_slots(cfg, "2030-01-07", "2030-01-07", 30, now=now)
    assert slots == ["2030-01-07T09:00:00+00:00", "2030-01-07T09:30:00+00:00", "2030-01-07T11:00:00+00:00", "2030-01-07T11:30:00+00:00"]
    fb = fake.sent("freeBusy")[0][2]
    assert fb["items"] == [{"id": "primary"}] and fb["timeMin"].startswith("2030-01-07T08:00")
    out = I.gcal_create_event(cfg, "Consultation", "2030-01-07T11:00", "", "hannah@x.com", "first visit")
    assert "evt1" in out and "11:00–11:30" in out
    ev = fake.sent("/events")[0]
    assert "sendUpdates=all" in ev[1] and ev[2]["attendees"] == [{"email": "hannah@x.com"}] and ev[2]["end"]["dateTime"].startswith("2030-01-07T11:30")
    assert "Clinic" in I.test_gcal(cfg)
    # weekend range yields nothing
    assert I.gcal_free_slots(cfg, "2030-01-05", "2030-01-06", 30, now=now) == []


def test_slack_and_notify(fake):
    assert I.slack_notify({"webhook_url": "https://hooks.slack.com/services/T/B/X", "channel": "#desk"}, "hi") == "posted to Slack"
    assert fake.sent("hooks.slack.com")[0][2] == {"text": "hi", "channel": "#desk"}
    I.notify([{"kind": "slack", "config": {"webhook_url": "https://fail.example/hook"}}, {"kind": "smtp", "config": {}}], "x")   # swallowed
    assert "test message" in I.test_slack({"webhook_url": "https://hooks.slack.com/services/T/B/X"})


def test_routing_and_deliver(fake):
    conns = [{"kind": "slack", "config": {}}, {"kind": "twilio", "config": {"account_sid": "AC1", "auth_token": "t", "from_number": "+4470"}},
             {"kind": "resend", "config": {"api_key": "k", "from_email": "d@h.co"}}, {"kind": "smtp", "config": {"host": "h", "from_email": "s@h.co"}}]
    assert I.outbound_connector(conns, "email")["kind"] == "smtp"            # smtp preferred over resend
    assert I.outbound_connector(conns, "sms")["kind"] == "twilio"
    assert I.outbound_connector(conns, "whatsapp") is None                    # twilio without whatsapp_from can't carry it
    assert I.outbound_connector(conns, "booking") is None
    assert "re_123" in I.deliver(conns[2], "email", "a@b.co", "S", "B")
    assert "SM123" in I.deliver(conns[1], "sms", "07700 900123", "", "B")
    gcal = {"kind": "gcal", "config": {"client_id": "i", "client_secret": "s", "refresh_token": "r"}}
    assert "evt1" in I.deliver(gcal, "booking", "hannah@x.com", "Consultation", json.dumps({"title": "Consultation", "start": "2030-01-07T11:00", "end": "2030-01-07T11:30"}))
    assert "test message" in I.test_connector("slack", {"webhook_url": "https://hooks.slack.com/x"})
    with pytest.raises(RuntimeError):
        I._http("GET", "https://fail.example/x")


# ----------------------------------------------------------------------------- portal flow
def _wait_idle(c, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not J(c.get("/api/live"))["runs"]:
            return True
        time.sleep(0.3)
    return False


def _login(c):
    if c.post("/login", json={"email": "int@example.com", "password": "password1"}).status_code != 200:
        assert J(c.post("/signup", json={"name": "Int", "email": "int@example.com", "password": "password1"}))["ok"]
    if J(c.get("/api/config")).get("needs_desk"):
        J(c.post("/api/desks", json={"name": "Integration Clinic", "template": "sales_desk", "tier": "free", "sender_name": "Maya"}))


def test_portal_real_send_crm_mirror_slack(app_client, fake):
    c = app_client
    _login(c)
    R = J(c.get("/api/connectors"))
    assert set(R["kinds"]) >= {"resend", "whatsapp", "twilio", "hubspot", "pipedrive", "gcal", "slack"}
    assert R["channels"] == {"email": False, "whatsapp": False, "sms": False, "booking": False}
    assert R["whatsapp_hook_url"].endswith("/whatsapp") and R["sms_hook_url"].endswith("/sms")
    for kind, name, cfg in [("resend", "resend", {"api_key": "re_k", "from_email": "desk@hermesops.co", "from_name": "Maya"}),
                            ("hubspot", "hubspot", {"access_token": "pat"}),
                            ("slack", "slack", {"webhook_url": "https://hooks.slack.com/services/T/B/X"})]:
        r = J(c.post("/api/connectors", json={"kind": kind, "name": name, "config": cfg}))
        assert r["id"], r
        t = J(c.post(f"/api/connectors/{r['id']}/test", json={}))
        assert t["ok"], t
    assert J(c.get("/api/connectors"))["channels"]["email"] is True
    # a lead through the webhook → demo run queues an email → approving it really sends via Resend
    token = R["hook_url"].rsplit("/", 1)[1]
    J(c.post(f"/hook/{token}", json={"name": "Hannah Weiss", "email": "hannah@x.com", "notes": "valuation please"}))
    assert _wait_idle(c)
    pend = [a for a in J(c.get("/api/actions?status=pending")) if a["to"] == "hannah@x.com"]
    assert pend, "demo desk should have queued an email for the lead"
    slack_before = len(fake.sent("hooks.slack.com"))
    assert slack_before >= 1                                          # approval ping from the orchestrator
    row = J(c.post(f"/api/actions/{pend[0]['id']}/decide", json={"status": "approved", "note": "go"}))
    assert row["status"] == "sent" and "sent via Resend as desk@hermesops.co" in row["note"], row["note"]
    sent = fake.sent("api.resend.com/emails")[-1][2]
    assert sent["to"] == ["hannah@x.com"] and sent["from"] == "Maya <desk@hermesops.co>"
    assert len(fake.sent("hooks.slack.com")) == slack_before + 1      # "Sent" ping
    assert fake.sent("/objects/contacts/search"), "contact should be mirrored to HubSpot on send"
    detail = J(c.get(f"/api/runs/{row['run_id']}"))
    assert any(e["kind"] == "tool" and "crm sync → HubSpot" in e["text"] for e in detail["events"])
    contact = next(x for x in J(c.get("/api/contacts")) if x["email"] == "hannah@x.com")
    assert contact["stage"] == "Contacted"


def test_inbound_whatsapp_and_sms_hooks(app_client, fake):
    c = app_client
    _login(c)
    R = J(c.get("/api/connectors"))
    token = R["hook_url"].rsplit("/", 1)[1]
    wa = J(c.post("/api/connectors", json={"kind": "whatsapp", "name": "wa", "config": {"phone_number_id": "111", "access_token": "EAAG", "verify_token": "shh"}}))
    assert wa["id"]
    # Meta verification handshake
    assert c.get(f"/hook/{token}/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=123").status_code == 403
    ok = c.get(f"/hook/{token}/whatsapp?hub.mode=subscribe&hub.verify_token=shh&hub.challenge=123")
    assert ok.status_code == 200 and ok.data == b"123"
    # inbound message → lead + run
    payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "447700900555", "profile": {"name": "Priya Raman"}}],
                                                  "messages": [{"from": "447700900555", "id": "wamid.9", "type": "text", "text": {"body": "Do you have anything Thursday?"}}]}}]}]}
    r = J(c.post(f"/hook/{token}/whatsapp", json=payload))
    assert r["ok"] and r["messages"] == 1 and len(r["runs"]) == 1
    lead = next(l for l in J(c.get("/api/leads")) if l["phone"] == "+447700900555")
    assert lead["name"] == "Priya Raman" and lead["source"] == "whatsapp" and "Thursday" in lead["notes"]
    assert _wait_idle(c)
    task = J(c.get(f"/api/runs/{r['runs'][0]}"))["task"]
    assert "kind=whatsapp" in task and "+447700900555" in task
    # Twilio SMS inbound → TwiML + lead
    r2 = c.post(f"/hook/{token}/sms", data={"From": "+447700900777", "Body": "Call me back", "ProfileName": "Tom"})
    assert r2.status_code == 200 and b"<Response></Response>" in r2.data and r2.headers["X-Hermes-Run"]
    lead2 = next(l for l in J(c.get("/api/leads")) if l["phone"] == "+447700900777")
    assert lead2["source"] == "sms" and lead2["name"] == "Tom"
    assert c.post(f"/hook/{token}/sms", data={"Body": "no sender"}).status_code == 400
    assert c.post("/hook/nope/sms", data={"From": "+1"}).status_code == 404
    assert _wait_idle(c)
    J(c.delete(f"/api/connectors/{wa['id']}"))
