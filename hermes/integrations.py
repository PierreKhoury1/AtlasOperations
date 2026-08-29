"""Real-world connectors: email out (SMTP), email in (IMAP), any HTTP API, plus helpers.

A connector is a row in the `connectors` table: {kind, name, config, auto}. `config` holds credentials
(stored server-side only; the API returns a masked copy). Nothing here decides *whether* an action is
allowed — the orchestrator + approval queue do that. These functions just perform the action.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import json
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from typing import Any

import httpx

KINDS = {
    "smtp": {"label": "Email sending (SMTP)", "fields": ["host", "port", "user", "password", "from_email", "from_name"],
             "hint": "Gmail: smtp.gmail.com:587 with an app password. Outlook: smtp.office365.com:587."},
    "imap": {"label": "Inbox watching (IMAP)", "fields": ["host", "port", "user", "password", "folder"],
             "hint": "Gmail: imap.gmail.com:993. New unread emails become leads."},
    "http": {"label": "HTTP API", "fields": ["base_url", "auth_type", "token", "headers", "notes"],
             "hint": "Any REST API. auth_type: bearer | header | query | basic | none. `notes` tells the agents what the API does and which paths exist."},
    "webhook": {"label": "Inbound webhook", "fields": [],
                "hint": "POST JSON {name, email, phone, company, notes, source} to the desk's hook URL — web forms, Zapier, Make, Typeform."},
}

SECRET_KEYS = ("password", "token", "secret", "api_key")


def mask(config: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in (config or {}).items():
        if any(s in k.lower() for s in SECRET_KEYS) and v:
            out[k] = "••••" + str(v)[-4:] if len(str(v)) > 4 else "••••"
        else:
            out[k] = v
    return out


def merge_secrets(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Keep the stored secret when the client sends back the masked placeholder."""
    out = dict(old or {})
    for k, v in (new or {}).items():
        if any(s in k.lower() for s in SECRET_KEYS) and isinstance(v, str) and v.startswith("••••"):
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------- email out
def send_email(cfg: dict[str, Any], to: str, subject: str, body: str) -> str:
    host = cfg.get("host", "").strip()
    port = int(cfg.get("port") or 587)
    user = cfg.get("user", "").strip()
    pw = cfg.get("password", "")
    from_email = (cfg.get("from_email") or user).strip()
    from_name = (cfg.get("from_name") or "").strip()
    if not host or not from_email:
        raise RuntimeError("SMTP connector needs host and from_email")
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if user:
                s.login(user, pw)
            s.send_message(msg)
    return f"sent via {host} as {from_email}"


