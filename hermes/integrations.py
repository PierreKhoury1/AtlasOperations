"""Real-world connectors: email out (SMTP / Resend), email in (IMAP), WhatsApp (Meta Cloud API / Twilio),
SMS (Twilio), CRMs (HubSpot / Pipedrive), Google Calendar, Slack notifications, any HTTP API, MCP servers.

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
    "mcp": {"label": "MCP server (any tool provider)", "fields": ["command", "env", "notes"],
            "hint": "Model Context Protocol server started as a subprocess. e.g. `npx -y @modelcontextprotocol/server-filesystem C:/clients/acme`, `npx -y @modelcontextprotocol/server-github` (env GITHUB_PERSONAL_ACCESS_TOKEN=...), Slack, Notion, Postgres, Gmail... Every tool the server offers becomes an agent tool. Writes need approval unless auto."},
    "webhook": {"label": "Inbound webhook", "fields": [],
                "hint": "POST JSON {name, email, phone, company, notes, source} to the desk's hook URL — web forms, Zapier, Make, Typeform."},
    "resend": {"label": "Email sending (Resend API)", "fields": ["api_key", "from_email", "from_name"],
               "hint": "resend.com — HTTPS email API, works where SMTP ports are blocked (Render, Fly). Verify your sending domain in Resend first; from_email must be on that domain."},
    "whatsapp": {"label": "WhatsApp (Meta Cloud API)", "fields": ["phone_number_id", "access_token", "verify_token", "notes"],
                 "hint": "Meta for Developers → WhatsApp → API setup: copy the Phone number ID and a permanent System User access token. Set the webhook URL shown below with your verify_token to receive replies. First message to a new contact must be a template unless they wrote first (24h window)."},
    "twilio": {"label": "SMS + WhatsApp (Twilio)", "fields": ["account_sid", "auth_token", "from_number", "whatsapp_from"],
               "hint": "Twilio console → Account SID + Auth token. from_number = your Twilio number (+44…). whatsapp_from = your WhatsApp-enabled sender (or the sandbox +14155238886) if you want WhatsApp via Twilio. Point the number's inbound webhook at the SMS hook URL below."},
    "hubspot": {"label": "CRM sync (HubSpot)", "fields": ["access_token"],
                "hint": "HubSpot → Settings → Integrations → Private apps → create with scopes crm.objects.contacts.read/write. Every crm_update the desk makes is mirrored to a HubSpot contact + timeline note."},
    "pipedrive": {"label": "CRM sync (Pipedrive)", "fields": ["company_domain", "api_token"],
                  "hint": "company_domain = the part before .pipedrive.com. API token: Personal preferences → API. Contacts are mirrored as persons with a note per update."},
    "gcal": {"label": "Google Calendar", "fields": ["client_id", "client_secret", "refresh_token", "calendar_id", "timezone", "day_start", "day_end"],
             "hint": "OAuth client (Desktop) in Google Cloud with the Calendar API enabled; get a refresh_token once via the OAuth playground (scope https://www.googleapis.com/auth/calendar). calendar_id defaults to primary, timezone to Europe/London, working hours 09:00–17:00. Agents can read free slots; bookings go through your approval unless auto."},
    "slack": {"label": "Slack notifications", "fields": ["webhook_url", "channel"],
              "hint": "Slack → Apps → Incoming Webhooks → add to a channel. The desk posts when something is waiting for approval and when it goes out."},
}

# outbound channel → connector kinds that can carry it, in order of preference
CHANNELS = {"email": ("smtp", "resend"), "whatsapp": ("whatsapp", "twilio"), "sms": ("twilio",), "booking": ("gcal",)}
CRM_KINDS = ("hubspot", "pipedrive")

SECRET_KEYS = ("password", "token", "secret", "api_key", "env", "webhook_url", "account_sid")


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
    if kind == "mcp":
        from . import mcp_client
        return mcp_client.test_server(cfg)
    if kind == "webhook":
        return "webhook connectors need no test — POST to the hook URL"
    if kind == "resend":
        return test_resend(cfg)
    if kind == "whatsapp":
        return test_whatsapp(cfg)
    if kind == "twilio":
        return test_twilio(cfg)
    if kind == "hubspot":
        return test_hubspot(cfg)
    if kind == "pipedrive":
        return test_pipedrive(cfg)
    if kind == "gcal":
        return test_gcal(cfg)
    if kind == "slack":
        return test_slack(cfg)
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
        elif c["kind"] == "mcp":
            extra = f" tools exposed as mcp__{c['name']}__<tool>" + (f" — {cfg.get('notes')}" if cfg.get("notes") else "")
        elif c["kind"] == "resend":
            extra = f" from={cfg.get('from_email','')} (email channel)"
        elif c["kind"] == "whatsapp":
            extra = " (whatsapp channel — queue_action kind=whatsapp, to=+phone)"
        elif c["kind"] == "twilio":
            extra = f" from={cfg.get('from_number','')} (sms channel" + (", whatsapp channel" if cfg.get("whatsapp_from") else "") + ")"
        elif c["kind"] in CRM_KINDS:
            extra = " (external CRM — crm_update is mirrored there automatically)"
        elif c["kind"] == "gcal":
            extra = f" calendar={cfg.get('calendar_id') or 'primary'} tz={cfg.get('timezone') or 'Europe/London'} (use calendar_free_slots / calendar_book)"
        elif c["kind"] == "slack":
            extra = " (owner gets a Slack ping for approvals and sends)"
        lines.append(f"- {c['name']} [{c['kind']}]{extra}  writes-without-approval={'yes' if c.get('auto') else 'no'}")
    return "\n".join(lines)



# ============================================================================ v2 connectors
# All providers below go through _http so tests can swap _TRANSPORT for an httpx.MockTransport.
import datetime as _dt
from urllib.parse import quote as _q
from zoneinfo import ZoneInfo

_TRANSPORT = None


def _http(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
          json_body: Any = None, data: dict[str, Any] | None = None, auth: tuple | None = None, timeout: float = 30) -> Any:
    h = {"User-Agent": "Hermes/0.3 (+desk agent)"}
    h.update(headers or {})
    with httpx.Client(timeout=timeout, transport=_TRANSPORT, follow_redirects=True) as c:
        r = c.request(method, url, headers=h, params=params, json=json_body, data=data, auth=auth)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {url.split('?')[0]} → HTTP {r.status_code}: {r.text[:300]}")
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {"text": r.text[:4000]}


def _digits(phone: str) -> str:
    raw = (phone or "").strip()
    d = re.sub(r"\D", "", raw)
    if d.startswith("00"):
        d = d[2:]
    elif raw.startswith("0") and len(d) == 11:
        d = "44" + d[1:]          # UK national → E.164
    return d


def _need(cfg: dict[str, Any], kind: str, *keys: str) -> None:
    missing = [k for k in keys if not str(cfg.get(k) or "").strip()]
    if missing:
        raise RuntimeError(f"{kind} connector needs {', '.join(missing)}")


# ---------------------------------------------------------------------------- Resend (email over HTTPS)
def send_resend(cfg: dict[str, Any], to: str, subject: str, body: str) -> str:
    _need(cfg, "Resend", "api_key", "from_email")
    frm = cfg["from_email"].strip()
    name = (cfg.get("from_name") or "").strip()
    r = _http("POST", "https://api.resend.com/emails", headers={"Authorization": f"Bearer {cfg['api_key'].strip()}"},
              json_body={"from": f"{name} <{frm}>" if name else frm, "to": [to], "subject": subject or "(no subject)", "text": body})
    return f"sent via Resend as {frm} (id {r.get('id', '?')})"


def test_resend(cfg: dict[str, Any]) -> str:
    _need(cfg, "Resend", "api_key")
    r = _http("GET", "https://api.resend.com/domains", headers={"Authorization": f"Bearer {cfg['api_key'].strip()}"}, timeout=15)
    doms = [d.get("name") for d in (r.get("data") or []) if isinstance(d, dict)]
    return "Resend API key OK" + (f", domains: {', '.join(doms[:4])}" if doms else " (no verified domains yet)")


# ---------------------------------------------------------------------------- WhatsApp Cloud API (Meta)
GRAPH = "https://graph.facebook.com/v21.0"


def send_whatsapp(cfg: dict[str, Any], to: str, body: str) -> str:
    _need(cfg, "WhatsApp", "phone_number_id", "access_token")
    to_ = _digits(to)
    if not to_:
        raise RuntimeError(f"WhatsApp needs a phone number, got {to!r}")
    r = _http("POST", f"{GRAPH}/{cfg['phone_number_id'].strip()}/messages",
              headers={"Authorization": f"Bearer {cfg['access_token'].strip()}"},
              json_body={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_,
                         "type": "text", "text": {"preview_url": False, "body": body}})
    mid = ((r.get("messages") or [{}])[0]).get("id", "?")
    return f"sent via WhatsApp Cloud API to +{to_} (id {mid})"


def test_whatsapp(cfg: dict[str, Any]) -> str:
    _need(cfg, "WhatsApp", "phone_number_id", "access_token")
    r = _http("GET", f"{GRAPH}/{cfg['phone_number_id'].strip()}", params={"fields": "display_phone_number,verified_name,quality_rating"},
              headers={"Authorization": f"Bearer {cfg['access_token'].strip()}"}, timeout=15)
    return f"WhatsApp number {r.get('display_phone_number', '?')} ({r.get('verified_name', '?')}, quality {r.get('quality_rating', '?')})"


def parse_whatsapp_webhook(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten a Meta webhook POST into [{from, name, text, id}] for text-ish messages."""
    out: list[dict[str, str]] = []
    for entry in payload.get("entry") or []:
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in v.get("contacts") or []}
            for m in v.get("messages") or []:
                t = m.get("type")
                text = ((m.get("text") or {}).get("body") or (m.get("button") or {}).get("text")
                        or ((m.get("interactive") or {}).get("button_reply") or {}).get("title")
                        or ((m.get("interactive") or {}).get("list_reply") or {}).get("title") or f"[{t} message]")
                out.append({"from": m.get("from", ""), "name": names.get(m.get("from"), ""), "text": text, "id": m.get("id", "")})
    return out


