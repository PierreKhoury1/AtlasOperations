"""Atlas Desk portal (Flask).

  /            marketing site (atlas/site)
  /desk        client portal SPA (accounts + one or more desks per account)
  /api/...     JSON API used by the portal — every data endpoint is scoped to the current desk

Run:  py -m atlas.desk            (PORT env, default 8094)
Env:  DESK_MODE=demo|live|auto     demo = scripted provider (no API key needed); auto = live if key present
      DESK_PROVIDER=openrouter     live mode provider (default: providers.json default_provider)
      DESK_SECRET=...              session-cookie secret (auto-generated into data/secret.key if unset)
      DESK_OPEN=1                  skip accounts (portal public on the built-in demo desk) — local dev only
      DEMO_DELAY=0.6               demo provider pacing
"""
from __future__ import annotations

import importlib
import json
import queue
import os
import base64
import ipaddress
import itertools
import mimetypes
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory, session, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

from .. import cases as C
from .. import config as cfg
from .. import designer as DS
from .. import integrations as I
from .. import metrics as MX
from .. import records as R
from .. import templates
from .. import tools as T
from .. import vision as V
from .. import rag as RAG
from .. import team as TM
from . import scheduler
from ..orchestrator import Event, Orchestrator
from ..store import Store

ROOT = cfg.ROOT
SITE_DIR = ROOT / "site"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_PATH = cfg.DATA_DIR / "desk.db"
mimetypes.add_type("font/woff2", ".woff2")           # the cyber page's local IBM Plex fonts (Windows maps it oddly)

app = Flask(__name__, static_folder=None)
# DATABASE_URL (postgres://...) makes accounts, desks and approvals survive deploys; without it SQLite in DATA_DIR.
store = Store(DB_PATH, url=os.environ.get("DATABASE_URL", "").strip() or None)


def _secret() -> str:
    env = os.environ.get("DESK_SECRET", "").strip()
    if env:
        return env
    f = cfg.DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), PERMANENT_SESSION_LIFETIME=30 * 86400)
OPEN = os.environ.get("DESK_OPEN", "").strip() in ("1", "true", "yes")
DEFAULT_TEMPLATE = os.environ.get("DESK_TEMPLATE", "sales_desk")

_runs: dict[str, dict[str, Any]] = {}          # run_id -> {"events": [...], "thread":..., "orch":..., "desk_id":...}
_runs_lock = threading.Lock()
_feed: list[dict[str, Any]] = []               # global live feed (all desks, incl. token deltas) for /api/stream
_feed_lock = threading.Lock()
_feed_seq = 0
_FEED_MAX = 30000
_last_error: dict[int, dict[str, Any]] = {}     # desk_id -> most recent error event (surfaced on the health panel)
_alert_sent: dict[str, float] = {}              # alert key -> last notification time (rate limit)
RUN_MAX_S = float(os.environ.get("RUN_MAX_S", "900"))          # watchdog: a run older than this is killed and marked failed
_BOOT_TS = time.time()


# ---------------------------------------------------------------------------- live feed
def _push_feed(run_id: str, desk_id: int, ev: Event):
    global _feed_seq
    with _feed_lock:
        _feed_seq += 1
        _feed.append({"seq": _feed_seq, "run_id": run_id, "desk_id": desk_id, "ts": ev.ts, "kind": ev.kind,
                      "agent": ev.agent, "text": ev.text, "data": ev.data})
        if len(_feed) > _FEED_MAX:
            del _feed[: _FEED_MAX // 3]


def _feed_after(seq: int) -> list[dict[str, Any]]:
    with _feed_lock:
        if not _feed or _feed[-1]["seq"] <= seq:
            return []
        lo = max(0, len(_feed) - (_feed[-1]["seq"] - seq))   # seqs are contiguous, so index by offset
        return _feed[lo:]


# ---------------------------------------------------------------------------- mode / configs
def _mode() -> str:
    m = os.environ.get("DESK_MODE", "auto").lower()
    if m in ("demo", "live"):
        return m
    return "live"      # auto = live. Scripted agents only when DESK_MODE=demo is set on purpose, never as a silent fallback.


def _desk_byo(desk: dict[str, Any] | None) -> dict[str, Any]:
    """The desk's own model accounts: {"default": name, "entries": {name: encrypted-config}}."""
    return ((desk or {}).get("config") or {}).get("byo") or {}


def _desk_byo_default(desk: dict[str, Any] | None) -> dict[str, Any] | None:
    """The decrypted entry the desk runs on, when the owner brought their own model account."""
    byo = _desk_byo(desk)
    blob = (byo.get("entries") or {}).get(byo.get("default") or "")
    e = SEC.decrypt_config(blob) if blob else {}
    return e if e.get("api_key") or e.get("preset") == "custom" else None


def _live_reason(desk: dict[str, Any] | None = None) -> str:
    """Why real runs cannot happen right now ('' = ready). A live desk without a model key says so instead of
    quietly running the scripted designer/agents in its place."""
    if _mode() == "demo":
        return ""
    if _desk_byo_default(desk):
        return ""
    prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    name = os.environ.get("DESK_PROVIDER", "").strip() or prov.get("default_provider", "openrouter")
    pc = (prov.get("providers") or {}).get(name) or cfg.DEFAULT_PROVIDERS["providers"].get(name) or {"type": "anthropic"}
    base = str(pc.get("base_url") or "")
    needs_key = pc.get("type") == "anthropic" or "openrouter" in base or "api.openai.com" in base
    if not needs_key or cfg.resolve_api_key({**pc, "name": name}):
        return ""
    env = "ANTHROPIC_API_KEY" if pc.get("type") == "anthropic" else "OPENROUTER_API_KEY"
    return f"no model key: set {env} for provider '{name}' - real models only, no scripted stand-in"


def _require_live(desk: dict[str, Any] | None = None) -> None:
    r = _live_reason(desk)
    if r:
        abort(Response(json.dumps({"error": "no_model_key", "message": r}), 503, mimetype="application/json"))


_LEGACY_COLOURS = {"#c084fc": "#0b5fcb", "#60a5fa": "#7c3aed", "#f472b6": "#db2777", "#34d399": "#1f9d63", "#fbbf24": "#b45309",
                   "#f87171": "#dc2626", "#fb923c": "#ea580c", "#a855f7": "#0b5fcb", "#22d3ee": "#0e7490", "#a78bfa": "#6d28d9",
                   "#4ade80": "#15803d", "#e879f9": "#a21caf", "#38bdf8": "#0369a1"}   # dark-theme palette -> light-theme palette


def desk_configs(desk: dict[str, Any]) -> dict[str, Any]:
    """Engine config for one desk: template + the desk's stored overrides + model tier + provider."""
    t = templates.get(desk.get("template") or DEFAULT_TEMPLATE)
    over = desk.get("config") or {}
    business = {**t["business"], **(over.get("business") or {})}
    agents = over.get("agents") or t["agents"]
    workflows = over.get("workflows") or t["workflows"]
    by_id = {a.get("id"): a for a in agents}
    for a in agents:                                   # function-first: short text, and no agent without a function
        if a.get("id") in ("atlas", "hermes"):
            continue
        if a.get("instructions"):
            a["instructions"] = [str(x)[:TM.RULE_CHARS] for x in a["instructions"]][:TM.MAX_RULES]
        if a.get("goal"):
            a["goal"] = TM._one_line(a["goal"], TM.GOAL_CHARS)
        if "Standing orders for this role:" in (a.get("system_prompt") or ""):   # written by the old, wordy prompt builder
            a["system_prompt"] = TM.agent_prompt({**a, "name": a.get("name") or a["id"], "role": a.get("role") or "specialist"},
                                                 business, by_id) + templates._SPECIALIST_SUFFIX
        if not any(t_ in TM.FUNCTION_TOOLS or t_ == "mcp" for t_ in a.get("tools") or []):
            a["tools"] = list(dict.fromkeys(list(a.get("tools") or ["read_file", "list_files"]) + ["record_find", "record_get"]))
    for a in agents:                                   # a roster edited in the portal is stored raw: fill what the engine expects
        a.setdefault("enabled", True)
        a.setdefault("tools", ["read_file", "list_files"])
        for old in templates._OLD_SPECIALIST_SUFFIXES:     # desks built before the shorter suffix
            if old.strip() in (a.get("system_prompt") or ""):
                a["system_prompt"] = (a.get("system_prompt") or "").replace(old, "").replace(old.strip(), "").rstrip()
        if a.get("id") != "atlas" and templates._SPECIALIST_SUFFIX.strip() not in (a.get("system_prompt") or ""):
            a["system_prompt"] = (a.get("system_prompt") or f"You are {a.get('name', a.get('id'))}, {a.get('role', '')}.").rstrip() + templates._SPECIALIST_SUFFIX
    for a in agents:                                   # what the owner granted, before any engine (Hermes) strips or adds tools
        a["granted_tools"] = list(a.get("tools", []))
    for a in agents:                                   # desks built before the rename stored the orchestrator as "hermes"
        if a.get("id") == "hermes":
            a["id"], a["name"] = "atlas", "Atlas"
        a["color"] = _LEGACY_COLOURS.get((a.get("color") or "").lower(), a.get("color"))
    mode = _mode()
    if mode == "demo":
        providers = {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": float(os.environ.get("DEMO_DELAY", "0.6")),
                                                                        "fault_rate": float(os.environ.get("DEMO_FAULT_RATE", "0") or 0),
                                                                        "fault_sleep": float(os.environ.get("DEMO_FAULT_SLEEP", "8") or 8)}}}
        for a in agents:
            a["provider"] = "demo"
            a["model"] = ""
    else:
        providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
        for name, pc in cfg.DEFAULT_PROVIDERS["providers"].items():   # backfill presets added later
            providers.setdefault("providers", {}).setdefault(name, dict(pc))
        prov = os.environ.get("DESK_PROVIDER", "").strip() or providers.get("default_provider", "openrouter")
        byo = over.get("byo") or {}                      # the owner's own model accounts, keys encrypted at rest
        for n, blob in (byo.get("entries") or {}).items():
            e = SEC.decrypt_config(blob)
            if e.get("type"):
                providers.setdefault("providers", {})["desk:" + n] = {k: v for k, v in e.items() if k not in ("preset", "last_test")}
        if byo.get("default") and ("desk:" + byo["default"]) in (providers.get("providers") or {}):
            prov = "desk:" + byo["default"]
        providers["default_provider"] = prov
        for a in agents:
            a["provider"] = prov
        if prov == "openrouter":
            templates.apply_tier(agents, desk.get("tier") or "free")
        else:
            for a in agents:
                a["model"] = ""
        for a in agents:                               # per-agent landscape overrides beat the tier default
            ov = (over.get("models") or {}).get(a["id"])
            if not ov:
                continue
            if ov.get("engine"):
                a["engine"] = ov["engine"]
            if ov.get("model") and ov["model"] != "hermes-agent":
                a["model"] = ov["model"]
                if ov.get("provider") and ov["provider"] != "hermes_agent":
                    a["provider"] = ov["provider"]
    # Hermes Agent engine (the real Nous Research agent, via its API server). Config comes from the desk's
    # hermes_agent connector, or globally from HERMES_AGENT_URL / HERMES_AGENT_KEY env. When
    # DESK_DEFAULT_ENGINE=hermes_agent, every specialist runs on it unless the agent explicitly says engine=atlas.
    # Atlas itself stays on our loop - the API server executes its own tools and cannot call delegate/queue_action.
    hcfg: dict[str, Any] | None = None
    _conns = store.connectors(desk["id"])
    if any(c["kind"] == "camera" for c in _conns):    # desks built before cameras existed keep their stored agent list
        for a in agents:
            if a["id"] == "atlas" or "camera_look" in a.get("tools", []):
                a["tools"] = list(dict.fromkeys(list(a.get("tools", [])) + ["camera_look", "camera_events", "camera_ask"]))
    ops_on = C.enabled(desk)
    for a in agents:                                   # the lead can always redesign its team and watch videos
        if a["id"] == "atlas":
            a["tools"] = list(dict.fromkeys(list(a.get("tools", [])) + ["assemble_team", "video_describe"] + T.RECORD_TOOLS
                                            + (T.CASE_TOOLS if ops_on else [])))
        elif any(t in a.get("tools", []) for t in RECORD_GRANT_WITH):
            a["tools"] = list(dict.fromkeys(list(a["tools"]) + T.RECORD_TOOLS))   # anyone who touches customers or cameras
                                                                                   # can read and save business records
        if "camera_events" in a.get("tools", []) and "camera_ask" not in a["tools"]:   # desks built before the RAG tool existed
            a["tools"] = list(a["tools"]) + ["camera_ask"]
    deny = set(business.get("deny_tools") or [])     # e.g. a security desk: log text is attacker-controlled, so no code,
    if deny:                                          # browser, raw HTTP or MCP tools, whatever a stored roster says
        for a in agents:
            a["tools"] = [t for t in a.get("tools", []) if t not in deny]
            a["granted_tools"] = [t for t in a.get("granted_tools", []) if t not in deny]
    no_hermes = bool(business.get("no_hermes_engine"))
    hermes_off = "Hermes Agent runtime is off on this desk (security desk: no shell or browser tools)"
    hconn = next((c for c in _conns if c["kind"] == "hermes_agent"), None)
    if hconn:
        hcfg = hconn["config"]
    elif os.environ.get("HERMES_AGENT_URL", "").strip():
        hcfg = {"base_url": os.environ["HERMES_AGENT_URL"].strip(), "api_key": os.environ.get("HERMES_AGENT_KEY", "").strip(),
                "session_prefix": "atlas"}
    default_hermes = os.environ.get("DESK_DEFAULT_ENGINE", "").strip().lower() == "hermes_agent" and mode != "demo"
    # The LEAD may also run on the Hermes Agent runtime (Model landscape engine override for atlas, or
    # DESK_LEAD_ENGINE=hermes_agent). Hermes ignores client tool schemas, so the lead keeps our desk tools through
    # the <atlas>{...}</atlas> text protocol (orchestrator.parse_text_calls) - policy and approvals unchanged.
    lead_ov = (over.get("models") or {}).get("atlas") or {}
    lead_hermes = mode != "demo" and (lead_ov.get("engine") == "hermes_agent"
                                      or os.environ.get("DESK_LEAD_ENGINE", "").strip().lower() == "hermes_agent")
    for a in agents:
        if no_hermes:                                  # the Hermes runtime brings its own shell/browser tools: never here
            if (a["id"] == "atlas" and lead_hermes) or (a["id"] != "atlas" and (
                    a.get("engine") == "hermes_agent" or (default_hermes and a.get("engine") != "atlas"))):
                a["engine_note"] = hermes_off
            if a.get("engine") == "hermes_agent":
                a["engine"] = "atlas"
            continue
        if a["id"] == "atlas":
            if lead_hermes and hcfg:
                tier_cfg = templates.TIERS.get(desk.get("tier") or "free", templates.TIERS["free"])
                hmodel = (lead_ov.get("model") if lead_ov.get("model") and lead_ov["model"] != "hermes-agent"
                          else tier_cfg.get("hermes_model", templates.FREE_MODEL))
                providers.setdefault("providers", {})["hermes_agent_atlas"] = {
                    "type": "hermes_agent", "base_url": hcfg.get("base_url", ""), "api_key": hcfg.get("api_key", ""),
                    "default_model": hmodel,
                    "session_key": f"{hcfg.get('session_prefix') or 'atlas'}:desk{desk['id']}:atlas"}
                a["provider"], a["model"], a["engine"], a["text_tools"] = "hermes_agent_atlas", hmodel, "hermes_agent", True
            elif lead_hermes:
                a["engine_note"] = "no Hermes Agent instance configured - lead running on the built-in engine"
            continue
        wants = a.get("engine") == "hermes_agent" or (default_hermes and a.get("engine") != "atlas")
        if not wants:
            continue
        if not hcfg:
            a["engine_note"] = "no Hermes Agent instance configured - running on the built-in engine"
            continue
        pname = f"hermes_agent_{a['id']}"
        # the tier decides which model runs INSIDE the Hermes runtime (free MiniMax / Haiku / Sonnet); a per-agent
        # override from the Model landscape wins if it names a real model
        tier_cfg = templates.TIERS.get(desk.get("tier") or "free", templates.TIERS["free"])
        ov = (over.get("models") or {}).get(a["id"]) or {}
        hmodel = ov.get("model") if ov.get("model") and ov["model"] != "hermes-agent" else tier_cfg.get("hermes_model", templates.FREE_MODEL)
        providers.setdefault("providers", {})[pname] = {"type": "hermes_agent", "base_url": hcfg.get("base_url", ""),
                                                         "api_key": hcfg.get("api_key", ""), "default_model": hmodel,
                                                         "session_key": f"{hcfg.get('session_prefix') or 'atlas'}:desk{desk['id']}:{a['id']}"}
        a["provider"], a["model"], a["tools"], a["engine"] = pname, hmodel, [], "hermes_agent"
    return {"providers": providers, "orchestration": cfg.load("orchestration", cfg.DEFAULT_ORCHESTRATION),
            "business": business, "agents": agents, "workflows": workflows, "ui": {}, "mode": mode,
            "desk_id": desk["id"], "case_id": None, "cyber": (desk.get("config") or {}).get("cyber") or {}}


RECORD_GRANT_WITH = ("crm_lookup", "crm_update", "camera_events", "camera_ask", "camera_look", "browse", "http_request",
                     "calendar_book", "queue_action")


def ensure_demo_desk() -> dict[str, Any]:
    d = store.desk(1)
    if d is None:                                      # DESK_TEMPLATE picks the built-in desk's template (default sales_desk)
        name = "Acme Estates" if DEFAULT_TEMPLATE == "sales_desk" else templates.get(DEFAULT_TEMPLATE)["business"].get("name", "Demo desk")
        d = store.add_desk(0, name, DEFAULT_TEMPLATE, "free", templates.build_desk(DEFAULT_TEMPLATE, {}))
    return d


# ---------------------------------------------------------------------------- auth + desk context
PUBLIC_API = {"/api/health", "/api/stats", "/api/me", "/api/vision/demo", "/api/vision/demo/ask", "/api/orch/live"}


def current_user() -> dict[str, Any] | None:
    uid = session.get("uid")
    return store.user(int(uid)) if uid else None


def current_desk() -> dict[str, Any] | None:
    u = current_user()
    if OPEN and not u:                     # testing mode: any desk, no account
        did = session.get("desk")
        d = store.desk(int(did)) if did else None
        return d or (store.all_desks() or [ensure_demo_desk()])[0]
    if not u:
        return None
    did = session.get("desk")
    d = store.desk(int(did)) if did else None
    if d and d["owner_id"] == u["id"]:
        return d
    ds_ = store.desks_for(u["id"])
    if ds_:
        session["desk"] = ds_[0]["id"]
        return ds_[0]
    return None


def need_desk() -> dict[str, Any]:
    d = current_desk()
    if not d:
        abort(Response(json.dumps({"error": "no_desk"}), 409, mimetype="application/json"))
    return d


def ds():
    return store.for_desk(need_desk()["id"])


from .. import secure as SEC