def test_smtp(cfg: dict[str, Any]) -> str:
    host = cfg.get("host", "").strip()
    port = int(cfg.get("port") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if cfg.get("user"):
                s.login(cfg["user"], cfg.get("password", ""))
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if cfg.get("user"):
                s.login(cfg["user"], cfg.get("password", ""))
    return f"login OK on {host}:{port}"


# ---------------------------------------------------------------------------- email in
def _decode(s: str | None) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _body_of(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return str(msg.get_payload())


def fetch_unseen(cfg: dict[str, Any], limit: int = 10, mark_seen: bool = True) -> list[dict[str, str]]:
    host = cfg.get("host", "").strip()
    port = int(cfg.get("port") or 993)
    folder = cfg.get("folder") or "INBOX"
    M = imaplib.IMAP4_SSL(host, port)
    try:
        M.login(cfg.get("user", ""), cfg.get("password", ""))
        M.select(folder)
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split()[-limit:] if typ == "OK" and data and data[0] else []
        out = []
        for uid in ids:
            typ, raw = M.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            name, addr = email.utils.parseaddr(_decode(msg.get("From")))
            out.append({"uid": uid.decode(), "from_name": name or addr.split("@")[0], "from_email": addr,
                        "subject": _decode(msg.get("Subject")), "body": _body_of(msg).strip()[:4000],
                        "date": msg.get("Date", "")})
            if mark_seen:
                M.store(uid, "+FLAGS", "\\Seen")
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def test_imap(cfg: dict[str, Any]) -> str:
    M = imaplib.IMAP4_SSL(cfg.get("host", "").strip(), int(cfg.get("port") or 993))
    try:
        M.login(cfg.get("user", ""), cfg.get("password", ""))
        typ, data = M.select(cfg.get("folder") or "INBOX", readonly=True)
        n = data[0].decode() if data and data[0] else "?"
        return f"login OK, {n} messages in {cfg.get('folder') or 'INBOX'}"
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------- generic HTTP API
def _headers(cfg: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], tuple | None]:
    headers: dict[str, str] = {"User-Agent": "Hermes/0.2 (+desk agent)"}
    params: dict[str, str] = {}
    auth = None
    raw = cfg.get("headers")
    if isinstance(raw, str) and raw.strip():
        try:
            headers.update({str(k): str(v) for k, v in json.loads(raw).items()})
        except Exception:
            for line in raw.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()
    elif isinstance(raw, dict):
        headers.update({str(k): str(v) for k, v in raw.items()})
    t = (cfg.get("auth_type") or "none").lower()
    tok = cfg.get("token") or ""
    if t == "bearer" and tok:
        headers["Authorization"] = f"Bearer {tok}"
    elif t == "header" and tok:
        headers[cfg.get("auth_header") or "X-API-Key"] = tok
    elif t == "query" and tok:
        params[cfg.get("auth_param") or "api_key"] = tok
    elif t == "basic" and tok:
        u, _, p = tok.partition(":")
        auth = (u, p)
    return headers, params, auth


def http_call(cfg: dict[str, Any], method: str, path: str, params: dict[str, Any] | None = None,
              body: Any = None, timeout: float = 30) -> dict[str, Any]:
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("HTTP connector needs base_url")
    url = path if re.match(r"^https?://", path or "") else base + "/" + (path or "").lstrip("/")
    if not url.startswith(base) and not cfg.get("allow_any_host"):
        raise RuntimeError(f"path escapes the connector's base_url ({base})")
    headers, qp, auth = _headers(cfg)
    qp.update({k: str(v) for k, v in (params or {}).items()})
    method = (method or "GET").upper()
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.request(method, url, params=qp, headers=headers, auth=auth,
                      json=body if (body is not None and method in ("POST", "PUT", "PATCH", "DELETE")) else None)
    out: dict[str, Any] = {"status": r.status_code, "url": str(r.url)}
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        try:
            out["json"] = r.json()
        except Exception:
            out["text"] = r.text[:6000]
    else:
        out["text"] = r.text[:6000]
    return out


def test_http(cfg: dict[str, Any]) -> str:
    r = http_call(cfg, "GET", cfg.get("test_path") or "", timeout=15)
    return f"HTTP {r['status']} from {r['url']}"


def test_connector(kind: str, cfg: dict[str, Any]) -> str:
    if kind == "smtp":
        return test_smtp(cfg)
    if kind == "imap":
        return test_imap(cfg)
    if kind == "http":
        return test_http(cfg)
    if kind == "webhook":
        return "webhook connectors need no test — POST to the hook URL"
    raise RuntimeError(f"unknown connector kind {kind}")


def describe(connectors: list[dict[str, Any]]) -> str:
    """Text roster for the agents' prompts."""
    if not connectors:
        return "(no connectors configured)"
    lines = []
    for c in connectors:
        cfg = c.get("config") or {}
        extra = ""
        if c["kind"] == "http":
            extra = f" base_url={cfg.get('base_url','')}" + (f" — {cfg.get('notes')}" if cfg.get("notes") else "")
        elif c["kind"] == "smtp":
            extra = f" from={cfg.get('from_email','')}"
        lines.append(f"- {c['name']} [{c['kind']}]{extra}  writes-without-approval={'yes' if c.get('auto') else 'no'}")
    return "\n".join(lines)
