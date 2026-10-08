"""Continue with Google + the mailed second factor (atlas/desk/auth.py and the /login, /login/verify, /auth/google routes)."""
import re

import pytest

import atlas.desk.app as A
import atlas.desk.auth as AUTH


@pytest.fixture()
def client(app_client):
    A._RL_AUTH._hits.clear()
    with A.app.test_client() as c:                 # own cookie jar per test
        yield c


@pytest.fixture()
def mailbox(monkeypatch):
    """2FA on, with the portal's mail captured instead of sent."""
    sent = []
    monkeypatch.setenv("TWO_FACTOR", "required")
    monkeypatch.setattr(AUTH, "send_mail", lambda to, subject, body: sent.append((to, subject, body)) or "ok")
    return sent


def _code(mailbox):
    return re.search(r"code is (\d{6})", mailbox[-1][2]).group(1)


def _signup(client, email, name="Pat"):
    r = client.post("/signup", json={"name": name, "company": "Co", "email": email, "password": "password1"})
    assert r.status_code == 200
    client.get("/logout")


def test_password_login_without_mail_configured_skips_the_code(client, monkeypatch):
    monkeypatch.delenv("TWO_FACTOR", raising=False)
    for k in ("ATLAS_SMTP_HOST", "ATLAS_RESEND_KEY", "ATLAS_MAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    _signup(client, "plain@example.com")
    r = client.post("/login", json={"email": "plain@example.com", "password": "password1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/me").get_json()["two_factor_available"] is False


def test_required_mode_fails_closed_when_mail_is_down(client, monkeypatch):
    monkeypatch.setenv("TWO_FACTOR", "required")
    monkeypatch.setattr(AUTH, "send_mail", lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down")))
    _signup(client, "closed@example.com")
    r = client.post("/login", json={"email": "closed@example.com", "password": "password1"})
    assert r.status_code == 503
    assert client.get("/api/me").get_json()["id"] is None


def test_code_flow_wrong_then_right_then_trusted_device(client, mailbox):
    _signup(client, "mfa@example.com")
    r = client.post("/login", json={"email": "mfa@example.com", "password": "password1"})
    assert r.status_code == 202 and r.get_json()["mfa"] is True
    assert client.get("/api/me").get_json()["id"] is None          # password alone is not a session
    assert mailbox[-1][0] == "mfa@example.com" and "Many thanks" in mailbox[-1][2]
    assert client.get("/login/verify").status_code == 200
    assert client.post("/login/verify", json={"code": "000000"}).status_code == 401
    good = _code(mailbox)
    r = client.post("/login/verify", json={"code": good, "trust": "1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/me").get_json()["email"] == "mfa@example.com"
    assert client.get_cookie("atlas_trust") is not None
    # a code never works twice
    client.get("/logout")
    assert client.post("/login/verify", json={"code": good}).status_code in (302, 401)
    # trusted browser: straight in next time, no mail
    n = len(mailbox)
    r = client.post("/login", json={"email": "mfa@example.com", "password": "password1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True and len(mailbox) == n


def test_five_wrong_codes_lock_the_challenge(client, mailbox):
    _signup(client, "lock@example.com")
    client.post("/login", json={"email": "lock@example.com", "password": "password1"})
    for _ in range(5):
        assert client.post("/login/verify", json={"code": "111111"}).status_code == 401
    good = _code(mailbox)
    r = client.post("/login/verify", json={"code": good})
    assert r.status_code == 401 and "Too many" in r.get_json()["error"]


def test_account_can_opt_out_of_the_code(client, mailbox):
    _signup(client, "optout@example.com")
    client.post("/login", json={"email": "optout@example.com", "password": "password1"})
    client.post("/login/verify", json={"code": _code(mailbox)})
    r = client.patch("/api/me", json={"two_factor": False})
    assert r.status_code == 200 and r.get_json()["two_factor"] is False
    client.get("/logout")
    n = len(mailbox)
    r = client.post("/login", json={"email": "optout@example.com", "password": "password1"})
    assert r.status_code == 200 and len(mailbox) == n


def test_google_button_hidden_until_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    html = client.get("/login").get_data(as_text=True)
    assert 'class="gbtn off"' in html and "{{" not in html
    assert client.get("/auth/google").status_code == 404
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s3cret")
    html = client.get("/signup").get_data(as_text=True)
    assert 'class="gbtn on"' in html
    r = client.get("/auth/google")
    assert r.status_code == 302 and r.headers["Location"].startswith(AUTH.GOOGLE_AUTH)
    assert "state=" in r.headers["Location"] and "redirect_uri=" in r.headers["Location"]


def test_google_callback_links_existing_email_and_creates_new_users(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    monkeypatch.setenv("TWO_FACTOR", "off")
    identities = {"code-A": {"sub": "g-111", "email": "linked@example.com", "name": "Linked Person"},
                  "code-B": {"sub": "g-222", "email": "fresh@example.com", "name": "Fresh Person"}}
    monkeypatch.setattr(AUTH, "google_exchange", lambda code, redirect: identities[code])
    _signup(client, "linked@example.com", name="Existing Name")
    # state mismatch is refused
    client.get("/auth/google")
    assert client.get("/auth/google/callback?state=wrong&code=code-A").status_code == 400
    # same verified email -> the existing account, now linked
    r = client.get("/auth/google")
    state = re.search(r"state=([^&]+)", r.headers["Location"]).group(1)
    r = client.get(f"/auth/google/callback?state={state}&code=code-A")
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace")
    me = client.get("/api/me").get_json()
    assert me["email"] == "linked@example.com" and me["name"] == "Existing Name" and me["google"] is True
    client.get("/logout")
    # unknown email -> a new password-less account; its password login is refused
    r = client.get("/auth/google")
    state = re.search(r"state=([^&]+)", r.headers["Location"]).group(1)
    assert client.get(f"/auth/google/callback?state={state}&code=code-B").status_code == 302
    me = client.get("/api/me").get_json()
    assert me["email"] == "fresh@example.com" and me["google"] is True
    client.get("/logout")
    assert client.post("/login", json={"email": "fresh@example.com", "password": ""}).status_code == 401
    # a used state cannot be replayed
    assert client.get(f"/auth/google/callback?state={state}&code=code-B").status_code == 400


def test_id_token_claims_are_checked(monkeypatch):
    import base64, json, time
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")

    def tok(**claims):
        base = {"iss": "https://accounts.google.com", "aud": "cid", "exp": time.time() + 60, "sub": "1",
                "email": "a@b.com", "email_verified": True, "name": "A"}
        base.update(claims)
        seg = base64.urlsafe_b64encode(json.dumps(base).encode()).decode().rstrip("=")
        return "h." + seg + ".s"

    class R:
        def __init__(self, t): self.status_code, self._t = 200, t
        def json(self): return {"id_token": self._t}

    for bad in ({"aud": "other"}, {"iss": "evil"}, {"exp": time.time() - 5}, {"email_verified": False}):
        monkeypatch.setattr(AUTH.httpx, "post", lambda *a, _t=tok(**bad), **k: R(_t))
        with pytest.raises(ValueError):
            AUTH.google_exchange("c", "https://x/cb")
    monkeypatch.setattr(AUTH.httpx, "post", lambda *a, _t=tok(), **k: R(_t))
    assert AUTH.google_exchange("c", "https://x/cb") == {"sub": "1", "email": "a@b.com", "name": "A"}
