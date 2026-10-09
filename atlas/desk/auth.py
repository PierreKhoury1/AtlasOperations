"""Sign-in helpers for the portal: "Continue with Google" and a second factor by email.

Env:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   OAuth 2.0 web client (console.cloud.google.com). Redirect URI to register:
                                            <PUBLIC_URL>/auth/google/callback. Both unset = no Google button.
  TWO_FACTOR=auto|required|off              auto (default): email codes whenever the portal can send mail, and an
                                            account may opt out (with its password); required: every password
                                            session needs a code - signup included, no opt-out - and a portal
                                            that cannot send mail refuses password sign-ins and says so (boot
                                            log, /api/health two_factor_ready); off: never.
  ATLAS_SMTP_HOST / ATLAS_SMTP_PORT / ATLAS_SMTP_USER / ATLAS_SMTP_PASSWORD / ATLAS_MAIL_FROM / ATLAS_MAIL_FROM_NAME
                                            the portal's own outbound mail (Gmail: smtp.gmail.com:587 + app password)
  ATLAS_RESEND_KEY + ATLAS_MAIL_FROM        ...or Resend instead of SMTP.

Google is the server-side authorization-code flow: no Google script on the login page (the CSP stays as it is), the
browser only follows two redirects. The id_token comes straight from Google's token endpoint over TLS, so its claims
are trusted without a JWKS signature check (Google's own guidance for this flow); issuer, audience, expiry and
email_verified are still checked.

The second factor is a 6-digit code mailed to the account address. The challenge lives on the server, on the user's
row (store.mfa_*): only a hash of the code, its expiry, the attempts spent and when codes were sent. The session holds
just the challenge id, so replaying an older cookie cannot hand back spent attempts, and every guess is counted before
it is checked. An account gets at most CODES_PER_HOUR codes, which bounds guessing even from many addresses. "Trust
this device" is a separate signed cookie good for 30 days.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import integrations as I

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

CODE_TTL_S = 10 * 60          # a mailed code is good for ten minutes
CODE_TRIES = 5                # ...and five guesses
RESEND_GAP_S = 45             # one new code per 45 s
CODES_PER_HOUR = 8            # per account, logins and resends together
TRUST_DAYS = 30


class TooManyCodes(RuntimeError):
    """This account was sent CODES_PER_HOUR codes in the last hour."""


# ---------------------------------------------------------------------------- mail
def mail_cfg() -> dict[str, Any] | None:
    """The portal's own sender, from env. None when nothing is configured."""
    frm = os.environ.get("ATLAS_MAIL_FROM", "").strip()
    name = os.environ.get("ATLAS_MAIL_FROM_NAME", "").strip() or "Atlas Desks"
    if os.environ.get("ATLAS_RESEND_KEY", "").strip() and frm:
        return {"kind": "resend", "api_key": os.environ["ATLAS_RESEND_KEY"].strip(), "from_email": frm, "from_name": name}
    host = os.environ.get("ATLAS_SMTP_HOST", "").strip()
    if host and frm:
        return {"kind": "smtp", "host": host, "port": os.environ.get("ATLAS_SMTP_PORT", "587").strip() or "587",
                "user": os.environ.get("ATLAS_SMTP_USER", "").strip(), "password": os.environ.get("ATLAS_SMTP_PASSWORD", ""),
                "from_email": frm, "from_name": name}
    return None


def mail_ready() -> bool:
    return mail_cfg() is not None


def send_mail(to: str, subject: str, body: str) -> str:
    cfg = mail_cfg()
    if not cfg:
        raise RuntimeError("portal mail is not configured (ATLAS_SMTP_* or ATLAS_RESEND_KEY)")
    if cfg["kind"] == "resend":
        return I.send_resend(cfg, to, subject, body)
    return I.send_email(cfg, to, subject, body)


# ---------------------------------------------------------------------------- second factor
def two_factor_mode() -> str:
    m = os.environ.get("TWO_FACTOR", "auto").strip().lower()
    return m if m in ("auto", "required", "off") else "auto"


def two_factor_active() -> bool:
    """Whether password logins get a mailed code right now."""
    m = two_factor_mode()
    return m == "required" or (m == "auto" and mail_ready())