# ---------------------------------------------------------------------------- Twilio (SMS + WhatsApp)
TWILIO = "https://api.twilio.com/2010-04-01"


def send_twilio(cfg: dict[str, Any], to: str, body: str, channel: str = "sms") -> str:
    _need(cfg, "Twilio", "account_sid", "auth_token")
    sid, tok = cfg["account_sid"].strip(), cfg["auth_token"].strip()
    if channel == "whatsapp":
        frm = (cfg.get("whatsapp_from") or "").strip()
        if not frm:
            raise RuntimeError("Twilio connector has no whatsapp_from sender")
        frm = frm if frm.startswith("whatsapp:") else "whatsapp:" + frm
        to_ = "whatsapp:+" + _digits(to)
    else:
        frm = (cfg.get("from_number") or "").strip()
        if not frm:
            raise RuntimeError("Twilio connector has no from_number")
        to_ = "+" + _digits(to)
    r = _http("POST", f"{TWILIO}/Accounts/{sid}/Messages.json", data={"To": to_, "From": frm, "Body": body}, auth=(sid, tok))
    return f"sent via Twilio {channel} from {frm} (sid {r.get('sid', '?')})"


def test_twilio(cfg: dict[str, Any]) -> str:
    _need(cfg, "Twilio", "account_sid", "auth_token")
    sid, tok = cfg["account_sid"].strip(), cfg["auth_token"].strip()
    r = _http("GET", f"{TWILIO}/Accounts/{sid}.json", auth=(sid, tok), timeout=15)
    return f"Twilio account {r.get('friendly_name', '?')} ({r.get('status', '?')})"