_RL_HOOK = SEC.RateLimiter(int(os.environ.get("RL_HOOK_PER_MIN", "30")), 60)          # per token+IP
_RL_AUTH = SEC.RateLimiter(int(os.environ.get("RL_AUTH_PER_10MIN", "20")), 600)       # per IP: login/signup attempts
_RL_MODEL = SEC.RateLimiter(int(os.environ.get("RL_MODEL_PER_MIN", "20")), 60)        # per IP: model-backed design calls
_RL_VISION = SEC.RateLimiter(int(os.environ.get("RL_VISION_PER_MIN", "12")), 60)      # per IP: public site vision demo
_VISION_SLOTS = threading.BoundedSemaphore(int(os.environ.get("VISION_DEMO_CONCURRENCY", "3") or 3))


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=(), payment=()")   # camera: the site's vision demo
    if os.environ.get("RENDER"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.before_request
def _rate_limits():
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    p = request.path
    if p.startswith("/hook/") and not _RL_HOOK.allow(f"{p}:{ip}"):
        return Response(json.dumps({"error": "rate limited"}), 429, mimetype="application/json")
    if request.method == "POST" and p in ("/login", "/signup", "/desk/login") and not _RL_AUTH.allow(ip):
        return Response(json.dumps({"error": "too many attempts - wait a few minutes"}), 429, mimetype="application/json")
    if request.method == "POST" and p.startswith("/api/design/") and (p.endswith("/say") or p.endswith("/study")) and not _RL_MODEL.allow(ip):
        return Response(json.dumps({"error": "rate limited - slow down a little"}), 429, mimetype="application/json")
    return None


@app.before_request
def _guard():
    if OPEN:                                   # open test mode: no accounts anywhere, auth pages go straight to the desk
        if request.method == "GET" and request.path in ("/login", "/signup", "/desk/login"):
            return redirect("/desk")
        return None
    p = request.path
    if p.startswith("/hook/"):
        return None
    if p.startswith("/api") and p not in PUBLIC_API:
        if not current_user():
            abort(401)
    elif p.startswith("/desk") and not p.startswith("/desk/static/") and p != "/desk/login":
        if not current_user():
            return redirect("/login?next=" + p)
    return None


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _page(template: str, **vars_) -> str:
    html = (STATIC_DIR / template).read_text(encoding="utf-8")
    for k, v in vars_.items():
        html = html.replace("{{%s}}" % k, _esc(str(v)))
    return html


@app.route("/login", methods=["GET", "POST"])
@app.route("/desk/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        d = request.form if request.form else (request.get_json(silent=True) or {})
        email = (d.get("email") or "").strip().lower()
        pw = d.get("password") or ""
        u = store.user_by_email(email) if email else None
        if not u or not check_password_hash(u["pw_hash"], pw):
            time.sleep(0.8)   # slow down guessing
            if request.is_json:
                return jsonify({"error": "wrong email or password"}), 401
            return _page("login.html", error="Wrong email or password.", email=email), 401
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        store.touch_login(u["id"])
        if request.is_json:
            return jsonify({"ok": True})
        nxt = request.args.get("next") or _front_door(u)
        return redirect(nxt if nxt.startswith("/desk") else _front_door(u))
    if current_user():
        return redirect(_front_door(current_user()))
    return _page("login.html", error="", email="")


def _front_door(u: dict[str, Any] | None) -> str:
    """Owners with a desk land on its Home; new owners start in the workspace chat, where Atlas builds the first one."""
    return "/desk" if u and store.desks_for(u["id"]) else "/desk/workspace"


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        d = request.form if request.form else (request.get_json(silent=True) or {})
        name = (d.get("name") or "").strip()
        company = (d.get("company") or "").strip()
        email = (d.get("email") or "").strip().lower()
        pw = d.get("password") or ""
        err = ""
        if not name or not email or "@" not in email:
            err = "Name and a valid email are required."
        elif len(pw) < 8:
            err = "Password must be at least 8 characters."
        elif store.user_by_email(email):
            err = "An account with that email already exists — log in instead."
        if err:
            if request.is_json:
                return jsonify({"error": err}), 400
            return _page("signup.html", error=err, name=name, company=company, email=email), 400
        u = store.add_user(email, name, company, generate_password_hash(pw))
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        if request.is_json:
            return jsonify({"ok": True})
        return redirect("/desk/workspace")                     # new owners start in the workspace chat
    if current_user():
        return redirect(_front_door(current_user()))
    return _page("signup.html", error="", name="", company="", email="")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/")


@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(u or {"id": None, "name": "Guest", "email": "", "company": "", "open": OPEN})


from . import showrun as _SR

_SHOW = _SR.ShowRunner({"store": store, "desk_configs": desk_configs, "mode": _mode, "blocked": _live_reason,
                        "templates": templates, "template": DEFAULT_TEMPLATE})


@app.get("/api/orch/live")
def api_orch_live():
    """Public: the site hero's live orchestration feed (a real run on the free model while people watch).
    ?since=<n> returns events after index n; ?run=<id> lets the client notice a new run. Polling, not SSE, so it
    never holds a gunicorn thread."""
    try:
        since = int(request.args.get("since", "0") or 0)
    except ValueError:
        since = 0
    resp = jsonify(_SHOW.snapshot(since, request.args.get("run", "")))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/health")
def api_health():
    reason = _live_reason()
    return jsonify({"ok": True, "mode": _mode(), "live_ready": not reason, "live_reason": reason,
                    "hermes_agent": bool(os.environ.get("HERMES_AGENT_URL", "").strip()),
                    "db": store.backend, "db_ok": store.ping()})


# ---------------------------------------------------------------------------- static
def _site_page(name: str):
    """Serve a marketing page. In open test mode every Log in / Sign up link opens the desk directly."""
    if not OPEN or not name.endswith(".html"):
        return send_from_directory(SITE_DIR, name)
    html = (SITE_DIR / name).read_text(encoding="utf-8")
    html = re.sub(r'href="/(?:login|signup)(?:\?[^"]*)?"', 'href="/desk"', html)
    html = re.sub(r">\s*Log in\s*<", ">Open desk<", html)
    html = re.sub(r">\s*Sign up(?: free)?\s*<", ">Open the desk<", html)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/")
def site_index():
    return _site_page("index.html")


@app.route("/site/<path:path>")
def site_files(path):
    return _site_page(path) if (SITE_DIR / path).is_file() else abort(404)


@app.route("/<path:path>")
def site_root_files(path):
    if (SITE_DIR / path).is_file():
        return _site_page(path)
    abort(404)


@app.route("/desk")
@app.route("/desk/")
def desk_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/desk/static/<path:path>")
def desk_static(path):
    resp = send_from_directory(STATIC_DIR, path)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------- api: desks (onboarding + switching)
def _desk_public(d: dict[str, Any]) -> dict[str, Any]:
    b = (d.get("config") or {}).get("business") or {}
    from .. import alerts as AL
    return {"id": d["id"], "name": d["name"], "template": d["template"], "tier": d.get("tier", "free"),
            "business_name": b.get("name", d["name"]), "created": d.get("created"),
            "ui_level": (d.get("config") or {}).get("ui_level") or "simple", "notify": AL.config(d)}


# tools a specialist may be given from the Team page. Everything else in ORCHESTRATOR_ONLY stays with Atlas;
# finish / assemble_team are never assignable. Approvals still gate every outbound tool.
SPECIALIST_OK = {"crm_lookup", "crm_update", "queue_action", "camera_look", "camera_events", "camera_ask", "remember", "recall",
                 "browse", "http_request", "calendar_free_slots", "calendar_book", "generate_media", "video_describe",
                 "log_search", "enrich", "correlate", "record_find", "record_get", "record_save"}
NEVER_ASSIGN = {"finish", "assemble_team"}
_COLOURS = ["#7c3aed", "#1f9d63", "#db2777", "#ea580c", "#b45309", "#0891b2"]


def _clean_roster(raw: Any, current: list[dict[str, Any]]) -> list[dict[str, Any]] | str:
    """Validate a roster from the portal. Returns the cleaned list or an error string."""
    if not isinstance(raw, list) or not raw:
        return "agents must be a non-empty list"
    prev = {a["id"]: a for a in current}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, a in enumerate(raw):
        if not isinstance(a, dict):
            return f"agent #{i + 1} is not an object"
        aid = re.sub(r"[^a-z0-9_]+", "_", str(a.get("id") or a.get("name") or "").strip().lower()).strip("_")[:24]
        if not aid:
            return f"agent #{i + 1} needs a name"
        if aid in seen:
            return f"two agents share the id '{aid}'"
        seen.add(aid)
        name = str(a.get("name") or aid).strip()[:40]
        role = str(a.get("role") or "").strip()[:200]
        prompt = str(a.get("system_prompt") or "").strip()[:6000]
        tools_in = [str(t) for t in (a.get("tools") or []) if isinstance(t, str)]
        if aid == "atlas":
            tools = [t for t in tools_in if t in T.SCHEMAS or t == "mcp"]
            for must in ("delegate", "list_agents", "save_deliverable", "read_file", "list_files"):
                if must not in tools:
                    tools.append(must)
            prompt = prompt or templates._BASE_ATLAS_PROMPT
            enabled = True
        else:
            tools = [t for t in tools_in if (t in T.SCHEMAS and (t not in T.ORCHESTRATOR_ONLY or t in SPECIALIST_OK)) or t == "mcp"]
            tools = [t for t in tools if t not in NEVER_ASSIGN]
            enabled = bool(a.get("enabled", True))
        colour = str(a.get("color") or "").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
            colour = prev.get(aid, {}).get("color") or ("#0b5fcb" if aid == "atlas" else _COLOURS[len(out) % len(_COLOURS)])
        keep = prev.get(aid, {})
        ent = {"id": aid, "name": name, "role": role, "enabled": enabled, "provider": keep.get("provider", ""),
               "model": keep.get("model", "") if aid != "atlas" else "", "tools": tools, "system_prompt": prompt, "color": colour}
        if keep.get("engine") in ("atlas", "hermes_agent"):
            ent["engine"] = keep["engine"]
        if a.get("engine") in ("atlas", "hermes_agent"):
            ent["engine"] = a["engine"]
        if aid != "atlas":
            rt = re.sub(r"[^a-z0-9_]+", "_", str(a.get("reports_to") if "reports_to" in a else keep.get("reports_to") or "atlas").strip().lower()).strip("_")[:24]
            ent["reports_to"] = rt or "atlas"
            for k in ("goal", "instructions"):
                v = a.get(k) if k in a else keep.get(k)
                if v:
                    ent[k] = v if k == "goal" else [str(x)[:240] for x in v][:8]
        pos = a.get("pos") if isinstance(a.get("pos"), dict) else keep.get("pos")
        if isinstance(pos, dict):
            try:
                ent["pos"] = {"x": max(0.0, min(8000.0, float(pos.get("x")))), "y": max(0.0, min(8000.0, float(pos.get("y"))))}
            except (TypeError, ValueError):
                pass
        out.append(ent)
    if "atlas" not in seen:
        return "the roster must include Atlas (id 'atlas')"
    out.sort(key=lambda x: 0 if x["id"] == "atlas" else 1)
    # hierarchy (shared contract with atlas/team.py): unknown lead / cycle / too deep -> atlas; leads can delegate to members
    shape, _errs = TM.validate_team({"agents": [{**a, "instructions": a.get("instructions") or ["as briefed", "as briefed"]}
                                                for a in out if a["id"] != "atlas"]},
                                    allowed_tools=[t for t in T.SCHEMAS if t not in NEVER_ASSIGN and t != "delegate"] + ["mcp"])
    struct = {s["id"]: s for s in shape["agents"]}
    for a in out:
        if a["id"] == "atlas":
            continue
        s = struct.get(a["id"], {})
        a["reports_to"] = s.get("reports_to", "atlas")
        a["members"] = list(s.get("members") or [])
        a["tools"] = [t for t in a["tools"] if t not in ("delegate", "list_agents")]
        if a["members"]:
            a["tools"] = list(dict.fromkeys(["delegate", "list_agents"] + a["tools"]))
    return out


@app.get("/api/templates")
def api_templates():
    return jsonify({"desks": templates.DESK_TYPES, "tiers": [{"id": k, **v} for k, v in templates.TIERS.items()],
                    "mode": _mode()})


@app.get("/api/desks")
def api_desks():
    u = current_user()
    rows = store.desks_for(u["id"]) if u else ((store.all_desks() or [ensure_demo_desk()]) if OPEN else [])
    cur = current_desk()
    return jsonify({"desks": [_desk_public(d) for d in rows], "current": cur["id"] if cur else None})


@app.post("/api/desks")
def api_create_desk():
    u = current_user()
    if not u and not OPEN:
        abort(401)
    d = request.get_json(force=True) or {}
    template = d.get("template") if d.get("template") in templates.BUILTIN else DEFAULT_TEMPLATE
    tier = d.get("tier") if d.get("tier") in templates.TIERS else "free"
    name = (d.get("name") or "").strip() or "My desk"
    conf = templates.build_desk(template, d)
    desk = store.add_desk(u["id"] if u else 0, name, template, tier, conf)
    session["desk"] = desk["id"]
    return jsonify(_desk_public(desk))


@app.post("/api/desks/<int:did>/select")
def api_select_desk(did):
    u = current_user()
    d = store.desk(did)
    if not d or (u and d["owner_id"] != u["id"] and not OPEN) or (not u and not OPEN):
        abort(404)
    session["desk"] = did
    return jsonify(_desk_public(d))


@app.patch("/api/desks/<int:did>")
def api_update_desk(did):
    u = current_user()
    d = store.desk(did)
    if not d or (u and d["owner_id"] != u["id"] and not OPEN):
        abort(404)
    body = request.get_json(force=True) or {}
    fields: dict[str, Any] = {}
    if body.get("tier") in templates.TIERS:
        fields["tier"] = body["tier"]
    if body.get("name"):
        fields["name"] = str(body["name"]).strip()
    if isinstance(body.get("business"), dict):
        conf = d.get("config") or {}
        conf["business"] = {**(conf.get("business") or {}), **body["business"]}
        fields["config"] = conf
    if body.get("ui_level") in ("simple", "full"):
        conf = fields.get("config") or d.get("config") or {}
        conf["ui_level"] = body["ui_level"]
        fields["config"] = conf
    if isinstance(body.get("notify"), dict):
        from .. import alerts as AL
        n = AL.clean(body["notify"])
        if isinstance(n, str):
            return jsonify({"error": n}), 400
        conf = fields.get("config") or d.get("config") or {}
        conf["notify"] = n
        fields["config"] = conf
    if body.get("reset_agents"):
        conf = fields.get("config") or d.get("config") or {}
        conf.pop("agents", None)
        fields["config"] = conf
    elif "agents" in body:
        conf = fields.get("config") or d.get("config") or {}
        cur = conf.get("agents") or templates.get(d.get("template") or DEFAULT_TEMPLATE)["agents"]
        roster = _clean_roster(body["agents"], cur)
        if isinstance(roster, str):
            return jsonify({"error": roster}), 400
        conf["agents"] = roster
        fields["config"] = conf
    if isinstance(body.get("models"), dict):           # per-agent model landscape: {agent_id: {model, provider, engine}}
        conf = fields.get("config") or d.get("config") or {}
        cur = conf.get("models") or {}
        for aid, ov in body["models"].items():
            if not isinstance(ov, dict):
                continue
            ent = {k: str(ov[k]) for k in ("model", "provider", "engine") if ov.get(k)}
            if ent:
                cur[str(aid)] = ent
            else:
                cur.pop(str(aid), None)
        conf["models"] = cur
        fields["config"] = conf
    store.update_desk(did, **fields)
    if isinstance(body.get("notify"), dict):
        from .. import alerts as AL
        AL.ensure_report_job(store, store.desk(did))
    return jsonify(_desk_public(store.desk(did)))


def _team_tree(agents: list[dict[str, Any]]) -> str:
    by_id = {a["id"]: a for a in agents}
    nested = {m for a in agents for m in (a.get("members") or [])}
    lines = ["atlas: Atlas"]
    for a in agents:
        if a["id"] == "atlas" or a["id"] in nested:
            continue
        lines.append(f"  - {a['id']}: {a['name']} — {a.get('role', '')}" + (f" (leads {', '.join(a['members'])})" if a.get("members") else ""))
        for m in a.get("members") or []:
            if m in by_id:
                lines.append(f"      - {m}: {by_id[m]['name']} — {by_id[m].get('role', '')}")
    return "\n".join(lines)


def _team_context(desk: dict[str, Any]) -> tuple[dict[str, Any], list[str], bool, list[str]]:
    configs = desk_configs(desk)
    conns = store.connectors(desk["id"])
    cams = [c["name"] for c in conns if c["kind"] == "camera"]
    cams += [h["name"] for h in store.hook_cameras(desk["id"], time.time() - 7 * 86400, tuple(cams)) if h.get("name")]
    hermes = configs["mode"] != "demo" and (any(c["kind"] == "hermes_agent" for c in conns) or bool(os.environ.get("HERMES_AGENT_URL", "").strip()))
    hermes = hermes and not configs["business"].get("no_hermes_engine")
    deny = set(configs["business"].get("deny_tools") or [])
    allowed = [t for t in TM.ALLOWED_TOOLS if t not in deny]   # camera tools stay designable before a camera is added
    return configs, cams, hermes, allowed


def _design_team_for(desk: dict[str, Any], task: str, reuse: bool = False) -> dict[str, Any]:
    configs, cams, hermes, allowed = _team_context(desk)
    if configs["mode"] == "demo":
        team = TM.demo_team(configs["business"], task, cams)
        res: dict[str, Any] = {"team": team, "errors": [], "warnings": [], "turns": 0}
    else:
        _require_live(desk)
        from ..providers import ProviderPool
        atlas_agent = next((a for a in configs["agents"] if a["id"] == "atlas"), {})
        prov = ProviderPool(configs["providers"]).get(atlas_agent.get("provider") or "")
        existing = [a for a in configs["agents"] if a["id"] != "atlas"] if reuse else None
        res = TM.design_team(configs["business"], task, prov, atlas_agent.get("model", "") or "", allowed_tools=allowed,
                             hermes_available=hermes, cameras=cams, existing=existing)
    res["tree"] = TM.tree(res["team"])
    res["mode"] = configs["mode"]
    return res


def _apply_team(desk: dict[str, Any], team_raw: Any) -> dict[str, Any] | str:
    configs, cams, hermes, allowed = _team_context(desk)
    team, errors = TM.validate_team(team_raw, allowed_tools=allowed, hermes_available=hermes)
    if not team["agents"]:
        return "team has no agents"
    conf = desk.get("config") or {}
    cur_atlas = next((a for a in (conf.get("agents") or []) if a.get("id") == "atlas"), None)
    agents = TM.team_to_agents(team, configs["business"], desk.get("tier") or "free", keep_atlas=cur_atlas)
    for a in agents:                                   # stored raw; desk_configs re-applies tier + engine at run time
        a.pop("model", None)
    conf["agents"] = agents
    conf["team"] = team
    wf = TM.workflow_for(team)
    if wf:
        wfs = [w for w in (conf.get("workflows") or templates.get(desk.get("template") or DEFAULT_TEMPLATE)["workflows"]) if w.get("id") != wf["id"]]
        conf["workflows"] = wfs + [wf]
    store.update_desk(desk["id"], config=conf)
    return {"team": team, "errors": errors, "agents": [a["id"] for a in agents], "tree": TM.tree(team),
            "workflow": wf["id"] if wf else ""}


def _owned_desk(did: int) -> dict[str, Any]:
    u = current_user()
    d = store.desk(did)
    if not d or (u and d["owner_id"] != u["id"] and not OPEN):
        abort(404)
    return d


# ---- the owner's own model accounts: paste an OpenAI / Anthropic / OpenRouter key (or point at any
# OpenAI-compatible server) and the desk's agents run on it. Keys are encrypted at rest and never echoed back.
BYO_PRESETS: dict[str, dict[str, Any]] = {
    "anthropic":  {"type": "anthropic", "label": "Claude (Anthropic API)", "key_hint": "sk-ant-…",
                   "default_model": "claude-sonnet-4-5", "models": ["claude-opus-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
                   "effort": "high", "thinking": "adaptive", "fallbacks": True, "max_tokens": 16000, "timeout": 600},
    "openai":     {"type": "openai", "label": "OpenAI (your ChatGPT / Codex API account)", "key_hint": "sk-…",
                   "base_url": "https://api.openai.com/v1", "default_model": "gpt-5.4-mini",
                   "models": ["gpt-5.4", "gpt-5.4-mini"], "temperature": 0.2, "max_tokens": 8000, "timeout": 600},
    "openrouter": {"type": "openai", "label": "OpenRouter (your account, any model)", "key_hint": "sk-or-…",
                   "base_url": "https://openrouter.ai/api/v1", "default_model": "anthropic/claude-sonnet-4.5",
                   "models": [],                       # filled from MODEL_CATALOG below, once it exists
                   "temperature": 0.2, "max_tokens": 8000, "timeout": 600},
    "custom":     {"type": "openai", "label": "Your own server (OpenAI-compatible: vLLM, Ollama, TGI, a rented GPU)",
                   "key_hint": "key, if the server wants one", "default_model": "", "models": [],
                   "temperature": 0.2, "max_tokens": 8000, "timeout": 600},
}


def _own_desk(did: int) -> dict[str, Any]:
    u = current_user()
    d = store.desk(did)
    if not d or (u and d["owner_id"] != u["id"] and not OPEN):
        abort(404)
    return d


def _byo_public(name: str, e: dict[str, Any]) -> dict[str, Any]:
    key = e.get("api_key") or ""
    return {"name": name, "preset": e.get("preset") or "custom",
            "label": BYO_PRESETS.get(e.get("preset") or "", {}).get("label", name),
            "base_url": e.get("base_url", ""), "model": e.get("default_model", ""),
            "key_hint": ("…" + key[-4:]) if len(key) >= 8 else ("set" if key else ""),
            "last_test": e.get("last_test") or None}


@app.get("/api/desks/<int:did>/providers")
def api_desk_providers(did):
    d = _own_desk(did)
    byo = _desk_byo(d)
    entries = [_byo_public(n, SEC.decrypt_config(b)) for n, b in (byo.get("entries") or {}).items()]
    return jsonify({"default": byo.get("default") or "", "entries": entries,
                    "presets": [{"id": k, "label": v["label"], "key_hint": v["key_hint"], "models": v["models"],
                                 "default_model": v["default_model"], "needs_url": k == "custom"} for k, v in BYO_PRESETS.items()],
                    "house_reason": _live_reason()})


@app.post("/api/desks/<int:did>/providers")
def api_desk_provider_set(did):
    d = _own_desk(did)
    b = request.get_json(force=True) or {}
    preset = str(b.get("preset") or "")
    if preset not in BYO_PRESETS:
        return jsonify({"error": "unknown preset"}), 400
    p = BYO_PRESETS[preset]
    conf = d.get("config") or {}
    byo = conf.setdefault("byo", {})
    old = SEC.decrypt_config((byo.get("entries") or {}).get(preset)) if (byo.get("entries") or {}).get(preset) else {}
    key = str(b.get("api_key") or "").strip() or old.get("api_key", "")   # keep the stored key when the field is left blank
    if preset != "custom" and not key:
        return jsonify({"error": "paste the API key"}), 400
    base = str(b.get("base_url") or "").strip() or p.get("base_url", "")
    if preset == "custom":
        if not base.startswith(("http://", "https://")):
            return jsonify({"error": "give the server's base URL (https://…/v1)"}), 400
        why = SEC.private_url_reason(base)
        if why:
            return jsonify({"error": f"that URL is not reachable from here: {why}"}), 400
    entry = {k: v for k, v in p.items() if k not in ("label", "key_hint", "models")}
    entry.update({"preset": preset, "api_key": key, "base_url": base,
                  "default_model": str(b.get("model") or "").strip() or p["default_model"]})
    if old.get("last_test"):
        entry["last_test"] = old["last_test"]
    byo.setdefault("entries", {})[preset] = SEC.encrypt_config(entry)
    if b.get("make_default") or not byo.get("default"):
        byo["default"] = preset
    store.update_desk(d["id"], config=conf)
    return jsonify({"ok": True, "entry": _byo_public(preset, entry), "default": byo.get("default") or ""})


@app.post("/api/desks/<int:did>/providers/<name>/test")
def api_desk_provider_test(did, name):
    d = _own_desk(did)
    byo = _desk_byo(d)
    blob = (byo.get("entries") or {}).get(name)
    if not blob:
        abort(404)
    e = SEC.decrypt_config(blob)
    from ..providers import make_provider
    pcfg = {k: v for k, v in e.items() if k not in ("preset", "last_test")}
    pcfg["max_tokens"] = 64
    pcfg["timeout"] = 45
    try:
        note = make_provider(pcfg).test(e.get("default_model") or "")
        ok = True
    except Exception as exc:
        note, ok = f"{type(exc).__name__}: {str(exc)[:240]}", False
    e["last_test"] = {"ok": ok, "note": note, "ts": time.time()}
    conf = d.get("config") or {}
    conf.setdefault("byo", {}).setdefault("entries", {})[name] = SEC.encrypt_config(e)
    store.update_desk(d["id"], config=conf)
    return jsonify({"ok": ok, "note": note})


@app.post("/api/desks/<int:did>/providers/default")
def api_desk_provider_default(did):
    d = _own_desk(did)
    name = str((request.get_json(force=True) or {}).get("name") or "")
    conf = d.get("config") or {}
    byo = conf.setdefault("byo", {})
    if name and name not in (byo.get("entries") or {}):
        abort(404)
    byo["default"] = name                              # "" = back to the house models
    store.update_desk(d["id"], config=conf)
    return jsonify({"ok": True, "default": name})


@app.delete("/api/desks/<int:did>/providers/<name>")
def api_desk_provider_del(did, name):
    d = _own_desk(did)
    conf = d.get("config") or {}
    byo = conf.get("byo") or {}
    if name not in (byo.get("entries") or {}):
        abort(404)
    byo["entries"].pop(name)
    if byo.get("default") == name:
        byo["default"] = next(iter(byo["entries"]), "")
    store.update_desk(d["id"], config=conf)
    return jsonify({"ok": True, "default": byo.get("default") or ""})


@app.post("/api/desks/<int:did>/team/design")
def api_team_design(did):
    """Design a team for a job (business + task -> validated hierarchy). Nothing is saved until /team/apply."""
    d = _owned_desk(did)
    body = request.get_json(force=True) or {}
    task = str(body.get("task") or "").strip()
    if not task:
        return jsonify({"error": "task required"}), 400
    try:
        res = _design_team_for(d, task, bool(body.get("reuse")))
    except Exception as exc:
        return jsonify({"error": f"design failed: {type(exc).__name__}: {str(exc)[:200]}"}), 502
    return jsonify({k: res[k] for k in ("team", "tree", "errors", "warnings", "turns", "mode")})


@app.post("/api/desks/<int:did>/team/apply")
def api_team_apply(did):
    d = _owned_desk(did)
    body = request.get_json(force=True) or {}
    res = _apply_team(d, body.get("team") if isinstance(body.get("team"), dict) else body)
    if isinstance(res, str):
        return jsonify({"error": res}), 400
    return jsonify(res)


# curated model catalogue for the landscape picker. `tools` = supports native tool-calling on OpenRouter.
MODEL_CATALOG = [
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "label": "Nemotron 3 Super (free)", "provider": "openrouter", "tools": True, "cost": "free tier", "engine": "atlas",
     "note": "the free text default since 24 Sep 2026 (Ling 3.0 Flash VL free was withdrawn, MiniMax M3 free before it)"},
    {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "label": "Nemotron 3 Nano Omni (free, vision)", "provider": "openrouter", "tools": True, "vision": True, "cost": "free tier", "engine": "atlas",
     "note": "free eyes (VISION_MODEL default); too flaky for chat - empty replies"},
    {"id": "nex-agi/nex-n2.5-mini:free", "label": "Nex N2.5 Mini (free, vision)", "provider": "openrouter", "tools": True, "vision": True, "cost": "free tier", "engine": "atlas",
     "note": "fallback_model when the free default is rate-limited or withdrawn"},
    {"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5", "provider": "openrouter", "tools": True, "vision": True, "cost": "≈£2.3/M in", "engine": "atlas", "paid": True},
    {"id": "anthropic/claude-haiku-4.5", "label": "Claude Haiku 4.5", "provider": "openrouter", "tools": True, "vision": True, "cost": "≈£0.8/M in", "engine": "atlas", "paid": True},
    {"id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash (vision)", "provider": "openrouter", "tools": True, "vision": True, "cost": "≈£0.25/M in", "engine": "atlas", "paid": True,
     "note": "cheap eyes - good default for camera_look / video_describe (set VISION_MODEL to use it for vision calls)"},
    {"id": "qwen/qwen2.5-vl-72b-instruct", "label": "Qwen 2.5 VL 72B (vision)", "provider": "openrouter", "tools": False, "vision": True, "cost": "≈£0.5/M in", "engine": "atlas", "paid": True,
     "note": "strong open-weights VLM; no native tool-calling - use as VISION_MODEL, not as an agent brain"},
    {"id": "nousresearch/hermes-4-70b", "label": "Nous Hermes 4 70B", "provider": "openrouter", "tools": False, "cost": "≈£0.1/M in", "engine": "atlas", "paid": True,
     "note": "no native tool-calling on OpenRouter - best used INSIDE the Hermes Agent runtime"},
    {"id": "hermes-agent", "label": "Hermes Agent runtime (own tools, memory, skills)", "provider": "hermes_agent", "tools": True, "cost": "runtime + its model", "engine": "hermes_agent"},
]


BYO_PRESETS["openrouter"]["models"] = [m["id"] for m in MODEL_CATALOG if m.get("provider") == "openrouter"]


@app.get("/api/models")
def api_models():
    return jsonify({"models": MODEL_CATALOG, "paid_unlocked": not _free_tier_hint()})


def _free_tier_hint() -> bool:
    return True                                        # flips once the OpenRouter account has credits; informational only


# ---------------------------------------------------------------------------- api: overview
@app.get("/api/config")
def api_config():
    desk = current_desk()
    if not desk:
        return jsonify({"mode": _mode(), "needs_desk": True, "protected": not OPEN})
    c = desk_configs(desk)
    reason = _live_reason(desk)
    return jsonify({
        "mode": c["mode"], "live_ready": not reason, "live_reason": reason,
        "template": desk["template"], "tier": desk.get("tier", "free"), "business": c["business"],
        "desk": _desk_public(desk),
        "ui_level": (desk.get("config") or {}).get("ui_level") or "simple",
        "custom_roster": bool((desk.get("config") or {}).get("agents")),
        "tools": [{"id": n, "description": (sc.get("description") or "").split(". ")[0][:140], "orchestrator_only": n in T.ORCHESTRATOR_ONLY}
                  for n, sc in T.SCHEMAS.items() if n not in NEVER_ASSIGN] + [{"id": "mcp", "description": "Tools from connected MCP servers", "orchestrator_only": False}],
        "team_tree": _team_tree(c["agents"]),
        "agents": [{"id": a["id"], "name": a["name"], "role": a.get("role", ""), "color": a.get("color", ""),
                    "reports_to": (a.get("reports_to") or "atlas") if a["id"] != "atlas" else "",
                    "members": list(a.get("members") or []), "goal": a.get("goal", ""), "instructions": list(a.get("instructions") or []),
                    "tools": a.get("granted_tools") or a.get("tools", []), "runtime_tools": a.get("tools", []),
                    "model": a.get("model", "") or "(provider default)",
                    "engine": a.get("engine") or "atlas", "provider": a.get("provider") or "",
                    "engine_note": a.get("engine_note") or "", "enabled": a.get("enabled", True), "pos": a.get("pos"),
                    "system_prompt": (a.get("system_prompt") or "").replace(templates._SPECIALIST_SUFFIX, "")} for a in c["agents"]],
        "workflows": [{"id": w["id"], "name": w["name"], "description": w.get("description", "")} for w in c["workflows"]],
        "protected": not OPEN,
    })


@app.get("/api/stats")
def api_stats():
    desk = current_desk()
    if not desk:
        return jsonify({"leads": 0, "pending": 0, "approved": 0, "qualified": 0, "active_runs": 0, "tokens_in": 0, "tokens_out": 0, "needs_desk": True})
    s = store.stats(desk["id"])
    s["active_runs"] = sum(1 for r in _runs.values() if r["desk_id"] == desk["id"] and r["thread"].is_alive())
    try:
        s["cases"] = C.counts(store, desk["id"])
    except Exception:
        s["cases"] = {}
    return jsonify(s)


# ---------------------------------------------------------------------------- api: leads + runs
def _lead_task(lead: dict[str, Any]) -> str:
    return (f"New inbound lead — handle end to end.\n"
            f"Name: {lead['name']}\nCompany: {lead['company'] or '(individual)'}\nEmail: {lead['email']}\n"
            f"Phone: {lead['phone'] or '-'}\nSource: {lead['source']}\n\nEnquiry:\n{lead['notes']}")


_KEEP_FINISHED_S = 3600


def _prune_runs() -> None:
    """Drop in-memory holders of runs that finished over an hour ago (their events live in the DB)."""
    now = time.time()
    for rid, h in list(_runs.items()):
        if not h["thread"].is_alive() and now - h.get("ended", h.get("started", now)) > _KEEP_FINISHED_S:
            _runs.pop(rid, None)


def _preflight(desk: dict[str, Any]) -> dict[str, Any]:
    """Refuse a run the desk cannot pay for or has no model for (503 no key / 402 spend cap). Returns the configs."""
    _require_live(desk)
    configs = desk_configs(desk)
    paid = any(":free" not in (a.get("model") or "") and a.get("model") for a in configs["agents"])
    if configs["mode"] != "demo" and paid:                # free-tier desks cost nothing and are never blocked by the spend cap
        blocked = _spend_blocked()
        if blocked:
            abort(Response(json.dumps({"error": blocked}), 402, mimetype="application/json"))
    return configs


def _start_run(desk: dict[str, Any], task: str, mode: str, lead_id: int | None = None, case_id: int | None = None) -> str:
    configs = _preflight(desk)
    if case_id:
        configs["case_id"] = int(case_id)                 # approvals queued in this run belong to the case
    dstore = store.for_desk(desk["id"])
    events: list[dict[str, Any]] = []
    orch: Orchestrator
    bound = {"case": False}

    def emit(ev: Event):
        if case_id and not bound["case"] and orch.run_id:   # first event: the run has its id, before any model call
            bound["case"] = True
            try:
                C.bind_run(store, int(case_id), orch.run_id)
            except Exception:
                pass
        if ev.kind != "token":                     # deltas are live-only; the full text arrives as a `log` event
            events.append({"ts": ev.ts, "kind": ev.kind, "agent": ev.agent, "text": ev.text, "data": ev.data})
        if ev.kind == "error":
            _last_error[desk["id"]] = {"ts": ev.ts, "run_id": orch.run_id, "text": (ev.text or "")[:400]}
        _push_feed(orch.run_id, desk["id"], ev)

    orch = Orchestrator(configs, dstore, emit)
    holder: dict[str, Any] = {"events": events, "orch": orch, "task": task, "desk_id": desk["id"], "started": time.time()}

    def work():
        res = None
        try:
            res = orch.run(task, mode)
            status = res.status
        except BaseException as exc:               # the orchestrator already catches run errors; this is the last line of defence
            status = "error"
            msg = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                dstore.finish_run(orch.run_id, "error", "crashed: " + msg, orch.tokens_in, orch.tokens_out)
            except Exception:
                pass
            _push_feed(orch.run_id, desk["id"], Event(kind="error", agent="system", text="run crashed: " + msg))
            _push_feed(orch.run_id, desk["id"], Event(kind="done", agent="system", text=msg, data={"status": "error"}))
        holder["ended"] = time.time()
        if lead_id is not None:
            dstore.set_lead(lead_id, status=("processed" if status == "done" else status), run_id=orch.run_id)
        cid = orch.case_id or case_id
        if cid:                                    # the case decides what happens next (atlas/cases.py)
            try:
                C.run_finished(store, int(cid), orch.run_id, status, res.summary if res else "")
            except Exception:
                import traceback as _tb
                _tb.print_exc()

    th = threading.Thread(target=work, daemon=True)
    holder["thread"] = th
    th.start()
    for _ in range(50):                            # wait briefly for run_id to exist
        if orch.run_id:
            break
        time.sleep(0.02)
    with _runs_lock:
        _runs[orch.run_id] = holder
        _prune_runs()
    if lead_id is not None:
        dstore.set_lead(lead_id, status="running", run_id=orch.run_id)
    return orch.run_id


def _run_status(run_id: str) -> str:
    """Live status of a run for the case engine: running while its thread lives, else what the DB says."""
    h = _runs.get(run_id)
    if h and h["thread"].is_alive():
        return "running"
    row = store.run(run_id)
    if row:
        return "lost" if row.get("status") == "running" else (row.get("status") or "")
    return "running" if h else "lost"


def _lead_case_run(desk: dict[str, Any], lid: int, mode: str = "auto", extra: str = "") -> str:
    """A lead is worked as an enquiry case: replies, follow-ups and bookings continue the same case. A workflow mode, or
    a desk with cases switched off, gets the plain one-off run it always had."""
    lead = store.lead(lid)
    task = _lead_task(lead) + (("\n\n" + extra.strip()) if extra else "")
    if mode != "auto" or not C.enabled(desk, "lead_cases"):
        return _start_run(desk, task, mode, lid)
    _preflight(desk)                                     # refuse (402/503) before a case exists, exactly like a plain run
    open_ = C.match_open(store, desk["id"], email=lead.get("email") or "", phone=lead.get("phone") or "")
    if open_:                                            # the same person again while their case is open: continue it
        store.set_lead(lid, status="merged")
        rid = C.inbound_reply(store, desk, open_, lead.get("source") or "lead", lead.get("notes") or "", actor=lead.get("name") or "",
                              extra=extra)
        if rid:
            store.set_lead(lid, status="running", run_id=rid)
        return rid or (C.get(store, open_["id"]) or {}).get("active_run") or ""
    person = R.person_for(store, desk["id"], email=lead.get("email") or "", phone=lead.get("phone") or "")
    first = ((lead.get("notes") or "").strip().splitlines() or [""])[0][:90]   # the person is the case's record, not its title
    title = first or f"Enquiry from {lead.get('name') or lead.get('email') or lead.get('phone') or 'unknown'}"
    c = C.open_case(store, desk["id"], "enquiry", title, (person or {}).get("id") or 0, source=lead.get("source") or "lead",
                    brief=task, desk=desk)
    return C.kick(store, desk, c, lead_id=lid)


@app.get("/api/leads")
def api_leads():
    return jsonify(ds().leads())


@app.post("/api/leads")
def api_add_lead():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip()
    if not name or not email:
        return jsonify({"error": "name and email required"}), 400
    dstore = store.for_desk(desk["id"])
    lid = dstore.add_lead(name, (d.get("company") or "").strip(), email, (d.get("phone") or "").strip(),
                          (d.get("source") or "web form").strip(), (d.get("notes") or "").strip())
    dstore.upsert_contact(email, {"name": name, "company": d.get("company", ""), "email": email,
                                  "phone": d.get("phone", ""), "stage": "New", "notes": "Inbound lead"})
    run_id = None
    if d.get("run", True):
        run_id = _lead_case_run(desk, lid, d.get("mode", "auto"))
    return jsonify({"id": lid, "run_id": run_id})


@app.post("/api/leads/<int:lid>/run")
def api_run_lead(lid):
    desk = need_desk()
    lead = store.lead(lid)
    if not lead or lead.get("desk_id") != desk["id"]:
        abort(404)
    mode = (request.get_json(silent=True) or {}).get("mode", "auto")
    return jsonify({"run_id": _lead_case_run(desk, lid, mode)})


@app.post("/api/runs")
def api_run_task():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    task = (d.get("task") or "").strip()
    if not task:
        return jsonify({"error": "task required"}), 400
    if d.get("design_team"):                            # one shot: design the team for this job, save it, then run it
        try:
            res = _design_team_for(desk, task, bool(d.get("reuse")))
        except Exception as exc:
            return jsonify({"error": f"design failed: {type(exc).__name__}: {str(exc)[:200]}"}), 502
        applied = _apply_team(desk, res["team"])
        if isinstance(applied, str):
            return jsonify({"error": applied}), 400
        desk = store.desk(desk["id"])
        return jsonify({"run_id": _start_run(desk, task, "auto"), "team": applied["team"], "tree": applied["tree"], "warnings": res["warnings"]})
    return jsonify({"run_id": _start_run(desk, task, d.get("mode", "auto"))})


@app.get("/api/runs")
def api_runs():
    rows = ds().runs(60)
    for r in rows:
        r["active"] = r["id"] in _runs and _runs[r["id"]]["thread"].is_alive()
        r["summary"] = (r.get("summary") or "")[:160]        # the list only needs a teaser; /api/runs/<id> has the full text
    return jsonify(rows)


@app.get("/api/runs/<run_id>")
def api_run(run_id):
    desk = need_desk()
    row = store.run(run_id)
    live = _runs.get(run_id)
    if not row and live and live["desk_id"] == desk["id"]:   # thread started but DB row not written yet
        row = {"id": run_id, "created": time.time(), "task": live["task"], "mode": "", "status": "running",
               "summary": "", "tokens_in": 0, "tokens_out": 0, "run_dir": "", "desk_id": desk["id"]}
    if not row or row.get("desk_id") != desk["id"]:
        abort(404)
    if live:
        events = [e for e in live["events"] if e["kind"] != "usage"]
        row["active"] = live["thread"].is_alive()
    else:
        events = store.events(run_id)
        row["active"] = False
    row["events"] = events
    run_dir = Path(row["run_dir"]) if row.get("run_dir") else None
    row["deliverables"] = []
    if run_dir and run_dir.is_dir():
        for p in sorted(run_dir.glob("*.md")):
            if p.name not in ("TASK.md",):
                row["deliverables"].append({"name": p.name, "content": p.read_text(encoding="utf-8", errors="replace")[:20000]})
    return jsonify(row)


@app.get("/api/stream")
def api_stream():
    """Server-sent events for the current desk: every orchestrator event, including token deltas.
    ?since=<seq> resumes after a sequence number; ?since=now (default) starts from the present."""
    desk = need_desk()
    arg = request.args.get("since", "now")
    run_filter = request.args.get("run", "")
    with _feed_lock:
        start = _feed_seq if arg == "now" else max(0, min(int(arg or 0), _feed_seq))
    desk_id = desk["id"]

    def gen():
        last = start
        idle = 0
        yield "retry: 2000\n\n"
        while True:
            new = _feed_after(last)
            if new:
                idle = 0
                for e in new:
                    last = e["seq"]
                    if e["desk_id"] != desk_id or (run_filter and e["run_id"] != run_filter):
                        continue
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                if idle % 100 == 0:
                    yield ": ping\n\n"
                time.sleep(0.1)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/live")
def api_live():
    desk = need_desk()
    out = []
    with _runs_lock:
        items = list(_runs.items())
    for rid, h in items:
        if h["desk_id"] != desk["id"]:
            continue
        alive = h["thread"].is_alive()
        if not alive and not request.args.get("all"):
            continue
        out.append({"id": rid, "active": alive, "task": h.get("task", ""),
                    "events": [e for e in h["events"] if e["kind"] != "usage"],
                    "tokens_in": h["orch"].tokens_in, "tokens_out": h["orch"].tokens_out})
    with _feed_lock:
        seq = _feed_seq
    return jsonify({"seq": seq, "runs": out})


@app.post("/api/runs/<run_id>/cancel")
def api_cancel(run_id):
    desk = need_desk()
    live = _runs.get(run_id)
    if live and live["desk_id"] == desk["id"]:
        live["orch"].cancel()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: approvals
@app.get("/api/actions")
def api_actions():
    return jsonify(ds().actions(request.args.get("status", "")))


def _contact_touch(dstore, addr: str) -> dict[str, Any]:
    """The contact a message went to: matched by email or phone digits, created only when truly new, and an owner-set
    stage is never knocked back (only New -> Contacted)."""
    c = dstore.contact_for(addr)
    if c:
        fields: dict[str, Any] = {"next_action": "Follow up in 3 days if no reply"}
        if (c.get("stage") or "New") == "New":
            fields["stage"] = "Contacted"
        return dstore.upsert_contact(c.get("email") or c.get("name") or addr, fields)
    f: dict[str, Any] = {"stage": "Contacted", "next_action": "Follow up in 3 days if no reply"}
    if "@" in (addr or ""):
        f["email"] = addr
    else:
        f["phone"] = "+" + "".join(ch for ch in (addr or "") if ch.isdigit())
    return dstore.upsert_contact(addr, f)


@app.post("/api/actions/<int:aid>/decide")
def api_decide(aid):
    desk = need_desk()
    dstore = store.for_desk(desk["id"])
    row = dstore.action(aid)
    if not row or row.get("desk_id") != desk["id"]:
        abort(404)
    d = request.get_json(force=True) or {}
    status = d.get("status")
    if status not in ("approved", "rejected"):
        return jsonify({"error": "status must be approved|rejected"}), 400
    u = current_user()
    by = (u["name"] if u else d.get("by", "owner"))
    row = dstore.decide_action(aid, status, by=by, note=d.get("note", ""), body=d.get("body"), subject=d.get("subject"))
    if status == "approved":
        note = d.get("note", "")
        try:
            result = _dispatch(desk, row)
            row = dstore.decide_action(aid, "sent", by=by, note=(note + " " + result).strip())
            dstore.add_event(row["run_id"], "sent", "owner", f"{row['kind']} → {row['to']} — {row['subject']} ({result})")
            if row["kind"] in ("email", "whatsapp", "sms") and row["to"]:
                dstore.add_message(row["kind"], "out", row["to"], row.get("body") or "", subject=row.get("subject") or "",
                                   actor=row.get("agent") or "", status="sent", run_id=row.get("run_id") or "", action_id=row["id"])
                contact = _contact_touch(dstore, row["to"])
                for m in I.crm_sync(dstore.connectors(), contact):
                    dstore.add_event(row["run_id"], "tool", "owner", f"crm sync → {m}")
            I.notify(dstore.connectors(), f":outbox_tray: *Sent* — {row['kind']} → {row['to']}: {row['subject'] or (row['body'] or '')[:80]} {result}")
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:300]}"
            row = dstore.decide_action(aid, "failed", by=by, note=(note + " send failed: " + err).strip())
            dstore.add_event(row["run_id"], "error", "owner", f"{row['kind']} → {row['to']} failed: {err}")
            if row["kind"] in ("email", "whatsapp", "sms") and row["to"]:
                dstore.add_message(row["kind"], "out", row["to"], row.get("body") or "", subject=row.get("subject") or "",
                                   actor=row.get("agent") or "", status="failed", run_id=row.get("run_id") or "", action_id=row["id"])
    else:
        dstore.add_event(row["run_id"], "rejected", "owner", f"{row['kind']} to {row['to']} rejected — {d.get('note','')}")
    _action_to_case(desk, row, by, d.get("note", ""))
    return jsonify(row)


