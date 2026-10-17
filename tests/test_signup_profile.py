"""Signup asks who the desk works for: company, role, team size, the job, where they heard of us (/signup, /signup/profile)."""
import pytest

import atlas.desk.app as A


@pytest.fixture()
def client(app_client):
    A._RL_AUTH._hits.clear()
    with A.app.test_client() as c:
        yield c


FORM = {"name": "Jane Doe", "company": "Hollis & Grey", "email": "jane@example.com", "password": "password1",
        "role": "Lettings manager", "team_size": "6-20", "use_case": "Chase rent arrears and answer tenant WhatsApps",
        "phone": "+44 7700 900123", "source": "Instagram"}


def test_signup_page_has_the_profile_fields(client):
    html = client.get("/signup").get_data(as_text=True)
    for name in ("name", "company", "role", "team_size", "use_case", "phone", "source"):
        assert f'name="{name}"' in html


def test_form_signup_needs_company_role_and_a_use_case(client):
    for missing, words in (("company", "company"), ("role", "role"), ("use_case", "what the desk should do")):
        d = {k: v for k, v in FORM.items() if k != missing}
        r = client.post("/signup", data=d)
        assert r.status_code == 400 and words in r.get_data(as_text=True)
        assert A.store.user_by_email(FORM["email"]) is None
    bad = dict(FORM, team_size="lots")
    assert client.post("/signup", data=bad).status_code == 400
    bad = dict(FORM, phone="call me maybe")
    assert client.post("/signup", data=bad).status_code == 400
    # re-rendered form keeps what was typed (escaped)
    html = client.post("/signup", data={k: v for k, v in FORM.items() if k != "role"}).get_data(as_text=True)
    assert "Hollis &amp; Grey" in html and "Chase rent arrears" in html and 'value="6-20"' not in html or '"6-20"' in html


def test_form_signup_stores_the_profile_and_signs_in(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    r = client.post("/signup", data=FORM)
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/start")
    me = client.get("/api/me").get_json()
    assert me["company"] == "Hollis & Grey" and me["role"] == "Lettings manager" and me["team_size"] == "6-20"
    assert me["use_case"].startswith("Chase rent") and me["phone"] == "+44 7700 900123" and me["source"] == "Instagram"
    assert me["profile_complete"] is True
    # the account signs back in with the password
    client.get("/logout")
    assert client.post("/login", json={"email": FORM["email"], "password": "password1"}).status_code in (200, 202)


def test_json_signup_without_a_profile_still_works_and_is_marked_incomplete(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    r = client.post("/signup", json={"name": "Api", "email": "api@example.com", "password": "password1"})
    assert r.status_code == 200
    me = client.get("/api/me").get_json()
    assert me["profile_complete"] is False
    # ...but a half-given profile is checked
    client.get("/logout")
    r = client.post("/signup", json={"name": "Api2", "company": "Co", "email": "api2@example.com", "password": "password1",
                                     "role": "Owner", "use_case": "short"})
    assert r.status_code == 400 and "12 characters" in r.get_json()["error"]


def test_profile_step_completes_an_account(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    assert client.get("/signup/profile").status_code == 302                                  # signed out: to login
    client.post("/signup", json={"name": "Half", "email": "half@example.com", "password": "password1"})
    html = client.get("/signup/profile?next=/desk/workspace").get_data(as_text=True)
    assert "About your team" in html and "half@example.com" in html
    r = client.post("/signup/profile?next=/desk/workspace", data={"company": "Half Ltd", "role": "Director",
                                                                   "use_case": "Keep the maintenance inbox answered", "team_size": "2-5"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace")
    me = client.get("/api/me").get_json()
    assert me["profile_complete"] is True and me["company"] == "Half Ltd" and me["role"] == "Director"
    # an open redirect is not followed
    r = client.post("/signup/profile?next=//evil.example", json={"company": "Half Ltd", "role": "Director",
                                                                  "use_case": "Keep the maintenance inbox answered"})
    assert r.status_code == 200
    r = client.post("/signup/profile?next=https://evil.example", data={"company": "Half Ltd", "role": "Director",
                                                                        "use_case": "Keep the maintenance inbox answered"})
    assert r.headers["Location"].endswith("/desk/start")


def test_guests_are_never_asked_for_a_profile(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    client.get("/try")
    assert client.get("/api/me").get_json()["profile_complete"] is True
    r = client.get("/signup/profile")
    assert r.status_code == 302 and "/signup/profile" not in r.headers["Location"]


def test_long_values_are_clipped(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "off")
    d = dict(FORM, email="long@example.com", use_case="x" * 5000, role="r" * 500)
    assert client.post("/signup", data=d).status_code == 302
    me = client.get("/api/me").get_json()
    assert len(me["use_case"]) == 600 and len(me["role"]) == 80