# ---------------------------------------------------------------------------- HubSpot
HS = "https://api.hubapi.com"
_HS_STAGE = {"New": "NEW", "Contacted": "ATTEMPTED_TO_CONTACT", "Qualified": "CONNECTED", "Proposal": "OPEN_DEAL",
             "Won": "OPEN_DEAL", "Lost": "UNQUALIFIED"}


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def hubspot_upsert(cfg: dict[str, Any], contact: dict[str, Any]) -> str:
    _need(cfg, "HubSpot", "access_token")
    email_ = (contact.get("email") or "").strip()
    if not email_:
        raise RuntimeError("HubSpot sync needs an email on the contact")
    h = {"Authorization": f"Bearer {cfg['access_token'].strip()}"}
    first, last = _split_name(contact.get("name") or "")
    props = {k: v for k, v in {"email": email_, "firstname": first, "lastname": last, "company": contact.get("company") or "",
                               "phone": contact.get("phone") or "", "hs_lead_status": _HS_STAGE.get(contact.get("stage") or "")}.items() if v}
    found = _http("POST", f"{HS}/crm/v3/objects/contacts/search", headers=h,
                  json_body={"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email_}]}],
                             "properties": ["email"], "limit": 1})
    hits = found.get("results") or []
    if hits:
        cid = hits[0]["id"]
        _http("PATCH", f"{HS}/crm/v3/objects/contacts/{cid}", headers=h, json_body={"properties": props})
        action = "updated"
    else:
        cid = _http("POST", f"{HS}/crm/v3/objects/contacts", headers=h, json_body={"properties": props}).get("id", "?")
        action = "created"
    note = " · ".join(x for x in (contact.get("notes") or "", ("next: " + contact["next_action"]) if contact.get("next_action") else "") if x)
    if note:
        _http("POST", f"{HS}/crm/v3/objects/notes", headers=h,
              json_body={"properties": {"hs_timestamp": str(int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000)),
                                        "hs_note_body": ("[Hermes] " + note)[:2000]},
                         "associations": [{"to": {"id": cid}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]})
    return f"HubSpot: {action} contact {cid}"


