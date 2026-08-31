"""Portal API tests (Flask test client, demo mode, accounts on)."""
import json
import time


def J(resp):
    return json.loads(resp.data)


def test_auth_gating(app_client):
    app_client.post("/logout")                       # order-independent: another test may have signed in
    assert app_client.get("/api/leads").status_code == 401
    assert app_client.get("/desk").status_code == 302
    assert app_client.get("/api/health").status_code == 200
    assert app_client.get("/login").status_code == 200
    assert app_client.get("/signup").status_code == 200


def test_signup_login_desk_flow(app_client):
    c = app_client
    r = c.post("/signup", json={"name": "T <b>x</b>", "company": "Co", "email": "t@example.com", "password": "password1"})
    assert J(r)["ok"]
    assert c.post("/signup", json={"name": "T", "email": "t@example.com", "password": "password1"}).status_code == 400
    assert J(c.get("/api/config"))["needs_desk"] is True
    assert c.get("/api/leads").status_code == 409
    r = c.post("/api/desks", json={"name": "My Clinic", "template": "sales_desk", "tier": "free", "sender_name": "Dr X", "no_money_figures": True})
    desk = J(r); assert desk["id"] and desk["template"] == "sales_desk"
    cfg = J(c.get("/api/config"))
    assert cfg["business"]["sender_name"] == "Dr X" and cfg["business"]["policy"]["no_money_figures"] is True
    assert cfg["mode"] == "demo"
    # second desk + switching
    d2 = J(c.post("/api/desks", json={"name": "Other", "template": "consultancy"}))
    assert J(c.get("/api/desks"))["current"] == d2["id"]
    c.post(f"/api/desks/{desk['id']}/select")
    assert J(c.get("/api/desks"))["current"] == desk["id"]
    # log out / in
    c.get("/logout")
    assert c.get("/api/leads").status_code == 401
    assert c.post("/login", json={"email": "T@EXAMPLE.com", "password": "password1"}).status_code == 200
    assert c.post("/login", json={"email": "t@example.com", "password": "nope"}).status_code == 401
    me = J(c.get("/api/me")); assert me["name"] == "T <b>x</b>"


def _wait_idle(c, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not J(c.get("/api/live"))["runs"]:
            return True
        time.sleep(0.3)
    return False


def test_seed_runs_approvals_crm_report(app_client):
    c = app_client
    c.post("/login", json={"email": "t@example.com", "password": "password1"})
    ids = J(c.post("/api/demo/seed"))
    assert len(ids) == 3 and all(x["run_id"] for x in ids)
    assert _wait_idle(c), "demo runs did not finish"
    runs = J(c.get("/api/runs"))
    assert len(runs) >= 3 and all(r["status"] == "done" for r in runs[:3])
    detail = J(c.get(f"/api/runs/{runs[0]['id']}"))
    assert detail["events"] and detail["summary"]
    pend = J(c.get("/api/actions?status=pending"))
    assert pend, "seed must queue approvals"
    a = pend[0]
    r = J(c.post(f"/api/actions/{a['id']}/decide", json={"status": "approved", "note": "ok"}))
    assert r["status"] == "sent" and "simulated" in r["note"]
    contact = next(x for x in J(c.get("/api/contacts")) if x["email"] == a["to"])
    assert contact["stage"] == "Contacted"
    rej = pend[1]
    assert J(c.post(f"/api/actions/{rej['id']}/decide", json={"status": "rejected", "note": "tone"}))["status"] == "rejected"
    rep = J(c.get("/api/report"))
    assert rep["sent"] == 1 and rep["rejected"] == 1 and rep["approval_rate"] == 50
    audit = J(c.get("/api/audit")); assert any(e["kind"] == "sent" for e in audit)
    stats = J(c.get("/api/stats")); assert stats["leads"] == 3


def test_webhook_connectors_jobs_memory(app_client):
    c = app_client
    c.post("/login", json={"email": "t@example.com", "password": "password1"})
    hook = J(c.get("/api/connectors"))["hook_url"]
    token = hook.rsplit("/", 1)[1]
    r = J(c.post(f"/hook/{token}", json={"name": "Hook Lead", "email": "hook@example.com", "notes": "hello"}))
    assert r["ok"] and r["run_id"]
    assert c.post("/hook/wrong-token", json={"name": "x"}).status_code == 404
    # connectors CRUD + masking
    conn = J(c.post("/api/connectors", json={"kind": "http", "name": "api", "config": {"base_url": "https://example.com", "token": "supersecret"}}))
    assert conn["config"]["token"].startswith("••••") and conn["config"]["token"].endswith("cret")
    upd = J(c.patch(f"/api/connectors/{conn['id']}", json={"config": {"base_url": "https://example.org", "token": conn["config"]["token"]}}))
    assert upd["config"]["base_url"] == "https://example.org" and upd["config"]["token"].endswith("cret")   # secret preserved
    assert c.post("/api/connectors", json={"kind": "http", "name": "api", "config": {}}).status_code == 400
    # jobs
    j = J(c.post("/api/jobs", json={"kind": "task", "name": "t", "task": "say hi", "every_min": 0, "in_min": 30}))
    assert j["enabled"] == 1 and j["next_run"] > time.time() + 60
    assert J(c.patch(f"/api/jobs/{j['id']}", json={"enabled": False}))["enabled"] == 0
    assert J(c.delete(f"/api/jobs/{j['id']}"))["ok"]
    # memory
    J(c.post("/api/memory", json={"key": "pref", "value": "mornings"}))
    assert any(m["key"] == "pref" for m in J(c.get("/api/memory")))
    assert J(c.delete("/api/memory/pref"))["ok"]
    assert not any(m["key"] == "pref" for m in J(c.get("/api/memory")))
    assert _wait_idle(c)
    J(c.delete(f"/api/connectors/{conn['id']}"))


def test_other_user_cannot_see_my_desk(app_client):
    c = app_client
    c.post("/login", json={"email": "t@example.com", "password": "password1"})
    mine = J(c.get("/api/desks"))["current"]
    c.get("/logout")
    c.post("/signup", json={"name": "U2", "email": "u2@example.com", "password": "password2"})
    assert c.post(f"/api/desks/{mine}/select").status_code == 404
    assert J(c.get("/api/config"))["needs_desk"] is True