def two_factor_problem() -> str:
    """A setup error to report (boot log, /api/health), or "": TWO_FACTOR=required on a portal that cannot send mail
    refuses every password sign-in, so it must not look like a passing mail outage."""
    if two_factor_mode() == "required" and not mail_ready():
        return ("TWO_FACTOR=required but the portal has no mail sender (set ATLAS_SMTP_HOST + ATLAS_MAIL_FROM, or "
                "ATLAS_RESEND_KEY + ATLAS_MAIL_FROM): every password sign-in is refused until it does")
    return ""


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def code_hash(code: str, secret: str) -> str:
    return hmac.new(secret.encode(), code.strip().encode(), hashlib.sha256).hexdigest()


def issue(email: str, secret: str, prev: dict[str, Any] | None = None, sender=None) -> dict[str, Any]:
    """Mail a fresh code and return the server-side challenge to store on the user row (store.mfa_put): a random id,
    only the code's hash, expiry, attempts spent, and the send times of the last hour (prev = the row's current
    record). Raises TooManyCodes past CODES_PER_HOUR, or whatever the mailer raises."""
    now = time.time()
    issued = [float(t) for t in (prev or {}).get("issued") or [] if now - float(t) < 3600]
    if len(issued) >= CODES_PER_HOUR:
        raise TooManyCodes("too many sign-in codes for this account in the last hour")
    code = new_code()
    (sender or send_mail)(email, "Your Atlas Desks sign-in code",
           f"Your sign-in code is {code}\n\nIt is valid for {CODE_TTL_S // 60} minutes. If you did not try to log in to "
           f"Atlas Desks, change your password.\n\nMany thanks,\nAtlas Desks")
    return {"id": secrets.token_urlsafe(18), "hash": code_hash(code, secret), "exp": now + CODE_TTL_S, "tries": 0,
            "sent": now, "issued": issued + [now]}


def check_code(rec: dict[str, Any] | None, code: str, secret: str, used: int) -> tuple[bool, str]:
    """(ok, error) for one guess at the server-side challenge `rec`. `used` is the attempts spent on it INCLUDING this
    guess: the caller counts it in the store first (store.mfa_spend), so parallel or replayed requests all pay."""
    if not rec or not rec.get("id") or used < 1:
        return False, "Start again from the login page."
    if time.time() > float(rec.get("exp") or 0):
        return False, "That code has expired - request a new one."
    if used > CODE_TRIES:
        return False, "Too many wrong codes - request a new one."
    if not hmac.compare_digest(code_hash(code, secret), str(rec.get("hash") or "")):
        left = CODE_TRIES - used
        return False, f"Wrong code. {left} attempt{'s' if left != 1 else ''} left." if left else "Too many wrong codes - request a new one."
    return True, ""


def can_resend(rec: dict[str, Any] | None) -> bool:
    return bool(rec) and time.time() - float(rec.get("sent") or 0) >= RESEND_GAP_S


def _trust(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="atlas-2fa-trust")


def trust_token(uid: int, secret: str) -> str:
    return _trust(secret).dumps({"uid": uid})


def trusted_uid(token: str | None, secret: str) -> int | None:
    if not token:
        return None
    try:
        return int(_trust(secret).loads(token, max_age=TRUST_DAYS * 86400)["uid"])
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------- google
def google_ready() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip() and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip())


def google_auth_url(state: str, redirect_uri: str) -> str:
    q = {"client_id": os.environ["GOOGLE_CLIENT_ID"].strip(), "redirect_uri": redirect_uri, "response_type": "code",
         "scope": "openid email profile", "state": state, "prompt": "select_account"}
    return GOOGLE_AUTH + "?" + urlencode(q)


def _b64json(seg: str) -> dict[str, Any]:
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg.encode()))


def google_exchange(code: str, redirect_uri: str) -> dict[str, Any]:
    """Swap the authorization code for the user's identity. Returns {sub, email, name} or raises ValueError."""
    data = {"code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(), "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"}
    r = httpx.post(GOOGLE_TOKEN, data=data, timeout=15)
    if r.status_code != 200:
        raise ValueError(f"Google token endpoint answered {r.status_code}")
    tok = r.json().get("id_token") or ""
    parts = tok.split(".")
    if len(parts) != 3:
        raise ValueError("no id_token in Google's reply")
    claims = _b64json(parts[1])
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("id_token issuer is not Google")
    if claims.get("aud") != os.environ["GOOGLE_CLIENT_ID"].strip():
        raise ValueError("id_token was issued for another client")
    if float(claims.get("exp", 0)) < time.time():
        raise ValueError("id_token has expired")
    if not claims.get("email") or not claims.get("email_verified"):
        raise ValueError("Google did not vouch for that email address")
    return {"sub": str(claims["sub"]), "email": str(claims["email"]).strip().lower(),
            "name": str(claims.get("name") or claims["email"].split("@")[0])}