def test_hubspot(cfg: dict[str, Any]) -> str:
    _need(cfg, "HubSpot", "access_token")
    r = _http("GET", f"{HS}/crm/v3/objects/contacts", params={"limit": 1}, headers={"Authorization": f"Bearer {cfg['access_token'].strip()}"}, timeout=15)
    return "HubSpot token OK" + (" (contacts readable)" if "results" in r else "")


# ---------------------------------------------------------------------------- Pipedrive
def _pd(cfg: dict[str, Any]) -> tuple[str, dict[str, str]]:
    _need(cfg, "Pipedrive", "company_domain", "api_token")
    dom = cfg["company_domain"].strip().replace("https://", "").split(".")[0]
    return f"https://{dom}.pipedrive.com/api/v1", {"api_token": cfg["api_token"].strip()}


def pipedrive_upsert(cfg: dict[str, Any], contact: dict[str, Any]) -> str:
    base, qp = _pd(cfg)
    email_ = (contact.get("email") or "").strip()
    if not email_:
        raise RuntimeError("Pipedrive sync needs an email on the contact")
    found = _http("GET", f"{base}/persons/search", params={**qp, "term": email_, "fields": "email", "exact_match": "true", "limit": 1})
    items = ((found.get("data") or {}).get("items") or [])
    body: dict[str, Any] = {"name": (contact.get("name") or email_).strip(), "email": [{"value": email_, "primary": True}]}
    if contact.get("phone"):
        body["phone"] = [{"value": contact["phone"], "primary": True}]
    if items:
        pid = items[0]["item"]["id"]
        _http("PUT", f"{base}/persons/{pid}", params=qp, json_body=body)
        action = "updated"
    else:
        pid = (_http("POST", f"{base}/persons", params=qp, json_body=body).get("data") or {}).get("id", "?")
        action = "created"
    note = " · ".join(x for x in (f"stage={contact.get('stage')}" if contact.get("stage") else "",
                                  f"company={contact['company']}" if contact.get("company") else "",
                                  contact.get("notes") or "", ("next: " + contact["next_action"]) if contact.get("next_action") else "") if x)
    if note and pid != "?":
        _http("POST", f"{base}/notes", params=qp, json_body={"content": ("[Hermes] " + note)[:4000], "person_id": pid})
    return f"Pipedrive: {action} person {pid}"