def _action_to_case(desk: dict[str, Any], row: dict[str, Any], by: str, note: str) -> None:
    """A decided approval lands on the person's record history and moves the case it belongs to."""
    try:
        what = f"{row['kind']} → {row['to']}: {row['subject'] or (row['body'] or '')[:120]}"
        person = R.person_for(store, desk["id"], email=row["to"] if "@" in (row["to"] or "") else "",
                              phone=row["to"] if "@" not in (row["to"] or "") else "")
        if row["status"] == "sent":
            if person:
                R.add_timeline(store, desk["id"], record_id=person["id"], kind="sent", actor=by, text=what, run_id=row.get("run_id") or "",
                               data={"action_id": row["id"], "body": (row.get("body") or "")[:1500]})
            if row.get("case_id"):
                C.fire(store, int(row["case_id"]), "booked" if row["kind"] == "booking" else "sent", what, actor=by,
                       run_id=row.get("run_id") or "")
        elif row["status"] == "rejected" and row.get("case_id"):
            C.fire(store, int(row["case_id"]), "rejected", f"rejected {what}" + (f" — owner's note: {note}" if note else ""), actor=by)
        elif row["status"] == "failed" and row.get("case_id"):
            C.note(store, int(row["case_id"]), f"sending failed: {what} ({row.get('note') or ''})"[:600], actor="system", kind="error")
    except Exception:
        import traceback as _tb
        _tb.print_exc()


# ---------------------------------------------------------------------------- api: crm / audit / report
# ---------------------------------------------------------------------------- api: messages (one thread per customer)
def _backfill_messages(desk: dict[str, Any]) -> None:
    """First visit on an older desk: the thread view starts from what already happened (sent approvals, inbound leads)."""
    ds = store.for_desk(desk["id"])
    if ds.message_count():
        return
    for a in ds.actions("", 500):
        if a["kind"] in ("email", "whatsapp", "sms") and a.get("to") and a["status"] in ("sent", "failed"):
            ds.add_message(a["kind"], "out", a["to"], a.get("body") or "", subject=a.get("subject") or "",
                           actor=a.get("agent") or "", status=a["status"], run_id=a.get("run_id") or "",
                           action_id=a["id"], ts=a.get("decided_at") or a.get("created"))
    for l in ds.leads(500):
        addr = l.get("email") or l.get("phone")
        if not addr:
            continue
        src = (l.get("source") or "").lower()
        ch = "email" if "email" in src else "whatsapp" if "whatsapp" in src else "sms" if src == "sms" else "form"
        ds.add_message(ch, "in", addr, l.get("notes") or "(no message)", actor=l.get("name") or addr, ts=l.get("created"))


def _thread_public(ds, t: dict[str, Any]) -> dict[str, Any]:
    c = ds.contact_for(t["contact_key"]) or {}
    return {"key": t["contact_key"], "name": c.get("name") or t.get("actor") or t["contact_key"], "stage": c.get("stage") or "",
            "email": c.get("email") or ("" if "@" not in t["contact_key"] else t["contact_key"]),
            "phone": c.get("phone") or ("" if "@" in t["contact_key"] else t["contact_key"]),
            "channels": t["channels"], "count": t["count"], "last": {k: t[k] for k in ("channel", "dir", "subject", "body", "status", "ts", "actor")}}


@app.get("/api/messages/threads")
def api_message_threads():
    desk = need_desk()
    _backfill_messages(desk)
    ds = store.for_desk(desk["id"])
    conns = ds.connectors()
    return jsonify({"threads": [_thread_public(ds, t) for t in ds.message_threads()],
                    "channels": {k: bool(I.outbound_connector(conns, k)) for k in ("email", "whatsapp", "sms")}})


@app.get("/api/messages")
def api_messages():
    desk = need_desk()
    key = store.message_key(request.args.get("contact") or "")
    if not key:
        return jsonify({"error": "contact required"}), 400
    ds = store.for_desk(desk["id"])
    c = ds.contact_for(key)
    pend = [a for a in ds.actions("pending", 100) if store.message_key(a.get("to") or "") == key]
    return jsonify({"key": key, "contact": c, "messages": ds.messages(key),
                    "pending": [{"id": a["id"], "kind": a["kind"], "subject": a["subject"], "body": a["body"]} for a in pend]})


@app.post("/api/messages/send")
def api_message_send():
    """The owner replies in their own words: sent through the connector at once (the owner IS the approval)."""
    desk = need_desk()
    d = request.get_json(force=True) or {}
    kind, to = str(d.get("channel") or ""), str(d.get("to") or "").strip()
    subject, body = str(d.get("subject") or "").strip(), str(d.get("body") or "").strip()
    if kind not in ("email", "whatsapp", "sms"):
        return jsonify({"error": "channel must be email, whatsapp or sms"}), 400
    if not to or not body:
        return jsonify({"error": "to and body required"}), 400
    ds = store.for_desk(desk["id"])
    conn = I.outbound_connector(ds.connectors(), kind)
    if not conn:
        return jsonify({"error": f"no {kind} connector yet — add one under Integrations"}), 400
    try:
        result, ok = I.deliver(conn, kind, to, subject, body), True
    except Exception as exc:
        result, ok = f"{type(exc).__name__}: {str(exc)[:200]}", False
    mid = ds.add_message(kind, "out", to, body, subject=subject, actor=_who(), status="sent" if ok else "failed")
    ds.add_event("", "sent" if ok else "error", "owner", f"{kind} → {to} — {subject or body[:80]} ({result})")
    contact = _contact_touch(ds, to)
    try:
        person = R.person_for(store, desk["id"], email=to if "@" in to else "", phone="" if "@" in to else to)
        if person:
            R.add_timeline(store, desk["id"], record_id=person["id"], kind="sent", actor=_who(),
                           text=f"{kind} → {to}: {subject or body[:120]}", data={"message_id": mid, "body": body[:1500]})
    except Exception:
        pass
    return (jsonify({"ok": True, "id": mid, "note": result, "contact": contact}) if ok
            else (jsonify({"ok": False, "id": mid, "error": result}), 502))


@app.get("/api/contacts")
def api_contacts():
    return jsonify(ds().contacts(request.args.get("q", "")))


@app.post("/api/contacts")
def api_upsert_contact():
    d = request.get_json(force=True) or {}
    contact = d.get("contact") or d.get("email") or d.get("name")
    if not contact:
        return jsonify({"error": "contact required"}), 400
    return jsonify(ds().upsert_contact(contact, d.get("fields") or d))


@app.get("/api/audit")
def api_audit():
    return jsonify(ds().all_events(400))


@app.get("/api/metrics")
def api_metrics():
    desk = need_desk()
    since = request.args.get("since")
    m = MX.compute(store, desk_id=desk["id"], since=float(since) if since else None)
    m.pop("per_run", None) if request.args.get("full") is None else None
    return jsonify(m)


@app.get("/api/capacity")
def api_capacity():
    active = sum(1 for r in _runs.values() if r["thread"].is_alive())
    return jsonify(MX.capacity(store, active))


# ---------------------------------------------------------------------------- ops: health, alerts, watchdog
def _health(desk_id: int | None, window_h: float = 24.0) -> dict[str, Any]:
    now = time.time()
    since = now - window_h * 3600
    runs = store.runs_between(since, desk_id)
    by = {}
    for r in runs:
        by[r["status"]] = by.get(r["status"], 0) + 1
    finished = [r for r in runs if r["status"] in ("done", "error", "failed", "interrupted", "cancelled")]
    bad = [r for r in finished if r["status"] in ("error", "failed", "interrupted")]
    durs = sorted((r["ended"] or now) - r["created"] for r in finished if r.get("ended"))
    p = lambda q: round(durs[min(len(durs) - 1, int(q * len(durs)))], 1) if durs else None
    with _runs_lock:
        live = [h for h in _runs.values() if h["thread"].is_alive() and (desk_id is None or h["desk_id"] == desk_id)]
        stalled = [h for h in live if now - h["started"] > RUN_MAX_S * 0.75]
    zombies = [r for r in store.running_runs(RUN_MAX_S) if desk_id is None or r["desk_id"] == desk_id]
    oldest = store.oldest_pending_action(desk_id)
    jobs = store.jobs(desk_id) if desk_id is not None else []
    cons = store.connectors(desk_id) if desk_id is not None else []
    tokens = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in runs)
    err = _last_error.get(desk_id) if desk_id is not None else (max(_last_error.values(), key=lambda e: e["ts"]) if _last_error else None)
    alerts: list[dict[str, str]] = []
    if zombies:
        alerts.append({"level": "critical", "key": "zombie", "text": f"{len(zombies)} run(s) stuck in 'running' beyond the watchdog limit"})
    if stalled:
        alerts.append({"level": "warn", "key": "stalled", "text": f"{len(stalled)} live run(s) older than {int(RUN_MAX_S * 0.75 / 60)} min - watchdog will kill at {int(RUN_MAX_S / 60)} min"})
    if len(finished) >= 5 and len(bad) / len(finished) > 0.2:
        alerts.append({"level": "critical", "key": "failure_rate", "text": f"failure rate {len(bad)}/{len(finished)} in the last {int(window_h)}h"})
    elif bad:
        alerts.append({"level": "warn", "key": "failures", "text": f"{len(bad)} failed run(s) in the last {int(window_h)}h"})
    if oldest and now - oldest > 24 * 3600:
        alerts.append({"level": "warn", "key": "approval_age", "text": f"oldest pending approval is {round((now - oldest) / 3600)}h old"})
    for j in jobs:
        if j.get("enabled") and j.get("last_status") == "error":
            alerts.append({"level": "warn", "key": f"job:{j['id']}", "text": f"automation '{j['name']}' failing: {str(j.get('last_result'))[:120]}"})
    for c in cons:
        if str(c.get("status") or "").startswith("error"):
            alerts.append({"level": "warn", "key": f"conn:{c['id']}", "text": f"connector '{c['name']}' unhealthy: {str(c['status'])[:120]}"})
    if err and now - err["ts"] < 3600:
        alerts.append({"level": "warn", "key": "provider", "text": "recent error: " + err["text"][:160]})
    sp = _spend_poll()
    blocked = _spend_blocked()
    if blocked:
        alerts.append({"level": "critical", "key": "spend", "text": blocked + " - new runs are refused"})
    elif SPEND_CAP_USD and sp.get("total_usage") is not None and sp["total_usage"] >= 0.8 * SPEND_CAP_USD:
        alerts.append({"level": "warn", "key": "spend80", "text": f"model spend ${sp['total_usage']:.2f} is over 80% of the ${SPEND_CAP_USD:.2f} cap"})
    return {
        "spend": {"used_usd": sp.get("total_usage"), "credits_usd": sp.get("total_credits"), "cap_usd": SPEND_CAP_USD or None,
                  "blocked": bool(blocked), "error": sp.get("error")},
        "ok": not any(a["level"] == "critical" for a in alerts), "window_h": window_h, "uptime_s": round(now - _BOOT_TS),
        "runs": {"total": len(runs), "by_status": by, "failure_rate": round(len(bad) / len(finished), 3) if finished else 0.0,
                 "p50_s": p(0.5), "p90_s": p(0.9), "active": len(live), "stalled": len(stalled), "zombies": len(zombies)},
        "tokens": tokens, "cost_gbp": round(tokens / 1e6 * 5 * 0.78, 2),
        "queue": {"pending": len(store.actions("pending", limit=100000, desk_id=desk_id)),
                  "oldest_pending_h": round((now - oldest) / 3600, 1) if oldest else 0},
        "jobs": [{"id": j["id"], "name": j["name"], "kind": j["kind"], "enabled": bool(j.get("enabled")), "last_status": j.get("last_status") or "",
                  "last_run": j.get("last_run"), "last_result": (j.get("last_result") or "")[:160]} for j in jobs],
        "connectors": [{"id": c["id"], "name": c["name"], "kind": c["kind"], "status": (c.get("status") or "untested")[:80]} for c in cons],
        "last_error": err, "alerts": alerts, "watchdog_s": RUN_MAX_S,
    }


@app.get("/api/health/full")
def api_health_full():
    desk = need_desk()
    return jsonify(_health(desk["id"], float(request.args.get("hours") or 24)))


@app.get("/api/health/all")
def api_health_all():
    """Cross-desk view for the soak monitor and ops dashboards (open mode / operator only)."""
    if not OPEN and not current_user():
        abort(401)
    return jsonify(_health(None, float(request.args.get("hours") or 24)))


def _notify_alerts(desk: dict[str, Any], alerts: list[dict[str, str]]) -> None:
    """Push critical alerts out through the desk's Slack / email connectors, at most once per hour per alert key."""
    now = time.time()
    due = [a for a in alerts if a["level"] == "critical" and now - _alert_sent.get(f"{desk['id']}:{a['key']}", 0) > 3600]
    if not due:
        return
    for a in due:
        _alert_sent[f"{desk['id']}:{a['key']}"] = now
    text = f"[Atlas Desk] {desk.get('name')}: " + " | ".join(a["text"] for a in due)
    try:
        cons = store.connectors(desk["id"])
        I.notify(cons, text)
        to = os.environ.get("ALERT_EMAIL", "").strip()
        smtp = next((c for c in cons if c["kind"] == "smtp"), None)
        if to and smtp:
            I.send_email(smtp["config"], to, "Atlas Desk alert", text)
    except Exception as exc:
        print("alert delivery failed:", exc)


SPEND_CAP_USD = float(os.environ.get("SPEND_CAP_USD", "0") or 0)          # 0 = no cap
_spend: dict[str, Any] = {"total_credits": None, "total_usage": None, "ts": 0.0, "error": ""}


def _spend_poll(force: bool = False) -> dict[str, Any]:
    """Read the OpenRouter balance at most every 5 minutes (real money now flows through the key)."""
    if not force and time.time() - _spend["ts"] < 300:
        return _spend
    try:
        prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)["providers"].get("openrouter") or {}
        key = cfg.resolve_api_key({**prov, "name": "openrouter"})
        if key:
            import httpx
            r = httpx.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            d = r.json().get("data", {}) if r.status_code < 400 else {}
            _spend.update({"total_credits": d.get("total_credits"), "total_usage": d.get("total_usage"), "error": "" if d else f"HTTP {r.status_code}"})
    except Exception as exc:
        _spend["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    _spend["ts"] = time.time()
    return _spend


def _spend_blocked() -> str:
    """Non-empty reason when new model work must not start: cap exceeded or balance gone."""
    s = _spend_poll()
    used, total = s.get("total_usage"), s.get("total_credits")
    if SPEND_CAP_USD and used is not None and used >= SPEND_CAP_USD:
        return f"spend cap reached: ${used:.2f} of ${SPEND_CAP_USD:.2f} used - raise SPEND_CAP_USD to continue"
    if used is not None and total is not None and total > 0 and used >= total:
        return f"OpenRouter balance exhausted (${used:.2f} of ${total:.2f})"
    return ""


def _watchdog_loop() -> None:
    """Every 15s: kill runs past RUN_MAX_S, mark orphaned DB rows failed, push critical alerts."""
    while True:
        try:
            now = time.time()
            _spend_poll()
            with _runs_lock:
                holders = list(_runs.items())
            for rid, h in holders:
                if h["thread"].is_alive() and now - h["started"] > RUN_MAX_S and not h.get("killed"):
                    h["killed"] = True
                    h["orch"].cancel()
                    msg = f"watchdog: run exceeded {int(RUN_MAX_S)}s and was killed"
                    store.finish_run(rid, "failed", msg, h["orch"].tokens_in, h["orch"].tokens_out)
                    _push_feed(rid, h["desk_id"], Event(kind="error", agent="system", text=msg))
                    _push_feed(rid, h["desk_id"], Event(kind="done", agent="system", text=msg, data={"status": "failed"}))
                    _last_error[h["desk_id"]] = {"ts": now, "run_id": rid, "text": msg}
                    h["ended"] = now
            live_ids = {rid for rid, h in holders if h["thread"].is_alive()}
            acted: dict[int, list[str]] = {}
            for rid, h in holders:
                if h.get("killed") and not h.get("reported"):
                    h["reported"] = True
                    acted.setdefault(h["desk_id"], []).append(f"killed {rid} after {int(RUN_MAX_S)}s")
            for r in store.running_runs(RUN_MAX_S):
                if r["id"] not in live_ids:                      # DB says running, nothing in memory owns it
                    store.finish_run(r["id"], "failed", "lost: no worker owns this run (crashed or restarted)", 0, 0)
                    _last_error[r["desk_id"]] = {"ts": now, "run_id": r["id"], "text": "lost run marked failed by watchdog"}
                    acted.setdefault(r["desk_id"], []).append(f"marked lost run {r['id']} failed")
            for d in store.all_desks():
                hz = _health(d["id"], 24)
                alerts = list(hz["alerts"])
                if acted.get(d["id"]):
                    alerts.append({"level": "critical", "key": "watchdog", "text": "watchdog acted: " + "; ".join(acted[d["id"]])})
                if any(a["level"] == "critical" for a in alerts):
                    _notify_alerts(d, alerts)
        except Exception as exc:
            print("watchdog error:", exc)
        time.sleep(15)


@app.get("/api/report")
def api_report():
    dstore = ds()
    s = dstore.stats()
    acts = dstore.actions()
    decided = [a for a in acts if a["status"] in ("sent", "approved", "rejected")]
    approval_rate = round(100 * sum(1 for a in decided if a["status"] != "rejected") / len(decided)) if decided else None
    runs = dstore.runs(500)
    done = [r for r in runs if r["status"] == "done"]
    flagged = sum(1 for a in acts if a.get("flags"))
    mins_saved = len(done) * 35  # assumption: ~35 min of research + drafting + CRM per lead
    return jsonify({
        "period": time.strftime("%B %Y"),
        "leads_handled": s["leads"], "runs_done": len(done), "runs_failed": len([r for r in runs if r["status"] == "error"]),
        "actions_queued": len(acts), "approval_rate": approval_rate, "policy_flagged": flagged,
        "sent": sum(1 for a in acts if a["status"] == "sent"), "rejected": s["rejected"], "pending": s["pending"],
        "contacts": s["contacts"], "qualified": s["qualified"],
        "hours_saved": round(mins_saved / 60, 1), "assumption": "35 min saved per processed lead (research + draft + CRM)",
        "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"],
        "est_model_cost_gbp": round((s["tokens_in"] * 5 + s["tokens_out"] * 25) / 1_000_000 * 0.78, 2),
        "changes": ["Policy layer now blocks figures, placeholders and invented time slots before the queue",
                    "CRM stage held at New until you approve the first send"],
    })


