"""The operator's admin page (/desk/admin, /api/admin/accounts): every account's use and limits, overrides set from the page."""
import pytest

import atlas.desk.app as A
import atlas.usage as U


@pytest.fixture()
def client(app_client):
    A._RL_AUTH._hits.clear()
    with A.app.test_client() as c:
        yield c


def _signup(client, email, name="Pat"):
    r = client.post("/signup", json={"name": name, "company": "Co", "email": email, "password": "password1"})
    assert r.status_code == 200


def _as_operator(monkeypatch, email):
    monkeypatch.setattr(U.CFG, "operator_emails", frozenset({email}))
    monkeypatch.setattr(A, "OPEN", False)


def test_admin_is_operator_only(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    monkeypatch.setattr(A, "OPEN", False)
    monkeypatch.setattr(U.CFG, "operator_emails", frozenset())
    assert client.get("/desk/admin").status_code == 302                                    # signed out
    _signup(client, "plain@example.com")
    assert client.get("/desk/admin").status_code == 403
    r = client.get("/api/admin/accounts")
    assert r.status_code == 403 and r.get_json()["error"] == "operator_only"
    assert client.put("/api/admin/accounts/1/limits", json={"limit_id": "paid_usd_month", "value": 5}).status_code == 403
    assert client.get("/api/me").get_json()["operator"] is False


def test_operator_sees_everyone_and_sets_limits(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    _signup(client, "other@example.com", name="Other Person")
    client.get("/logout")
    _signup(client, "boss@example.com", name="Boss")
    _as_operator(monkeypatch, "boss@example.com")
    assert client.get("/api/me").get_json()["operator"] is True
    assert client.get("/desk/admin").status_code == 200
    d = client.get("/api/admin/accounts").get_json()
    emails = {a["email"]: a for a in d["accounts"]}
    assert "other@example.com" in emails and "boss@example.com" in emails
    other = emails["other@example.com"]
    assert {l["id"] for l in other["limits"]} == set(U.OWNER_LIMITS)
    assert all(l["override"] is None for l in other["limits"])
    assert set(other["today"]) == {"ai_requests_day", "ai_requests_auto_day", "paid_usd_month", "refused"}
    # set a paid cap
    r = client.put(f"/api/admin/accounts/{other['id']}/limits", json={"limit_id": "paid_usd_month", "value": "12.5", "note": "trial"})
    assert r.status_code == 200
    lim = next(l for l in r.get_json()["account"]["limits"] if l["id"] == "paid_usd_month")
    assert lim["value"] == 12.5 and lim["override"]["note"] == "trial" and lim["override"]["set_by"] == "web:boss@example.com"
    assert U.limit_value(other["id"], "paid_usd_month") == 12.5
    # none = no limit, default = drop the override
    r = client.put(f"/api/admin/accounts/{other['id']}/limits", json={"limit_id": "ai_requests_day", "value": "none"})
    assert r.status_code == 200 and next(l for l in r.get_json()["account"]["limits"] if l["id"] == "ai_requests_day")["value"] is None
    r = client.put(f"/api/admin/accounts/{other['id']}/limits", json={"limit_id": "ai_requests_day", "value": "default"})
    assert r.status_code == 200 and next(l for l in r.get_json()["account"]["limits"] if l["id"] == "ai_requests_day")["override"] is None
    # bad input
    assert client.put(f"/api/admin/accounts/{other['id']}/limits", json={"limit_id": "pool_free_day", "value": 1}).status_code == 400
    assert client.put(f"/api/admin/accounts/{other['id']}/limits", json={"limit_id": "runs_at_once", "value": "2.5"}).status_code == 400
    assert client.put(f"/api/admin/accounts/{other['id']}/limits", json={"limit_id": "runs_at_once", "value": "-1"}).status_code == 400
    assert client.put("/api/admin/accounts/999999/limits", json={"limit_id": "runs_at_once", "value": "2"}).status_code == 404
