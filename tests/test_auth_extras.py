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


def _mail_env(monkeypatch, mode):
    monkeypatch.setenv("TWO_FACTOR", mode)
    monkeypatch.setenv("ATLAS_SMTP_HOST", "smtp.invalid")         # "configured" - nothing is ever sent
    monkeypatch.setenv("ATLAS_MAIL_FROM", "desk@example.com")


@pytest.fixture()
def mailbox(monkeypatch):
    """2FA required, the portal's mail configured but captured instead of sent."""
    sent = []
    _mail_env(monkeypatch, "required")
    monkeypatch.setattr(AUTH, "send_mail", lambda to, subject, body: sent.append((to, subject, body)) or "ok")
    return sent


def _code(mailbox):
    return re.search(r"code is (\d{6})", mailbox[-1][2]).group(1)


def _account(email, name="Pat"):
    """An existing password account (signup itself is tested separately: in required mode it needs a code too)."""
    from werkzeug.security import generate_password_hash
    if not A.store.user_by_email(email):
        A.store.add_user(email, name, "Co", generate_password_hash("password1"))


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
    h = client.get("/api/health").get_json()
    assert h["two_factor"] is False and h["two_factor_ready"] is True and h["two_factor_mode"] == "auto"


def test_required_without_any_mail_says_it_is_a_setup_problem(client, monkeypatch):
    for k in ("ATLAS_SMTP_HOST", "ATLAS_RESEND_KEY", "ATLAS_MAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    _account("nomail@example.com")
    monkeypatch.setenv("TWO_FACTOR", "required")
    assert AUTH.two_factor_problem().startswith("TWO_FACTOR=required but the portal has no mail sender")
    h = client.get("/api/health").get_json()
    assert h["two_factor"] is True and h["two_factor_ready"] is False and h["mail"] is False
    r = client.post("/login", json={"email": "nomail@example.com", "password": "password1"})
    assert r.status_code == 503 and "cannot send email yet" in r.get_json()["error"]
    page = client.post("/login", data={"email": "nomail@example.com", "password": "password1"})
    assert page.status_code == 503 and "cannot send email yet" in page.get_data(as_text=True)
    assert client.get("/api/me").get_json()["id"] is None
    # no account is created that could never sign in
    r = client.post("/signup", json={"name": "N", "email": "nomail-new@example.com", "password": "password1"})
    assert r.status_code == 503 and A.store.user_by_email("nomail-new@example.com") is None


def test_required_mode_fails_closed_when_mail_is_down(client, monkeypatch):
    _mail_env(monkeypatch, "required")
    monkeypatch.setattr(AUTH, "send_mail", lambda *a: (_ for _ in ()).throw(RuntimeError("smtp down")))
    _account("closed@example.com")
    r = client.post("/login", json={"email": "closed@example.com", "password": "password1"})
    assert r.status_code == 503 and "Try again in a minute" in r.get_json()["error"]
    assert client.get("/api/me").get_json()["id"] is None


def test_required_mode_signup_proves_the_address_with_a_code(client, mailbox):
    r = client.post("/signup", json={"name": "New", "company": "Co", "email": "fresh-2fa@example.com", "password": "password1"})
    assert r.status_code == 202 and r.get_json()["mfa"] is True
    assert A.store.user_by_email("fresh-2fa@example.com") is not None
    assert client.get("/api/me").get_json()["id"] is None              # the password alone is not a session
    assert mailbox[-1][0] == "fresh-2fa@example.com"
    r = client.post("/login/verify", data={"code": _code(mailbox)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/start")      # a new owner starts the chain
    assert client.get("/api/me").get_json()["email"] == "fresh-2fa@example.com"


def test_code_flow_wrong_then_right_then_trusted_device(client, mailbox):
    _account("mfa@example.com")
    r = client.post("/login", json={"email": "mfa@example.com", "password": "password1"})
    assert r.status_code == 202 and r.get_json()["mfa"] is True
    assert client.get("/api/me").get_json()["id"] is None          # password alone is not a session
    assert mailbox[-1][0] == "mfa@example.com" and "Many thanks" in mailbox[-1][2]
    pending = client.get_cookie("session").value                   # holds only the challenge id
    assert client.get("/login/verify").status_code == 200
    assert client.post("/login/verify", json={"code": "000000"}).status_code == 401
    good = _code(mailbox)
    r = client.post("/login/verify", json={"code": good, "trust": "1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/api/me").get_json()["email"] == "mfa@example.com"
    assert client.get_cookie("atlas_trust") is not None
    # a code never works twice: not after logout, not with the pre-verify cookie replayed
    client.get("/logout")
    assert client.post("/login/verify", json={"code": good}).status_code in (302, 401)
    client.set_cookie("session", pending)
    r = client.post("/login/verify", json={"code": good})
    assert r.status_code == 401 and "Start again" in r.get_json()["error"]
    client.get("/logout")
    # trusted browser: straight in next time, no mail
    n = len(mailbox)
    r = client.post("/login", json={"email": "mfa@example.com", "password": "password1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True and len(mailbox) == n


def test_five_wrong_codes_lock_the_challenge_even_with_a_replayed_cookie(client, mailbox):
    _account("lock@example.com")
    client.post("/login", json={"email": "lock@example.com", "password": "password1"})
    fresh = client.get_cookie("session").value                       # the cookie right after the password step
    for _ in range(5):
        assert client.post("/login/verify", json={"code": "111111"}).status_code == 401
    good = _code(mailbox)
    r = client.post("/login/verify", json={"code": good})
    assert r.status_code == 401 and "Too many" in r.get_json()["error"]
    # the attempts are counted on the server: an older cookie does not hand them back
    client.set_cookie("session", fresh)
    r = client.post("/login/verify", json={"code": good})
    assert r.status_code == 401 and "Too many" in r.get_json()["error"]
    assert client.get("/api/me").get_json()["id"] is None


def test_resend_replaces_the_code_and_codes_per_hour_are_capped(client, mailbox, monkeypatch):
    _account("resend@example.com")
    client.post("/login", json={"email": "resend@example.com", "password": "password1"})
    first = _code(mailbox)
    assert client.post("/login/verify", data={"resend": "1"}).status_code == 429     # moments after the first one
    monkeypatch.setattr(AUTH, "RESEND_GAP_S", 0)
    assert client.post("/login/verify", data={"resend": "1"}).status_code == 200
    second = _code(mailbox)
    if second != first:                                               # (1 in a million they match)
        assert client.post("/login/verify", json={"code": first}).status_code == 401
    assert client.post("/login/verify", json={"code": second}).status_code == 200
    client.get("/logout")
    monkeypatch.setattr(AUTH, "CODES_PER_HOUR", 3)                    # 2 sent above; one more, then refused
    assert client.post("/login", json={"email": "resend@example.com", "password": "password1"}).status_code == 202
    client.get("/logout")
    n = len(mailbox)
    r = client.post("/login", json={"email": "resend@example.com", "password": "password1"})
    assert r.status_code == 429 and "Too many sign-in codes" in r.get_json()["error"] and len(mailbox) == n


def test_opting_out_needs_the_password_and_is_off_limits_when_required(client, monkeypatch):
    sent = []
    _mail_env(monkeypatch, "auto")
    monkeypatch.setattr(AUTH, "send_mail", lambda to, subject, body: sent.append((to, subject, body)) or "ok")
    _account("optout@example.com")
    client.post("/login", json={"email": "optout@example.com", "password": "password1"})
    client.post("/login/verify", json={"code": _code(sent)})
    assert client.get("/api/me").get_json()["two_factor"] is True
    assert client.patch("/api/me", json={"two_factor": False}).status_code == 403             # session alone: no
    assert client.patch("/api/me", json={"two_factor": False, "password": "nope"}).status_code == 403
    r = client.patch("/api/me", json={"two_factor": False, "password": "password1"})
    assert r.status_code == 200 and r.get_json()["two_factor"] is False
    client.get("/logout")
    n = len(sent)
    r = client.post("/login", json={"email": "optout@example.com", "password": "password1"})
    assert r.status_code == 200 and len(sent) == n
    # TWO_FACTOR=required: the opt-out no longer counts, and cannot be set
    monkeypatch.setenv("TWO_FACTOR", "required")
    assert client.patch("/api/me", json={"two_factor": False, "password": "password1"}).status_code == 400
    client.get("/logout")
    r = client.post("/login", json={"email": "optout@example.com", "password": "password1"})
    assert r.status_code == 202 and len(sent) == n + 1


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
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/start")      # a new owner starts the chain
    me = client.get("/api/me").get_json()
    assert me["email"] == "linked@example.com" and me["name"] == "Existing Name" and me["google"] is True
    client.get("/logout")
    # unknown email -> a new password-less account; its password login is refused
    r = client.get("/auth/google")
    state = re.search(r"state=([^&]+)", r.headers["Location"]).group(1)
    r = client.get(f"/auth/google/callback?state={state}&code=code-B")
    assert r.status_code == 302 and r.headers["Location"].startswith("/signup/profile?next=")   # Google knows the name, not the job
    me = client.get("/api/me").get_json()
    assert me["email"] == "fresh@example.com" and me["google"] is True and me["profile_complete"] is False
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