# ---------------------------------------------------------------------------- demo helpers
def _sample_leads(desk: dict[str, Any]) -> list[dict[str, str]]:
    """Template samples for the stock business; for a customised business ask the (cheap) model to invent
    three realistic enquiries so the demo matches what the client actually does."""
    if desk.get("template") == "soc_desk":           # a security desk gets no invented leads: its input is real log data
        return []
    stock = templates.SAMPLE_LEADS.get(desk["template"], templates.SAMPLE_LEADS["sales_desk"])
    c = desk_configs(desk)
    b = c["business"]
    default_name = templates.get(desk["template"])["business"].get("name")
    if c["mode"] == "demo" or b.get("name") == default_name:
        return stock
    try:
        from ..providers import ProviderPool
        pool = ProviderPool(c["providers"])
        prov = pool.get()
        model = templates.FREE_MODEL if c["providers"].get("default_provider") == "openrouter" else ""
        prompt = (f"Invent 3 realistic inbound enquiries for this business. Return ONLY a JSON array of objects with keys "
                  f"name, company, email, phone, source, notes (notes = 1-2 sentence enquiry in the customer's words). "
                  f"Use example.com emails and +44 7700 9001xx phones.\n\nBusiness: {b.get('name')}\n{b.get('description','')}\n"
                  f"Services: {', '.join(b.get('services', []))}\nTarget clients: {b.get('target_clients','')}")
        r = prov.chat("You output strict JSON only.", [prov.user_message(prompt)], [], model)
        txt = r.text.strip()
        txt = txt[txt.index("["): txt.rindex("]") + 1]
        rows = json.loads(txt)
        out = []
        for x in rows[:3]:
            out.append({"name": str(x.get("name", "")).strip() or "Enquiry", "company": str(x.get("company", "") or ""),
                        "email": str(x.get("email", "") or "lead@example.com"), "phone": str(x.get("phone", "") or ""),
                        "source": str(x.get("source", "") or "website form"), "notes": str(x.get("notes", "") or "")})
        return out or stock
    except Exception:
        return stock


@app.post("/api/demo/seed")
def api_seed():
    desk = need_desk()
    dstore = store.for_desk(desk["id"])
    ids = []
    for L in _sample_leads(desk):
        lid = dstore.add_lead(L["name"], L["company"], L["email"], L["phone"], L["source"], L["notes"])
        dstore.upsert_contact(L["email"], {"name": L["name"], "company": L["company"], "email": L["email"], "phone": L["phone"], "stage": "New", "notes": "Inbound lead"})
        ids.append({"id": lid, "run_id": _lead_case_run(desk, lid)})
        time.sleep(0.05)
    return jsonify(ids)


@app.post("/api/demo/reset")
def api_reset():
    desk = need_desk()
    if any(r["desk_id"] == desk["id"] and r["thread"].is_alive() for r in _runs.values()):
        return jsonify({"error": "runs in progress"}), 409
    for j in store.jobs(desk["id"]):                   # the reset deletes the jobs: close any open replay reader
        scheduler._REPLAYS.pop(j["id"], None)
    scheduler.cyber_forget(desk["id"])                 # a fresh take: no leftover trigger queue or run cooldown
    with _GRAPH_MEMO_LOCK:
        for memo in (_GRAPH_MEMO, _GRAPH_RECENT):
            for k in [k for k in memo if k[0] == desk["id"]]:
                memo.pop(k, None)
    store.for_desk(desk["id"]).reset()
    for rid in [k for k, v in _runs.items() if v["desk_id"] == desk["id"]]:
        _runs.pop(rid, None)
    return jsonify({"ok": True})


def _dispatch(desk: dict[str, Any], row: dict[str, Any]) -> str:
    """Perform an approved action for real when a connector exists; otherwise simulate and say so."""
    dstore = store.for_desk(desk["id"])
    kind = row["kind"]
    if kind == "containment":
        from .. import cyber as CY
        from .. import policy as P
        try:
            stored = json.loads(row["body"] or "{}")
        except ValueError:
            raise RuntimeError("containment body is not valid JSON")
        if not isinstance(stored, dict):
            raise RuntimeError("containment body is not valid JSON")
        spec, errs = CY.containment_spec(stored)
        v = errs + P.check_containment(spec["targets"], spec["evidence"], dstore.sec_events_by_ids(spec["evidence"]))
        if v:                                    # an edited body cannot add a target the evidence does not show
            raise RuntimeError("containment refused at dispatch: " + "; ".join(v))
        plan = ", ".join(f"{t['action']} {t['value']}" for t in spec["targets"])
        name = spec["connector"]
        conn = dstore.connector_by_name(name) if name else None
        if not conn:
            why = f"no connector named {name!r}" if name else "no connector named in the action"
            return f"[simulated containment — {why}; nothing was blocked: {plan}]"
        if conn["kind"] != "http":
            return f"[simulated containment — connector {conn['name']} is {conn['kind']}, not HTTP; nothing was blocked: {plan}]"
        if conn.get("auto"):
            return (f"[simulated containment — connector {conn['name']} allows writes without approval, so it is not used "
                    f"for containment; nothing was blocked: {plan}]")
        path = str(conn["config"].get("containment_path") or "/contain")
        results, failed = [], False
        for t in spec["targets"]:
            body = {"action": t["action"], "kind": t["kind"], "value": t["value"], "evidence": spec["evidence"],
                    "approval_id": row["id"]}
            try:
                res = I.http_call(conn["config"], "POST", path, None, body, timeout=15)
                results.append(f"{t['action']} {t['value']} → HTTP {res['status']}")
                failed = failed or res["status"] >= 400
            except Exception as exc:
                results.append(f"{t['action']} {t['value']} → {type(exc).__name__}: {str(exc)[:80]}")
                failed = True
        summary = f"containment via {conn['name']}: " + "; ".join(results)
        if failed:
            raise RuntimeError(summary)          # api_decide marks the action "failed" with this note
        return f"[{summary}]"
    if kind in I.CHANNELS:
        conn = I.outbound_connector(dstore.connectors(), kind)
        if not conn:
            return f"[simulated {kind} — no {' / '.join(I.CHANNELS[kind])} connector; add one under Integrations]"
        return "[" + I.deliver(conn, kind, row["to"], row["subject"] or "", row["body"] or "") + "]"
    if kind == "browser_action":
        from .. import browser as BR
        try:
            spec = json.loads(row["body"] or "{}")
            return "[" + BR.perform_pending(spec.get("pending") or {}, spec.get("profile") or f"desk{desk['id']}") + "]"
        except Exception as exc:
            return f"[browser action failed - {type(exc).__name__}: {str(exc)[:160]}]"
    if kind == "api_call":
        conn = dstore.connector_by_name(row["to"])
        if not conn:
            return "[failed — connector missing]"
        spec = json.loads(row["body"] or "{}")
        if conn["kind"] == "higgsfield" or "media" in spec:
            res = I.higgsfield_generate(conn["config"], spec.get("media", "image"), spec.get("prompt", ""), spec.get("image_url") or "",
                                        spec.get("duration"), spec.get("aspect_ratio") or "16:9")
            urls = ", ".join(res["outputs"]) or "no output URL returned"
            return f"[Higgsfield {res['model']} {res['status']}: {urls}]"
        if conn["kind"] == "mcp" or "mcp_tool" in spec:
            from .. import mcp_client as M
            out = M.REGISTRY.get(conn).call(spec["mcp_tool"], spec.get("arguments") or {})
            return f"[MCP {spec['mcp_tool']}: {out[:160]}]"
        res = I.http_call(conn["config"], spec.get("method", "POST"), spec.get("path", ""), spec.get("params"), spec.get("body"))
        return f"[HTTP {res['status']} {res['url']}]"
    return f"[simulated {kind} — no connector for this channel yet]"


# ---------------------------------------------------------------------------- api: connectors (integrations)
def _conn_public(c: dict[str, Any]) -> dict[str, Any]:
    return {**c, "config": I.mask(c.get("config") or {})}


@app.get("/api/connectors")
def api_connectors():
    desk = need_desk()
    hook = request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"])
    return jsonify({"connectors": [_conn_public(c) for c in store.connectors(desk["id"])],
                    "kinds": I.KINDS, "hook_url": hook, "whatsapp_hook_url": hook + "/whatsapp", "sms_hook_url": hook + "/sms",
                    "channels": {k: bool(I.outbound_connector(store.connectors(desk["id"]), k)) for k in I.CHANNELS}})


@app.post("/api/connectors")
def api_add_connector():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    kind = d.get("kind")
    if kind not in I.KINDS:
        return jsonify({"error": "unknown kind"}), 400
    name = (d.get("name") or kind).strip()
    if store.connector_by_name(desk["id"], name):
        return jsonify({"error": "a connector with that name exists"}), 400
    config = d.get("config") or {}
    if kind == "camera":                                   # "sample:<clip>" is resolved once, here, like the designer does
        config["source"] = DS.resolve_camera_source(str(config.get("source") or "")) or str(config.get("source") or "")
    c = store.add_connector(desk["id"], kind, name, config, bool(d.get("auto")))
    return jsonify(_conn_public(c))


@app.patch("/api/connectors/<int:cid>")
def api_update_connector(cid):
    desk = need_desk()
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    d = request.get_json(force=True) or {}
    fields: dict[str, Any] = {}
    if "config" in d:
        if c["kind"] == "camera" and "source" in (d["config"] or {}):
            src = str(d["config"].get("source") or "")
            d["config"]["source"] = DS.resolve_camera_source(src) or src
        fields["config"] = I.merge_secrets(c["config"], d["config"] or {})
    if "auto" in d:
        fields["auto"] = 1 if d["auto"] else 0
    if d.get("name"):
        fields["name"] = str(d["name"]).strip()
    store.update_connector(cid, **fields)
    return jsonify(_conn_public(store.connector(cid)))


@app.delete("/api/connectors/<int:cid>")
def api_delete_connector(cid):
    desk = need_desk()
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    store.delete_connector(cid)
    if c["kind"] == "camera":                              # its watch job goes too, or it keeps ticking on a missing camera
        for j in store.jobs(desk["id"]):
            if j["kind"] == "camera_watch" and f'"connector": "{c["name"]}"' in (j["task"] or ""):
                store.delete_job(j["id"])
    from .. import mcp_client as M
    M.REGISTRY.drop(cid)
    return jsonify({"ok": True})


@app.post("/api/connectors/<int:cid>/test")
def api_test_connector(cid):
    desk = need_desk()
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    try:
        msg = I.test_connector(c["kind"], c["config"])
        store.update_connector(cid, status="ok: " + msg, last_test=time.time())
        return jsonify({"ok": True, "result": msg})
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.update_connector(cid, status="error: " + err, last_test=time.time())
        return jsonify({"ok": False, "result": err}), 400


@app.get("/api/memory")
def api_memory():
    return jsonify(ds().recall(request.args.get("q", ""), 200))


@app.post("/api/memory")
def api_memory_add():
    d = request.get_json(force=True) or {}
    if not d.get("key"):
        return jsonify({"error": "key required"}), 400
    return jsonify(ds().remember(d["key"], d.get("value", ""), source="owner"))


@app.delete("/api/memory/<path:key>")
def api_memory_del(key):
    ds().forget(key)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: jobs (automations)
JOB_KINDS = {
    "task": "Run a task (once or every N minutes)",
    "inbox_watch": "Watch the inbox — every new email becomes a lead",
    "followups": "Chase contacts stuck at Contacted for N days",
    "http_poll": "Poll an HTTP API and hand the result to the desk",
    "camera_watch": "Watch the cameras — detect, log, wake the desk when a rule fires",
    "log_replay": "Replay a log file at N× speed (original timestamps kept)",
    "log_watch": "Watch a log file and feed new lines to the desk",
}


def _log_job_spec(kind: str, raw: Any) -> tuple[dict[str, Any], str]:
    """A log_replay / log_watch task: a dict or a JSON string, validated (paths, formats, limits)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return {}, "task must be a JSON object"
    return scheduler.validate_log_spec(kind, raw if isinstance(raw, dict) else None)


@app.get("/api/jobs")
def api_jobs():
    desk = need_desk()
    return jsonify({"jobs": store.jobs(desk["id"]), "kinds": JOB_KINDS, "now": time.time()})


@app.post("/api/jobs")
def api_add_job():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    kind = d.get("kind") if d.get("kind") in JOB_KINDS else "task"
    every = int(d.get("every_min") or 0)
    delay = int(d.get("in_min") or 0)
    at = str(d.get("at") or "").strip()                 # "07:30" = daily at that local time, instead of a bare interval
    if at:
        if not re.fullmatch(r"[0-2]?\d:[0-5]\d", at) or int(at.split(":")[0]) > 23:
            return jsonify({"error": "at must be HH:MM"}), 400
        from .. import alerts as AL
        every, nxt = 1440, AL.report_due(at)
    else:
        nxt = time.time() + (delay * 60 if delay else (every * 60 if every and not d.get("run_now") else 0))
    task = d.get("task") or ""
    if kind in scheduler.LOG_JOB_KINDS:                 # runs every tick until done; validated before it is stored
        spec, err = _log_job_spec(kind, task)
        if err:
            return jsonify({"error": err}), 400
        j = store.add_job(desk["id"], kind, d.get("name") or JOB_KINDS[kind], json.dumps(spec), 0,
                          time.time() + (delay * 60 if delay else 0))
        return jsonify(j)
    if kind == "followups" and not task:
        task = json.dumps({"days": int(d.get("days") or 3)})
    if kind in ("http_poll", "camera_watch") and isinstance(task, dict):
        task = json.dumps(task)
    j = store.add_job(desk["id"], kind, d.get("name") or JOB_KINDS[kind], task, every, nxt)
    return jsonify(j)


@app.patch("/api/jobs/<int:jid>")
def api_update_job(jid):
    desk = need_desk()
    j = store.job(jid)
    if not j or j["desk_id"] != desk["id"]:
        abort(404)
    d = request.get_json(force=True) or {}
    fields = {k: d[k] for k in ("name", "task", "every_min") if k in d}
    at = str(d.get("at") or "").strip()
    if at:
        if not re.fullmatch(r"[0-2]?\d:[0-5]\d", at) or int(at.split(":")[0]) > 23:
            return jsonify({"error": "at must be HH:MM"}), 400
        from .. import alerts as AL
        fields["every_min"], fields["next_run"] = 1440, AL.report_due(at)
    if j["kind"] in scheduler.LOG_JOB_KINDS:
        fields.pop("every_min", None)
        if "task" in d:                                # an edited replay/watch is re-validated and starts over
            spec, err = _log_job_spec(j["kind"], d["task"])
            if err:
                return jsonify({"error": err}), 400
            fields["task"] = json.dumps(spec)
            scheduler._REPLAYS.pop(jid, None)
    if "enabled" in d:
        fields["enabled"] = 1 if d["enabled"] else 0
        if d["enabled"] and not j.get("next_run"):
            fields["next_run"] = time.time()
    store.update_job(jid, **fields)
    return jsonify(store.job(jid))


@app.post("/api/jobs/<int:jid>/run")
def api_run_job(jid):
    desk = need_desk()
    j = store.job(jid)
    if not j or j["desk_id"] != desk["id"]:
        abort(404)
    store.update_job(jid, next_run=time.time() - 1, enabled=1)
    return jsonify({"ok": True, "note": "will run within 20 seconds"})


@app.delete("/api/jobs/<int:jid>")
def api_delete_job(jid):
    desk = need_desk()
    j = store.job(jid)
    if not j or j["desk_id"] != desk["id"]:
        abort(404)
    store.delete_job(jid)
    scheduler._REPLAYS.pop(jid, None)                   # close a replay's open files
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: cameras / vision
def _camera(cid: int, desk: dict[str, Any]) -> dict[str, Any]:
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"] or c["kind"] != "camera":
        abort(404)
    return c


def _vev_public(e: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in e.items() if k != "snapshot"}
    out["snapshot_url"] = f"/api/vision/snapshot/{e['id']}" if e.get("snapshot") else ""
    out["seen"] = V.counts_text(e.get("counts") or {})
    return out


@app.get("/api/agents/links")
def api_agent_links():
    """Which agent watches which camera and sends through which connector, with the gaps: read by every tab."""
    from .. import agent_links as AL
    desk = need_desk()
    conns = store.connectors(desk["id"])
    cams = [c for c in conns if c["kind"] == "camera"]
    out = AL.links(desk_configs(desk)["agents"], cams, [c for c in conns if c["kind"] != "camera"])
    out["camera_ids"] = {c["name"]: c["id"] for c in cams}
    return jsonify(out)


@app.post("/api/cameras/<int:cid>/assign")
def api_camera_assign(cid):
    """{agent, on=true}: name this camera in the agent's instructions (and grant the camera tools), or take it out."""
    from .. import agent_links as AL
    desk = need_desk()
    cam = _camera(cid, desk)
    d = request.get_json(silent=True) or {}
    aid = str(d.get("agent") or "")
    conf = dict(desk.get("config") or {})
    roster = json.loads(json.dumps(conf.get("agents") or templates.get(desk.get("template") or DEFAULT_TEMPLATE)["agents"]))
    i = next((k for k, a in enumerate(roster) if a.get("id") == aid), None)
    if i is None or aid == "atlas":
        return jsonify({"error": "pick a specialist agent of this desk"}), 400
    roster[i] = AL.assign_camera(roster[i], cam["name"], bool(d.get("on", True)))
    conf["agents"] = roster
    store.update_desk(desk["id"], config=conf)
    return jsonify({"ok": True, "agent": aid, "camera": cam["name"], "on": bool(d.get("on", True))})


@app.get("/api/cameras")
def api_cameras():
    desk = need_desk()
    cams = []
    for c in store.connectors(desk["id"]):
        if c["kind"] != "camera":
            continue
        seen = scheduler.last_seen(desk["id"], c["name"]) or {}
        last = store.last_vision_event(desk["id"], c["name"])
        cams.append({**_conn_public(c), "source_kind": V.source_kind(str(c["config"].get("source", ""))),
                     "rule": V.rule_config(c["config"]),
                     "seen": {k: v for k, v in seen.items() if k not in ("annotated", "detections")},
                     "last_event": _vev_public(last) if last else None,
                     "watch_job": next((j for j in store.jobs(desk["id"]) if j["kind"] == "camera_watch" and
                                        (j["task"] or "").find(f'"connector": "{c["name"]}"') >= 0), None)})
    hook = request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"]) + "/vision"
    nodes = [{**n, "last_event": _vev_public(n["last_event"]) if n.get("last_event") else None}
             for n in store.hook_cameras(desk["id"], time.time() - 86400, tuple(c["name"] for c in cams))]
    return jsonify({"cameras": cams, "nodes": nodes, "mode": _mode(),
                    "detector": {"available": V.DETECTOR.available, "error": V.DETECTOR.error, "weights": getattr(V.DETECTOR, "label", os.path.basename(V.YOLO_WEIGHTS)),
                                 "runtime_error": getattr(V.DETECTOR, "_ov_error", "")},
                    "vlm": {"ready": V.vlm_ready() and _mode() != "demo", "model": V.DEFAULT_VLM},
                    "stats": store.vision_stats(desk["id"], time.time() - 86400), "hook_url": hook,
                    "planned": _planned_cameras(desk, cams),
                    "samples": [{"name": n, "label": DS.SAMPLE_LABELS.get(n, n.replace("-", " "))} for n in DS.sample_clips()]})


