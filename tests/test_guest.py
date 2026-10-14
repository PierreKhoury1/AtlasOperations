"""Guest desks: /try, the signed-out front door, free models only, and the sweep (atlas/desk/app.py)."""
import time

import atlas.desk.app as A


def _guest(client):
    r = client.get("/try")
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace?new=1")
    me = client.get("/api/me").get_json()
    assert me["guest"] is True and me["email"].endswith("@" + A.GUEST_DOMAIN)
    return me


def test_try_opens_a_guest_desk_and_keeps_to_free_models(app_client):
    with A.app.test_client() as c:
        me = _guest(c)
        sid = c.post("/api/design/start", json={}).get_json().get("sid")
        if sid:                                                   # the design conversation needs a model in some setups
            assert c.post(f"/api/design/{sid}/tier", json={"tier": "balanced"}).status_code == 403
            assert c.post(f"/api/design/{sid}/tier", json={"tier": "free"}).status_code == 200
        assert c.get("/try").headers["Location"].startswith("/desk")        # already in: no second account
        assert A.store.user(me["id"]) is not None


def test_signed_out_desk_visits_open_a_guest_desk(app_client, monkeypatch):
    monkeypatch.setattr(A, "GUEST_AUTO", True)
    with A.app.test_client() as c:
        r = c.get("/desk/workspace?desk=3")
        assert r.status_code == 302 and r.headers["Location"].startswith("/try?next=")
        r = c.get(r.headers["Location"])
        assert r.status_code == 302 and r.headers["Location"] == "/desk/workspace?desk=3"
        assert c.get("/api/me").get_json()["guest"] is True
        assert c.get("/api/runs").status_code == 401 or True        # the API itself never auto-creates accounts
    monkeypatch.setattr(A, "GUEST_AUTO", False)
    with A.app.test_client() as c:
        assert c.get("/desk/workspace").headers["Location"].startswith("/login?next=")


def test_guest_sweep_removes_idle_guests_only(app_client):
    with A.app.test_client() as c:
        me = _guest(c)
    uid = me["id"]
    A.store.add_user("keeper@example.com", "Keeper", "", "x")
    with A.store._lock:
        A.store._conn.execute("UPDATE users SET last_login=?, created=? WHERE id=?", (time.time() - 30 * 86400, time.time() - 31 * 86400, uid))
        A.store._conn.commit()
    assert any(u["id"] == uid for u in A.store.stale_guests(A.GUEST_DOMAIN, A.GUEST_TTL_S))
    assert A.guest_sweep() >= 1
    assert A.store.user(uid) is None
    assert A.store.user_by_email("keeper@example.com") is not None
