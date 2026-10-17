"""Teammates on a desk: invites, roles (manager / member / viewer), the invite link, a viewer that changes nothing."""
import re

import pytest

import atlas.desk.app as A
import atlas.desk.auth as AUTH


@pytest.fixture()
def owner(app_client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    monkeypatch.setattr(A, "OPEN", False)
    A._RL_AUTH._hits.clear()
    c = A.app.test_client()
    if c.post("/signup", json={"name": "Olive Owner", "company": "Hollis & Grey", "email": "owner@example.com", "password": "password1"}).status_code != 200:
        c.post("/login", json={"email": "owner@example.com", "password": "password1"})          # the store outlives one test
    did = c.post("/api/desks", json={"name": "Lettings desk", "template": "lettings"}).get_json()["id"]
    c.did = did
    return c


def _user(email, name="Tam"):
    c = A.app.test_client()
    if c.post("/signup", json={"name": name, "company": "Co", "email": email, "password": "password1"}).status_code != 200:
        c.post("/login", json={"email": email, "password": "password1"})
    return c


def _link(sent):
    return re.search(r"(https?://\S+/invite/\S+)", sent[-1][2]).group(1)


def test_invite_mailed_link_joins_and_lists(owner, monkeypatch):
    sent = []
    monkeypatch.setattr(AUTH, "mail_ready", lambda: True)
    monkeypatch.setattr(AUTH, "send_mail", lambda to, subject, body: sent.append((to, subject, body)) or "ok")
    did = owner.did
    r = owner.post(f"/api/desks/{did}/members", json={"email": "tam@example.com", "role": "member"})
    assert r.status_code == 200 and r.get_json()["mailed"] is True and r.get_json()["link"] is None
    assert sent[-1][0] == "tam@example.com" and "Hollis" in sent[-1][1] or "Lettings desk" in sent[-1][1]
    link = _link(sent)
    path = link[link.index("/invite/"):]
    t = owner.get(f"/api/desks/{did}/members").get_json()
    assert t["role"] == "owner" and t["owner"]["email"] == "owner@example.com" and t["invites"][0]["email"] == "tam@example.com"
    # the link while signed out, no account yet: to signup with the address filled in
    anon = A.app.test_client()
    r = anon.get(path)
    assert r.status_code == 302 and r.headers["Location"].startswith("/signup?next=") and "tam%40example.com" in r.headers["Location"]
    html = anon.get(r.headers["Location"]).get_data(as_text=True)
    assert 'value="tam@example.com"' in html
    # signing up through the form with ?next lands on the invite, which joins the desk
    r = anon.post("/signup?next=" + path, data={"name": "Tam", "company": "Co", "email": "tam@example.com", "password": "password1",
                                                  "role": "Negotiator", "use_case": "Answer tenant emails every day"})
    assert r.status_code == 302 and r.headers["Location"] == path
    r = anon.get(path)
    assert r.status_code == 302 and r.headers["Location"] == "/desk"
    desks = anon.get("/api/desks").get_json()
    mine = next(d for d in desks["desks"] if d["id"] == did)
    assert mine["role"] == "member" and desks["current"] == did
    t = owner.get(f"/api/desks/{did}/members").get_json()
    assert [m["email"] for m in t["members"]] == ["tam@example.com"] and t["invites"] == []
    # a used link is dead
    assert anon.get(path).status_code == 410
    # the member works the desk (writes allowed) but cannot configure it or see the team
    assert anon.post("/api/jobs", json={"kind": "task", "name": "x", "task": "y", "every_min": 60}).status_code == 200
    assert anon.patch(f"/api/desks/{did}", json={"name": "Renamed"}).status_code == 404
    assert anon.get(f"/api/desks/{did}/members").status_code == 404
    # the owner changes the role, then removes them
    assert owner.patch(f"/api/desks/{did}/members/{t['members'][0]['user_id']}", json={"role": "manager"}).status_code == 200
    assert anon.patch(f"/api/desks/{did}", json={"name": "Renamed"}).status_code == 200
    assert owner.delete(f"/api/desks/{did}/members/{t['members'][0]['user_id']}").status_code == 200
    assert all(d["id"] != did for d in anon.get("/api/desks").get_json()["desks"])


def test_no_mail_returns_the_link_and_wrong_account_is_refused(owner, monkeypatch):
    monkeypatch.setattr(AUTH, "mail_ready", lambda: False)
    did = owner.did
    r = owner.post(f"/api/desks/{did}/members", json={"email": "vee@example.com", "role": "viewer"}).get_json()
    assert r["mailed"] is False and "/invite/" in r["link"]
    path = r["link"][r["link"].index("/invite/"):]
    other = _user("someone-else@example.com")
    r = other.get(path)
    assert r.status_code == 403 and "vee@example.com" in r.get_data(as_text=True)
    vee = _user("vee@example.com", "Vee")
    assert vee.get(path).status_code == 302
    # a viewer reads everything and changes nothing
    assert vee.get("/api/jobs").status_code == 200
    r = vee.post("/api/jobs", json={"kind": "task", "name": "x", "task": "y", "every_min": 60})
    assert r.status_code == 403 and r.get_json()["error"] == "viewer"
    assert vee.post(f"/api/desks/{did}/select", json={}).status_code == 200          # picking a desk is fine
    assert vee.patch("/api/me", json={"two_factor": True}).status_code in (200, 400, 503)   # their own account too
    # bad invites
    assert owner.post(f"/api/desks/{did}/members", json={"email": "nope", "role": "member"}).status_code == 400
    assert owner.post(f"/api/desks/{did}/members", json={"email": "owner@example.com", "role": "member"}).status_code == 400
    assert owner.post(f"/api/desks/{did}/members", json={"email": "x@example.com", "role": "god"}).status_code == 400
    # revoke a pending invite
    r = owner.post(f"/api/desks/{did}/members", json={"email": "late@example.com", "role": "member"}).get_json()
    assert owner.delete(f"/api/desks/{did}/invites/{r['invite']['id']}").get_json()["invites"] == []
    # the owner cannot be removed; a member can leave
    assert owner.delete(f"/api/desks/{did}/members/{A.store.user_by_email('owner@example.com')['id']}").status_code == 400
    vid = A.store.user_by_email("vee@example.com")["id"]
    r = vee.delete(f"/api/desks/{did}/members/{vid}")                               # leaving is the one change a viewer may make
    assert r.status_code == 200 and r.get_json()["left"] is True
    assert all(d["id"] != did for d in vee.get("/api/desks").get_json()["desks"])