def _planned_cameras(desk: dict[str, Any], cams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cameras Atlas planned for this desk that are not connected yet (built without a stream address)."""
    have = {c["name"] for c in cams}
    bp = (desk.get("config") or {}).get("blueprint") or {}
    return [{k: cam.get(k, "") for k in ("name", "notes", "focus", "watch_for")} for cam in bp.get("cameras") or []
            if cam.get("name") and cam["name"] not in have]


@app.post("/api/cameras/planned")
def api_camera_planned():
    """{name, source}: connect a camera Atlas planned (journal, focus and notes as designed) and start watching it."""
    desk = need_desk()
    d = request.get_json(silent=True) or {}
    bp = (desk.get("config") or {}).get("blueprint") or {}
    cam = next((c for c in bp.get("cameras") or [] if c.get("name") == d.get("name")), None)
    if not cam:
        return jsonify({"error": "no planned camera by that name"}), 404
    if not DS.resolve_camera_source(str(d.get("source") or "")):
        return jsonify({"error": "give a stream address (rtsp://, http://, a webcam index) or pick sample footage"}), 400
    made, missing = _build_cameras(desk, {"cameras": [dict(cam, source=str(d["source"]))]})
    if not made:
        return jsonify({"error": f"could not use that source for {cam['name']}"}), 400
    return jsonify({"ok": True, "camera": made[0]})


# ---- attaching a feed to a camera tile: upload a recording, test any source, attach it (built or planned camera)
CAMERA_UPLOAD_DIR = cfg.DATA_DIR / "uploads" / "cameras"
CAMERA_UPLOAD_EXT = V.VIDEO_EXT | {".jpg", ".jpeg", ".png"}
CAMERA_UPLOAD_MAX = int(os.environ.get("CAMERA_UPLOAD_MAX_MB", "2048")) * 1024 * 1024


def _signed_in() -> None:
    if not current_user() and not OPEN:
        abort(401)


@app.post("/api/cameras/upload")
def api_camera_upload():
    """multipart "file": a recording (mp4, mov, mkv, ...) or a still image, stored on the server and played as a camera
    (recordings loop on the shared clock, like the sample footage). Returns {"source": path} for /attach."""
    _signed_in()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "choose a video or image file"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in CAMERA_UPLOAD_EXT:
        return jsonify({"error": f"{ext or 'that file type'} is not supported; use " + ", ".join(sorted(CAMERA_UPLOAD_EXT))}), 400
    if request.content_length and request.content_length > CAMERA_UPLOAD_MAX:
        return jsonify({"error": f"file is larger than {CAMERA_UPLOAD_MAX // (1024 * 1024)} MB"}), 413
    CAMERA_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(f.filename).stem).strip("-")[:40] or "camera"
    dest = CAMERA_UPLOAD_DIR / f"{stem}-{secrets.token_hex(4)}{ext}"
    f.save(dest)
    return jsonify({"source": str(dest), "name": f.filename, "kind": V.source_kind(str(dest)), "bytes": dest.stat().st_size})


@app.post("/api/cameras/probe")
def api_camera_probe():
    """{source}: grab one frame to prove the source works before attaching it. {ok, kind, preview (data URL)} or {ok:false, error}."""
    _signed_in()
    d = request.get_json(silent=True) or {}
    raw = str(d.get("source") or "").strip()
    src = DS.resolve_camera_source(raw)
    if not src:
        return jsonify({"ok": False, "error": "enter a stream address, pick a file or choose sample footage"}), 400
    kind = V.source_kind(src)
    try:
        jpeg = V.grab(src)
    except Exception as exc:
        return jsonify({"ok": False, "kind": kind, "error": str(exc)[:300] or type(exc).__name__})
    return jsonify({"ok": True, "kind": kind, "source": src, "preview": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()})


@app.post("/api/cameras/attach")
def api_camera_attach():
    """{name, source, cid?, notes?, focus?}: point a camera at a feed. An existing camera keeps its journal settings
    and only swaps the source; a planned or brand-new camera is created with a journal and a watch job."""
    desk = need_desk()
    d = request.get_json(silent=True) or {}
    name = re.sub(r"\s+", "-", str(d.get("name") or "").strip())[:60]
    src = DS.resolve_camera_source(str(d.get("source") or ""))
    if not name:
        return jsonify({"error": "name the camera"}), 400
    if not src:
        return jsonify({"error": "give a stream address (rtsp://, http://), a webcam number, an uploaded file or sample footage"}), 400
    c = store.connector(int(d["cid"])) if d.get("cid") else store.connector_by_name(desk["id"], name)
    if c and (c["desk_id"] != desk["id"] or c["kind"] != "camera"):
        abort(404)
    if c:
        store.update_connector(c["id"], config=I.merge_secrets(c["config"], {"source": src}), status="")
        c = store.connector(c["id"])
        if not any(j["kind"] == "camera_watch" and f'"connector": "{c["name"]}"' in (j["task"] or "") for j in store.jobs(desk["id"])):
            store.add_job(desk["id"], "camera_watch", f"Watch {c['name']}", json.dumps({"connector": c["name"], "every_s": 8}), 1, time.time())
        cam = {"id": c["id"], "name": c["name"]}
    else:
        bp = (desk.get("config") or {}).get("blueprint") or {}
        plan = next((x for x in bp.get("cameras") or [] if x.get("name") == name), None) or {}
        made, _ = _build_cameras(desk, {"cameras": [{**plan, "name": name, "source": src,
                                                     "notes": plan.get("notes") or str(d.get("notes") or ""),
                                                     "focus": plan.get("focus") or str(d.get("focus") or "")}]})
        if not made:
            return jsonify({"error": f"could not use that source for {name}"}), 400
        cam = made[0]
    return jsonify({"ok": True, "camera": {**cam, "source_kind": V.source_kind(src)}})


@app.post("/api/cameras/<int:cid>/look")
def api_camera_look(cid):
    desk = need_desk()
    c = _camera(cid, desk)
    d = request.get_json(silent=True) or {}
    try:
        r = scheduler.camera_tick(store, desk, c, _start_run, _mode() != "demo",
                                  question=str(d.get("question") or "").strip()[:500], force=bool(d.get("trigger")))
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.update_connector(cid, status="error: " + msg, last_test=time.time())
        return jsonify({"ok": False, "error": msg}), 400
    store.update_connector(cid, status=f"ok: {V.counts_text(r['counts'])} ({r['backend']})", last_test=time.time())
    r.pop("annotated", None)
    return jsonify({"ok": True, **r})


@app.get("/api/cameras/<int:cid>/frame.jpg")
def api_camera_frame(cid):
    desk = need_desk()
    c = _camera(cid, desk)
    seen = scheduler.last_seen(desk["id"], c["name"])
    if seen and seen.get("annotated"):
        return Response(seen["annotated"], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    last = store.last_vision_event(desk["id"], c["name"])
    if last and last.get("snapshot") and Path(last["snapshot"]).is_file():
        return send_file(last["snapshot"], mimetype="image/jpeg", max_age=0)
    abort(404)


@app.get("/api/cameras/<int:cid>/live.mjpg")
def api_camera_live_mjpg(cid):
    """The camera as live video: one decode loop per source, every frame through YOLO with boxes and track ids
    drawn, served as motion JPEG. Works for webcams, RTSP and recordings (which play at real speed and loop).
    ?fps=N caps the delivery rate for slow links; the loop itself runs as fast as the source allows."""
    desk = need_desk()
    c = _camera(cid, desk)
    from .. import live as LIVE
    try:
        feed = LIVE.open(str(c["config"].get("source", "")), c["name"])
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}), 400
    try:
        max_fps = float(request.args.get("fps") or 0)
    except ValueError:
        max_fps = 0.0
    resp = Response(stream_with_context(LIVE.mjpeg(feed, max_fps)),
                    mimetype="multipart/x-mixed-replace; boundary=atlasframe")
    resp.headers["Cache-Control"] = "no-store, no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "close"
    return resp


@app.get("/api/cameras/<int:cid>/live")
def api_camera_live_status(cid):
    """Live-loop status for a camera (fps, detector ms, viewers, counts). ?start=1 starts the loop without a viewer."""
    desk = need_desk()
    c = _camera(cid, desk)
    from .. import live as LIVE
    src = str(c["config"].get("source", ""))
    feed = LIVE.get(src)
    if feed is None and request.args.get("start") in ("1", "true"):
        try:
            feed = LIVE.open(src, c["name"])
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}), 400
    out = feed.status() if feed else {"running": False, "viewers": 0, "fps": 0, "counts": {}, "error": ""}
    from .. import journal as JR
    return jsonify({"ok": True, "camera": c["name"], "id": cid, "live": out, "listeners": JR.subscribers(desk["id"]),
                    "url": f"/api/cameras/{cid}/live.mjpg"})


@app.post("/api/cameras/<int:cid>/live")
def api_camera_live_ctl(cid):
    """{on:false} stops the live loop for this camera now (it also stops by itself when nobody watches)."""
    desk = need_desk()
    c = _camera(cid, desk)
    from .. import live as LIVE
    d = request.get_json(silent=True) or {}
    src = str(c["config"].get("source", ""))
    if d.get("on", True):
        try:
            feed = LIVE.open(src, c["name"])
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}), 400
        return jsonify({"ok": True, "live": feed.status()})
    feed = LIVE.get(src)
    if feed:
        feed.stop()
    return jsonify({"ok": True, "live": {"running": False}})


@app.get("/api/vision/journal/stream")
def api_vision_journal_stream():
    """Server-sent events from the camera journal as it happens: every scheduler tick (counts, motion), then for each
    note note_start -> note_delta (one per token) -> note_done, plus digest summaries and errors.
    ?camera=<name> filters to one camera. While at least one listener is connected, notes are streamed from the model."""
    desk = need_desk()
    from .. import journal as JR
    cam = str(request.args.get("camera") or "")
    q = JR.subscribe(desk["id"])
    desk_id = desk["id"]

    def gen():
        try:
            yield "retry: 2000\n\n"
            yield "data: " + json.dumps({"kind": "hello", "camera": cam, "ts": time.time(), "listeners": JR.subscribers(desk_id)}) + "\n\n"
            idle = 0.0
            while True:
                try:
                    ev = q.get(timeout=1.0)
                except queue.Empty:
                    idle += 1
                    if idle >= 15:
                        idle = 0
                        yield ": ping\n\n"
                    continue
                idle = 0
                if cam and ev.get("camera") != cam:
                    continue
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
        finally:
            JR.unsubscribe(desk_id, q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


# ---------------------------------------------------------------------------- object catalogue
def _objects_mod():
    """atlas.objects (the catalogue worker) and atlas.audit are optional installs; without them the routes say so
    instead of dying with a 500."""
    try:
        OBJ = importlib.import_module("atlas.objects")
        AUD = importlib.import_module("atlas.audit")
    except ImportError:
        abort(Response(json.dumps({"error": "object catalogue not installed (atlas/objects.py, atlas/audit.py)"}), 501,
                       mimetype="application/json"))
    return OBJ, AUD


def _obj_public(o: dict[str, Any], names: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    o = dict(o)
    o.pop("emb", None)
    n = None
    if o.get("name_id") and o.get("name_by") != "owner-no":
        n = (names or {}).get(o["name_id"]) if names is not None else store.named(o["name_id"])
    o["name"] = n["name"] if n else ""
    o["name_kind"] = n["kind"] if n else ""
    o["crop_url"] = f"/api/objects/{o['id']}/crop.jpg?v={int(o.get('last_ts') or 0)}"
    o["scene_url"] = f"/api/objects/{o['id']}/scene.jpg?v={int(o.get('last_ts') or 0)}"
    o["seconds"] = round(max(0.0, (o.get("last_ts") or 0) - (o.get("first_ts") or 0)), 1)
    o["path"] = (o.get("path") or [])[-120:]
    return o


def _object(oid: int) -> tuple[dict[str, Any], dict[str, Any]]:
    desk = need_desk()
    o = store.for_desk(desk["id"]).vision_object(oid)
    if not o:
        abort(404)
    return desk, o


# ---------------------------------------------------------------------------- review: synced multi-camera playback vs labels
REVIEW_DATA = Path(os.environ.get("ATLAS_REVIEW_DATA") or (Path.home() / "AtlasDemo" / "wildtrack")).expanduser()
REVIEW_OUT = Path(os.environ.get("ATLAS_REVIEW_OUT") or "").expanduser() if os.environ.get("ATLAS_REVIEW_OUT") else None


def _review_out() -> Path:
    from .. import mcam_eval as MC
    return REVIEW_OUT or MC.OUT_DIR


@app.get("/desk/review")
def desk_review_page():
    if not current_user() and not OPEN:
        return redirect("/login?next=/desk/review")
    return send_from_directory(STATIC_DIR, "review.html")


@app.get("/api/review/data")
def api_review_data():
    """The evaluation's per-instant boxes (review.json) and its scores (results.json): `py -m atlas mcam-eval` writes both;
    notes.json, when `py -m atlas mcam-notes` has run, carries each camera agent's notes and how they were checked."""
    if not current_user() and not OPEN:
        abort(401)
    out = _review_out()
    rv, rs, nt = out / "review.json", out / "results.json", out / "notes.json"
    if not rv.is_file():
        return jsonify({"error": "no evaluation yet: run  py -m atlas mcam-eval  (WILDTRACK under ~/AtlasDemo/wildtrack)"}), 404
    rd = lambda f: f.read_text(encoding="utf-8") if f.is_file() else "null"
    return Response('{"review":' + rv.read_text(encoding="utf-8") + ',"results":' + rd(rs) + ',"notes":' + rd(nt) + "}",
                    mimetype="application/json")


@app.get("/api/review/frame/<cam>/<frame>.jpg")
def api_review_frame(cam, frame):
    if not current_user() and not OPEN:
        abort(401)
    if not re.fullmatch(r"C[1-7]", cam) or not re.fullmatch(r"\d{8}", frame):
        abort(404)
    p = REVIEW_DATA / "frames-hd" / cam / f"{frame}.jpg"          # full 1080p when fetched with --hd; labels are scaled, so same boxes
    if not p.is_file():
        p = REVIEW_DATA / "frames" / cam / f"{frame}.jpg"
    if not p.is_file():
        abort(404)
    return send_file(p, mimetype="image/jpeg", max_age=3600)


@app.get("/desk/objects")
def desk_objects_page():
    if not current_user() and not OPEN:
        return redirect("/login?next=/desk/objects")
    did = request.args.get("desk", type=int)
    if did:
        u, d = current_user(), store.desk(did)
        if d and (OPEN or (u and d["owner_id"] == u["id"])):
            session["desk"] = did
    return send_from_directory(STATIC_DIR, "objects.html")


@app.get("/api/objects")
def api_objects():
    """The catalogue: every distinct thing the cameras saw. Filters: camera, label, status, verdict, minutes, watch."""
    OBJ, AUD = _objects_mod()
    desk = need_desk()
    ds = store.for_desk(desk["id"])
    a = request.args
    minutes = a.get("minutes", type=float) or 0
    rows = ds.vision_objects(camera=a.get("camera", ""), label=a.get("label", ""), status=a.get("status", ""),
                             since=time.time() - minutes * 60 if minutes else 0, watch=a.get("watch") == "1",
                             limit=min(1000, a.get("limit", type=int) or 400))
    verdict = a.get("verdict", "")
    if verdict:
        rows = [o for o in rows if (o.get("verdict") or "unverified") == verdict]
    elif a.get("rejected") != "1":
        rows = [o for o in rows if o.get("verdict") != "rejected"]
    names = {n["id"]: n for n in ds.named_things()}
    if a.get("name", type=int):
        rows = [o for o in rows if o.get("name_id") == a.get("name", type=int) and o.get("name_by") != "owner-no"]
    everything = ds.vision_objects(limit=5000)
    facets = {"camera": {}, "label": {}, "verdict": {}, "name": {}}
    for o in everything:
        for k, v in (("camera", o["camera"]), ("label", o["label"]), ("verdict", o.get("verdict") or "unverified")):
            facets[k][v] = facets[k].get(v, 0) + 1
        if o.get("name_id") in names and o.get("name_by") != "owner-no":
            nm = names[o["name_id"]]["name"]
            facets["name"][nm] = facets["name"].get(nm, 0) + 1
    return jsonify({"objects": [_obj_public(o, names) for o in rows], "facets": facets, "workers": OBJ.statuses(desk["id"]),
                    "names": [{"id": n["id"], "name": n["name"], "kind": n["kind"], "notes": n.get("notes") or "",
                               "exemplars": len(n["exemplars"]), "sightings": n.get("sightings") or 0} for n in names.values()],
                    "audit": AUD.latest(), "live": _mode() == "live" and V.vlm_ready()})


@app.get("/api/objects/<int:oid>")
def api_object(oid):
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    ds = store.for_desk(desk["id"])
    try:
        sim = [_obj_public(x) for x in OBJ.similar(store, o)]
    except Exception:
        sim = []
    return jsonify({"object": _obj_public(o), "notes": ds.object_notes(oid, 60), "similar": sim})


@app.get("/api/objects/<int:oid>/<which>.jpg")
def api_object_image(oid, which):
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    if which not in ("crop", "scene"):
        abort(404)
    p = OBJ.obj_dir(desk["id"]) / (f"{oid}.jpg" if which == "crop" else f"{oid}-scene.jpg")
    if not p.is_file():
        abort(404)
    return send_file(p, mimetype="image/jpeg", max_age=0)


def _object_live(desk: dict[str, Any], o: dict[str, Any]):
    """(frame, box, vision model) for an object: the live frame while it is still in view, else its stored pictures."""
    OBJ, _ = _objects_mod()
    w = OBJ.worker(desk["id"], o["camera"])
    frame, box = w.latest(o["id"]) if w else (None, None)
    conn = next((c for c in store.connectors(desk["id"]) if c["kind"] == "camera" and c["name"] == o["camera"]), None)
    return frame, box, str(((conn or {}).get("config") or {}).get("vlm_model") or "")


@app.post("/api/objects/<int:oid>/call")
def api_object_call(oid):
    """Call an object: the vision model examines it now, and (watch on) keeps following it while it stays in view."""
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    d = request.get_json(silent=True) or {}
    on = bool(d.get("on", True))
    ds = store.for_desk(desk["id"])
    ds.update_vision_object(oid, watch=1 if on else 0)
    if not on:
        return jsonify({"ok": True, "object": _obj_public(ds.vision_object(oid))})
    _require_live(desk)
    frame, box, model = _object_live(desk, o)
    try:
        res = OBJ.call(store, o, model=model, frame=frame, box=box)
    except Exception as exc:
        ds.add_object_note(oid, "error", f"vision model unavailable: {str(exc)[:200]}")
        return jsonify({"ok": False, "error": str(exc)[:300], "object": _obj_public(ds.vision_object(oid)), "notes": ds.object_notes(oid, 60)}), 502
    return jsonify({"ok": True, "result": res, "object": _obj_public(ds.vision_object(oid)), "notes": ds.object_notes(oid, 60)})


@app.post("/api/objects/<int:oid>/ask")
def api_object_ask(oid):
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    q = str((request.get_json(force=True) or {}).get("question") or "").strip()[:500]
    if not q:
        return jsonify({"error": "empty question"}), 400
    _require_live(desk)
    frame, box, model = _object_live(desk, o)
    try:
        text = OBJ.ask(store, o, q, model=model, frame=frame, box=box)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:300]}), 502
    return jsonify({"ok": True, "answer": text, "notes": store.for_desk(desk["id"]).object_notes(oid, 60)})


@app.post("/api/objects/<int:oid>/name")
def api_object_name(oid):
    """The owner names this sighting ("Marco", "the Bidfood van"): a new named thing or one more exemplar of one.
    Appearance only - the crop's CLIP embedding - never a face."""
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    d = request.get_json(force=True) or {}
    name = re.sub(r"\s+", " ", str(d.get("name") or "")).strip()[:60]
    if not name:
        return jsonify({"error": "name required"}), 400
    kind = str(d.get("kind") or "").strip().lower()
    if kind not in ("", "person", "vehicle", "object"):
        return jsonify({"error": "kind must be person, vehicle or object"}), 400
    thing = OBJ.name_object(store, o, name, kind=kind, notes=str(d.get("notes") or "").strip()[:300])
    ds = store.for_desk(desk["id"])
    return jsonify({"ok": True, "thing": {k: thing[k] for k in ("id", "name", "kind", "notes")}, "object": _obj_public(ds.vision_object(oid)),
                    "notes": ds.object_notes(oid, 60)})


@app.delete("/api/objects/<int:oid>/name")
def api_object_unname(oid):
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    OBJ.unname_object(store, o)
    ds = store.for_desk(desk["id"])
    return jsonify({"ok": True, "object": _obj_public(ds.vision_object(oid)), "notes": ds.object_notes(oid, 60)})


@app.get("/api/names")
def api_names():
    desk = need_desk()
    ds = store.for_desk(desk["id"])
    out = []
    for n in ds.named_things():
        seen = [o for o in ds.vision_objects(name_id=n["id"], limit=2000) if o.get("name_by") != "owner-no"]
        out.append({"id": n["id"], "name": n["name"], "kind": n["kind"], "notes": n.get("notes") or "", "label": n.get("label") or "",
                    "exemplars": n["exemplars"], "sightings": len(seen),
                    "last_seen": max((o.get("last_ts") or 0 for o in seen), default=None),
                    "cameras": sorted({o["camera"] for o in seen})})
    return jsonify({"names": out})


@app.patch("/api/names/<int:nid>")
def api_name_update(nid):
    desk = need_desk()
    ds = store.for_desk(desk["id"])
    if not ds.named(nid):
        abort(404)
    d = request.get_json(force=True) or {}
    f: dict[str, Any] = {}
    if d.get("name"):
        f["name"] = re.sub(r"\s+", " ", str(d["name"])).strip()[:60]
    if d.get("kind") in ("person", "vehicle", "object"):
        f["kind"] = d["kind"]
    if "notes" in d:
        f["notes"] = str(d.get("notes") or "").strip()[:300]
    ds.update_named(nid, **f)
    n = ds.named(nid)
    return jsonify({"ok": True, "thing": {k: n[k] for k in ("id", "name", "kind", "notes")}})


@app.delete("/api/names/<int:nid>")
def api_name_delete(nid):
    desk = need_desk()
    ds = store.for_desk(desk["id"])
    if not ds.named(nid):
        abort(404)
    ds.delete_named(nid)
    return jsonify({"ok": True})


@app.post("/api/report/day/send")
def api_report_day_send():
    """Queue (or auto-send) the named day's camera report through the alert channel, right now."""
    from .. import alerts as AL
    from .. import report as REP
    desk = need_desk()
    d = request.get_json(silent=True) or {}
    date = d.get("date") or time.strftime("%Y-%m-%d")
    try:
        data = REP.daily(store, desk["id"], date)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    nc = AL.config(desk)
    channel, to = d.get("channel") or nc["channel"], d.get("to") or nc["to"]
    if not channel or not to:
        return jsonify({"error": "set the alert channel and recipient first (Cameras → alert settings)"}), 400
    biz = ((desk.get("config") or {}).get("business") or {}).get("name") or desk.get("name") or ""
    subj, body = AL.report_message(desk, date, REP.markdown(data, biz), channel)
    row = AL.queue(store, desk, channel, to, subj, body, f"daily camera report {date}", nc["auto"], _dispatch)
    return jsonify({"ok": True, "status": row["status"], "action_id": row["id"],
                    "note": "sent" if row["status"] == "sent" else ("queued for your approval" if row["status"] == "pending" else row.get("note") or row["status"])})


@app.get("/api/report/day")
def api_report_day():
    """The daily camera report, by name: ?date=YYYY-MM-DD (today), ?format=md for the readable version."""
    from .. import report as REP
    desk = need_desk()
    date = request.args.get("date") or time.strftime("%Y-%m-%d")
    try:
        data = REP.daily(store, desk["id"], date)
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    if request.args.get("format") == "md":
        biz = ((desk.get("config") or {}).get("business") or {}).get("name") or desk.get("name") or ""
        return Response(REP.markdown(data, biz), mimetype="text/markdown; charset=utf-8")
    return jsonify(data)


@app.post("/api/objects/<int:oid>/verdict")
def api_object_verdict(oid):
    """The owner's word is final: confirm the label, reject the object, or correct the label."""
    desk, o = _object(oid)
    d = request.get_json(force=True) or {}
    v = str(d.get("verdict") or "")
    if v not in ("confirmed", "rejected", ""):
        return jsonify({"error": "verdict must be confirmed, rejected or empty"}), 400
    ds = store.for_desk(desk["id"])
    fields: dict[str, Any] = {"verdict": v, "verdict_by": "owner" if v else ""}
    label = re.sub(r"[^a-z0-9 -]+", "", str(d.get("label") or "").lower()).strip()[:40]
    if label and label != o["label"]:
        fields.update({"label": label, "verdict": "confirmed", "verdict_by": "owner",
                       "attrs": {**(o.get("attrs") or {}), "detector_label": o["label"]}})
        ds.add_object_note(oid, "verify", f"Owner corrected the label: {o['label']} -> {label}.")
    elif v:
        ds.add_object_note(oid, "verify", f"Owner {v} this {o['label']}.")
    ds.update_vision_object(oid, **fields)
    return jsonify({"ok": True, "object": _obj_public(ds.vision_object(oid))})


@app.post("/api/cameras/<int:cid>/watch")
def api_camera_watch(cid):
    desk = need_desk()
    c = _camera(cid, desk)
    d = request.get_json(silent=True) or {}
    existing = [j for j in store.jobs(desk["id"]) if j["kind"] == "camera_watch" and (j["task"] or "").find(f'"connector": "{c["name"]}"') >= 0]
    if not d.get("on", True):
        for j in existing:
            store.delete_job(j["id"])
        return jsonify({"ok": True, "watching": False})
    if existing:
        store.update_job(existing[0]["id"], enabled=1, next_run=time.time())
        return jsonify({"ok": True, "watching": True, "job": store.job(existing[0]["id"])})
    every_s = max(5, min(int(d.get("every_s") or 30), 3600))
    j = store.add_job(desk["id"], "camera_watch", f"Watch {c['name']}", json.dumps({"connector": c["name"], "every_s": every_s}), 1, time.time())
    return jsonify({"ok": True, "watching": True, "job": j})


@app.post("/api/vision/video")
def api_vision_video():
    """Upload a video file (or pass {url}) and have the vision analyst describe it as a timeline.
    The result is logged as a vision event, so 'ask the cameras' can recall it later."""
    desk = need_desk()
    from .. import vision as V
    question = (request.form.get("question") or (request.json or {}).get("question", "") if not request.files else request.form.get("question", "")) or ""
    try:
        frames = min(int(request.form.get("frames") or 6), 10)
    except ValueError:
        frames = 6
    f = request.files.get("video")
    if f and f.filename:
        vdir = Path("data") / "videos" / f"desk{desk['id']}"
        vdir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\- ]+", "_", f.filename)[-80:] or "clip.mp4"
        src = vdir / f"{int(time.time())}_{name}"
        f.save(str(src))
        if src.stat().st_size > 300 * 1024 * 1024:
            src.unlink(missing_ok=True)
            return jsonify({"error": "video too large (max 300 MB)"}), 413
        label = name
        src = str(src)
    else:
        src = str((request.json or {}).get("url", "") or "").strip()
        if not src:
            return jsonify({"error": "attach a video file or pass {url}"}), 400
        from .. import secure as SEC
        why = SEC.private_url_reason(src)
        if why:
            return jsonify({"error": f"refusing to fetch: {why}"}), 400
        label = src.rsplit("/", 1)[-1][:60]
    try:
        res = V.describe_video(src, question, frames=frames, context=desk.get("name", ""))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
    snap = ""
    try:
        snap = V.save_snapshot(desk["id"], "video", res["frames"][0]["jpeg"])
    except Exception:
        pass
    ev = store.add_vision_event(desk["id"], f"video:{label}", {}, backend="vlm", source="video",
                                question=question or "describe", answer=(res["answer"] or "")[:2000], snapshot=snap)
    return jsonify({"ok": True, "answer": res["answer"], "duration_s": res["duration_s"],
                    "frames": len(res["frames"]), "event_id": ev["id"]})


@app.get("/api/theatre/clips")
def api_theatre_clips():
    need_desk()
    d = Path(os.environ.get("THEATRE_CLIPS", "data/theatre_clips"))
    clips = sorted(f.name for f in d.glob("*.mp4")) if d.is_dir() else []
    return jsonify({"clips": clips})


@app.get("/desk/theatre/clip/<path:name>")
def desk_theatre_clip(name):
    need_desk()
    d = Path(os.environ.get("THEATRE_CLIPS", "data/theatre_clips")).resolve()
    f = (d / name).resolve()
    if d not in f.parents or not f.is_file():
        abort(404)
    return send_file(f, mimetype="video/mp4", conditional=True, max_age=3600)


@app.get("/desk/workspace")
def desk_workspace():
    """The cinematic front door: talk to Atlas, watch it assemble the team, build, deploy, see every agent work live."""
    if not current_user() and not OPEN:
        return redirect("/login?next=/desk/workspace")
    did = request.args.get("desk", type=int)
    if did:                                                # a link to a desk opens THAT desk (same rule as /select), not the cookie's
        u, d = current_user(), store.desk(did)
        if d and (OPEN or (u and d["owner_id"] == u["id"])):
            session["desk"] = did
    return send_from_directory(STATIC_DIR, "workspace.html")


@app.get("/desk/theatre")
def desk_theatre():
    need_desk()
    return send_from_directory(STATIC_DIR, "theatre.html")


@app.post("/api/vision/detect")
def api_vision_detect():
    """Local YOLO on one frame — milliseconds, free, no external call. Powers the theatre's live overlay."""
    need_desk()
    from .. import vision as V
    j = request.json or {}
    try:
        jpeg = base64.b64decode(j.get("image", ""))
    except Exception:
        jpeg = b""
    if len(jpeg) < 100:
        return jsonify({"error": "no frame"}), 400
    if not V.DETECTOR.available:
        return jsonify({"detector": "", "boxes": [], "counts": {}})
    dets = V.DETECTOR.detect(jpeg)
    return jsonify({"detector": "yolo", "boxes": dets, "counts": V.counts(dets)})


@app.post("/api/vision/live")
def api_vision_live():
    """Live theatre: one frame in, commentary tokens streamed straight back (chunked text).
    The finished note is logged as a vision event so the RAG log covers live watching too."""
    desk = need_desk()
    from .. import vision as V
    j = request.json or {}
    try:
        jpeg = base64.b64decode(j.get("image", ""))
    except Exception:
        jpeg = b""
    if len(jpeg) < 100:
        return jsonify({"error": "no frame"}), 400
    camera = re.sub(r"[^\w.\- ]+", "_", str(j.get("camera", "feed")))[:60] or "feed"
    model = str(j.get("model", "") or "")
    context = str(j.get("context", "") or "")
    t = str(j.get("t", "") or "")
    _base, _key, chosen = V._vlm_cfg(model)

    def gen():
        parts = []
        try:
            for delta in V.describe_stream(jpeg, "What is happening in this frame? What changed?", model=model,
                                           context=context):
                if isinstance(delta, dict):                    # provider/model meta arrives first
                    yield "\x01" + json.dumps(delta) + "\n"
                    continue
                parts.append(delta)
                yield delta
        except Exception as exc:
            yield f"[vision error: {exc}]"
        note = "".join(parts).strip()
        if note and not note.startswith("[vision error"):
            try:
                snap = V.save_snapshot(desk["id"], f"live-{camera}", jpeg)
                store.add_vision_event(desk["id"], f"live:{camera}", {}, backend="vlm", source="live",
                                       question=f"live frame at clip {t}s" if t else "live frame",
                                       answer=note[:1000], snapshot=snap)
            except Exception:
                pass

    resp = Response(stream_with_context(gen()), mimetype="text/plain")
    resp.headers["X-Vision-Model"] = chosen
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/vision/journal")
def api_vision_journal():
    """The readable camera diary for one day (Markdown), optionally one camera. ?download=1 serves it as a file."""
    desk = need_desk()
    from .. import journal as JR
    day = str(request.args.get("date") or "")
    camera = str(request.args.get("camera") or "")
    text = JR.diary_read(desk["id"], day, camera)
    path = JR.diary_path(desk["id"], day)
    if request.args.get("format") == "html":
        import html as _html
        days = JR.diary_days(desk["id"])
        cams = sorted({c["name"] for c in store.connectors(desk["id"]) if c["kind"] == "camera"})
        parts = []
        for block in re.split(r"(?m)^(?=### )", text or ""):
            block = block.strip()
            if not block:
                continue
            if block.startswith("# "):
                continue
            head, _, body = block.partition("\n")
            head = head.lstrip("# ").strip()
            summary = " Summary " in f" {head} "
            parts.append(f'<article class="{"sum" if summary else "note"}"><h3>{_html.escape(head)}</h3>'
                         + "".join(f"<p>{_html.escape(line)}</p>" for line in body.strip().splitlines() if line.strip()) + "</article>")
        nav = " ".join(f'<a href="?format=html&date={d}{"&camera=" + camera if camera else ""}"{" class=on" if d == path.stem else ""}>{d}</a>' for d in days[:14])
        camnav = " ".join([f'<a href="?format=html&date={path.stem}"{" class=on" if not camera else ""}>all cameras</a>']
                          + [f'<a href="?format=html&date={path.stem}&camera={_html.escape(c)}"{" class=on" if c == camera else ""}>{_html.escape(c)}</a>' for c in cams])
        page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera journal {path.stem}</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#667085;--line:#e4e7ec;--accent:#2563eb;--sum:#eef4ff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#171a21;--ink:#e6e8ec;--mute:#98a2b3;--line:#2a2f3a;--accent:#7aa2ff;--sum:#1a2233}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:24px 16px 60px}} h1{{font-size:22px;margin:0 0 4px}} .mute{{color:var(--mute);font-size:13px}}