def test_pipedrive(cfg: dict[str, Any]) -> str:
    base, qp = _pd(cfg)
    r = _http("GET", f"{base}/users/me", params=qp, timeout=15)
    return f"Pipedrive user {(r.get('data') or {}).get('name', '?')} on {base.split('//')[1].split('.')[0]}"


def crm_sync(connectors: list[dict[str, Any]], contact: dict[str, Any]) -> list[str]:
    """Mirror a desk contact to every configured external CRM. Errors are returned as strings, never raised."""
    out: list[str] = []
    for c in connectors or []:
        try:
            if c["kind"] == "hubspot":
                out.append(hubspot_upsert(c["config"], contact))
            elif c["kind"] == "pipedrive":
                out.append(pipedrive_upsert(c["config"], contact))
        except Exception as exc:
            out.append(f"{c['kind']} sync failed: {type(exc).__name__}: {str(exc)[:160]}")
    return out


# ---------------------------------------------------------------------------- Google Calendar
GCAL = "https://www.googleapis.com/calendar/v3"


def _gcal_token(cfg: dict[str, Any]) -> str:
    _need(cfg, "Google Calendar", "client_id", "client_secret", "refresh_token")
    r = _http("POST", "https://oauth2.googleapis.com/token",
              data={"client_id": cfg["client_id"].strip(), "client_secret": cfg["client_secret"].strip(),
                    "refresh_token": cfg["refresh_token"].strip(), "grant_type": "refresh_token"}, timeout=20)
    tok = r.get("access_token")
    if not tok:
        raise RuntimeError("Google did not return an access token — refresh_token revoked?")
    return tok


def _gcal_ctx(cfg: dict[str, Any]) -> tuple[dict[str, str], str, ZoneInfo]:
    return ({"Authorization": f"Bearer {_gcal_token(cfg)}"}, (cfg.get("calendar_id") or "primary").strip(),
            ZoneInfo((cfg.get("timezone") or "Europe/London").strip()))