nav{{margin:12px 0;display:flex;flex-wrap:wrap;gap:6px}} nav a{{font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:99px;color:var(--ink);text-decoration:none;background:var(--card)}}
nav a.on{{border-color:var(--accent);color:var(--accent)}}
article{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:10px 0}}
article.sum{{background:var(--sum);border-color:var(--accent)}} h3{{margin:0 0 6px;font-size:13px;color:var(--mute);font-weight:600}}
article.sum h3{{color:var(--accent)}} p{{margin:4px 0;overflow-wrap:anywhere}}
</style></head><body><main><h1>Camera journal</h1><div class="mute">{_html.escape(path.stem)} · {len(parts)} entries · newest at the bottom ·
<a href="?download=1&date={path.stem}{"&camera=" + _html.escape(camera) if camera else ""}">download</a></div>
<nav>{camnav}</nav><nav>{nav}</nav>{"".join(parts) or '<p class="mute">Nothing written yet for this day.</p>'}</main></body></html>"""
        return Response(page, mimetype="text/html; charset=utf-8")
    if request.args.get("download") or request.args.get("format") == "text":
        body = text or f"No journal entries for {path.stem}" + (f" on {camera}" if camera else "") + ".\n"
        headers = {"Content-Disposition": f'attachment; filename="journal-{path.stem}.md"'} if request.args.get("download") else {}
        return Response(body, mimetype="text/plain; charset=utf-8", headers=headers)
    return jsonify({"date": path.stem, "camera": camera, "markdown": text, "days": JR.diary_days(desk["id"])})


@app.get("/api/vision/events")
def api_vision_events():
    desk = need_desk()
    try:
        hours = float(request.args.get("hours") or 24)
    except ValueError:
        hours = 24.0
    rows = store.vision_events(desk["id"], request.args.get("camera", ""), time.time() - hours * 3600,
                               request.args.get("q", ""), min(int(request.args.get("limit") or 100), 500),
                               request.args.get("alerts") in ("1", "true"))
    return jsonify([_vev_public(r) for r in rows])


@app.get("/api/vision/events/<int:vid>")
def api_vision_event(vid):
    """One event by id, for citation pop-overs: the note, counts, reason and snapshot url."""
    desk = need_desk()
    ev = store.vision_events_by_ids([vid])
    ev = ev[0] if ev and ev[0].get("desk_id") == desk["id"] else None
    if not ev:
        abort(404)
    return jsonify(_vev_public(ev))


@app.get("/api/vision/snapshot/<int:vid>")
def api_vision_snapshot(vid):
    desk = need_desk()
    e = store.vision_event(vid)
    if not e or e["desk_id"] != desk["id"] or not e.get("snapshot") or not Path(e["snapshot"]).is_file():
        abort(404)
    return send_file(e["snapshot"], mimetype="image/jpeg", max_age=3600)


def _rag_rows(desk_id: int, question: str, camera: str, hours: float, limit: int = 60) -> list[dict[str, Any]]:
    """Retrieval for 'ask the cameras' (kept for callers): hybrid semantic + keyword + time-window, see atlas/rag.py."""
    return RAG.retrieve(store, desk_id, question, hours, camera, k=limit)["rows"]


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/api/vision/demo")
def api_vision_demo():
    """Public site demo: one small frame + the browser detector's counts in, the vision model's narration streamed
    out as SSE (`data: {"t": "..."}` per token, then `{"done": true}` or `{"error": "..."}`). Rate limited per IP,
    a few concurrent slots, tiny answers, subject to the spend cap — real model, no scripted fallback."""
    import base64

    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    hdr = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if not _RL_VISION.allow(ip):
        return Response(_sse({"error": "rate limited - one narration every few seconds per visitor"}), 429, mimetype="text/event-stream", headers=hdr)
    data = request.get_json(silent=True) or {}
    raw = str(data.get("image") or "")
    if "," in raw[:40]:
        raw = raw.split(",", 1)[1]
    try:
        jpeg = base64.b64decode(raw, validate=False)
    except Exception:
        jpeg = b""
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 400_000:
        return Response(_sse({"error": "expected a JPEG frame under 400 KB"}), 400, mimetype="text/event-stream", headers=hdr)
    counts = {}
    for k, v in list((data.get("counts") or {}).items())[:12]:
        try:
            counts[re.sub(r"[^a-z ]", "", str(k).lower())[:24]] = max(0, min(99, int(v)))
        except (TypeError, ValueError):
            continue
    prev = re.sub(r"\s+", " ", str(data.get("prev") or ""))[:400]
    source = re.sub(r"[^a-zA-Z ]", "", str(data.get("source") or "camera"))[:40]
    base, key, model = _demo_cfg()
    if not key:
        return Response(_sse({"error": "no vision model key configured on the server"}), 503, mimetype="text/event-stream", headers=hdr)
    if ":free" not in model:
        blocked = _spend_blocked()
        if blocked:
            return Response(_sse({"error": blocked}), 503, mimetype="text/event-stream", headers=hdr)
    jpeg = V._shrink(jpeg, 448)
    counts_txt = ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing above threshold"
    system = ("You narrate a live camera feed for a small business's operations desk, one frame at a time. "
              "Say only what is visible. Count carefully; the on-device detector's counts are a hint, not the truth - "
              "if they look wrong say what you actually see. Note what changed since the previous narration when there was one, "
              "otherwise describe the scene. 25 to 45 words, plain text, one paragraph, no markdown, no lists. "
              "Never identify a person, never read number plates or text that could identify someone.")
    user_txt = (f"Source: {source}. Detector counts this frame: {counts_txt}. "
                + (f"Previous narration: \"{prev}\" " if prev else "This is the first frame. ")
                + "Narrate now.")
    payload = {"model": model, "max_tokens": 110, "temperature": 0.2, "stream": True,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": [{"type": "text", "text": user_txt},
                                                         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}]}]}
    headers = _demo_headers(key, "Atlas Desks site vision demo")

    return Response(_demo_stream(base, headers, payload, model), mimetype="text/event-stream", headers=hdr)


def _demo_cfg() -> tuple[str, str, str]:
    """Provider for the public site demos. VISION_DEMO_KEY / VISION_DEMO_MODEL let the demo run on its own key and a
    free model while the desks stay in scripted mode (DESK_MODE=demo)."""
    base, key, model = V._vlm_cfg(os.environ.get("VISION_DEMO_MODEL", ""))
    key = (os.environ.get("VISION_DEMO_KEY") or key or "").strip()
    return base, key, model


def _demo_headers(key: str, title: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream",
            "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": title}


def _demo_stream(base: str, headers: dict[str, str], payload: dict[str, Any], model: str, first: dict[str, Any] | None = None):
    """SSE generator shared by the site demos: one concurrency slot, tokens as {"t": ...}, then {"done": true}."""
    import httpx
    t0 = time.time()
    if not _VISION_SLOTS.acquire(blocking=False):          # acquired inside the generator so a slot is never leaked
        yield _sse({"error": "busy - a few other visitors are using the demo right now, retrying shortly"})
        return
    try:
        if first:
            yield _sse(first)
        with httpx.Client(timeout=60) as c, c.stream("POST", base + "/chat/completions", headers=headers, json=payload) as r:
            if r.status_code >= 400:
                body = b"".join(r.iter_bytes())[:200].decode("utf-8", "replace")
                yield _sse({"error": f"model HTTP {r.status_code}: {body}"})
                return
            sent = 0
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    j = json.loads(chunk)
                except ValueError:
                    continue
                if j.get("error"):
                    yield _sse({"error": str(j["error"].get("message") if isinstance(j["error"], dict) else j["error"])[:200]})
                    return
                for ch in j.get("choices") or []:
                    t = (ch.get("delta") or {}).get("content") or ""
                    if t:
                        sent += len(t)
                        yield _sse({"t": t, "model": model.split("/")[-1]})
            if not sent:
                yield _sse({"error": "model returned no text"})
                return
            yield _sse({"done": True, "model": model.split("/")[-1], "ms": int((time.time() - t0) * 1000)})
    except Exception as exc:
        yield _sse({"error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    finally:
        _VISION_SLOTS.release()


_ASK_STOP = {"the", "was", "were", "what", "when", "how", "many", "did", "there", "any", "last", "this", "that", "and",
             "with", "from", "have", "has", "been", "are", "you", "see", "camera", "cameras", "time", "times"}


@app.post("/api/vision/demo/ask")
def api_vision_demo_ask():
    """Public site demo, the RAG half: the browser sends the question plus its own event log (one entry per narration:
    counts + analyst text + clock time). We retrieve the best-matching events, tell the browser which ones ([#n]) were
    used, then stream the model's answer, which must cite them. Stateless - nothing is stored server side."""
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    hdr = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if not _RL_VISION.allow(ip):
        return Response(_sse({"error": "rate limited - a few questions a minute per visitor"}), 429, mimetype="text/event-stream", headers=hdr)
    data = request.get_json(silent=True) or {}
    q = re.sub(r"\s+", " ", str(data.get("question") or "")).strip()[:300]
    if len(q) < 3:
        return Response(_sse({"error": "ask a question first"}), 400, mimetype="text/event-stream", headers=hdr)
    events: list[dict[str, Any]] = []
    for raw in list(data.get("events") or [])[:80]:
        if not isinstance(raw, dict):
            continue
        try:
            n = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        counts = {}
        for k, v in list((raw.get("counts") or {}).items())[:12]:
            try:
                counts[re.sub(r"[^a-z ]", "", str(k).lower())[:24]] = max(0, min(99, int(v)))
            except (TypeError, ValueError):
                continue
        events.append({"n": n, "time": re.sub(r"[^0-9:]", "", str(raw.get("time") or ""))[:8],
                       "camera": re.sub(r"[^A-Za-z0-9 ]", "", str(raw.get("camera") or ""))[:32],
                       "counts": counts, "text": re.sub(r"\s+", " ", str(raw.get("text") or ""))[:400]})
    if not events:
        return Response(_sse({"error": "no events yet - let the analyst narrate a few frames first"}), 400, mimetype="text/event-stream", headers=hdr)
    words = {w for w in re.findall(r"[a-z]{3,}", q.lower()) if w not in _ASK_STOP}

    def score(e: dict[str, Any]) -> int:
        blob = (" ".join(f"{v} {k}" for k, v in e["counts"].items()) + " " + e["text"]).lower()
        return sum(1 for w in words if w in blob)
    ranked = sorted(events, key=lambda e: (score(e), e["n"]), reverse=True)
    picked = sorted(ranked[:12], key=lambda e: e["n"])
    used = [e["n"] for e in picked]
    base, key, model = _demo_cfg()
    if not key:
        return Response(_sse({"error": "no model key configured on the server"}), 503, mimetype="text/event-stream", headers=hdr)
    if ":free" not in model:
        blocked = _spend_blocked()
        if blocked:
            return Response(_sse({"error": blocked}), 503, mimetype="text/event-stream", headers=hdr)
    log = "\n".join(f"[#{e['n']}] {e['time']} | " + (f"camera: {e['camera']} | " if e["camera"] else "") + (", ".join(f"{v} {k}" for k, v in e['counts'].items()) or "nothing detected")
                    + (f" | analyst: {e['text']}" if e["text"] else "") for e in picked)
    system = ("You answer questions about a camera feed using ONLY the event log below, which an operations desk built from "
              "the feed (one line per narrated frame: clock time, camera name, detector counts, analyst note). Cite the events you rely on "
              "as [#n]. If the log cannot answer, say so plainly and say what would be needed. 30 to 70 words, plain text, "
              "no markdown, no lists. Never identify a person.\n\nEVENT LOG\n" + log)
    payload = {"model": model, "max_tokens": 160, "temperature": 0.2, "stream": True,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": q}]}
    headers = _demo_headers(key, "Atlas Desks site vision ask")
    return Response(_demo_stream(base, headers, payload, model, first={"used": used, "of": len(events)}),
                    mimetype="text/event-stream", headers=hdr)


@app.post("/api/vision/ask")
def api_vision_ask():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    q = str(d.get("question") or "").strip()
    if not q:
        return jsonify({"error": "question required"}), 400
    try:
        hours = float(d.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24.0
    configs = desk_configs(desk)
    text_answer = None
    if _mode() != "demo":
        from ..providers import ProviderPool
        atlas_agent = next((a for a in configs["agents"] if a["id"] == "atlas"), {})
        prov = ProviderPool(configs["providers"]).get(atlas_agent.get("provider") or "")

        def text_answer(system: str, prompt: str) -> str:   # noqa: F811 - text-only fallback on the desk's Atlas model
            return (prov.chat(system, [prov.user_message(prompt)], [], model=atlas_agent.get("model", "")).text or "").strip()
    try:
        vlm = str(d.get("model") or "") or (DS.PAID_VLM if (desk.get("tier") or "free") != "free" else "")
        res = RAG.ask(store, desk["id"], q, hours, str(d.get("camera") or ""), configs["business"], mode=_mode(),
                      text_answer=text_answer, vlm_model=vlm)
    except Exception as exc:
        return jsonify({"error": f"model error: {type(exc).__name__}: {str(exc)[:200]}"}), 502
    return jsonify({"answer": res["answer"], "evidence": [_vev_public(r) for r in res["evidence"]], "mode": _mode(),
                    "events_considered": res["events_considered"], "retrieval": res["retrieval"]})


@app.route("/hook/<token>/vision", methods=["GET", "POST"])
def hook_vision(token):
    """External detectors post here: ESP32-CAM / PIR nodes, Frigate, NVR alarm outputs, Home Assistant.
    JSON {camera, labels: {"person": 2} | ["person","person"], note, image (base64 JPEG, optional), trigger (default true),
    ts (optional: when it happened, epoch seconds or ISO 8601; an unreadable value means arrival time)}."""
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    if request.method == "GET":
        return jsonify({"ok": True, "desk": desk["name"], "post": "JSON {camera, labels, note, image(base64 jpeg), trigger}"})
    d = request.get_json(silent=True) or request.form.to_dict() or {}
    camera = str(d.get("camera") or d.get("source") or d.get("device") or "sensor").strip()[:60]
    labels = d.get("labels") or d.get("objects") or {}
    counts: dict[str, int] = {}
    if isinstance(labels, dict):
        for k, v in labels.items():
            try:
                counts[V._norm_label(str(k))] = int(v)
            except (TypeError, ValueError):
                continue
    elif isinstance(labels, list):
        for k in labels:
            kk = V._norm_label(str(k))
            counts[kk] = counts.get(kk, 0) + 1
    elif isinstance(labels, str) and labels.strip():
        for k in labels.split(","):
            if k.strip():
                kk = V._norm_label(k)
                counts[kk] = counts.get(kk, 0) + 1
    note = str(d.get("note") or d.get("message") or d.get("event") or "").strip()[:500]
    snap = ""
    if d.get("image"):
        try:
            import base64 as _b64
            raw = _b64.b64decode(str(d["image"]).split(",")[-1])
            if raw[:2] == b"\xff\xd8":
                snap = V.save_snapshot(desk["id"], camera, V.annotate(V._shrink(raw), [], f"{time.strftime('%d %b %H:%M:%S')}  {camera}: {note[:60]}"))
        except Exception:
            snap = ""
    trigger = str(d.get("trigger", "1")).lower() not in ("0", "false", "no")
    backend = "".join(ch for ch in str(d.get("backend") or "external")[:32] if ch.isalnum() or ch in "/-_.") or "external"
    ts = None
    if d.get("ts") not in (None, ""):                 # a forwarded or replayed event keeps the time it happened
        from .. import cyber as CY
        ts = CY.parse_time(d.get("ts"))
    dstore = store.for_desk(desk["id"])
    ev = dstore.add_vision_event(camera, counts, motion=float(d.get("motion") or 0), backend=backend,
                                 reason=note or "external event", snapshot=snap, triggered=trigger, source="hook", ts=ts)
    rid = ""
    if trigger:
        when = time.strftime("%A %d %B %H:%M", time.localtime(ts) if ts is not None else time.localtime())
        task = ("Assess this sensor/camera event, log it, and tell the right person only if it matters.\n\n"
                f"EXTERNAL EVENT — {camera} at {when}\n"
                f"Reported: {V.counts_text(counts) if counts else 'no object counts'}" + (f"; note: {note}" if note else "") + "\n"
                f"Event id: {ev['id']}." + (" Snapshot attached." if snap else "") + " Use camera_events for history; camera_look works only for cameras the desk can reach itself.")
        if C.enabled(desk, "camera_cases"):
            _preflight(desk)
            rid = C.camera_alert(store, desk, camera, "CAMERA ALERT (external sensor)\n" + task, note or "external event", ev["id"])
        else:
            rid = _start_run(desk, task, "auto")
        dstore.set_vision_run(ev["id"], rid)
    return jsonify({"ok": True, "event_id": ev["id"], "run_id": rid})


# ---------------------------------------------------------------------------- security desk (cyber)
# Log lines come in through the logs hook, uploads and the log_replay / log_watch jobs (scheduler.cyber_*), are parsed
# and checked by deterministic rules (atlas/cyber.py), and runs only PROPOSE containment: a person approves it, then
# _dispatch carries it out through a named HTTP connector or records a simulated action. Every log-derived string is
# attacker-controlled data: these routes return it only as JSON fields and the page renders it as text.
HOOK_LOGS_MAX_BYTES = 2 * 1024 * 1024
HOOK_LOGS_MAX_ITEMS = 5000
UPLOAD_MAX_LINES = 500_000
UPLOAD_MAX_TEXT_BYTES = 1024 * 1024 * 1024           # decompressed: a small .gz cannot expand without bound
UPLOAD_LINE_MAX = 64 * 1024
try:                                                 # a long film take (a 60x replay, then the run) passes 20 MB
    CYBER_REC_MAX_BYTES = int(max(1.0, float(os.environ.get("CYBER_REC_MAX_MB") or 20)) * 1024 * 1024)
except ValueError:
    CYBER_REC_MAX_BYTES = 20 * 1024 * 1024
_REC_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_GRAPH_MEMO: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()   # exact: same query, same data
_GRAPH_RECENT: "OrderedDict[tuple, tuple[float, dict[str, Any]]]" = OrderedDict()   # same query, data still arriving
_GRAPH_MEMO_LOCK = threading.Lock()
# while a replay or a busy hook keeps adding events, one query's graph is rebuilt at most this often (a rebuild over
# ~140k events costs about 2 s of CPU); the answer is always a real graph of the events it reports in `window`
CYBER_GRAPH_MIN_S = float(os.environ.get("CYBER_GRAPH_MIN_S", "10"))


def _cy_err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _cyber_conf(desk: dict[str, Any]) -> dict[str, Any]:
    return (desk.get("config") or {}).get("cyber") or {}


def _upload_max_bytes() -> int:
    try:
        mb = float(os.environ.get("CYBER_UPLOAD_MAX_MB", "50") or 50)
    except ValueError:
        mb = 50.0
    return int(mb * 1024 * 1024)


def _desk_modes(desk: dict[str, Any]) -> set[str]:
    wfs = (desk.get("config") or {}).get("workflows") or templates.get(desk.get("template") or DEFAULT_TEMPLATE)["workflows"]
    return {"auto"} | {str(w.get("id")) for w in wfs or [] if isinstance(w, dict)}


def _ip_or_text(v: Any, limit: int = 200) -> str:
    """Stored addresses are in compressed form (2001:db8::1): a typed IP is normalised the same way."""
    s = str(v or "").strip()[:limit]
    try:
        return ipaddress.ip_address(s).compressed
    except ValueError:
        return s


def _cy_time(args, key: str) -> float | None:
    from .. import cyber as CY
    raw = args.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    t = CY.parse_time(raw)
    if t is None:
        raise ValueError(f"{key} must be an ISO time or epoch seconds")
    return t


def _cy_filters(args) -> dict[str, Any]:
    """Query-string filters for /api/cyber/events -> DeskStore.sec_events keyword arguments."""
    from .. import cyber as CY
    f: dict[str, Any] = {}
    for k in ("since", "until"):
        t = _cy_time(args, k)
        if t is not None:
            f[k] = t
    if args.get("after_id"):
        try:
            f["after_id"] = max(0, int(args.get("after_id")))
        except ValueError:
            raise ValueError("after_id must be a number")
    if args.get("ids"):
        try:
            ids = [int(x) for x in str(args.get("ids")).split(",") if x.strip()]
        except ValueError:
            raise ValueError("ids must be a comma list of event ids")
        if len(ids) > 200:
            raise ValueError("at most 200 ids")
        f["ids"] = ids
    for k in ("sensor", "source", "sig"):
        v = str(args.get(k) or "").strip()[:200]
        if v:
            f[k] = v
    if args.get("user"):
        f["user"] = str(args.get("user"))[:128]           # exactly as logged
    for k in ("src", "dst", "ip"):
        v = _ip_or_text(args.get(k))
        if v:
            f[k] = v
    kinds = [k.strip() for k in str(args.get("kind") or "").split(",") if k.strip()]
    if kinds:
        f["kind"] = kinds if len(kinds) > 1 else kinds[0]
    sev = str(args.get("min_severity") or "").strip().lower()
    if sev:
        if sev not in CY.SEVERITIES:
            raise ValueError(f"min_severity must be one of {', '.join(CY.SEVERITIES)}")
        f["min_severity"] = sev
    return f


def _cy_options(d: dict[str, Any], desk: dict[str, Any], tmin_fallback: str) -> dict[str, Any]:
    """Shared parse options + trigger options of the hook and the upload. Raises ValueError with the reason."""
    from .. import cyber as CY
    out: dict[str, Any] = {"sensor": CY.safe_text(d.get("sensor"), 60)}
    year = d.get("year")
    if year in (None, ""):
        out["year"] = None
    else:
        try:
            out["year"] = int(year)
        except (TypeError, ValueError):
            raise ValueError("year must be a number such as 2012")
        if not 1970 <= out["year"] <= 2100:
            raise ValueError("year must be between 1970 and 2100")
    out["tz"] = str(d.get("tz") if d.get("tz") is not None else "UTC").strip()
    out["zeek_path"] = str(d.get("zeek_path") or "").strip().lower()
    if out["zeek_path"] not in ("", "notice", "ssh", "conn"):
        raise ValueError("zeek_path must be notice, ssh or conn")
    out["trigger"] = scheduler._flag(d.get("trigger"), True)
    tmin = str(d.get("trigger_min") or "").strip().lower()
    if tmin and tmin not in CY.SEVERITIES:
        raise ValueError(f"trigger_min must be one of {', '.join(CY.SEVERITIES)}")
    out["trigger_min"] = scheduler.cyber_trigger_min(tmin, desk, tmin_fallback)
    out["mode"] = str(d.get("mode") or "").strip()
    if out["mode"] and out["mode"] not in _desk_modes(desk):
        raise ValueError(f"unknown mode '{out['mode'][:40]}' for this desk")
    return out


@app.route("/hook/<token>/logs", methods=["GET", "POST"])
def hook_logs(token):
    """Log shippers post here (token-addressed; rate limited like every /hook/ path, so send batches): raw lines in
    one of the parsed formats, or structured events. At most 5,000 lines or events and 2 MB per POST.
    JSON {fmt, sensor, lines: [...] | text, year, tz, zeek_path, trigger, trigger_min, mode} or {events: [...]};
    any other content type is plain text lines with the options in the query string."""
    desk = store.desk_by_token(token)
    if not desk:
        return _cy_err("unknown hook token", 404)
    from .. import cyber as CY
    if request.method == "GET":
        return jsonify({"ok": True, "desk": desk["name"], "formats": list(CY.FORMATS),
                        "post": "JSON {fmt, sensor, lines:[...]} | JSON {events:[...]} | text/plain lines with ?fmt=&sensor="})
    if (request.content_length or 0) > HOOK_LOGS_MAX_BYTES:
        return _cy_err("body too large: at most 2 MB per POST (send smaller batches)", 413)
    request.max_content_length = HOOK_LOGS_MAX_BYTES      # also bounds a body sent without a Content-Length
    if request.is_json:
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            return _cy_err("body must be a JSON object")
    else:
        d = {k: request.args.get(k) for k in ("fmt", "sensor", "year", "tz", "zeek_path", "trigger", "trigger_min", "mode")
             if request.args.get(k) is not None}
        d["text"] = request.get_data(as_text=True)
    try:
        o = _cy_options(d, desk, "high")
    except ValueError as exc:
        return _cy_err(str(exc))
    if d.get("events") is not None:
        items = d["events"]
        if not isinstance(items, list):
            return _cy_err("events must be a list of objects")
        if len(items) > HOOK_LOGS_MAX_ITEMS:
            return _cy_err(f"too many events: at most {HOOK_LOGS_MAX_ITEMS:,} per POST (send smaller batches)", 413)
        events = [e for e in (CY.normalize_event(x, sensor=o["sensor"] or "hook") for x in items) if e]
        fmt, n_lines, parsed, skipped = "custom", len(items), len(events), len(items) - len(events)
    else:
        lines = d.get("lines")
        if lines is None and isinstance(d.get("text"), str):
            lines = d["text"].splitlines()
        if not isinstance(lines, list):
            return _cy_err("send lines (a list of strings), text, or events")
        if len(lines) > HOOK_LOGS_MAX_ITEMS:
            return _cy_err(f"too many lines: at most {HOOK_LOGS_MAX_ITEMS:,} per POST (send smaller batches)", 413)
        if not all(isinstance(x, str) for x in lines):
            return _cy_err("lines must be strings")
        fmt = str(d.get("fmt") or "auto").strip().lower()
        if fmt == "auto":
            fmt, opts = CY.detect_format([x for x in lines if x.strip()][:50])
            if not fmt:
                return _cy_err(f"could not detect the log format: pass fmt ({', '.join(CY.FORMATS)})")
            o["zeek_path"] = o["zeek_path"] or (opts or {}).get("zeek_path") or ""
        elif fmt not in CY.FORMATS:
            return _cy_err(f"unknown fmt '{fmt[:20]}': use auto or one of {', '.join(CY.FORMATS)}")
        try:
            events, st = CY.parse(fmt, lines, sensor=o["sensor"], year=o["year"], tz=o["tz"], zeek_path=o["zeek_path"])
        except ValueError as exc:
            return _cy_err(str(exc)[:200])
        n_lines, parsed, skipped = st.get("lines", len(lines)), st.get("events", len(events)), st.get("skipped", 0)
    res = scheduler.cyber_ingest(store, desk, events, "hook", _start_run, trigger=o["trigger"],
                                 trigger_min=o["trigger_min"], mode=o["mode"])
    return jsonify({"ok": True, "fmt": fmt, "sensor": o["sensor"] or (events[0]["sensor"] if events else ""),
                    "lines": n_lines, "parsed": parsed, "skipped": skipped, "inserted": res["inserted"],
                    "first_id": res["first_id"], "last_id": res["last_id"], "first_ts": res["first_ts"],
                    "last_ts": res["last_ts"], "detect": res["detect"], "new": res["new"][:10], "run_id": res["run_id"]})


def _upload_lines(path: Path, info: dict[str, Any]):
    """Lines of an uploaded log, plain or gzip (by its magic bytes), bounded: UPLOAD_MAX_LINES lines and
    UPLOAD_MAX_TEXT_BYTES decompressed; a line over 64 KB keeps its start. A bound or a damaged end of file stops the
    read and is reported in `info` (the lines before it still count)."""
    import gzip
    import zlib
    with open(path, "rb") as raw:
        magic = raw.read(6)
        raw.seek(0)
        if magic.startswith(b"7z\xbc\xaf\x27\x1c") or magic.startswith(b"PK\x03\x04"):
            raise ValueError("unpack the archive first")
        fh = gzip.GzipFile(fileobj=raw) if magic[:2] == b"\x1f\x8b" else raw
        total = n = 0
        while True:
            try:
                line = fh.readline(UPLOAD_LINE_MAX)
                total += len(line)
                if len(line) >= UPLOAD_LINE_MAX and not line.endswith(b"\n"):
                    while total <= UPLOAD_MAX_TEXT_BYTES:      # skip the rest of an overlong line
                        more = fh.readline(UPLOAD_LINE_MAX)
                        total += len(more)
                        if not more or more.endswith(b"\n"):
                            break
            except (OSError, EOFError, zlib.error) as exc:
                if not n:
                    raise ValueError(f"cannot read the file ({type(exc).__name__}): is it a valid log or .gz?")
                info["read_error"] = f"the file ends early or is damaged after line {n:,} ({type(exc).__name__})"
                return
            if not line:
                return
            if n >= UPLOAD_MAX_LINES or total > UPLOAD_MAX_TEXT_BYTES:
                info["truncated"] = True
                return
            n += 1
            yield line.decode("utf-8", "replace").rstrip("\r\n")


@app.post("/api/cyber/upload")
def api_cyber_upload():
    """Multipart `file` (plain or .gz; unpack .7z/.zip first) + form fields fmt (auto), sensor, year, tz, zeek_path,
    trigger (1), trigger_min (medium: an owner who uploads a file wants it assessed), mode. Parsed as a stream,
    stored with origin upload:<name>, then one detection pass."""
    desk = need_desk()
    from .. import cyber as CY
    cap = _upload_max_bytes()
    if (request.content_length or 0) > cap:
        return _cy_err(f"file too large: at most {cap / 1048576:g} MB (CYBER_UPLOAD_MAX_MB)", 413)
    request.max_content_length = cap
    f = request.files.get("file")
    if not f or not f.filename:
        return _cy_err("attach the log file as 'file'")
    base = os.path.basename(str(f.filename).replace("\\", "/"))
    name = re.sub(r"[^\w.\- ]+", "_", base).strip(" .")[-60:] or "upload.log"
    if name.lower().endswith((".7z", ".zip")):
        return _cy_err("unpack the archive first")
    form = request.form.to_dict()
    try:
        o = _cy_options(form, desk, "medium")
    except ValueError as exc:
        return _cy_err(str(exc))
    fmt = str(form.get("fmt") or "auto").strip().lower()
    if fmt != "auto" and fmt not in CY.FORMATS:
        return _cy_err(f"unknown fmt '{fmt[:20]}': use auto or one of {', '.join(CY.FORMATS)}")
    updir = cfg.DATA_DIR / "cyber" / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    tmp = updir / f"{secrets.token_hex(8)}.upload"
    origin = f"upload:{name}"
    info: dict[str, Any] = {}
    st: dict[str, Any] = {}
    tot: dict[str, Any] = {"inserted": 0, "first_ts": None, "last_ts": None, "sensor": ""}
    it = None
    try:
        f.save(str(tmp))
        it = _upload_lines(tmp, info)
        head: list[str] = []
        try:
            for line in it:
                head.append(line)
                if sum(1 for x in head if x.strip()) >= 50:
                    break
        except ValueError as exc:
            return _cy_err(str(exc))
        if fmt == "auto":
            fmt, opts = CY.detect_format([x for x in head if x.strip()][:50], name)
            if not fmt:
                return _cy_err(f"could not detect the log format of {name}: choose fmt ({', '.join(CY.FORMATS)})")
            o["zeek_path"] = o["zeek_path"] or (opts or {}).get("zeek_path") or ""

        def flush(chunk: list[dict[str, Any]]) -> None:
            r = scheduler.cyber_insert(store, desk, chunk, origin)
            tot["inserted"] += r["inserted"]
            for k, pick in (("first_ts", min), ("last_ts", max)):
                if r[k] is not None:
                    tot[k] = r[k] if tot[k] is None else pick(tot[k], r[k])
            tot["sensor"] = tot["sensor"] or chunk[0].get("sensor", "")

        chunk: list[dict[str, Any]] = []
        try:
            for ev in CY.iter_parse(fmt, itertools.chain(head, it), sensor=o["sensor"], year=o["year"], tz=o["tz"],
                                    zeek_path=o["zeek_path"], stats=st):
                chunk.append(ev)
                if len(chunk) >= 5000:
                    flush(chunk)
                    chunk = []
        except ValueError as exc:                  # a bad tz is refused before the first event
            if not tot["inserted"] and not chunk:
                return _cy_err(str(exc)[:200])
            info["read_error"] = str(exc)[:200]
        if chunk:
            flush(chunk)
    finally:
        if it is not None:
            it.close()
        try:
            tmp.unlink()
        except OSError:
            pass
    det = scheduler.cyber_detect(store, desk, _start_run, trigger=o["trigger"], trigger_min=o["trigger_min"],
                                 mode=o["mode"], force=True)
    out = {"ok": True, "file": name, "fmt": fmt, "sensor": o["sensor"] or tot["sensor"], "lines": st.get("lines", 0),
           "parsed": st.get("events", 0), "skipped": st.get("skipped", 0), "inserted": tot["inserted"],
           "first_ts": tot["first_ts"], "last_ts": tot["last_ts"], "detections": det["detections"],
           "new": det["new"][:50], "run_id": det["run_id"], "errors": list(st.get("errors") or [])[:5]}
    if info.get("truncated"):
        out["truncated"] = f"stopped after {UPLOAD_MAX_LINES:,} lines: split the file to load the rest"
    if info.get("read_error"):
        out["read_error"] = info["read_error"]
    return jsonify(out)


@app.get("/api/cyber/events")
def api_cyber_events():
    desk = need_desk()
    a = request.args
    try:
        f = _cy_filters(a)
        limit = max(1, min(int(a.get("limit") or 200), 5000))
    except ValueError as exc:
        return _cy_err(str(exc))
    order = str(a.get("order") or ("id" if f.get("after_id") else "desc"))
    if order not in ("asc", "desc", "id"):
        return _cy_err("order must be asc, desc or id")
    dstore = store.for_desk(desk["id"])
    rows = dstore.sec_events(**f, order=order, limit=limit, with_raw=a.get("raw") == "1")
    return jsonify({"events": rows, "stats": dstore.sec_event_stats(**f)})


def _cy_window(dstore, since: float | None, until: float | None) -> tuple[float, float]:
    """A missing end comes from the desk's log (first/last event); no events at all: the last hour."""
    if since is None or until is None:
        st = dstore.sec_event_stats()
        if st["total"]:
            since = st["first_ts"] if since is None else since
            until = st["last_ts"] if until is None else until
        else:
            until = time.time() if until is None else until
            since = until - 3600 if since is None else since
    if until <= since:                                  # one event, or an empty range: keep a usable width
        until = since + 60
    return float(since), float(until)


def _replay_jobs(desk_id: int, n: int = 3) -> list[dict[str, Any]]:
    out = []
    jobs = sorted((j for j in store.jobs(desk_id) if j["kind"] == "log_replay"), key=lambda j: j.get("created") or 0, reverse=True)
    for j in jobs[:n]:
        try:
            spec = json.loads(j.get("task") or "{}")
        except ValueError:
            spec = {}
        spec = spec if isinstance(spec, dict) else {}
        st = spec.get("state") if isinstance(spec.get("state"), dict) else {}
        out.append({"id": j["id"], "name": j["name"], "speed": spec.get("speed"), "enabled": bool(j.get("enabled")),
                    "done": bool(st.get("done")), "inserted": int(st.get("inserted") or 0), "last_ts": st.get("last_ts"),
                    "files": [Path(str(x.get("path") or "")).name for x in spec.get("files") or [] if isinstance(x, dict)]})
    return out


@app.get("/api/cyber/timeline")
def api_cyber_timeline():
    desk = need_desk()
    from .. import cyber as CY
    a = request.args
    try:
        since, until = _cy_time(a, "since"), _cy_time(a, "until")
        bins = int(a.get("bins") or 120)
    except ValueError as exc:
        return _cy_err(str(exc))
    dstore = store.for_desk(desk["id"])
    since, until = _cy_window(dstore, since, until)
    tl = CY.timeline(dstore.sec_event_points(since, until), since, until, bins)
    year_assumed = False
    for lane in tl["lanes"]:                            # a lane whose first event has no year: show times without one
        first = dstore.sec_events(since=since, until=until, sensor=lane["sensor"], source=lane["source"], order="asc", limit=1)
        if first and (first[0].get("attrs") or {}).get("year_assumed"):
            year_assumed = True
            break
    hi = dstore.sec_events(since=since, until=until, min_severity="high", order="desc", limit=300)
    hi.reverse()
    marks = [{**CY.compact_event(e, msg_len=120), "ts": e["ts"]} for e in hi]
    spans = [{"id": d["id"], "rule": d["rule"], "severity": d["severity"], "first_ts": d["first_ts"],
              "last_ts": d["last_ts"], "title": d["title"]}
             for d in dstore.sec_detections(since=since, until=until, min_severity="medium", limit=50)]
    vev = [e for e in store.vision_events(desk["id"], "", since, "", 20000) if (e.get("ts") or 0) <= until]
    cams = []
    if vev:
        ctl = CY.timeline([(e["ts"], e["camera"], "high" if e.get("triggered") else "info", "camera") for e in vev],
                          since, until, tl["bins"])
        for lane in ctl["lanes"]:
            trig = sorted((e for e in vev if e["camera"] == lane["sensor"] and e.get("triggered")), key=lambda e: e["ts"])
            cams.append({"camera": lane["sensor"], "total": lane["total"], "counts": lane["counts"], "alerts": lane["alerts"],
                         "marks": [{"id": e["id"], "ts": e["ts"], "reason": (e.get("reason") or "")[:120]} for e in trig[-100:]]})
    return jsonify({"since": tl["since"], "until": tl["until"], "bins": tl["bins"], "bin_s": tl["bin_s"],
                    "year_assumed": year_assumed, "lanes": tl["lanes"], "marks": marks, "spans": spans, "cameras": cams,
                    "replays": _replay_jobs(desk["id"]), "last_ts": dstore.sec_event_stats(since=since, until=until)["last_ts"],
                    "now": time.time()})


@app.get("/api/cyber/detections")
def api_cyber_detections():
    desk = need_desk()
    from .. import cyber as CY
    a = request.args
    try:
        since, until = _cy_time(a, "since"), _cy_time(a, "until")
        limit = max(1, min(int(a.get("limit") or 50), 200))
    except ValueError as exc:
        return _cy_err(str(exc))
    sev = str(a.get("min_severity") or "").strip().lower()
    if sev and sev not in CY.SEVERITIES:
        return _cy_err(f"min_severity must be one of {', '.join(CY.SEVERITIES)}")
    rows = store.for_desk(desk["id"]).sec_detections(since=since, until=until, min_severity=sev,
                                                     rule=str(a.get("rule") or "").strip(), limit=5000)
    by = {s: 0 for s in reversed(CY.SEVERITIES)}
    for d in rows:
        by[d["severity"]] = by.get(d["severity"], 0) + 1
    return jsonify({"detections": rows[:limit], "total": len(rows), "by_severity": by,
                    "trigger_status": scheduler.CYBER_STATUS.get(desk["id"])})


@app.post("/api/cyber/detect")
def api_cyber_detect():
    """Run the rules now (e.g. after a threshold change). Starts no run."""
    desk = need_desk()
    res = scheduler.cyber_detect(store, desk, _start_run, trigger=False, force=True)
    return jsonify({"detect": res["detect"], "detections": res["detections"], "new": res["new"]})


@app.get("/api/cyber/graph")
def api_cyber_graph():
    desk = need_desk()
    from .. import cyber as CY
    a = request.args
    try:
        since, until = _cy_time(a, "since"), _cy_time(a, "until")
        max_nodes = max(1, min(int(a.get("max_nodes") or 40), 80))
    except ValueError as exc:
        return _cy_err(str(exc))
    entity = _ip_or_text(a.get("entity"))
    dstore = store.for_desk(desk["id"])
    dets = dstore.sec_detections(since=since, until=until, limit=200)
    site_map = dict(_cyber_conf(desk).get("site_map") or {})
    newest_cam = 0
    if site_map:
        last = store.vision_events(desk["id"], "", 0, "", 1)
        newest_cam = last[0]["id"] if last else 0
    query = (desk["id"], since, until, entity, max_nodes, json.dumps(site_map, sort_keys=True))
    key = query + (dstore.sec_event_stats()["max_id"], newest_cam, max((d.get("updated") or 0) for d in dets) if dets else 0)
    with _GRAPH_MEMO_LOCK:
        hit = _GRAPH_MEMO.get(key)
        if hit is not None:
            _GRAPH_MEMO.move_to_end(key)
            return jsonify(hit)
        recent = _GRAPH_RECENT.get(query)
        if recent is not None and time.time() - recent[0] < CYBER_GRAPH_MIN_S:
            return jsonify(recent[1])
    events = dstore.sec_events(since=since, until=until, exclude_kinds=("disconnect",), order="desc", limit=CY.MAX_DETECT_EVENTS)
    events.reverse()
    cams: list[dict[str, Any]] = []
    if site_map and events:                             # camera events near the log events (the graph links only mapped hosts)
        lo, hi = events[0]["ts"] - 300, events[-1]["ts"] + 300
        cams = [e for e in store.vision_events(desk["id"], "", lo, "", 20000) if (e.get("ts") or 0) <= hi]
    g = CY.graph(events, dets, site_map=site_map or None, camera_events=cams, max_nodes=max_nodes, focus=entity)
    out = {**g, "window": {"since": since if since is not None else (events[0]["ts"] if events else None),
                           "until": until if until is not None else (events[-1]["ts"] if events else None),
                           "events": len(events)}}
    with _GRAPH_MEMO_LOCK:
        _GRAPH_MEMO[key] = out
        _GRAPH_RECENT[query] = (time.time(), out)
        _GRAPH_RECENT.move_to_end(query)
        for memo in (_GRAPH_MEMO, _GRAPH_RECENT):
            while len(memo) > 8:
                memo.popitem(last=False)
    return jsonify(out)


@app.get("/api/cyber/incident")
def api_cyber_incident():
    """The incident card: a detection or report run (?run=<id>, else the newest), its summary split from the verified
    footer, every [#id] / [cam #id] citation resolved on THIS desk, live activity, its containment actions."""
    desk = need_desk()
    from .. import cyber as CY
    dstore = store.for_desk(desk["id"])
    rid = str(request.args.get("run") or "").strip()
    row = None
    if rid:
        row = store.run(rid)
        if not row or row.get("desk_id") != desk["id"]:
            return _cy_err("run not found", 404)
    else:
        for r in dstore.runs(60):
            if str(r.get("task") or "").startswith((scheduler.TASK_PREFIX_DETECTION, scheduler.TASK_PREFIX_REPORT)):
                row = store.run(r["id"])
                break
        if row is None:
            return jsonify({"run": None})
    live = _runs.get(row["id"])
    active = bool(live and live["thread"].is_alive())
    summary, verified = CY.split_summary(row.get("summary") or "")
    cites = CY.citations(summary)
    sec = {e["id"]: e for e in dstore.sec_events_by_ids([c["id"] for c in cites if c["kind"] == "sec"])}
    cam = {e["id"]: e for e in store.vision_events_by_ids([c["id"] for c in cites if c["kind"] == "cam"])
           if e.get("desk_id") == desk["id"]}
    citations = []
    for c in cites:
        if c["kind"] == "sec":
            ev = sec.get(c["id"])
            citations.append({"kind": "sec", "id": c["id"], "found": ev is not None,
                              "event": CY.compact_event(ev) if ev else None})
        else:
            ev = cam.get(c["id"])
            citations.append({"kind": "cam", "id": c["id"], "found": ev is not None,
                              "event": {"id": ev["id"], "camera": ev.get("camera") or "", "ts": ev.get("ts"),
                                        "reason": (ev.get("reason") or "")[:120], "answer": (ev.get("answer") or "")[:200]}
                              if ev else None})
    activity = []
    if active:
        evs = [e for e in list(live["events"]) if e.get("kind") not in ("token", "usage")][-6:]
        activity = [{"ts": e.get("ts"), "agent": e.get("agent") or "", "kind": e.get("kind") or "",
                     "text": str(e.get("text") or "")[:160]} for e in evs]
    return jsonify({
        "run": {"id": row["id"], "status": row.get("status"), "active": active, "created": row.get("created"),
                "ended": row.get("ended"), "mode": row.get("mode") or "", "title": str(row.get("task") or "").split("\n", 1)[0][:200]},
        "summary": summary, "verified": verified, "citations": citations, "activity": activity,
        "actions": [x["id"] for x in dstore.actions(limit=1000) if x.get("kind") == "containment" and x.get("run_id") == row["id"]],
        "detections": [d["id"] for d in dstore.sec_detections(limit=5000) if d.get("run_id") == row["id"]]})


@app.post("/api/cyber/incident/report")
def api_cyber_report():
    """Start an incident-report run for a window of original event time (default: the whole log on this desk)."""
    desk = need_desk()
    from .. import cyber as CY
    d = request.get_json(silent=True) or {}
    try:
        since, until = _cy_time(d, "since"), _cy_time(d, "until")
    except ValueError as exc:
        return _cy_err(str(exc))
    dstore = store.for_desk(desk["id"])
    w = {k: v for k, v in (("since", since), ("until", until)) if v is not None}
    st = dstore.sec_event_stats(**w)
    if not st["total"]:
        return _cy_err("no security log events in this window")
    since = since if since is not None else st["first_ts"]
    until = until if until is not None else st["last_ts"]
    first = dstore.sec_events(since=since, until=until, order="asc", limit=1)
    yr = not (first and (first[0].get("attrs") or {}).get("year_assumed"))
    task = (f"{scheduler.TASK_PREFIX_REPORT} — window {CY.fmt_ts(since, year=yr)} to {CY.fmt_ts(until, year=yr)} UTC\n"
            "Write the incident report for this window: timeline with [#id] citations, what the sensors agree on, what "
            "the evidence does not show, and the containment status.")
    return jsonify({"run_id": _start_run(desk, task, scheduler.cyber_mode(desk, "incident_report"))})


def _containment_view(a: dict[str, Any], dstore, conns: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One containment action for the approval card: the spec re-parsed from the stored body and the policy re-run."""
    from .. import cyber as CY
    from .. import policy as P
    try:
        stored = json.loads(a.get("body") or "{}")
    except ValueError:
        stored = None
    spec, errs = CY.containment_spec(stored if isinstance(stored, dict) else {})
    events = dstore.sec_events_by_ids(spec["evidence"])
    v = (["containment body is not valid JSON"] if not isinstance(stored, dict) else []) + list(errs) + \
        P.check_containment(spec["targets"], spec["evidence"], events)
    name = spec["connector"]
    c = conns.get(name.strip().lower()) if name else None
    status = ("none" if not name else "missing" if not c else "not_http" if c["kind"] != "http"
              else "auto_on" if c.get("auto") else "ready")
    return {"id": a["id"], "status": a.get("status"), "created": a.get("created"), "decided_at": a.get("decided_at"),
            "decided_by": a.get("decided_by") or "", "note": a.get("note") or "", "agent": a.get("agent") or "",
            "run_id": a.get("run_id") or "",
            "question": CY.containment_question(spec["targets"]) if spec["targets"] else (a.get("subject") or ""),
            "targets": spec["targets"], "evidence": spec["evidence"], "connector": name, "connector_status": status,
            "justification": spec["justification"],
            "policy": {"ok": not v, "line": "" if v else CY.POLICY_LINE, "violations": v},
            "evidence_events": [CY.compact_event(e) for e in events[:20]]}


@app.get("/api/cyber/containment")
def api_cyber_containment():
    desk = need_desk()
    try:
        limit = max(1, min(int(request.args.get("limit") or 20), 200))
    except ValueError:
        return _cy_err("limit must be a number")
    dstore = store.for_desk(desk["id"])
    rows = [a for a in dstore.actions(str(request.args.get("status") or "").strip(), limit=5000) if a.get("kind") == "containment"]
    conns = {c["name"].strip().lower(): c for c in dstore.connectors()}
    other = sum(1 for a in dstore.actions("pending", limit=100000) if a.get("kind") != "containment")
    return jsonify({"actions": [_containment_view(a, dstore, conns) for a in rows[:limit]], "other_pending": other})


def _cyber_config_view(desk: dict[str, Any]) -> dict[str, Any]:
    from .. import cyber as CY
    c = _cyber_conf(desk)
    params = {**CY.DEFAULT_PARAMS, **{k: v for k, v in (c.get("params") or {}).items() if k in CY.DEFAULT_PARAMS}}
    intel = CY.intel_dir()
    dbip = any(intel.glob("dbip-country-lite-*.csv*")) or any(intel.glob("dbip-asn-lite-*.csv*"))
    return {"site_map": dict(c.get("site_map") or {}), "mask_public_ips": bool(c.get("mask_public_ips")),
            "trigger_min": c.get("trigger_min") if c.get("trigger_min") in CY.SEVERITIES else "high", "params": params,
            "hook_url": request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"]) + "/logs",
            "formats": list(CY.FORMATS),
            "connectors": [{"name": x["name"], "auto": bool(x.get("auto"))} for x in store.connectors(desk["id"]) if x["kind"] == "http"],
            "jobs": [{"id": j["id"], "kind": j["kind"], "name": j["name"], "enabled": bool(j.get("enabled")),
                      "last_result": j.get("last_result") or "", "last_status": j.get("last_status") or ""}
                     for j in store.jobs(desk["id"]) if j["kind"] in scheduler.LOG_JOB_KINDS],
            "notices": {"nvd": CY.NVD_NOTICE, "dbip": CY.DBIP_ATTRIBUTION if dbip else ""}}


@app.get("/api/cyber/config")
def api_cyber_config():
    return jsonify(_cyber_config_view(need_desk()))


@app.patch("/api/cyber/config")
def api_cyber_config_patch():
    """{site_map: {host: camera}, mask_public_ips, trigger_min, params: {rule threshold: number}}; null resets a key."""
    desk = need_desk()
    from .. import cyber as CY
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return _cy_err("body must be a JSON object")
    conf = desk.get("config") or {}
    cy = dict(conf.get("cyber") or {})
    if "site_map" in d:
        sm = d["site_map"] or {}
        if not isinstance(sm, dict):
            return _cy_err("site_map must be an object {host: camera}")
        if len(sm) > 50:
            return _cy_err("site_map holds at most 50 pairs")
        clean: dict[str, str] = {}
        for h, cam in sm.items():
            h, cam = str(h).strip(), cam.strip() if isinstance(cam, str) else ""
            if not h or not cam or len(h) > 60 or len(cam) > 60:
                return _cy_err("site_map pairs are a host or IP and a camera name, each 1 to 60 characters")
            clean[_ip_or_text(h, 60)] = cam
        cy["site_map"] = clean
    if "mask_public_ips" in d:
        if not isinstance(d["mask_public_ips"], bool):
            return _cy_err("mask_public_ips must be true or false")
        cy["mask_public_ips"] = d["mask_public_ips"]
    if "trigger_min" in d:
        tm = d["trigger_min"]
        if tm in (None, ""):
            cy.pop("trigger_min", None)
        elif tm not in CY.SEVERITIES:
            return _cy_err(f"trigger_min must be one of {', '.join(CY.SEVERITIES)}")
        else:
            cy["trigger_min"] = tm
    if "params" in d:
        p = d["params"]
        if p is None:
            cy.pop("params", None)
        elif not isinstance(p, dict):
            return _cy_err("params must be an object {name: number}")
        else:
            cur = dict(cy.get("params") or {})
            for k, v in p.items():
                if k not in CY.DEFAULT_PARAMS:
                    return _cy_err(f"unknown param '{str(k)[:40]}': one of {', '.join(CY.DEFAULT_PARAMS)}")
                if v is None:
                    cur.pop(k, None)
                    continue
                whole = isinstance(CY.DEFAULT_PARAMS[k], int)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 < v <= 10_000_000 \
                        or (whole and float(v) != int(v)):
                    return _cy_err(f"{k} must be a positive {'whole ' if whole else ''}number")
                cur[k] = int(v) if whole else float(v)
            cy["params"] = cur
    conf["cyber"] = cy
    store.update_desk(desk["id"], config=conf)
    return jsonify(_cyber_config_view(store.desk(desk["id"])))


def _rec_dir(desk_id: int) -> Path:
    return cfg.DATA_DIR / "cyber" / "recordings" / str(int(desk_id))


@app.get("/api/cyber/recordings")
def api_cyber_recordings():
    desk = need_desk()
    d = _rec_dir(desk["id"])
    items = []
    if d.is_dir():
        for p in d.glob("*.json"):
            if _REC_NAME.match(p.stem):
                s = p.stat()
                items.append({"name": p.stem, "size": s.st_size, "modified": s.st_mtime})
    items.sort(key=lambda x: -x["modified"])
    return jsonify({"recordings": items})


@app.get("/api/cyber/recordings/<name>")
def api_cyber_recording(name):
    desk = need_desk()
    if not _REC_NAME.match(name):
        return _cy_err("bad recording name")
    p = _rec_dir(desk["id"]) / f"{name}.json"
    if not p.is_file():
        return _cy_err("recording not found", 404)
    return send_file(p, mimetype="application/json", max_age=0)


@app.post("/api/cyber/recordings")
def api_cyber_recording_save():
    """{name, bundle}: a REPLAY bundle the cyber page recorded (stored as given, per desk, written atomically)."""
    desk = need_desk()
    if (request.content_length or 0) > CYBER_REC_MAX_BYTES:
        return _cy_err(f"recording too large: at most {CYBER_REC_MAX_BYTES // 1048576} MB", 413)
    request.max_content_length = CYBER_REC_MAX_BYTES
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return _cy_err("body must be a JSON object {name, bundle}")
    name = str(d.get("name") or "")
    if not _REC_NAME.match(name):
        return _cy_err("name must be 1 to 64 lower-case letters, digits, '-' or '_' (starting with a letter or digit)")
    bundle = d.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("kind") != "atlas-cyber-recording":
        return _cy_err("bundle.kind must be 'atlas-cyber-recording'")
    folder = _rec_dir(desk["id"])
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.json"
    tmp = folder / f".{name}.{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, p)
    return jsonify({"ok": True, "name": name, "size": p.stat().st_size})


@app.get("/desk/cyber")
def desk_cyber():
    """The security desk's filming surface: same login rule as /desk/workspace (?desk=N picks an owned desk), then the
    static page with the rest of the query string (replay, record, since, until, mask)."""
    if not current_user() and not OPEN:
        return redirect("/login?next=/desk/cyber")
    did = request.args.get("desk", type=int)
    if did:
        u, d = current_user(), store.desk(did)
        if d and (OPEN or (u and d["owner_id"] == u["id"])):
            session["desk"] = did
    qs = urlencode([(k, v) for k, v in request.args.items(multi=True) if k != "desk"])
    return redirect("/desk/static/cyber.html" + ("?" + qs if qs else ""))


# ---------------------------------------------------------------------------- operations: records + cases
@app.get("/desk/ops")
def desk_ops_page():
    if not current_user() and not OPEN:
        return redirect("/login?next=/desk/ops")
    did = request.args.get("desk", type=int)
    if did:
        u, d = current_user(), store.desk(did)
        if d and (OPEN or (u and d["owner_id"] == u["id"])):
            session["desk"] = did
    return send_from_directory(STATIC_DIR, "ops.html")


def _own_record(rid: int) -> tuple[dict[str, Any], dict[str, Any]]:
    desk = need_desk()
    rec = R.get(store, rid)
    if not rec or rec["desk_id"] != desk["id"]:
        abort(404)
    return desk, rec


def _own_case(cid: int) -> tuple[dict[str, Any], dict[str, Any]]:
    desk = need_desk()
    c = C.get(store, cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    return desk, c


@app.get("/api/records/types")
def api_record_types():
    desk = need_desk()
    R.ensure_synced(store, desk["id"])
    return jsonify({"types": R.types_for(store, desk["id"]), "total": sum(R.type_counts(store, desk["id"]).values())})


@app.get("/api/records")
def api_records():
    desk = need_desk()
    R.ensure_synced(store, desk["id"])
    rows = R.search(store, desk["id"], request.args.get("type", ""), request.args.get("q", ""), request.args.get("limit", 200, type=int),
                    include_archived=request.args.get("archived") == "1")
    ids = [r["id"] for r in rows]
    n_links: dict[int, int] = {}
    n_cases: dict[int, int] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for a, b in store._conn.execute(f"SELECT src, COUNT(*) FROM record_links WHERE src IN ({marks}) GROUP BY src", ids).fetchall():
            n_links[a] = n_links.get(a, 0) + b
        for a, b in store._conn.execute(f"SELECT dst, COUNT(*) FROM record_links WHERE dst IN ({marks}) GROUP BY dst", ids).fetchall():
            n_links[a] = n_links.get(a, 0) + b
        for a, b in store._conn.execute(f"SELECT record_id, COUNT(*) FROM case_records WHERE record_id IN ({marks}) GROUP BY record_id", ids).fetchall():
            n_cases[a] = b
    for r in rows:
        r["links"] = n_links.get(r["id"], 0)
        r["cases"] = n_cases.get(r["id"], 0)
    return jsonify(rows)


@app.post("/api/records")
def api_record_save():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    try:
        rec, changed = R.upsert(store, desk["id"], str(d.get("type") or ""), d.get("props") or {}, key=str(d.get("key") or ""),
                                title=str(d.get("title") or ""), source="owner", actor=_who())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({**rec, "changed": changed})


@app.get("/api/records/<int:rid>")
def api_record(rid):
    desk, rec = _own_record(rid)
    cs = C.for_record(store, rid)
    return jsonify({**rec, "links": R.links(store, rid), "cases": [C.public(store, c, desk) for c in cs],
                    "timeline": R.timeline_for(store, rid, [c["id"] for c in cs], 150)})


@app.patch("/api/records/<int:rid>")
def api_record_update(rid):
    _desk, rec = _own_record(rid)
    d = request.get_json(force=True) or {}
    return jsonify(R.update(store, rid, d.get("props"), d.get("title"), d.get("status"), actor=_who()))


@app.delete("/api/records/<int:rid>")
def api_record_delete(rid):
    _own_record(rid)
    R.delete(store, rid)
    return jsonify({"ok": True})


@app.get("/api/records/<int:rid>/graph")
def api_record_graph(rid):
    _own_record(rid)
    return jsonify(R.graph(store, rid, request.args.get("depth", 2, type=int)))


@app.post("/api/records/<int:rid>/link")
def api_record_link(rid):
    desk, rec = _own_record(rid)
    d = request.get_json(force=True) or {}
    dst = R.resolve_ref(store, desk["id"], d.get("to"))
    if not dst or dst["desk_id"] != desk["id"]:
        return jsonify({"error": "no such record to link to"}), 400
    R.link(store, desk["id"], rid, str(d.get("rel") or "related_to"), dst["id"], source="owner", actor=_who())
    return jsonify({"ok": True, "links": R.links(store, rid)})


@app.delete("/api/records/links/<int:lid>")
def api_record_unlink(lid):
    desk = need_desk()
    row = store._conn.execute("SELECT desk_id FROM record_links WHERE id=?", (lid,)).fetchone()
    if not row or row[0] != desk["id"]:
        abort(404)
    R.unlink(store, lid)
    return jsonify({"ok": True})


@app.post("/api/records/<int:rid>/note")
def api_record_note(rid):
    desk, rec = _own_record(rid)
    text = str((request.get_json(force=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    R.add_timeline(store, desk["id"], record_id=rid, kind="note", actor=_who(), text=text[:3000])
    return jsonify({"ok": True})


@app.post("/api/records/sync")
def api_records_sync():
    desk = need_desk()
    return jsonify(R.sync_desk(store, desk["id"]))


@app.get("/api/cases")
def api_cases():
    desk = need_desk()
    rows = C.list_cases(store, desk["id"], open_only=request.args.get("open") == "1", ctype=request.args.get("type", ""),
                        state=request.args.get("state", ""), limit=request.args.get("limit", 300, type=int))
    return jsonify({"cases": [C.public(store, c, desk) for c in rows], "counts": C.counts(store, desk["id"]),
                    "playbooks": {k: {"label": v.get("label", k), "description": v.get("description", "")} for k, v in C.playbooks(desk).items()},
                    "enabled": C.settings(desk)})


@app.get("/api/cases/playbooks")
def api_case_playbooks():
    desk = need_desk()
    return jsonify(C.playbooks(desk))


@app.post("/api/cases")
def api_case_open():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    rec_id = 0
    if d.get("record_id"):
        rec = R.get(store, int(d["record_id"]))
        if not rec or rec["desk_id"] != desk["id"]:
            return jsonify({"error": "no such record"}), 400
        rec_id = rec["id"]
    elif isinstance(d.get("person"), dict) and (d["person"].get("email") or d["person"].get("phone") or d["person"].get("name")):
        try:
            rec, _ = R.upsert(store, desk["id"], "person", d["person"], source="owner", actor=_who())
            rec_id = rec["id"]
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    title = str(d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    if d.get("run", True):
        _preflight(desk)
    c = C.open_case(store, desk["id"], str(d.get("type") or "task"), title, rec_id, str(d.get("priority") or "normal"),
                    source="owner", brief=str(d.get("brief") or ""), actor=_who(), desk=desk)
    rid = C.kick(store, desk, c) if d.get("run", True) else ""
    return jsonify({"case": C.public(store, C.get(store, c["id"]), desk, detail=True), "run_id": rid})


@app.get("/api/cases/<int:cid>")
def api_case(cid):
    desk, c = _own_case(cid)
    return jsonify(C.public(store, c, desk, detail=True))


@app.post("/api/cases/<int:cid>/move")
def api_case_move(cid):
    desk, c = _own_case(cid)
    d = request.get_json(force=True) or {}
    try:
        rid = C.enter(store, cid, str(d.get("state") or ""), str(d.get("note") or "moved by the owner"), actor=_who(),
                      outcome=str(d.get("outcome") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"case": C.public(store, C.get(store, cid), desk, detail=True), "run_id": rid})


@app.post("/api/cases/<int:cid>/run")
def api_case_run(cid):
    desk, c = _own_case(cid)
    if c.get("closed_at"):
        return jsonify({"error": "the case is closed - move it to an open state first"}), 400
    d = request.get_json(silent=True) or {}
    _preflight(desk)
    extra = str(d.get("instruction") or "").strip()
    rid = C.kick(store, desk, c, extra=("The owner asks: " + extra) if extra else "The owner asked Atlas to work this step now.")
    if not rid and c.get("active_run"):
        return jsonify({"error": "Atlas is already working this case", "run_id": c["active_run"]}), 409
    return jsonify({"case": C.public(store, C.get(store, cid), desk, detail=True), "run_id": rid})


@app.post("/api/cases/<int:cid>/note")
def api_case_note(cid):
    desk, c = _own_case(cid)
    d = request.get_json(force=True) or {}
    text = str(d.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if d.get("as_reply"):                               # the owner pastes a reply that came in another way (phone call, in person)
        rid = C.inbound_reply(store, desk, c, str(d.get("channel") or "owner"), text, actor=_who())
        return jsonify({"case": C.public(store, C.get(store, cid), desk, detail=True), "run_id": rid})
    C.note(store, cid, text, actor=_who())
    return jsonify({"case": C.public(store, C.get(store, cid), desk, detail=True)})


@app.patch("/api/cases/<int:cid>")
def api_case_patch(cid):
    desk, c = _own_case(cid)
    d = request.get_json(force=True) or {}
    fields: dict[str, Any] = {}
    if d.get("priority") in C.PRIORITIES:
        fields["priority"] = d["priority"]
    if str(d.get("title") or "").strip():
        fields["title"] = str(d["title"]).strip()[:200]
    if d.get("snooze_hours"):
        try:
            fields.update({"waiting_for": "time", "wake_at": time.time() + float(d["snooze_hours"]) * 3600})
        except (TypeError, ValueError):
            return jsonify({"error": "snooze_hours must be a number"}), 400
    if fields:
        C._set(store, cid, **fields)
        R.add_timeline(store, desk["id"], case_id=cid, kind="updated", actor=_who(), text=", ".join(f"{k}={v}" for k, v in fields.items() if k != "wake_at"))
    return jsonify(C.public(store, C.get(store, cid), desk, detail=True))


@app.get("/api/ops/activity")
def api_ops_activity():
    """Desk-wide feed for the Operations page: the newest timeline lines with the case / record they belong to."""
    desk = need_desk()
    rows = R.recent_timeline(store, desk["id"], max(1, min(request.args.get("limit", 60, type=int), 300)))
    cids = sorted({r["case_id"] for r in rows if r["case_id"]})
    rids = sorted({r["record_id"] for r in rows if r["record_id"]})
    ctitle, rtitle = {}, {}
    if cids:
        marks = ",".join("?" * len(cids))
        ctitle = {a: (b, c) for a, b, c in store._conn.execute(f"SELECT id, title, type FROM cases WHERE id IN ({marks})", cids).fetchall()}
    if rids:
        marks = ",".join("?" * len(rids))
        rtitle = {a: (b, c) for a, b, c in store._conn.execute(f"SELECT id, title, type FROM records WHERE id IN ({marks})", rids).fetchall()}
    for r in rows:
        if r["case_id"] in ctitle:
            r["case_title"], r["case_type"] = ctitle[r["case_id"]]
        if r["record_id"] in rtitle:
            r["record_title"], r["record_type"] = rtitle[r["record_id"]]
    return jsonify(rows)


def _who() -> str:
    u = current_user()
    return (u or {}).get("name") or "owner"


# ---------------------------------------------------------------------------- inbound webhook (public, token-addressed)
# ---------------------------------------------------------------------------- api: design studio
def _providers_cfg() -> dict[str, Any]:
    providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    for name, pc in cfg.DEFAULT_PROVIDERS["providers"].items():
        providers.setdefault("providers", {}).setdefault(name, dict(pc))
    prov = os.environ.get("DESK_PROVIDER", "").strip() or providers.get("default_provider", "openrouter")
    providers["default_provider"] = prov
    return providers


def _design_session(sid: str) -> DS.DesignSession:
    s = DS.SESSIONS.get(sid)
    if not s:
        saved = store.load_design_session(sid)          # design chats survive restarts and deploys
        if saved:
            s = DS.DesignSession.from_dict(saved)
            DS.SESSIONS[s.id] = s
    if not s:
        abort(Response(json.dumps({"error": "no_session"}), 404, mimetype="application/json"))
    return s


def _save_design(s: DS.DesignSession) -> None:
    try:
        store.save_design_session(s.id, s.to_dict())
    except Exception as exc:
        print("design session save failed:", exc)


@app.post("/api/design/start")
def api_design_start():
    u = current_user()
    if not u and not OPEN:
        abort(401)
    _require_live()
    d = request.get_json(silent=True) or {}
    tier = d.get("tier") if d.get("tier") in templates.TIERS else "free"
    s = DS.new_session(_mode(), tier)
    if u and u.get("company"):
        s.transcript[0]["text"] = s.transcript[0]["text"].replace("Describe the business", f"Describe {u['company']}", 1)
    links = d.get("links") or []
    if isinstance(links, str):
        links = re.split(r"[\s,]+", links)
    s.links = [str(x).strip() for x in links if str(x).strip()][:4]
    _save_design(s)
    return jsonify(s.public())


@app.post("/api/design/<sid>/study")
def api_design_study(sid):
    """Read the owner's links (website, socials, booking page), stream progress, then seed the designer with a company
    profile + first-draft blueprint. SSE: {"t":"status","d":...}* then {"t":"done", profile, transcript, suggestions, blueprint}."""
    from .. import study as ST
    s = _design_session(sid)
    d = request.get_json(silent=True) or {}
    links = d.get("links") or s.links
    if isinstance(links, str):
        links = re.split(r"[\s,]+", links)
    links = [str(x).strip() for x in links if str(x).strip()][:4]
    if not links:
        return jsonify({"error": "no links"}), 400
    s.links = links
    if s.mode != "demo":
        _require_live()
    import queue
    q: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    providers = None if s.mode == "demo" else _providers_cfg()

    def work():
        try:
            prov, model = None, ""
            if providers:
                from ..providers import ProviderPool
                prov = ProviderPool(providers).get()
                if providers.get("default_provider") == "openrouter":
                    model = templates.STRONG_MODEL if s.tier != "free" else templates.FREE_MODEL
            profile = ST.study(links, on_status=lambda t: q.put({"t": "status", "d": t}), provider=prov, model=model)
            with s.lock:
                s.apply_profile(profile)
            _save_design(s)
            q.put({"t": "done", "profile": profile, "transcript": s.transcript, "suggestions": s.suggestions,
                   "blueprint": s.blueprint, "ready": s.ready})
        except Exception as exc:
            q.put({"t": "error", "error": f"{type(exc).__name__}: {exc}"})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield "retry: 1000\n\n"
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/design/<sid>")
def api_design_get(sid):
    return jsonify(_design_session(sid).public())


@app.post("/api/design/<sid>/say")
def api_design_say(sid):
    """One designer turn, streamed as SSE: {"t":"tok","d":...}* then {"t":"done", ...result}."""
    s = _design_session(sid)
    d = request.get_json(force=True) or {}
    text = str(d.get("text") or "").strip()[:4000]
    if not text:
        return jsonify({"error": "empty"}), 400
    if s.mode != "demo":
        _require_live()
    import queue
    q: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    providers = None if s.mode == "demo" else _providers_cfg()
    model = ""
    if providers and providers.get("default_provider") == "openrouter":
        model = templates.STRONG_MODEL if s.tier != "free" else templates.FREE_MODEL

    def work():
        try:
            def on_tok(t: str):
                if t.startswith(DS.STATUS_MARK):
                    q.put({"t": "status", "d": t[1:]})
                else:
                    q.put({"t": "tok", "d": t})
            res = DS.reply(s, text, on_token=on_tok, providers_cfg=providers, designer_model=model)
            _save_design(s)
            q.put({"t": "done", **res})
        except Exception as exc:
            q.put({"t": "error", "error": f"{type(exc).__name__}: {exc}"})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield "retry: 1000\n\n"
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.post("/api/design/<sid>/blueprint")
def api_design_blueprint(sid):
    """Owner edits from the sketch canvas (rename, tools, add/remove agent, triggers)."""
    s = _design_session(sid)
    d = request.get_json(force=True) or {}
    bp = DS.normalise(d.get("blueprint"), s.blueprint)
    if bp:
        s.blueprint = bp
        s.ready = bool(bp.get("agents"))
        _save_design(s)
    return jsonify({"blueprint": s.blueprint, "ready": s.ready})


@app.post("/api/design/<sid>/tier")
def api_design_tier(sid):
    """Switch a design conversation between free and paid models (the owner's explicit choice in the workspace)."""
    s = _design_session(sid)
    if not current_user() and not OPEN:
        abort(401)
    tier = str((request.get_json(silent=True) or {}).get("tier") or "")
    if tier not in templates.TIERS:
        return jsonify({"error": "unknown tier"}), 400
    s.tier = tier
    _save_design(s)
    return jsonify({"tier": s.tier, "label": templates.TIERS[tier]["label"]})


@app.post("/api/design/<sid>/build")
def api_design_build(sid):
    """Approve the blueprint: create the desk, schedule its triggers, return what still needs connecting."""
    s = _design_session(sid)
    u = current_user()
    if not u and not OPEN:
        abort(401)
    d = request.get_json(force=True) or {}
    bp = DS.normalise(d.get("blueprint"), s.blueprint) or s.blueprint
    if not bp or not bp.get("agents"):
        return jsonify({"error": "The blueprint has no agents yet - keep talking to the designer first."}), 400
    tier = d.get("tier") if d.get("tier") in templates.TIERS else s.tier
    name = (d.get("name") or (bp.get("business") or {}).get("name") or (u or {}).get("company") or "New desk").strip()
    conf = DS.blueprint_to_desk(bp, tier, name=name)
    if not (bp.get("business") or {}).get("name"):                 # never show the template's placeholder business name
        conf["business"]["name"] = name
    if s.desk_id and store.desk(s.desk_id):
        store.update_desk(s.desk_id, name=name, tier=tier, config=conf)
        desk = store.desk(s.desk_id)
    else:
        desk = store.add_desk(u["id"] if u else 0, name, "custom", tier, conf)
        s.desk_id = desk["id"]
    session["desk"] = desk["id"]
    s.blueprint = bp
    # triggers -> automations
    existing = {j["name"] for j in store.jobs(desk["id"])}
    jobs = []
    for w in bp.get("workflows") or []:
        t = w.get("trigger") or {}
        jname = f"{w['name']} ({t.get('kind')})"
        if jname in existing:
            continue
        if t.get("kind") == "schedule":
            jobs.append(store.add_job(desk["id"], "task", jname, f"Run workflow '{w['name']}': {t.get('detail') or 'scheduled sweep'}. Mode: {w['id']}", 1440, time.time() + 86400))
        elif t.get("kind") == "inbox":
            jobs.append(store.add_job(desk["id"], "inbox_watch", jname, "", 2, time.time() + 120))
    cameras, cams_missing = _build_cameras(desk, bp)
    _save_design(s)
    return jsonify({"desk": _desk_public(desk), "connect": _connect_plan(desk, bp), "jobs": jobs,
                    "cameras": cameras, "cameras_missing": cams_missing})


def _build_cameras(desk: dict[str, Any], bp: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Blueprint cameras -> camera connectors (journal on by default) + a fast watch job each. Cameras without a
    usable source are returned by name so the owner can add the address later."""
    made, missing = [], []
    have = {c["name"]: c for c in store.connectors(desk["id"]) if c["kind"] == "camera"}
    for cam in bp.get("cameras") or []:
        src = DS.resolve_camera_source(cam.get("source", ""))
        if not src:
            missing.append(cam["name"])
            continue
        journal = bool(cam.get("journal", True))
        config = {"source": src, "watch_for": cam.get("watch_for") or "person", "min_count": 1,
                  "alerts": "1" if cam.get("alerts") else "0", "notes": cam.get("notes", ""),
                  "journal": "1" if journal else "", "journal_every_s": "30", "journal_min_gap_s": "8",
                  "journal_rollup_min": "15", "journal_focus": cam.get("focus", "")}
        if (desk.get("tier") or "free") != "free":
            config["vlm_model"] = DS.PAID_VLM
        c = have.get(cam["name"])
        if c:
            store.update_connector(c["id"], config=config)
            c = store.connector(c["id"])
        else:
            c = store.add_connector(desk["id"], "camera", cam["name"], config, False)
        if not any(j["kind"] == "camera_watch" and f'"connector": "{cam["name"]}"' in (j["task"] or "") for j in store.jobs(desk["id"])):
            store.add_job(desk["id"], "camera_watch", f"Watch {cam['name']}",
                          json.dumps({"connector": cam["name"], "every_s": 8 if journal else 30}), 1, time.time())
        made.append({"id": c["id"], "name": c["name"], "sample": str(cam.get("source", "")).startswith("sample:"),
                     "journal": journal, "alerts": bool(cam.get("alerts"))})
    return made, missing


def _connect_plan(desk: dict[str, Any], bp: dict[str, Any]) -> dict[str, Any]:
    have = store.connectors(desk["id"])
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for c in have:
        by_kind.setdefault(c["kind"], []).append(c)
    items = []
    wanted = list(bp.get("connectors") or [])
    if any(a.get("engine") == "hermes_agent" for a in bp.get("agents") or []) and not any(c["kind"] == "hermes_agent" for c in wanted):
        who = ", ".join(a["name"] for a in bp["agents"] if a.get("engine") == "hermes_agent")
        wanted.append({"kind": "hermes_agent", "name": "Hermes Agent", "purpose": f"Runs {who}", "required": True})
    for c in wanted:
        match = (by_kind.get(c["kind"]) or [None])[0]
        items.append({**c, "fields": I.KINDS[c["kind"]]["fields"], "hint": I.KINDS[c["kind"]]["hint"],
                      "connector_id": match["id"] if match else None, "status": (match or {}).get("status") or ("built-in" if c["kind"] == "webhook" else "not connected")})
    return {"connectors": items, "hook_url": request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"]),
            "kinds": I.KINDS}


@app.get("/api/design/<sid>/connect")
def api_design_connect(sid):
    s = _design_session(sid)
    if not s.desk_id or not store.desk(s.desk_id):
        return jsonify({"error": "not built"}), 400
    return jsonify(_connect_plan(store.desk(s.desk_id), s.blueprint or {}))



@app.route("/hook/<token>", methods=["GET", "POST"])
def hook(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    if request.method == "GET":
        return jsonify({"ok": True, "desk": desk["name"], "post": "JSON {name, email, phone, company, notes, source}"})
    d = request.get_json(silent=True) or request.form.to_dict() or {}
    name = (d.get("name") or d.get("full_name") or "").strip()
    email_ = (d.get("email") or "").strip()
    notes = (d.get("notes") or d.get("message") or d.get("enquiry") or "").strip()
    if not name and not email_:
        return jsonify({"error": "name or email required"}), 400
    dstore = store.for_desk(desk["id"])
    lid = dstore.add_lead(name or email_.split("@")[0], (d.get("company") or "").strip(), email_, (d.get("phone") or "").strip(),
                          (d.get("source") or "webhook").strip(), notes)
    if email_ or notes:
        dstore.add_message("form", "in", email_ or name, notes or "(no message)", actor=name or email_)
    if email_ and not dstore.contact_for(email_):
        dstore.upsert_contact(email_, {"name": name, "company": d.get("company", ""), "email": email_, "phone": d.get("phone", ""), "stage": "New", "notes": "Inbound via webhook"})
    rid = _lead_case_run(desk, lid)
    return jsonify({"ok": True, "lead_id": lid, "run_id": rid})


def _inbound_message(desk: dict[str, Any], phone: str, name: str, text: str, source: str) -> str:
    """A WhatsApp / SMS message: a reply on the sender's open case if they have one, otherwise a new lead + case
    (an existing contact keeps its name/company)."""
    dstore = store.for_desk(desk["id"])
    phone = "+" + I._digits(phone) if phone else ""
    channel = "whatsapp" if "whatsapp" in source else "sms"
    how = f"This arrived by {source}. Reply on the same channel (queue_action kind={channel}, to={phone}) — keep it short."
    dstore.add_message(channel, "in", phone or (name or "unknown"), text, actor=name or phone)
    open_ = C.match_open(store, desk["id"], phone=phone) if phone and C.enabled(desk, "lead_cases") else None
    if open_:
        return C.inbound_reply(store, desk, open_, source, text, actor=name or phone, extra=how) or open_.get("active_run") or ""
    known = dstore.contact_for(phone) if phone else None
    name = name or (known or {}).get("name") or (phone or "unknown")
    lid = dstore.add_lead(name, (known or {}).get("company", ""), (known or {}).get("email", "") or "", phone, source, text)
    if known:                                            # an existing customer keeps their stage and notes
        if not known.get("name") and name and name != phone:
            dstore.upsert_contact(known.get("email") or known.get("name"), {"name": name})
    else:
        dstore.upsert_contact(name if name != phone else phone, {"name": name if name != phone else "", "phone": phone,
                                                                 "stage": "New", "notes": f"First contact via {source}"})
    return _lead_case_run(desk, lid, extra=how)


@app.route("/hook/<token>/whatsapp", methods=["GET", "POST"])
def hook_whatsapp(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    conn = next((c for c in store.connectors(desk["id"]) if c["kind"] == "whatsapp"), None)
    if request.method == "GET":                      # Meta verification handshake
        want = (conn or {}).get("config", {}).get("verify_token") or ""
        if request.args.get("hub.mode") == "subscribe" and want and request.args.get("hub.verify_token") == want:
            return request.args.get("hub.challenge", ""), 200
        return jsonify({"error": "verify_token mismatch or no WhatsApp connector"}), 403
    msgs = I.parse_whatsapp_webhook(request.get_json(silent=True) or {})
    runs = [_inbound_message(desk, m["from"], m["name"], m["text"], "whatsapp") for m in msgs if m.get("from")]
    return jsonify({"ok": True, "messages": len(msgs), "runs": runs})


@app.post("/hook/<token>/sms")
def hook_sms(token):
    """Twilio inbound webhook (SMS or WhatsApp sandbox): form fields From, Body, ProfileName."""
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    f = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    frm, body = (f.get("From") or f.get("from") or ""), (f.get("Body") or f.get("body") or "")
    if not frm:
        return jsonify({"error": "From required"}), 400
    source = "twilio whatsapp" if frm.startswith("whatsapp:") else "sms"
    rid = _inbound_message(desk, frm.replace("whatsapp:", ""), f.get("ProfileName") or "", body, source)
    return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>", mimetype="application/xml", headers={"X-Atlas-Run": rid})


_n_interrupted = store.mark_interrupted()            # restart recovery: nothing can still be running from a previous process
if _n_interrupted:
    print(f"marked {_n_interrupted} orphaned run(s) as interrupted")
_n_enc = store.encrypt_legacy_connectors()           # secrets at rest: encrypt any plaintext connector rows from before
if _n_enc:
    print(f"encrypted {_n_enc} legacy connector config(s)")
if OPEN and os.environ.get("RENDER"):
    print("WARNING: DESK_OPEN=1 on a public deployment - the portal and every desk are reachable without login")
scheduler.LIVE = lambda desk=None: _mode() != "demo" and not _live_reason(desk)
scheduler.DISPATCH = _dispatch
scheduler.LEAD_RUN = _lead_case_run
C.START_RUN = _start_run
C.RUN_STATUS = _run_status
C.NOTIFY = lambda desk_id, text: I.notify(store.connectors(desk_id), text)
scheduler.BASE_URL = lambda: os.environ.get("PUBLIC_URL", "").strip()
scheduler.start(store, _start_run, store.desk)
threading.Thread(target=_watchdog_loop, daemon=True, name="atlas-watchdog").start()


def main():
    port = int(os.environ.get("PORT", "8094"))
    host = os.environ.get("DESK_HOST", "127.0.0.1").strip() or "127.0.0.1"   # this machine only; DESK_HOST=0.0.0.0 to share on the LAN
    print(f"Atlas Desk  mode={_mode()}  accounts={'off' if OPEN else 'on'}  http://localhost:{port}/desk  (bound to {host})")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