def _parse_when(s: str, tz: ZoneInfo, end_of_day: bool = False) -> _dt.datetime:
    s = (s or "").strip().replace("Z", "+00:00")
    if len(s) == 10:                       # YYYY-MM-DD
        d = _dt.date.fromisoformat(s)
        t = _dt.time(23, 59) if end_of_day else _dt.time(0, 0)
        return _dt.datetime.combine(d, t, tzinfo=tz)
    dt = _dt.datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def gcal_free_slots(cfg: dict[str, Any], from_when: str, to_when: str, duration_min: int = 30, max_slots: int = 12,
                    now: _dt.datetime | None = None) -> list[str]:
    h, cal, tz = _gcal_ctx(cfg)
    start = _parse_when(from_when, tz)
    end = _parse_when(to_when, tz, end_of_day=True)
    now = now or _dt.datetime.now(tz=tz)
    if start < now:
        start = now
    fb = _http("POST", f"{GCAL}/freeBusy", headers=h,
               json_body={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": str(tz), "items": [{"id": cal}]})
    busy = [(_parse_when(b["start"], tz), _parse_when(b["end"], tz)) for b in ((fb.get("calendars") or {}).get(cal) or {}).get("busy", [])]
    ds_h, ds_m = [int(x) for x in (cfg.get("day_start") or "09:00").split(":")]
    de_h, de_m = [int(x) for x in (cfg.get("day_end") or "17:00").split(":")]
    step = _dt.timedelta(minutes=max(15, int(duration_min or 30)))
    out: list[str] = []
    day = start.date()
    while day <= end.date() and len(out) < max_slots:
        if day.weekday() < 5:
            cur = _dt.datetime.combine(day, _dt.time(ds_h, ds_m), tzinfo=tz)
            day_end = _dt.datetime.combine(day, _dt.time(de_h, de_m), tzinfo=tz)
            while cur + step <= day_end and len(out) < max_slots:
                fin = cur + step
                if cur >= start and fin <= end and not any(b0 < fin and b1 > cur for b0, b1 in busy):
                    out.append(cur.isoformat())
                cur = fin
        day += _dt.timedelta(days=1)
    return out


def gcal_create_event(cfg: dict[str, Any], title: str, start_iso: str, end_iso: str, attendee_email: str = "", description: str = "") -> str:
    h, cal, tz = _gcal_ctx(cfg)
    start = _parse_when(start_iso, tz)
    end = _parse_when(end_iso, tz) if end_iso else start + _dt.timedelta(minutes=30)
    body: dict[str, Any] = {"summary": title or "Appointment", "description": description or "Booked by Hermes desk",
                            "start": {"dateTime": start.isoformat(), "timeZone": str(tz)}, "end": {"dateTime": end.isoformat(), "timeZone": str(tz)}}
    if attendee_email and "@" in attendee_email:
        body["attendees"] = [{"email": attendee_email}]
    r = _http("POST", f"{GCAL}/calendars/{_q(cal, safe='')}/events", headers=h, params={"sendUpdates": "all"}, json_body=body)
    return f"booked '{body['summary']}' {start.strftime('%a %d %b %H:%M')}–{end.strftime('%H:%M')} {tz} (event {r.get('id', '?')})"


def test_gcal(cfg: dict[str, Any]) -> str:
    h, cal, tz = _gcal_ctx(cfg)
    r = _http("GET", f"{GCAL}/users/me/calendarList/{_q(cal, safe='')}", headers=h, timeout=15)
    return f"Google Calendar OK: {r.get('summary', cal)} ({tz})"


# ---------------------------------------------------------------------------- Slack
def slack_notify(cfg: dict[str, Any], text: str) -> str:
    _need(cfg, "Slack", "webhook_url")
    body: dict[str, Any] = {"text": text}
    if cfg.get("channel"):
        body["channel"] = cfg["channel"]
    _http("POST", cfg["webhook_url"].strip(), json_body=body, timeout=15)
    return "posted to Slack"


def test_slack(cfg: dict[str, Any]) -> str:
    slack_notify(cfg, ":white_check_mark: Hermes desk connected — you'll get a ping here for approvals and sends.")
    return "posted a test message to Slack"


def notify(connectors: list[dict[str, Any]], text: str) -> None:
    """Best-effort owner notification on every Slack connector. Never raises."""
    for c in connectors or []:
        if c.get("kind") == "slack":
            try:
                slack_notify(c["config"], text)
            except Exception:
                pass


# ---------------------------------------------------------------------------- outbound routing
def outbound_connector(connectors: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    """First connector able to carry this channel, in CHANNELS preference order."""
    for want in CHANNELS.get(kind, ()):
        for c in connectors or []:
            if c.get("kind") != want:
                continue
            if want == "twilio" and kind == "whatsapp" and not (c.get("config") or {}).get("whatsapp_from"):
                continue
            return c
    return None


def deliver(conn: dict[str, Any], kind: str, to: str, subject: str, body: str) -> str:
    cfg = conn.get("config") or {}
    k = conn.get("kind")
    if kind == "email":
        return send_email(cfg, to, subject, body) if k == "smtp" else send_resend(cfg, to, subject, body)
    if kind == "whatsapp":
        text = (subject + "\n\n" + body).strip() if subject and subject not in body else body
        return send_whatsapp(cfg, to, text) if k == "whatsapp" else send_twilio(cfg, to, text, "whatsapp")
    if kind == "sms":
        return send_twilio(cfg, to, body, "sms")
    if kind == "booking":
        spec = json.loads(body or "{}") if (body or "").lstrip().startswith("{") else {"title": subject, "start": body}
        return gcal_create_event(cfg, spec.get("title") or subject, spec.get("start", ""), spec.get("end", ""),
                                 spec.get("attendee_email") or (to if "@" in (to or "") else ""), spec.get("description", ""))
    raise RuntimeError(f"no delivery path for {kind} via {k}")
