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
      GOOGLE_CLIENT_ID/SECRET      "Continue with Google" (see atlas/desk/auth.py)
      ATLAS_SMTP_* / ATLAS_RESEND_KEY + ATLAS_MAIL_FROM   portal mail; TWO_FACTOR=auto|required|off (emailed code)
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
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory, session, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

from .. import cases as C
from .. import config as cfg
from .. import designer as DS
from .. import integrations as I
from .. import metrics as MX
from .. import nextsteps as NS
from .. import records as R
from .. import templates
from .. import tools as T
from .. import vision as V
from .. import rag as RAG
from .. import team as TM
from .. import usage as U
from . import scheduler
from . import auth as AUTH
from . import hermes_proxy
from ..orchestrator import Event, Orchestrator
from ..store import Store

ROOT = cfg.ROOT
SITE_DIR = ROOT / "site"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_PATH = cfg.DATA_DIR / "desk.db"
mimetypes.add_type("font/woff2", ".woff2")           # the cyber page's local IBM Plex fonts (Windows maps it oddly)


def _no_peeking() -> None:
    """Linux: mark this process non-dumpable. Its /proc/<pid>/environ then belongs to root, so a child process running as
    the same user (a model-written run_python script, say) cannot read the keys the portal was started with. Scrubbing
    the child's own environment is not enough on its own: a child can read its parent's environment unless this is set."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(4, 0, 0, 0, 0)     # PR_SET_DUMPABLE = 4
    except (OSError, AttributeError):
        pass


_no_peeking()

app = Flask(__name__, static_folder=None)
# /internal/llm/v1: the Hermes runtime's model calls, metered per account (usage spec §1.8). Loopback and token only;
# _guard does not cover /internal, the routes check for themselves.
app.register_blueprint(hermes_proxy.bp)
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
# Served over HTTPS wherever it is public: Render, or Caddy on the Hetzner box (PUBLIC_URL is https there). The session
# cookie must never travel in plain HTTP, so Secure follows that, not the platform name alone.
_HTTPS = bool(os.environ.get("RENDER")) or os.environ.get("PUBLIC_URL", "").strip().lower().startswith("https://")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=_HTTPS, PERMANENT_SESSION_LIFETIME=30 * 86400,
                  # every request body is bounded before it is parsed; the few upload routes that take more (camera
                  # recordings, videos, log files) raise request.max_content_length for themselves
                  MAX_CONTENT_LENGTH=int(float(os.environ.get("MAX_BODY_MB", "64") or 64) * 1024 * 1024))
OPEN = os.environ.get("DESK_OPEN", "").strip() in ("1", "true", "yes")
DEFAULT_TEMPLATE = os.environ.get("DESK_TEMPLATE", "sales_desk")

_runs: dict[str, dict[str, Any]] = {}          # run_id -> {"events": [...], "thread":..., "orch":..., "desk_id":...}
_runs_lock = threading.Lock()
_starts: dict[tuple[int, str], tuple[float, str]] = {}   # (desk, job text) -> (when POST /api/runs took it, its run id once known)
_starts_lock = threading.Lock()
START_DEDUPE_S = 5.0                           # the same job on the same desk again within this, while it is starting or running: refused
_build_lock = threading.Lock()                 # two builds of one draft at once (a double press, two tabs) make one desk
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
                # url_guard: the endpoint is re-checked on every call, so a hostname that passed when it was saved
                # cannot later be re-pointed (DNS) at loopback, the LAN or the metadata service.
                # env_key=False: an entry saved without a key (a custom server) sends no key, never the house one
                providers.setdefault("providers", {})["desk:" + n] = {**{k: v for k, v in e.items() if k not in ("preset", "last_test")},
                                                                      "url_guard": True, "env_key": False}
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
        # an owner's override may name the house catalogue provider, the desk default or the desk's own accounts -
        # never another house entry (e.g. the local Ollama presets), which would get the house key on the server's loopback
        may_use = {prov, "openrouter"} | {k for k in (providers.get("providers") or {}) if k.startswith("desk:")}
        for a in agents:                               # per-agent landscape overrides beat the tier default
            ov = (over.get("models") or {}).get(a["id"])
            if not ov:
                continue
            if ov.get("engine"):
                a["engine"] = ov["engine"]
            if ov.get("model") and ov["model"] != "hermes-agent":
                a["model"] = ov["model"]
                if ov.get("provider") in may_use:
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
    # an instance the desk owner typed in is checked on every call (it may sit on their LAN, never on this server's
    # loopback or the metadata service); the operator's own HERMES_AGENT_URL is trusted and may be local
    h_guard: Any = "internal" if hconn else False
    if hconn:
        hcfg = hconn["config"]
    elif os.environ.get("HERMES_AGENT_URL", "").strip():
        hcfg = {"base_url": os.environ["HERMES_AGENT_URL"].strip(), "api_key": os.environ.get("HERMES_AGENT_KEY", "").strip(),
                "session_prefix": "atlas"}
    # Atlas's own instance only while its inner model calls are counted per account (usage spec §1.8): with
    # USAGE_HERMES_METER=off the lead and the specialists stay on the built-in engine. A desk's own instance runs on
    # the owner's key and is never held back (spec §2).
    house_hermes_off = hcfg is not None and not hconn and not U.hermes_house_on()
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
            if lead_hermes and house_hermes_off:
                a["engine_note"] = U.HERMES_OFF_NOTE
            elif lead_hermes and hcfg:
                tier_cfg = templates.TIERS.get(desk.get("tier") or "free", templates.TIERS["free"])
                hmodel = (lead_ov.get("model") if lead_ov.get("model") and lead_ov["model"] != "hermes-agent"
                          else tier_cfg.get("hermes_model", templates.FREE_MODEL))
                providers.setdefault("providers", {})["hermes_agent_atlas"] = {
                    "type": "hermes_agent", "base_url": hcfg.get("base_url", ""), "api_key": hcfg.get("api_key", ""),
                    "default_model": hmodel, "url_guard": h_guard, "env_key": False,
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
        if house_hermes_off:
            a["engine_note"] = U.HERMES_OFF_NOTE
            continue
        pname = f"hermes_agent_{a['id']}"
        # the tier decides which model runs INSIDE the Hermes runtime (free MiniMax / Haiku / Sonnet); a per-agent
        # override from the Model landscape wins if it names a real model
        tier_cfg = templates.TIERS.get(desk.get("tier") or "free", templates.TIERS["free"])
        ov = (over.get("models") or {}).get(a["id"]) or {}
        hmodel = ov.get("model") if ov.get("model") and ov["model"] != "hermes-agent" else tier_cfg.get("hermes_model", templates.FREE_MODEL)
        providers.setdefault("providers", {})[pname] = {"type": "hermes_agent", "base_url": hcfg.get("base_url", ""),
                                                         "api_key": hcfg.get("api_key", ""), "default_model": hmodel,
                                                         "url_guard": h_guard, "env_key": False,
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


def _pinned_desk_id() -> int | None:
    """A page that shows ONE desk (the workspace) names it on every API call - an X-Desk header, or ?desk= where a header
    cannot be sent (images, event streams) - so another tab switching desk never changes what this page reads or writes."""
    if not request.path.startswith("/api/"):
        return None
    raw = request.headers.get("X-Desk") or request.args.get("desk") or ""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def current_desk() -> dict[str, Any] | None:
    u = current_user()
    pin = _pinned_desk_id()
    if pin:                                # same ownership rule as /api/desks/<id>/select; anything else falls through
        d = store.desk(pin)
        if d and (OPEN or (u and d["owner_id"] == u["id"])):
            return d
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


# The pages call only this origin. The marketing site also loads gsap and lenis from cdnjs / jsDelivr and the fonts from
# Google. Inline scripts and handlers are still allowed ('unsafe-inline': the pages are written that way), but whatever
# gets injected cannot send the session, a key or the typed input to a host of its choosing: connect, img, form and
# frame targets are all this origin.
_CSP = "; ".join([
    "default-src 'self'",
    # 'wasm-unsafe-eval' lets the vision demo compile its WebAssembly; plain eval stays blocked
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    # the home page's vision demo fetches the ONNX Runtime WASM from jsDelivr, and may start its worker from there
    "connect-src 'self' https://cdn.jsdelivr.net",
    "worker-src 'self' blob: https://cdn.jsdelivr.net",
    "frame-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=(), payment=()")   # camera: the site's vision demo
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    if _HTTPS:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.errorhandler(U.UsageLimit)
def _usage_limit(exc: U.UsageLimit):
    """A usage limit refused the work and nothing was sent (usage spec §4.5): 429 with the limit's body, or 409
    outside a channel's reply window, plus Retry-After when something retries it. Routes only raise."""
    return Response(json.dumps(exc.body()), exc.http_status, mimetype="application/json", headers=exc.headers())


def _client_ip() -> str:
    """The address the rate limits key on. Behind Caddy (or Render's router) the TCP peer is the proxy, and the proxy
    appends the real client to X-Forwarded-For, so the LAST entry is the one to trust. The first entry is whatever the
    client sent: keying on it let anyone pick a fresh address per request and get past every per-IP limit."""
    peer = request.remote_addr or "?"
    if peer in ("127.0.0.1", "::1") or os.environ.get("RENDER"):
        hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return peer


def _rate_limited(limiter: Any, key: str, message: str) -> Response:
    """A per-address limiter's 429 (abuse, not usage): the same shape as a usage limit's (usage spec §4.5), so every
    page shows the message and when to try again."""
    ra = max(1, int(-(-limiter.retry_after(key) // 1)))
    return Response(json.dumps({"error": "rate_limited", "message": message, "retry_after_s": ra}), 429,
                    mimetype="application/json", headers={"Retry-After": str(ra)})


@app.before_request
def _rate_limits():
    ip = _client_ip()
    p = request.path
    if p.startswith("/hook/") and not _RL_HOOK.allow(f"{p}:{ip}"):
        return _rate_limited(_RL_HOOK, f"{p}:{ip}", "Too many requests to this hook from one address.")
    if ((request.method == "POST" and p in ("/login", "/signup", "/desk/login", "/login/verify"))
            or (request.method == "PATCH" and p == "/api/me")) and not _RL_AUTH.allow(ip):
        return _rate_limited(_RL_AUTH, ip, "Too many sign-in attempts from this address: wait a few minutes.")
    if request.method == "POST" and p.startswith("/api/design/") and (p.endswith("/say") or p.endswith("/study")) and not _RL_MODEL.allow(ip):
        return _rate_limited(_RL_MODEL, ip, "Too many design messages from this address in a minute.")
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
            return redirect(_login_next())
    return None


def _login_next() -> str:
    """/login?next=<this page>, keeping its query (a link to /desk/workspace?desk=5 comes back to desk 5 after the login)."""
    qs = request.query_string.decode("latin-1")
    return "/login?next=" + request.path + (quote("?" + qs, safe="") if qs else "")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _page(template: str, **vars_) -> str:
    html = (STATIC_DIR / template).read_text(encoding="utf-8")
    for k, v in vars_.items():
        html = html.replace("{{%s}}" % k, _esc(str(v)))
    return re.sub(r"\{\{\w+\}\}", "", html)                 # optional slots (e.g. a notice) render empty


@app.route("/login", methods=["GET", "POST"])
@app.route("/desk/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        d = request.form if request.form else (request.get_json(silent=True) or {})
        email = (d.get("email") or "").strip().lower()
        pw = d.get("password") or ""
        u = store.user_by_email(email) if email else None
        if not u or not u.get("pw_hash") or not check_password_hash(u["pw_hash"], pw):
            time.sleep(0.8)   # slow down guessing
            if request.is_json:
                return jsonify({"error": "wrong email or password"}), 401
            return _page("login.html", error="Wrong email or password.", email=email, google=_google_cls()), 401
        nxt = request.args.get("next") or _front_door(u)           # a desk's Home, or the workspace chat for new owners
        nxt = nxt if nxt.startswith("/desk") else _front_door(u)
        if _needs_code(u):
            sess, why, code = _mfa_begin(u, nxt)                   # mail down or not set up: never the password alone
            if not sess:
                if request.is_json:
                    return jsonify({"error": why}), code
                return _page("login.html", error=why, email=email, google=_google_cls()), code
            session.clear()
            session["mfa"] = sess
            if request.is_json:
                return jsonify({"ok": False, "mfa": True, "verify": "/login/verify"}), 202
            return redirect("/login/verify")
        _sign_in(u["id"])
        if request.is_json:
            return jsonify({"ok": True})
        return redirect(nxt)
    if current_user():
        return redirect(_front_door(current_user()))
    return _page("login.html", error="", email="", google=_google_cls())


def _front_door(u: dict[str, Any] | None) -> str:
    """Owners with a desk land on its Home; new owners start in the workspace chat, where Atlas builds the first one."""
    return "/desk" if u and store.desks_for(u["id"]) else "/desk/workspace"


def _sign_in(uid: int) -> None:
    session.clear()
    session.permanent = True
    session["uid"] = uid
    store.touch_login(uid)


def _google_cls() -> str:
    """Class on the login/signup Google button: shown only when the OAuth client is configured."""
    return "on" if AUTH.google_ready() else "off"


def _needs_code(u: dict[str, Any]) -> bool:
    """Second factor on a password login: platform policy on, the account has not opted out (TWO_FACTOR=required
    ignores an opt-out), and this browser is not trusted. Accounts that only sign in with Google never reach here."""
    if not AUTH.two_factor_active():
        return False
    if AUTH.two_factor_mode() != "required" and not int(u.get("two_factor") or 0):
        return False
    return AUTH.trusted_uid(request.cookies.get("atlas_trust"), app.secret_key) != u["id"]


_MFA_UNCONFIGURED = "Sign-in codes are required on this server, but it cannot send email yet. Ask the administrator to set up the portal's mail."
_MFA_MAIL_DOWN = "We could not send your sign-in code. Try again in a minute."
_MFA_TOO_MANY = "Too many sign-in codes were requested for this account. Wait an hour, then try again."


def _mfa_begin(u: dict[str, Any], nxt: str) -> tuple[dict[str, Any] | None, str, int]:
    """Mail a code and keep its challenge on the user row (atlas/desk/auth.py). Returns (session record, "", 0): the
    session holds only the challenge id. On failure (None, the message for the person, HTTP status): a setup error
    says so instead of posing as a passing mail outage."""
    if AUTH.two_factor_problem():
        print("2fa:", AUTH.two_factor_problem())
        return None, _MFA_UNCONFIGURED, 503
    try:
        rec = AUTH.issue(u["email"], app.secret_key, store.mfa_get(u["id"]))
    except AUTH.TooManyCodes:
        return None, _MFA_TOO_MANY, 429
    except Exception as e:
        print("2fa mail failed:", e)
        return None, _MFA_MAIL_DOWN, 503
    store.mfa_put(u["id"], rec)
    return {"uid": u["id"], "cid": rec["id"], "email": u["email"], "next": nxt}, "", 0


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    sess = session.get("mfa")
    if not sess or not sess.get("cid"):
        return redirect("/login")
    masked = sess["email"][:2] + "…@" + sess["email"].split("@")[-1]
    if request.method == "POST":
        d = request.form if request.form else (request.get_json(silent=True) or {})
        if d.get("resend"):
            live = store.mfa_get(sess["uid"])
            u = store.user(sess["uid"])
            if not u or live.get("id") != sess["cid"]:         # used, or replaced by a newer login
                return _page("verify.html", error="Start again from the login page.", email=masked), 400
            if not AUTH.can_resend(live):
                return _page("verify.html", error="A code was sent moments ago - check your spam folder, then try again.",
                             email=masked), 429
            new, why, code = _mfa_begin(u, sess.get("next") or "")
            if not new:
                return _page("verify.html", error=why, email=masked), code
            session["mfa"] = new
            return _page("verify.html", error="", email=masked, notice="New code sent.")
        rec, used = store.mfa_spend(sess["uid"], sess["cid"])  # the guess is counted before it is checked
        ok, err = AUTH.check_code(rec, str(d.get("code") or ""), app.secret_key, used)
        if not ok:
            if request.is_json:
                return jsonify({"error": err}), 401
            return _page("verify.html", error=err, email=masked), 401
        store.mfa_clear(sess["uid"])                           # a code works once
        nxt = sess.get("next") or _front_door(store.user(sess["uid"]))
        _sign_in(sess["uid"])
        resp = jsonify({"ok": True}) if request.is_json else redirect(nxt)
        if d.get("trust"):
            resp.set_cookie("atlas_trust", AUTH.trust_token(sess["uid"], app.secret_key), max_age=AUTH.TRUST_DAYS * 86400,
                            httponly=True, secure=_HTTPS, samesite="Lax")
        return resp
    return _page("verify.html", error="", email=masked)


@app.get("/auth/google")
def auth_google():
    if not AUTH.google_ready():
        abort(404)
    state = secrets.token_urlsafe(24)
    nxt = request.args.get("next") or ""                         # none given: the front door, decided once we know who it is
    session["oauth"] = {"state": state, "next": nxt if nxt.startswith("/desk") else ""}
    return redirect(AUTH.google_auth_url(state, _google_redirect()))


@app.get("/auth/google/callback")
def auth_google_callback():
    pend = session.pop("oauth", None) or {}
    if not AUTH.google_ready() or not pend or request.args.get("state") != pend.get("state"):
        return _page("login.html", error="Google sign-in did not complete - try again.", email="", google=_google_cls()), 400
    if request.args.get("error") or not request.args.get("code"):
        return _page("login.html", error="Google sign-in was cancelled.", email="", google=_google_cls()), 400
    try:
        g = AUTH.google_exchange(request.args["code"], _google_redirect())
    except Exception as e:
        print("google exchange failed:", e)
        return _page("login.html", error="Google sign-in failed - try again or use your password.", email="", google=_google_cls()), 400
    u = store.user_by_google(g["sub"])
    if not u:
        u = store.user_by_email(g["email"])                    # same verified address: link the existing account
        if u:
            store.link_google(u["id"], g["sub"])
        else:
            u = store.add_user(g["email"], g["name"], "", "", google_sub=g["sub"])
    _sign_in(u["id"])
    return redirect(pend.get("next") or _front_door(u))


def _google_redirect() -> str:
    base = os.environ.get("PUBLIC_URL", "").strip().rstrip("/") or request.url_root.rstrip("/")
    return base + "/auth/google/callback"


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
            return _page("signup.html", error=err, name=name, company=company, email=email, google=_google_cls()), 400
        required = AUTH.two_factor_mode() == "required"
        if required and AUTH.two_factor_problem():               # refuse before an account exists that cannot sign in
            print("2fa:", AUTH.two_factor_problem())
            if request.is_json:
                return jsonify({"error": _MFA_UNCONFIGURED}), 503
            return _page("signup.html", error=_MFA_UNCONFIGURED, name=name, company=company, email=email, google=_google_cls()), 503
        u = store.add_user(email, name, company, generate_password_hash(pw))
        if required:                    # TWO_FACTOR=required: no session from a password alone, a brand-new one included
            sess, why, code = _mfa_begin(u, "/desk/workspace")
            if not sess:
                why = "Your account is ready, but we could not send your sign-in code. Log in again in a minute."
                if request.is_json:
                    return jsonify({"error": why}), code
                return _page("login.html", error=why, email=email, google=_google_cls()), code
            session.clear()
            session["mfa"] = sess
            if request.is_json:
                return jsonify({"ok": False, "mfa": True, "verify": "/login/verify"}), 202
            return redirect("/login/verify")
        _sign_in(u["id"])
        if request.is_json:
            return jsonify({"ok": True})
        return redirect("/desk/workspace")                     # new owners start in the workspace chat
    if current_user():
        return redirect(_front_door(current_user()))
    return _page("signup.html", error="", name="", company="", email="", google=_google_cls())


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/")


@app.get("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"id": None, "name": "Guest", "email": "", "company": "", "open": OPEN})
    out = {k: v for k, v in u.items() if k != "google_sub"}
    out["google"] = bool(u.get("google_sub"))
    required = AUTH.two_factor_mode() == "required"
    out["two_factor"] = AUTH.two_factor_active() and (required or bool(int(u.get("two_factor") or 0)))
    out["two_factor_available"] = AUTH.two_factor_active()
    out["two_factor_required"] = required
    return jsonify(out)


@app.patch("/api/me")
def api_me_patch():
    """Account settings: {two_factor: bool} turns the mailed sign-in code on or off for this account. Off needs the
    account password ({password}): a session cookie alone must not be able to strip the second factor. With
    TWO_FACTOR=required there is no opting out."""
    u = current_user()
    if not u:
        abort(401)
    d = request.get_json(silent=True) or {}
    if "two_factor" in d:
        on = bool(d["two_factor"])
        if not on:
            if AUTH.two_factor_mode() == "required":
                return jsonify({"error": "sign-in codes are required on this server"}), 400
            full = store.user_by_email(u["email"]) or {}
            if full.get("pw_hash") and not check_password_hash(full["pw_hash"], str(d.get("password") or "")):
                time.sleep(0.8)
                return jsonify({"error": "enter your password to turn sign-in codes off"}), 403
        store.set_two_factor(u["id"], on)
    return api_me()


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


def _house_hermes() -> bool:
    """Atlas's own Hermes instance is configured and may be used (not USAGE_HERMES_METER=off, usage spec §1.8)."""
    return bool(os.environ.get("HERMES_AGENT_URL", "").strip()) and U.hermes_house_on()


@app.get("/api/health")
def api_health():
    reason = _live_reason()
    return jsonify({"ok": True, "mode": _mode(), "live_ready": not reason, "live_reason": reason,
                    "hermes_agent": _house_hermes(),
                    "db": store.backend, "db_ok": store.ping(),
                    "mail": AUTH.mail_ready(), "two_factor": AUTH.two_factor_active(), "google": AUTH.google_ready(),
                    "two_factor_mode": AUTH.two_factor_mode(), "two_factor_ready": not AUTH.two_factor_problem()})


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
    hermes = configs["mode"] != "demo" and (any(c["kind"] == "hermes_agent" for c in conns) or _house_hermes())
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
        # the person's own action on the desk's provider: a design and at most one repair (overrun 1 past the check)
        meter = U.desk_meter(desk, "team.design", lane="you", overrun=1)
        prov = ProviderPool(configs["providers"], meter=meter).get(atlas_agent.get("provider") or "")
        # S7, S8: refused as JSON before anything is sent; the owner's own key is never limited by Atlas
        U.precheck(meter, provider=prov.usage_provider, model=atlas_agent.get("model") or prov.default_model,
                   key_kind=prov.key_kind)
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
    # only "custom" takes an address from the browser; the vendor presets always call the vendor (a base_url sent with
    # preset=openai used to be stored unchecked - a way around the address check below)
    base = str(b.get("base_url") or "").strip() if preset == "custom" else p.get("base_url", "")
    if preset == "custom":
        if not base.startswith(("http://", "https://")):
            return jsonify({"error": "give the server's base URL (https://…/v1)"}), 400
        why = SEC.private_url_reason(base, unresolvable_ok=False)   # a name that resolves nowhere today is not stored
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
    pcfg["url_guard"] = True                           # checked again now, not only when it was saved
    pcfg["env_key"] = False                            # no key of its own -> none is sent (never the house key)
    meter = U.Meter(owner_id=int(d.get("owner_id") or 0), desk_id=int(d["id"]), scope="owner", lane="you",
                    surface="byo.test", ref=f"key:{name}"[:60])   # on the owner's key: metered, never limited
    try:
        note = make_provider(pcfg, meter=meter).test(e.get("default_model") or "")
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
    except U.UsageLimit:
        raise                                            # S7: the 429 body
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
    tin, tout = int(s.get("tokens_in") or 0), int(s.get("tokens_out") or 0)
    # the one model-cost figure Home and the workspace both show, with the same words: this desk's tokens, all time,
    # priced at paid-model rates ($5 in / $25 out per million, in GBP). An estimate, not a bill - runs on free models cost
    # nothing, and the runs do not record which tier they used - so "at paid rates" always leads the line; what the
    # provider reported for each request is on the Usage page (usage spec §5.5).
    tier, tok = str(desk.get("tier") or "free"), tin + tout
    compact = f"{tok / 1e6:.1f}M" if tok >= 1_000_000 else f"{tok / 1e3:.1f}K" if tok >= 1000 else str(tok)
    gbp = round((tin * 5 + tout * 25) / 1e6 * 0.78, 2)
    # the headline both pages show: a free-tier desk's models cost nothing, so it leads with that and keeps the paid-rate
    # figure as context; a paid desk leads with the estimate
    free = tier == "free"
    s["cost"] = {"gbp": gbp, "basis": "estimate", "scope": "desk",
                 "window": "all time", "tokens": tok, "tier": tier,
                 "label": "Model cost" if free else "Model cost · est.", "short": "Cost" if free else "Cost · est.",
                 "value": "Free" if free else f"£{gbp:.2f}", "small": "models" if free else "",
                 "note": f"at paid rates £{gbp:.2f}" if free else f"at paid rates · {compact} tokens",
                 "title": (f"An estimate, not a bill: this desk's {tok:,} tokens (all time) priced at paid-model rates, "
                           "$5 in / $25 out per million, in GBP."
                           + (" This desk runs on free models, which cost nothing; the runs do not record which tier they used."
                              if tier == "free" else "")
                           + (" Demo mode: the agents are scripted and their token counts are simulated." if _mode() == "demo" else "")
                           + " Provider-reported costs are on the Usage page.")}
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


def _run_lane(lane: str | None = None) -> str:
    """Whose work a run is (usage spec §1.1, §3.2): the lane a background entry point bound (jobs, hooks, the case
    clock, deferred starts), else "you" inside a signed-in /api/ request (a person typed or clicked), else "auto"."""
    if lane in ("you", "auto"):
        return lane
    bound_ = U.bound_lane()
    if bound_:
        return bound_
    try:
        if request.path.startswith("/api/") and (OPEN or current_user()):
            return "you"
    except RuntimeError:                                  # no request: a scheduler or worker thread
        pass
    return "auto"


def _run_pools(configs: dict[str, Any]) -> dict[str, bool]:
    """What a run of these agents may draw on (usage spec §1.3 L1, L3, L5, §2). house: an agent on a provider that
    pays with Atlas's keys (Atlas's own Hermes instance included); free / paid: the house pool its model is in. An
    agent on the owner's own account (a BYO entry, the desk's own Hermes instance) is never limited by Atlas.
    Scripted (demo) agents are house only under USAGE_DEMO=enforce."""
    from ..providers import provider_id
    provs = (configs.get("providers") or {}).get("providers") or {}
    default = (configs.get("providers") or {}).get("default_provider") or ""
    out = {"house": False, "free": False, "paid": False}
    for a in configs.get("agents") or []:
        if not a.get("enabled", True):
            continue
        pc = provs.get(a.get("provider") or default) or {}
        kind = (pc.get("type") or "").lower()
        if kind == "demo":
            if U.demo_enforced():
                out["house"] = out["free"] = True
            continue
        if pc.get("url_guard"):
            continue
        out["house"] = True
        name = "anthropic" if kind == "anthropic" else "hermes" if kind == "hermes_agent" else provider_id(pc.get("base_url") or "")
        pool = U.classify(name, a.get("model") or pc.get("default_model") or "", "house")
        out["free" if pool.startswith("free:") else "paid"] = True
    return out


def _start_run(desk: dict[str, Any], task: str, mode: str, lead_id: int | None = None, case_id: int | None = None,
               lane: str | None = None) -> str:
    """Start a run on its own thread and return its id. Usage (spec §1.3, §4.2): the run gets one meter (its lane,
    the per-run cap) that every request of the run counts for. It does not start, and raises UsageLimit (the error
    handler's 429), when the free pool, the lane or the owner's allowance cannot carry it (its headroom), when the paid
    allowance is spent, or when the owner already has the most runs going at once."""
    configs = _preflight(desk)
    if case_id:
        configs["case_id"] = int(case_id)                 # approvals queued in this run belong to the case
    meter = U.run_meter(desk, _run_lane(lane))
    pools = _run_pools(configs)
    U.run_begin(meter, house=pools["house"], free=pools["free"], paid=pools["paid"])
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

    try:
        orch = Orchestrator(configs, dstore, emit, meter=meter)
    except BaseException:
        U.run_end(meter)                           # nothing started: the runs-at-once slot goes back
        raise
    holder: dict[str, Any] = {"events": events, "orch": orch, "task": task, "desk_id": desk["id"], "started": time.time(),
                              "lead_lock": threading.Lock()}

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
        finally:
            U.run_end(meter)                       # L5: no longer going, before the case may start its next step
        with holder["lead_lock"]:                  # a run that ends before its start returns keeps its lead's final status
            holder["ended"] = time.time()
            if lead_id is not None:
                dstore.set_lead(lead_id, status=("processed" if status == "done" else status), run_id=orch.run_id)
        cid = orch.case_id or case_id
        if cid:                                    # the case decides what happens next (atlas/cases.py), by limit too
            try:
                C.run_finished(store, int(cid), orch.run_id, status, res.summary if res else "",
                               limit=getattr(res, "limit", None) if res else None)
            except Exception:
                import traceback as _tb
                _tb.print_exc()

    th = threading.Thread(target=work, daemon=True)
    holder["thread"] = th
    try:
        th.start()
    except BaseException:
        U.run_end(meter)
        raise
    for _ in range(50):                            # wait briefly for run_id to exist
        if orch.run_id:
            break
        time.sleep(0.02)
    with _runs_lock:
        _runs[orch.run_id] = holder
        _prune_runs()
    U.run_started(meter, orch.run_id)              # the run's start on the ledger (runs, runs:auto), under its id
    if lead_id is not None:
        with holder["lead_lock"]:
            if not holder.get("ended"):
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
    a desk with cases switched off, gets the plain one-off run it always had. A first start refused at a usage limit
    raises UsageLimit either way; with a case, the case is open and held first (its clock starts the step after the
    limit's retry time), the lead reads `waiting` and the exception carries `case_id`. A lead that continues a case
    already open leaves a refusal to that case's clock."""
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
    try:
        return C.kick(store, desk, c, lead_id=lid, raise_limit=True)
    except U.UsageLimit as exc:                          # the case holds the step until the retry time
        store.set_lead(lid, status="waiting")
        exc.case_id = c["id"]
        raise


def _lead_run_or_wait(desk: dict[str, Any], lid: int, extra: str = "") -> str:
    """S12 (usage spec §1.6): a lead from outside (a web form, a message, a mail) is saved before any run is tried,
    and a refused start never fails the sender. The lead waits instead: its case's clock retries it (cases on), or a
    deferred start does after the limit's retry time (cases off). Returns the run id, or ""."""
    try:
        return _lead_case_run(desk, lid, extra=extra)
    except U.UsageLimit as exc:
        if not getattr(exc, "case_id", None):
            U.defer(U.desk_meter(desk, "run.auto"), "lead", lid, exc)
        return ""


def _waiting_notes(desk: dict[str, Any], leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leads whose run waits for a usage limit carry `waiting`: the limit as it stands and when they run (§1.6)."""
    if not any(l.get("status") == "waiting" for l in leads):
        return leads
    held = {lid: (lim.get("limit_id") or "", lim.get("retry_at")) for lid, lim in C.held_leads(store, desk["id"]).items()}
    held.update(U.deferred_leads(desk["id"]))
    owner = int(desk.get("owner_id") or 0)
    out = []
    for l in leads:
        hit = held.get(int(l["id"])) if l.get("status") == "waiting" else None
        out.append({**l, "waiting": U.lead_note(owner, hit[0], hit[1])} if hit else l)
    return out


@app.get("/api/leads")
def api_leads():
    desk = need_desk()
    return jsonify(_waiting_notes(desk, store.for_desk(desk["id"]).leads()))


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
        except U.UsageLimit:
            raise                                       # S8: the 429 body; nothing was designed or saved
        except Exception as exc:
            return jsonify({"error": f"design failed: {type(exc).__name__}: {str(exc)[:200]}"}), 502
        applied = _apply_team(desk, res["team"])
        if isinstance(applied, str):
            return jsonify({"error": applied}), 400
        desk = store.desk(desk["id"])
        return jsonify({"run_id": _start_run(desk, task, "auto"), "team": applied["team"], "tree": applied["tree"], "warnings": res["warnings"]})
    key = _claim_start(desk["id"], task)
    if key is None:                                     # a double click / second Enter / second tab: one run, one cost
        return jsonify({"error": "duplicate_run", "message": "That job was started a moment ago and is still going: one run, not two."}), 409
    rid = ""
    try:
        rid = _start_run(desk, task, d.get("mode", "auto"))
    finally:
        _release_start(key, rid)
    return jsonify({"run_id": rid})


def _claim_start(desk_id: int, task: str) -> tuple[int, str] | None:
    """Take (desk, job) for a new run unless the same job on the same desk was taken in the last START_DEDUPE_S seconds
    and is still starting or running. Atomic, so two identical requests arriving together start one run."""
    key, now = (int(desk_id), " ".join(task.split()).lower()), time.time()
    with _starts_lock:
        for k, (t, _) in list(_starts.items()):
            if now - t > START_DEDUPE_S:
                _starts.pop(k, None)
        prev = _starts.get(key)
        if prev:
            h = _runs.get(prev[1]) if prev[1] else None
            if not prev[1] or (h and h["thread"].is_alive()):
                return None
        _starts[key] = (now, "")
    return key


def _release_start(key: tuple[int, str], run_id: str) -> None:
    with _starts_lock:
        if key in _starts:
            if run_id:
                _starts[key] = (_starts[key][0], run_id)   # held while that run is alive (within the window)
            else:
                _starts.pop(key, None)                     # it did not start: the next press may try again


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
    meter = U.desk_meter(desk, _action_surface(row), lane="you", ref=f"action:{aid}")
    # S14: the channel's own rules come before the decision: outside WhatsApp's or Instagram's 24-hour reply window
    # -> 409, the desk's SMS segments for today used -> 429. The action stays pending and nothing is sent.
    res = _send_reserve(desk, row, meter) if status == "approved" else None
    try:
        row = dstore.decide_action(aid, status, by=by, note=d.get("note", ""), body=d.get("body"), subject=d.get("subject"))
    except BaseException:
        if res is not None:
            res.release()
        raise
    if status == "approved":
        note = d.get("note", "")
        try:
            result = _dispatch(desk, row, res=res, meter=meter)
            row = dstore.decide_action(aid, "sent", by=by, note=(note + " " + result).strip())
            dstore.add_event(row["run_id"], "sent", "owner", f"{row['kind']} → {row['to']} — {row['subject']} ({result})")
            if row["kind"] in ("email", "whatsapp", "sms", "instagram") and row["to"]:
                to_key = ("ig:" + row["to"]) if row["kind"] == "instagram" and not str(row["to"]).startswith("ig:") else row["to"]
                dstore.add_message(row["kind"], "out", to_key, row.get("body") or "", subject=row.get("subject") or "",
                                   actor=row.get("agent") or "", status="sent", run_id=row.get("run_id") or "", action_id=row["id"])
                contact = _contact_touch(dstore, row["to"]) if row["kind"] != "instagram" else None
                for m in (I.crm_sync(dstore.connectors(), contact) if contact else []):
                    dstore.add_event(row["run_id"], "tool", "owner", f"crm sync → {m}")
            I.notify(dstore.connectors(), f":outbox_tray: *Sent* — {row['kind']} → {row['to']}: {row['subject'] or (row['body'] or '')[:80]} {result}")
        except U.UsageLimit:                     # S14: refused while sending, so nothing went out: pending again, 409/429
            if res is not None:
                res.release()
            dstore.decide_action(aid, "pending", by="", note=note)
            raise
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:300]}"
            row = dstore.decide_action(aid, "failed", by=by, note=(note + " send failed: " + err).strip())
            dstore.add_event(row["run_id"], "error", "owner", f"{row['kind']} → {row['to']} failed: {err}")
            if row["kind"] in ("email", "whatsapp", "sms", "instagram") and row["to"]:
                to_key = ("ig:" + row["to"]) if row["kind"] == "instagram" and not str(row["to"]).startswith("ig:") else row["to"]
                dstore.add_message(row["kind"], "out", to_key, row.get("body") or "", subject=row.get("subject") or "",
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
                    "channels": {k: bool(I.outbound_connector(conns, k)) for k in ("email", "whatsapp", "sms", "instagram")}})


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
    if kind not in ("email", "whatsapp", "sms", "instagram"):
        return jsonify({"error": "channel must be email, whatsapp, sms or instagram"}), 400
    if not to or not body:
        return jsonify({"error": "to and body required"}), 400
    ds = store.for_desk(desk["id"])
    conn = I.outbound_connector(ds.connectors(), kind)
    if not conn:
        return jsonify({"error": f"no {kind} connector yet — add one under Integrations"}), 400
    addr = to.replace("ig:", "") if kind == "instagram" else to
    # S15: outside WhatsApp's or Instagram's reply window -> 409, the desk's SMS segments for today -> 429; then nothing
    # is sent and nothing is stored on the thread
    res = U.channel_send(desk, kind, addr, meter=U.desk_meter(desk, f"send.{kind}", lane="you"))
    try:
        result, ok = _deliver_metered(desk, conn, kind, addr, subject, body, res=res), True
    except U.UsageLimit:
        raise
    except Exception as exc:
        result, ok = f"{type(exc).__name__}: {str(exc)[:200]}", False
    mid = ds.add_message(kind, "out", ("ig:" + to) if kind == "instagram" and not to.startswith("ig:") else to,
                         body, subject=subject, actor=_who(), status="sent" if ok else "failed")
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
    # a run stopped at a usage limit finished, but did not fail: counted on its own as `limited` (usage spec §5.5)
    finished = [r for r in runs if r["status"] in ("done", "error", "failed", "interrupted", "cancelled", "limit")]
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
    tin, tout = sum(r["tokens_in"] or 0 for r in runs), sum(r["tokens_out"] or 0 for r in runs)
    tokens = tin + tout
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
        alerts.append({"level": "critical", "key": "spend", "text": blocked + " - paid models on Atlas's account are refused"})
    elif sp["house_paid_limit"] and sp["house_paid_used"] >= 0.8 * sp["house_paid_limit"]:
        alerts.append({"level": "warn", "key": "spend80", "text": f"paid models on Atlas's account: ${sp['house_paid_used']:.2f} "
                                                                  f"of ${sp['house_paid_limit']:.2f} this UTC month"})
    return {
        "spend": {**{k: sp.get(k) for k in ("usage_monthly", "limit", "limit_remaining", "free_used", "free_limit", "error",
                                             "house_paid_used", "house_paid_limit")}, "blocked": bool(blocked)},
        "ok": not any(a["level"] == "critical" for a in alerts), "window_h": window_h, "uptime_s": round(now - _BOOT_TS),
        "runs": {"total": len(runs), "by_status": by, "failure_rate": round(len(bad) / len(finished), 3) if finished else 0.0,
                 "p50_s": p(0.5), "p90_s": p(0.9), "active": len(live), "stalled": len(stalled), "zombies": len(zombies),
                 "limited": by.get("limit", 0)},
        "tokens": tokens, "cost_gbp": round((tin * 5 + tout * 25) / 1e6 * 0.78, 2),   # the Model cost tile's basis (paid rates), over this window
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


def _spend_poll(force: bool = False) -> dict[str, Any]:
    """The OpenRouter key snapshot (GET /api/v1/key, every USAGE_KEY_POLL_S by the usage thread) plus this month's
    paid spend on Atlas's account as the ledger sees it. The old credits read needed a management key and compared
    all-time usage with the cap, so it never worked."""
    if force:
        U.poll_key()
    return U.spend_view()


def _spend_blocked() -> str:
    """Non-empty when paid models on Atlas's account are refused: L13, the house's paid cap for this UTC month
    (USAGE_HOUSE_PAID_USD_MONTH, else SPEND_CAP_USD, else $10). Free-model work is never blocked by it."""
    return U.house_paid_blocked()


# ---------------------------------------------------------------------------- usage: the page and its API (usage spec §5, §6)
def _usage_who() -> tuple[int, bool]:
    """Whose usage a request reads, and whether the caller has the operator view (spec §5.4): the signed-in account
    (owner 0 in open test mode, where the operator view is open too). ?owner=<id> reads another account: operator
    only, 403 for anyone else."""
    u = current_user()
    operator = OPEN or U.is_operator(u)
    owner = int(u["id"]) if u else 0
    raw = request.args.get("owner")
    if raw not in (None, ""):
        if not operator:
            abort(Response(json.dumps({"error": "operator_only", "message": "Only the operator can read another "
                                                                            "account's usage."}), 403, mimetype="application/json"))
        try:
            owner = int(raw)
        except ValueError:
            abort(Response(json.dumps({"error": "bad_owner"}), 400, mimetype="application/json"))
    return owner, operator


def _usage_desks(owner: int) -> list[dict[str, Any]]:
    """The owner's desks, with what the page needs from each: its name, its connectors, whether it can send SMS."""
    out = []
    for d in store.desks_for(owner):
        cons = store.connectors(d["id"])
        name = ((d.get("config") or {}).get("business") or {}).get("name") or d["name"]
        out.append({"id": int(d["id"]), "name": str(name), "desk": d, "cons": cons,
                    "sms": any(c["kind"] == "twilio" for c in cons)})
    return out


def _usage_desk_filter(desks: list[dict[str, Any]]) -> int | None:
    """?desk=<id>: one of the owner's desks (404 otherwise); none = all desks."""
    raw = request.args.get("desk")
    if raw in (None, "", "0", "all"):
        return None
    try:
        did = int(raw)
    except ValueError:
        did = -1
    if not any(d["id"] == did for d in desks):
        abort(Response(json.dumps({"error": "no_such_desk"}), 404, mimetype="application/json"))
    return did


def _usage_cameras(owner: int, desks: list[dict[str, Any]], desk_id: int | None) -> list[dict[str, Any]]:
    """Each camera's AI work as the page shows it (spec §1.5, §1.7): local detector or AI counts, the last tick's
    state (paced, paused), the pacing gaps that make the day's allowance last until 00:00 UTC, and the journal's own
    cadence beside them."""
    from .. import journal as JR
    out: list[dict[str, Any]] = []
    cams = [(d, c) for d in desks if not desk_id or d["id"] == desk_id for c in d["cons"] if c["kind"] == "camera"]
    if not cams:
        return out
    local = bool(V.DETECTOR.available)
    scope = U.desk_scope(owner)
    gap1 = U.pace_gap(owner, per_action=1, scope=scope)
    gap2 = U.pace_gap(owner, per_action=2, scope=scope)
    jobs: dict[int, list[dict[str, Any]]] = {}
    for d, c in cams:
        conf = c.get("config") or {}
        jc, rule = JR.config(conf), V.rule_config(conf)
        seen = scheduler.last_seen(d["id"], c["name"]) or {}
        js = jobs.setdefault(d["id"], store.jobs(d["id"]))
        job = next((j for j in js if j["kind"] == "camera_watch" and (j["task"] or "").find(f'"connector": "{c["name"]}"') >= 0), None)
        jgap = gap2 if jc["verify"] else gap1
        backend = str(seen.get("backend") or "")
        out.append({"desk_id": d["id"], "desk": d["name"], "name": c["name"], "watching": bool(job and job.get("enabled")),
                    "detector": "local" if local else "vlm",
                    "detect": backend if backend in ("paced", "paused", "error") else ("ok" if seen else ""),
                    "detect_status": str(seen.get("detect_status") or ""),
                    "detect_gap_s": round(gap1) if not local and gap1 else None,
                    "journal": jc["on"], "journal_every_s": jc["every_s"],
                    "journal_status": str(seen.get("journal_status") or "") if jc["on"] else "",
                    "journal_gap_s": round(jgap) if jc["on"] and jgap > jc["min_gap_s"] else None,
                    "alert_on_motion": bool(rule.get("alert_on_motion")), "last_tick": seen.get("ts")})
    return out


def _usage_byo(owner: int, desks: list[dict[str, Any]], desk_id: int | None, now: float) -> list[dict[str, Any]]:
    """The owner's own model keys (spec §2 rule 4): today's requests on each as the ledger counted them, and for an
    OpenRouter key the provider's own figures for it, read with the owner's key while serving this request (at most
    every 5 minutes per key)."""
    from ..providers import provider_id
    out: list[dict[str, Any]] = []
    for d in desks:
        if desk_id and d["id"] != desk_id:
            continue
        byo = _desk_byo(d["desk"])
        for name, blob in (byo.get("entries") or {}).items():
            e = SEC.decrypt_config(blob)
            pub = _byo_public(name, e)
            pid = "anthropic" if e.get("type") == "anthropic" else provider_id(str(e.get("base_url") or ""))
            view = U.byo_key_view(f"byo:{d['id']}:{name}", str(e.get("api_key") or "")) if pid == "openrouter" else None
            lt = pub.get("last_test") or {}
            out.append({"desk_id": d["id"], "desk": d["name"], "name": name, "label": pub["label"], "provider": pid,
                        "key_hint": pub["key_hint"], "default": byo.get("default") == name,
                        "last_test": {"ok": bool(lt.get("ok")), "ts": lt.get("ts")} if lt else None,
                        "today": U.byo_today(owner, d["id"], pid, now), "provider_view": view,
                        "provider_note": "" if pid == "openrouter" else
                        f"Usage not available from {pub['label']}; Atlas counts tokens from each response."})
    return out


def _usage_rules(owner: int, desks: list[dict[str, Any]], held_today: int) -> list[dict[str, Any]]:
    """W1, the reply window (spec §1.3): which of the owner's channels it holds, how many sends it held today, and
    whether camera alerts go by WhatsApp (they reach the owner only inside the window too)."""
    from .. import alerts as AL
    chans: set[str] = set()
    for d in desks:
        for c in d["cons"]:
            if c["kind"] == "whatsapp" or (c["kind"] == "twilio" and (c.get("config") or {}).get("whatsapp_from")):
                chans.add("whatsapp")
            elif c["kind"] == "instagram":
                chans.add("instagram")
    return [{"id": "reply_window", "label": "WhatsApp and Instagram: 24-hour reply window",
             "channels": sorted(chans), "held_today": held_today,
             "alerts_on_whatsapp": any(AL.config(d["desk"])["channel"] == "whatsapp" for d in desks),
             "basis": U.basis(owner, "reply_window")}]


@app.get("/desk/usage")
def desk_usage_page():
    """The Usage page (spec §6): limits with their bars and resets, where today's requests went, recent usage, keys."""
    if not current_user() and not OPEN:
        return redirect(_login_next())
    return send_from_directory(STATIC_DIR, "usage.html")


@app.get("/api/usage")
def api_usage():
    """GET /api/usage[?desk=<id>] (spec §5.1): the caller's own limits, today's usage, the last 30 days and the newest
    events. ?owner=<id> is the operator's."""
    owner, operator = _usage_who()
    desks = _usage_desks(owner)
    did = _usage_desk_filter(desks)
    now = time.time()
    data = U.page(owner, desks=[{"id": d["id"], "name": d["name"], "sms": d["sms"]} for d in desks], desk_id=did,
                  operator=operator, now=now)
    cams = _usage_cameras(owner, desks, did)
    paced = {(c["desk_id"], c["name"]): c for c in cams}
    for t in data["today"]:                            # a camera's rows carry its pacing (one note every N s)
        c = paced.get((t.get("desk_id"), t.get("camera")))
        gap = c and (c["journal_gap_s"] if t["surface"] == "camera.note" else c["detect_gap_s"] if t["surface"] == "camera.detect" else None)
        if gap:
            t["paced"] = {c["name"]: gap}
    data.update(mode=_mode(), open=OPEN, desk_filter=did, desks=[{"id": d["id"], "name": d["name"]} for d in desks],
                cameras=cams, byo=_usage_byo(owner, desks, did, now),
                rules=_usage_rules(owner, desks, data["sends"]["held_today"]))
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/usage/events")
def api_usage_events():
    """GET /api/usage/events?cursor=&desk=&status=all|refused|error&limit=50 (spec §5.2): grouped, newest first."""
    owner, _operator = _usage_who()
    desks = _usage_desks(owner)
    did = _usage_desk_filter(desks)
    return jsonify(U.events_page(owner, cursor=request.args.get("cursor") or "", desk_id=did,
                                 status=request.args.get("status") or "all", limit=request.args.get("limit", type=int) or 50,
                                 names={d["id"]: d["name"] for d in desks}))


@app.get("/api/usage/detail")
def api_usage_detail():
    """One Recent usage line, expanded: ?ids=<item ids> (as /api/usage/events gives them) -> each request with its
    model, status, HTTP status, tokens and cost, and where to see the run."""
    owner, _operator = _usage_who()
    ids = [x for x in str(request.args.get("ids") or "").split(",") if x.strip()][:20]
    rows = U.event_detail(owner, ids)
    links = []
    if any(r["kind"] != "refusal" and (r["surface"] in U.RUN_START_SURFACES or r["surface"] in ("run.step", "browse.step",
                                                                                                  "hermes.inner")) for r in rows):
        links.append({"href": "/desk#runs", "label": "Run history"})        # a run that started: its result is there
    if any(str(r["surface"]).startswith("design.") for r in rows):
        links.append({"href": "/desk/workspace", "label": "Atlas workspace"})
    return jsonify({"rows": rows, "links": links})


@app.get("/api/usage/brief")
def api_usage_brief():
    """GET /api/usage/brief (spec §5.3): the rail badge's state and the limit nearest to full."""
    u = current_user()
    return jsonify(U.brief_view(int(u["id"]) if u else 0))


@app.get("/api/usage/house")
def api_usage_house():
    """GET /api/usage/house (spec §5.4), operator only (USAGE_OPERATOR_EMAILS, or open test mode)."""
    _owner, operator = _usage_who()
    if not operator:
        return jsonify({"error": "operator_only", "message": "The house view is for the operator."}), 403
    return jsonify(U.house_view())


_last_prune = [0.0]
_last_reconcile = [0.0]


def _folder_bytes(root: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                continue
    return total


def _storage_walk() -> dict[int, int]:
    """What each owner's uploads take on disk (usage spec §1.3 L9 step 3), under ATLAS_DATA_DIR: camera recordings
    (uploads/cameras/u<owner>; open mode's shared folder is owner 0), watched videos (videos/desk<id>) and saved cyber
    recordings (cyber/recordings/<desk id>), each desk's folder counted for its owner. Cyber log uploads are deleted
    once parsed; the folder of a deleted desk belongs to nobody."""
    sizes: dict[int, int] = {}
    owners: dict[int, int | None] = {}

    def add(oid: int | None, folder: Path) -> None:
        if oid is not None:
            sizes[oid] = sizes.get(oid, 0) + _folder_bytes(folder)

    def desk_owner(did: int) -> int | None:
        if did not in owners:
            d = store.desk(did)
            owners[did] = int(d.get("owner_id") or 0) if d else None
        return owners[did]

    for base, pattern, by_desk in ((CAMERA_UPLOAD_DIR, r"u(\d+)|open", False), (VIDEO_DIR, r"desk(\d+)", True),
                                   (cfg.DATA_DIR / "cyber" / "recordings", r"(\d+)", True)):
        if not base.is_dir():
            continue
        for folder in base.iterdir():
            m = re.fullmatch(pattern, folder.name)
            if not (m and folder.is_dir()):
                continue
            num = int(m.group(1)) if m.group(1) else 0
            add(desk_owner(num) if by_desk else num, folder)
    return sizes


def _watchdog_loop() -> None:
    """Every 15s: kill runs past RUN_MAX_S, mark orphaned DB rows failed, push critical alerts. Hourly: usage retention.
    At boot and then daily: each owner's stored uploads measured on disk (the L9 reconcile)."""
    while True:
        try:
            now = time.time()
            if now - _last_prune[0] > 3600:
                _last_prune[0] = now
                U.prune(now)
            if now - _last_reconcile[0] > 86400:
                _last_reconcile[0] = now
                U.reconcile_storage(_storage_walk())
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
def _sample_leads(desk: dict[str, Any], flags: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Template samples for the stock business; for a customised business ask the (cheap) model to invent
    three realistic enquiries so the demo matches what the client actually does. At a usage limit the stock samples
    stand in, and `flags` says so (samples: "stock (limit)")."""
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
        pool = ProviderPool(c["providers"], meter=U.desk_meter(desk, "seed.leads", lane="you"))
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
    except U.UsageLimit:                                 # S9: no request was sent; the stock samples, flagged
        if flags is not None:
            flags["samples"] = "stock (limit)"
        return stock
    except Exception:
        return stock


@app.post("/api/demo/seed")
def api_seed():
    """Sample leads, each worked by a run. A run refused at a usage limit leaves its lead saved and says why
    (`limited`, spec §5.5): one refusal no longer fails the whole seed."""
    desk = need_desk()
    dstore = store.for_desk(desk["id"])
    ids = []
    flags: dict[str, str] = {}
    for L in _sample_leads(desk, flags):
        lid = dstore.add_lead(L["name"], L["company"], L["email"], L["phone"], L["source"], L["notes"])
        dstore.upsert_contact(L["email"], {"name": L["name"], "company": L["company"], "email": L["email"], "phone": L["phone"], "stage": "New", "notes": "Inbound lead"})
        item: dict[str, Any] = {"id": lid}
        try:
            item["run_id"] = _lead_case_run(desk, lid)
        except U.UsageLimit as exc:                      # S9: the lead stays; its run did not start
            item.update(run_id="", limited=exc.message)
        ids.append({**item, **flags})
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


_MESSAGE_KINDS = ("email", "whatsapp", "sms", "instagram", "slack")    # sends (usage spec §1.4 send.*); booking is an action


def _action_surface(row: dict[str, Any]) -> str:
    """The ledger surface of an approved action (usage spec §1.4): send.<channel> for a message, else action.<what>."""
    kind = row["kind"]
    if kind in _MESSAGE_KINDS:
        return "send." + kind
    return {"booking": "action.calendar", "browser_action": "action.browser", "api_call": "action.http",
            "containment": "action.containment"}.get(kind, "action." + str(kind or "other")[:30])


def _send_reserve(desk: dict[str, Any], row: dict[str, Any], meter: U.Meter) -> U.Reservation | None:
    """The channel's own rules for one approved message, before anything is decided or sent (S14): outside WhatsApp's
    or Instagram's 24-hour reply window raises ChannelWindow (409), the desk's SMS segments for today UsageLimit (429).
    None when it is not a message or no connector carries it: then nothing really goes out (a simulated send)."""
    kind = row["kind"]
    if kind not in _MESSAGE_KINDS or not I.outbound_connector(store.connectors(desk["id"]), kind):
        return None
    return U.channel_send(desk, kind, row["to"] or "", meter=meter)


def _deliver_metered(desk: dict[str, Any], conn: dict[str, Any], kind: str, to: str, subject: str, body: str, *,
                     res: U.Reservation | None = None, meter: U.Meter | None = None) -> str:
    """One message out through the owner's connector, metered (usage spec §1.3 W1 and L7, §4.2): the reply-window read
    and the SMS segment reservation first (unless the caller holds them already), and nothing is sent when they refuse;
    the send itself outside the store lock; then the segments and price Twilio reported are confirmed, or the
    reservation goes back when nothing went out (the ledger keeps the error's type and HTTP status, never its text)."""
    res = res or U.channel_send(desk, kind, to, meter=meter)
    meta: dict[str, Any] = {}
    try:
        out = I.deliver(conn, kind, to, subject, body, meta=meta)
    except BaseException as exc:
        res.release(error=U.send_error_note(exc), provider=str(meta.get("provider") or ""))
        raise
    res.confirm(meta.get("segments"), provider=str(meta.get("provider") or ""), cost_usd=meta.get("cost_usd"),
                note=str(meta.get("price_note") or ""))
    return out


def _counted(meter: U.Meter, surface: str, fn) -> Any:
    """An approved action that really runs: one ledger row, counted and never limited here (L8 is checked before an
    unattended one acts); a failure is recorded with the error's type and re-raised."""
    try:
        out = fn()
    except Exception as exc:
        U.action(meter, "action", surface=surface, status="error", note=U.send_error_note(exc))
        raise
    U.action(meter, "action", surface=surface, note="approved")
    return out


def _dispatch(desk: dict[str, Any], row: dict[str, Any], *, res: U.Reservation | None = None,
              meter: U.Meter | None = None) -> str:
    """Perform an approved action for real when a connector exists; otherwise simulate and say so. A message goes out
    through _deliver_metered (`res`: the reply window and SMS segment the caller already reserved); anything else that
    really runs is counted on the ledger. `meter`: who it counts for; else the meter the caller bound in this thread
    for this desk (alerts.queue: the auto alert's), else the desk's owner in the bound lane."""
    dstore = store.for_desk(desk["id"])
    kind = row["kind"]
    bound = U.current()
    if meter is None and bound is not None and int(bound.desk_id or 0) == int(desk["id"]):
        meter = bound
    meter = meter or U.desk_meter(desk, _action_surface(row), lane=U.bound_lane() or "you", ref=f"action:{row['id']}")
    if res is not None and kind not in _MESSAGE_KINDS:
        res.release()
        res = None
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
                hit = I.http_call(conn["config"], "POST", path, None, body, timeout=15)
                results.append(f"{t['action']} {t['value']} → HTTP {hit['status']}")
                failed = failed or hit["status"] >= 400
            except Exception as exc:
                results.append(f"{t['action']} {t['value']} → {type(exc).__name__}: {str(exc)[:80]}")
                failed = True
        U.action(meter, "action", surface="action.containment", status="error" if failed else "ok",
                 note=f"{len(spec['targets'])} target(s)")
        summary = f"containment via {conn['name']}: " + "; ".join(results)
        if failed:
            raise RuntimeError(summary)          # api_decide marks the action "failed" with this note
        return f"[{summary}]"
    if kind in I.CHANNELS:
        conn = I.outbound_connector(dstore.connectors(), kind)
        if not conn:
            if res is not None:                  # nothing goes out: the reserved segment goes back
                res.release()
            return f"[simulated {kind} — no {' / '.join(I.CHANNELS[kind])} connector; add one under Integrations]"
        args = (conn, kind, row["to"], row["subject"] or "", row["body"] or "")
        if kind == "booking":                    # a calendar booking is an action, not a message
            return "[" + _counted(meter, "action.calendar", lambda: I.deliver(*args)) + "]"
        return "[" + _deliver_metered(desk, *args, res=res, meter=meter) + "]"
    if kind == "browser_action":
        from .. import browser as BR
        try:
            spec = json.loads(row["body"] or "{}")
            out = "[" + BR.perform_pending(spec.get("pending") or {}, spec.get("profile") or f"desk{desk['id']}") + "]"
        except Exception as exc:
            U.action(meter, "action", surface="action.browser", status="error", note=U.send_error_note(exc))
            return f"[browser action failed - {type(exc).__name__}: {str(exc)[:160]}]"
        U.action(meter, "action", surface="action.browser", note="approved")
        return out
    if kind == "api_call":
        conn = dstore.connector_by_name(row["to"])
        if not conn:
            return "[failed — connector missing]"
        spec = json.loads(row["body"] or "{}")
        if conn["kind"] == "higgsfield" or "media" in spec:
            gen = _counted(meter, "action.higgsfield", lambda: I.higgsfield_generate(
                conn["config"], spec.get("media", "image"), spec.get("prompt", ""), spec.get("image_url") or "",
                spec.get("duration"), spec.get("aspect_ratio") or "16:9"))
            urls = ", ".join(gen["outputs"]) or "no output URL returned"
            return f"[Higgsfield {gen['model']} {gen['status']}: {urls}]"
        if conn["kind"] == "mcp" or "mcp_tool" in spec:
            from .. import mcp_client as M
            out = _counted(meter, "action.mcp", lambda: M.REGISTRY.get(conn).call(spec["mcp_tool"], spec.get("arguments") or {}))
            return f"[MCP {spec['mcp_tool']}: {out[:160]}]"
        hit = _counted(meter, "action.http", lambda: I.http_call(conn["config"], spec.get("method", "POST"),
                                                                 spec.get("path", ""), spec.get("params"), spec.get("body")))
        return f"[HTTP {hit['status']} {hit['url']}]"
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
                    "instagram_hook_url": hook + "/instagram",
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
        why = _camera_source_reason(config["source"], desk.get("owner_id")) if config["source"] else None
        if why:
            return jsonify({"error": why}), 400
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
            why = _camera_source_reason(d["config"]["source"], desk.get("owner_id")) if d["config"]["source"] else None
            if why:
                return jsonify({"error": why}), 400
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
    scheduler.RUN_NOW[jid] = int(desk.get("owner_id") or 0)   # a person pressed it: its run counts in lane you
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


def _camera_status(c: dict[str, Any], seen: dict[str, Any], last: dict[str, Any] | None, job: dict[str, Any] | None,
                   now: float) -> dict[str, Any]:
    """One honest answer to "is this camera live?", the same for every page: live only while its watch job is on AND
    the newest frame is recent (within 3 passes, at least a minute). A frame from days ago is stale, never live."""
    conf = c.get("config") or {}
    try:
        every = float(json.loads((job or {}).get("task") or "{}").get("every_s") or 30)
    except (TypeError, ValueError):
        every = 30.0
    frame_ts = float(seen.get("ts") or (last or {}).get("ts") or 0)
    watching = bool(job and job.get("enabled"))
    fresh = bool(frame_ts) and now - frame_ts <= max(3 * every, 60)
    problem = ("stored settings cannot be read - re-enter the stream address" if conf.get("_decrypt_error")
               else "" if str(conf.get("source", "") or "") else "no stream address yet")
    return {"state": "live" if watching and fresh else ("waiting" if watching else "off"), "watching": watching,
            "every_s": every, "frame_ts": frame_ts, "problem": problem}


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
    now = time.time()
    notes = store.last_vision_notes(desk["id"], now - 30 * 86400)
    jobs = store.jobs(desk["id"])
    for c in store.connectors(desk["id"]):
        if c["kind"] != "camera":
            continue
        seen = scheduler.last_seen(desk["id"], c["name"]) or {}
        last = store.last_vision_event(desk["id"], c["name"])
        job = next((j for j in jobs if j["kind"] == "camera_watch" and
                    (j["task"] or "").find(f'"connector": "{c["name"]}"') >= 0), None)
        src = str(c["config"].get("source", "") or "")
        cams.append({**_conn_public(c), "source_kind": V.source_kind(src) if src else "",
                     "sample": DS.sample_name(src),   # sample footage: shown as sample:<name>
                     "rule": V.rule_config(c["config"]),
                     "seen": {k: v for k, v in seen.items() if k not in ("annotated", "detections")},
                     "last_event": _vev_public(last) if last else None,
                     "last_note": notes.get(c["name"]),
                     "journal": str(c["config"].get("journal", "")).lower() in ("1", "true", "on", "yes"),
                     "status": _camera_status(c, seen, last, job, now),
                     "watch_job": job})
    hook = request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"]) + "/vision"
    nodes = [{**n, "last_event": _vev_public(n["last_event"]) if n.get("last_event") else None}
             for n in store.hook_cameras(desk["id"], time.time() - 86400, tuple(c["name"] for c in cams))]
    return jsonify({"cameras": cams, "nodes": nodes, "mode": _mode(), "last_note_ts": store.last_note_ts(desk["id"]),
                    "detector": {"available": V.DETECTOR.available, "error": V.DETECTOR.error, "weights": getattr(V.DETECTOR, "label", os.path.basename(V.YOLO_WEIGHTS)),
                                 "runtime_error": getattr(V.DETECTOR, "_ov_error", "")},
                    "vlm": {"ready": V.vlm_ready() and _mode() != "demo", "model": V.DEFAULT_VLM},
                    "stats": store.vision_stats(desk["id"], time.time() - 86400), "hook_url": hook,
                    "planned": _planned_cameras(desk, cams),
                    "samples": [{"name": n, "label": DS.sample_label(n)} for n in DS.sample_clips()]})


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
    src = DS.resolve_camera_source(str(d.get("source") or ""))
    if not src:
        return jsonify({"error": "give a stream address (rtsp://, http://, a webcam index) or pick sample footage"}), 400
    why = _camera_source_reason(src, desk.get("owner_id"))
    if why:
        return jsonify({"error": why}), 400
    made, missing = _build_cameras(desk, {"cameras": [dict(cam, source=str(d["source"]))]})
    if not made:
        return jsonify({"error": f"could not use that source for {cam['name']}"}), 400
    return jsonify({"ok": True, "camera": made[0]})


# ---- attaching a feed to a camera tile: upload a recording, test any source, attach it (built or planned camera)
CAMERA_UPLOAD_DIR = cfg.DATA_DIR / "uploads" / "cameras"
CAMERA_UPLOAD_EXT = V.VIDEO_EXT | {".jpg", ".jpeg", ".png"}
CAMERA_UPLOAD_MAX = int(os.environ.get("CAMERA_UPLOAD_MAX_MB", "2048")) * 1024 * 1024
# Any file on this machine as a camera: only on a single-user install (open mode, or CAMERA_LOCAL_FILES=1). On a shared
# server a file source is the owner's own upload or the bundled sample footage, never a path someone typed: the probe
# hands back a frame of whatever it reads, so a path would let one tenant read another's upload or object crops.
CAMERA_LOCAL_FILES = os.environ.get("CAMERA_LOCAL_FILES", "").strip().lower() in ("1", "true", "yes")
_CAMERA_FILE_REFUSED = "camera source refused: use a stream address, a webcam, sample footage or a file you uploaded"


def _signed_in() -> None:
    if not current_user() and not OPEN:
        abort(401)


def _upload_dir(uid: Any) -> Path:
    """Each account's camera uploads live in their own folder (open mode: one shared folder)."""
    return CAMERA_UPLOAD_DIR / (f"u{int(uid)}" if uid else "open")


def _upload_meter(owner: int, desk: dict[str, Any] | None, surface: str) -> U.Meter:
    """Who an upload counts for (usage spec §1.3 L9): the owner's files, on the desk when there is one."""
    return U.Meter(owner_id=int(owner or 0), desk_id=int((desk or {}).get("id") or 0), scope=U.desk_scope(int(owner or 0)),
                   lane="you", surface=surface)


def _charge_upload(owner: int, path: Path, size: int, meter: U.Meter, *, charge: int | None = None) -> None:
    """Charge a saved upload to the owner's files (L9) and check the disk floor, after the write: over either, the
    file is deleted and the 429 raised, so a body sent without a Content-Length cannot get past the limit."""
    try:
        U.take_storage(owner, size, meter=meter, surface=meter.surface, charge=charge)
    except U.UsageLimit:
        Path(path).unlink(missing_ok=True)
        raise


def _camera_source_reason(src: str, uid: Any) -> str | None:
    """Why `uid` may not point a camera at `src`, or None. Streams may sit on the LAN (cameras do) but never on
    loopback, link-local or the metadata service; files must be this owner's uploads or the sample footage."""
    kind = V.source_kind(src)
    if kind in ("rtsp", "http"):
        return V.stream_reason(src)
    if kind == "webcam" or OPEN or CAMERA_LOCAL_FILES:
        return None
    try:
        p = Path(src.strip()).resolve()
    except (OSError, ValueError, RuntimeError):
        return _CAMERA_FILE_REFUSED
    for root in ((_upload_dir(uid) if uid else None), DS.sample_dir()):
        try:
            if root is not None and p.is_relative_to(root.resolve()):
                return None
        except (OSError, ValueError, RuntimeError):
            continue
    return _CAMERA_FILE_REFUSED


@app.get("/api/cameras/samples")
def api_camera_samples():
    """The sample clips on this server. No desk needed: an owner without a desk can try sample footage while Atlas
    drafts the first one (the clip goes into the draft and goes live when the desk is built). "camera" is the name the
    clip's camera usually gets (the till, the lobby), offered when the owner picks it."""
    _signed_in()
    return jsonify({"samples": [{"name": n, "label": DS.sample_label(n), "camera": DS.sample_camera_name(n)} for n in DS.sample_clips()]})


@app.post("/api/cameras/upload")
def api_camera_upload():
    """multipart "file": a recording (mp4, mov, mkv, ...) or a still image, stored on the server and played as a camera
    (recordings loop on the shared clock, like the sample footage). Returns {"source": path} for /attach."""
    _signed_in()
    if (request.content_length or 0) > CAMERA_UPLOAD_MAX:     # before the body is parsed, not after
        return jsonify({"error": f"file is larger than {CAMERA_UPLOAD_MAX // (1024 * 1024)} MB"}), 413
    u = current_user()
    owner = int(u["id"]) if u else 0
    um = _upload_meter(owner, None, "upload.camera")
    if request.content_length:                               # L9 and the disk floor before the body is read (429)
        U.check_storage(owner, request.content_length, meter=um)
    request.max_content_length = CAMERA_UPLOAD_MAX           # also bounds a body sent without a Content-Length
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "choose a video or image file"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in CAMERA_UPLOAD_EXT:
        return jsonify({"error": f"{ext or 'that file type'} is not supported; use " + ", ".join(sorted(CAMERA_UPLOAD_EXT))}), 400
    folder = _upload_dir(u["id"] if u else None)
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(f.filename).stem).strip("-")[:40] or "camera"
    dest = folder / f"{stem}-{secrets.token_hex(8)}{ext}"
    f.save(dest)
    size = dest.stat().st_size
    _charge_upload(owner, dest, size, um)
    return jsonify({"source": str(dest), "name": f.filename, "kind": V.source_kind(str(dest)), "bytes": size})


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
    u = current_user()
    why = _camera_source_reason(src, u["id"] if u else None)
    if why:
        return jsonify({"ok": False, "kind": kind, "error": why}), 400
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
    why = _camera_source_reason(src, desk.get("owner_id"))
    if why:
        return jsonify({"error": why}), 400
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
    meter = U.desk_meter(desk, "camera.look", lane="you", ref=f"cam:{c['name']}", overrun=1)
    question = str(d.get("question") or "").strip()[:500]
    if question and _mode() != "demo":                   # S4: the question needs the vision model; refused as JSON first
        V.vlm_precheck(meter, str(c["config"].get("vlm_model") or ""))
    try:
        r = scheduler.camera_tick(store, desk, c, _start_run, _mode() != "demo",
                                  question=question, force=bool(d.get("trigger")),
                                  meter=meter)
    except U.UsageLimit:
        raise                                            # S4: the 429 body; a limit says nothing about the camera itself
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
    if not rv.is_file():                                          # nothing to review yet: an honest empty answer, not a 404
        return jsonify({"review": None, "results": None, "notes": None,
                        "error": "no evaluation yet: run  py -m atlas mcam-eval  (WILDTRACK under ~/AtlasDemo/wildtrack)"})
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


def _camera_vlm_model(desk: dict[str, Any], camera: str) -> str:
    """The vision model a camera's config pins ("" = the default chain)."""
    conn = next((c for c in store.connectors(desk["id"]) if c["kind"] == "camera" and c["name"] == camera), None)
    return str(((conn or {}).get("config") or {}).get("vlm_model") or "")


def _object_live(desk: dict[str, Any], o: dict[str, Any]):
    """(frame, box, vision model) for an object: the live frame while it is still in view, else its stored pictures."""
    OBJ, _ = _objects_mod()
    w = OBJ.worker(desk["id"], o["camera"])
    frame, box = w.latest(o["id"]) if w else (None, None)
    return frame, box, _camera_vlm_model(desk, o["camera"])


@app.post("/api/objects/<int:oid>/call")
def api_object_call(oid):
    """Call an object: the vision model examines it now, and (watch on) keeps following it while it stays in view."""
    OBJ, _ = _objects_mod()
    desk, o = _object(oid)
    d = request.get_json(silent=True) or {}
    on = bool(d.get("on", True))
    ds = store.for_desk(desk["id"])
    meter = U.desk_meter(desk, "objects.call", lane="you", ref=f"obj:{oid}", overrun=1)
    if on:                                               # S5: refused as JSON before anything changes
        V.vlm_precheck(meter, _camera_vlm_model(desk, o["camera"]))
    ds.update_vision_object(oid, watch=1 if on else 0)
    if not on:
        return jsonify({"ok": True, "object": _obj_public(ds.vision_object(oid))})
    _require_live(desk)
    frame, box, model = _object_live(desk, o)
    try:
        res = OBJ.call(store, o, model=model, frame=frame, box=box, meter=meter)
    except U.UsageLimit:
        raise                                            # S5: the 429 body, and no "vision model unavailable" note
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
    meter = U.desk_meter(desk, "objects.ask", lane="you", ref=f"obj:{oid}", overrun=1)
    V.vlm_precheck(meter, _camera_vlm_model(desk, o["camera"]))   # S6: refused as JSON first
    _require_live(desk)
    frame, box, model = _object_live(desk, o)
    try:
        text = OBJ.ask(store, o, q, model=model, frame=frame, box=box, meter=meter)
    except U.UsageLimit:
        raise                                            # S6: the 429 body
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
    held = row.get("held") or ""                      # an auto send held at a limit or by the channel's rule says why
    return jsonify({"ok": True, "status": row["status"], "action_id": row["id"],
                    "note": "sent" if row["status"] == "sent" else (("queued for your approval" + (f": {held}" if held else ""))
                                                                   if row["status"] == "pending" else row.get("note") or row["status"])})


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


VIDEO_UPLOAD_MAX = 300 * 1024 * 1024
# Watched videos live under ATLAS_DATA_DIR (they used to go to a data/videos folder relative to the working directory,
# outside the backups and the storage reconcile): one folder per desk.
VIDEO_DIR = cfg.DATA_DIR / "videos"


@app.post("/api/vision/video")
def api_vision_video():
    """Upload a video file (or pass {url}) and have the vision analyst describe it as a timeline.
    The result is logged as a vision event, so 'ask the cameras' can recall it later."""
    desk = need_desk()
    from .. import vision as V
    if (request.content_length or 0) > VIDEO_UPLOAD_MAX:
        return jsonify({"error": f"video too large (max {VIDEO_UPLOAD_MAX // (1024 * 1024)} MB)"}), 413
    meter = U.desk_meter(desk, "video.describe", lane="you", overrun=1)
    V.vlm_precheck(meter)                                      # S1: refused as JSON before the upload is read or saved
    owner = int(desk.get("owner_id") or 0)
    um = _upload_meter(owner, desk, "upload.video")
    if request.content_length and request.mimetype == "multipart/form-data":
        U.check_storage(owner, request.content_length, meter=um)   # L9 and the disk floor before an upload is read (429)
    request.max_content_length = VIDEO_UPLOAD_MAX              # before the form is parsed
    question = (request.form.get("question") or (request.json or {}).get("question", "") if not request.files else request.form.get("question", "")) or ""
    try:
        frames = min(int(request.form.get("frames") or 6), 10)
    except ValueError:
        frames = 6
    f = request.files.get("video")
    saved, size = "", 0
    if f and f.filename:
        vdir = VIDEO_DIR / f"desk{desk['id']}"
        vdir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\- ]+", "_", f.filename)[-80:] or "clip.mp4"
        src = vdir / f"{int(time.time())}_{name}"
        f.save(str(src))
        size = src.stat().st_size
        if size > VIDEO_UPLOAD_MAX:
            src.unlink(missing_ok=True)
            return jsonify({"error": "video too large (max 300 MB)"}), 413
        _charge_upload(owner, src, size, um)
        label = name
        src = saved = str(src)
    else:
        src = str((request.json or {}).get("url", "") or "").strip()
        if not src:
            return jsonify({"error": "attach a video file or pass {url}"}), 400
        from .. import secure as SEC
        why = SEC.private_url_reason(src)
        if why:
            return jsonify({"error": f"refusing to fetch: {why}"}), 400
        label = src.rsplit("/", 1)[-1][:60]
    meter.ref = f"video:{label}"[:120]
    try:
        res = V.describe_video(src, question, frames=frames, context=desk.get("name", ""), meter=meter)
    except U.UsageLimit:                                       # S1: refused after the check: nothing is kept
        if saved:
            Path(saved).unlink(missing_ok=True)
            U.release_storage(owner, size)
        raise
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
        return redirect(_login_next())
    did = request.args.get("desk", type=int)
    if did:                                                # a link to a desk opens THAT desk (same rule as /select), not the cookie's
        u, d = current_user(), store.desk(did)
        if d and (OPEN or (u and d["owner_id"] == u["id"])):
            session["desk"] = did
    elif "new" not in request.args and current_user():
        d = current_desk()                                 # /desk/workspace means "my desk"; ?new=1 means "a new one"
        if d:
            return redirect(f"/desk/workspace?desk={d['id']}")
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
    # built here, used by the stream after the view returned: one narration, the viewer's own action
    meter = U.desk_meter(desk, "theatre.narrate", lane="you", ref=f"live:{camera}", overrun=1)
    V.vlm_precheck(meter, model)                               # S2: a 429 JSON body before the stream starts

    def gen():
        parts = []
        limited = False
        try:
            for delta in V.describe_stream(jpeg, "What is happening in this frame? What changed?", model=model,
                                           context=context, meter=meter):
                if isinstance(delta, dict):                    # provider/model meta arrives first
                    yield "\x01" + json.dumps(delta) + "\n"
                    continue
                parts.append(delta)
                yield delta
        except U.UsageLimit as exc:                            # S2: refused after the check: a meta line, no event
            limited = True
            yield "\x01" + json.dumps(exc.body()) + "\n"
        except Exception as exc:
            yield f"[vision error: {exc}]"
        note = "".join(parts).strip()
        if note and not limited and not note.startswith("[vision error"):
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


def _rag_rows(desk_id: int, question: str, camera: str, hours: float, limit: int = 60, *,
              meter: "U.Meter") -> list[dict[str, Any]]:
    """Retrieval for 'ask the cameras' (kept for callers): hybrid semantic + keyword + time-window, see atlas/rag.py.
    `meter` is what its embeddings count for."""
    return RAG.retrieve(store, desk_id, question, hours, camera, k=limit, meter=meter)["rows"]


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/api/vision/demo")
def api_vision_demo():
    """Public site demo: one small frame + the browser detector's counts in, the vision model's narration streamed
    out as SSE (`data: {"t": "..."}` per token, then `{"done": true}` or `{"error": "..."}`). Rate limited per IP,
    a few concurrent slots, tiny answers, subject to the spend cap — real model, no scripted fallback."""
    import base64

    ip = _client_ip()
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

    return Response(_demo_stream(base, headers, payload, model, meter=_site_meter("site.demo")),
                    mimetype="text/event-stream", headers=hdr)


def _demo_cfg() -> tuple[str, str, str]:
    """Provider for the public site demos. VISION_DEMO_KEY / VISION_DEMO_MODEL let the demo run on its own key and a
    free model while the desks stay in scripted mode (DESK_MODE=demo)."""
    base, key, model = V._vlm_cfg(os.environ.get("VISION_DEMO_MODEL", ""))
    key = (os.environ.get("VISION_DEMO_KEY") or key or "").strip()
    return base, key, model


def _demo_headers(key: str, title: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream",
            "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": title}


def _site_meter(surface: str) -> "U.Meter":
    """The public website's meter (usage spec §1.3 L12): the website's own share of the free pool, never an account's.
    Lane auto: an anonymous visitor never takes the per-minute slots kept for owners' own work."""
    return U.Meter(owner_id=0, desk_id=0, scope="public", lane="auto", surface=surface)


def _sse_limit(exc: U.UsageLimit) -> str:
    """The site demos' SSE body at a usage limit (S16): the page shows the message."""
    return _sse({"error": "usage_limit", "message": exc.message, "resets_at": exc.resets_at})


def _demo_stream(base: str, headers: dict[str, str], payload: dict[str, Any], model: str, first: dict[str, Any] | None = None,
                 *, meter: "U.Meter"):
    """SSE generator shared by the site demos: one concurrency slot, tokens as {"t": ...}, then {"done": true}. The one
    model request is metered for `meter` (the website's share): refused at a limit, nothing is sent and the stream says
    so with {"error": "usage_limit", message, resets_at}; otherwise the request is recorded with the provider's usage."""
    import httpx
    from ..providers import provider_id
    t0 = time.time()
    if not _VISION_SLOTS.acquire(blocking=False):          # acquired inside the generator so a slot is never leaked
        yield _sse({"error": "busy - a few other visitors are using the demo right now, retrying shortly"})
        return
    try:
        if first:
            yield _sse(first)
        try:
            tk = U.before(meter, provider=provider_id(base), model=model, key_kind="house")
        except U.UsageLimit as exc:                         # S16: the website's share or the pool is used up
            yield _sse_limit(exc)
            return
        seen: dict[str, Any] = {"status": 0, "usage": None, "error": ""}
        try:
            with httpx.Client(timeout=60) as c, c.stream("POST", base + "/chat/completions", headers=headers, json=payload) as r:
                seen["status"] = r.status_code
                if r.status_code >= 400:
                    raw = b"".join(r.iter_bytes())[:400].decode("utf-8", "replace")
                    try:
                        err = (json.loads(raw) or {}).get("error")
                        msg = str((err.get("message") if isinstance(err, dict) else err) or raw)
                    except (ValueError, AttributeError):
                        msg = raw
                    U.after(tk, http_status=r.status_code, error=f"HTTP {r.status_code}: {msg[:200]}")
                    if tk.limit is not None:                # the provider's daily free-model quota: closed until 00:00 UTC
                        yield _sse_limit(tk.limit)
                    else:
                        yield _sse({"error": f"model HTTP {r.status_code}: {raw[:200]}"})
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
                    if not isinstance(j, dict):
                        continue
                    if j.get("usage"):
                        seen["usage"] = j["usage"]
                    if j.get("error"):
                        msg = str(j["error"].get("message") if isinstance(j["error"], dict) else j["error"])[:200]
                        seen["error"] = "stream error: " + msg
                        yield _sse({"error": msg})
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
            seen["error"] = seen["error"] or f"{type(exc).__name__}: {str(exc)[:120]}"
            yield _sse({"error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        finally:                                            # recorded even when the visitor closes the page early
            U.after(tk, http_status=seen["status"], usage=seen["usage"], error=seen["error"])
    finally:
        _VISION_SLOTS.release()


_ASK_STOP = {"the", "was", "were", "what", "when", "how", "many", "did", "there", "any", "last", "this", "that", "and",
             "with", "from", "have", "has", "been", "are", "you", "see", "camera", "cameras", "time", "times"}


@app.post("/api/vision/demo/ask")
def api_vision_demo_ask():
    """Public site demo, the RAG half: the browser sends the question plus its own event log (one entry per narration:
    counts + analyst text + clock time). We retrieve the best-matching events, tell the browser which ones ([#n]) were
    used, then stream the model's answer, which must cite them. Stateless - nothing is stored server side."""
    ip = _client_ip()
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
    return Response(_demo_stream(base, headers, payload, model, first={"used": used, "of": len(events)},
                                 meter=_site_meter("site.ask")),
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
    # one Ask: the vision re-look and the embeddings on Atlas's account; the text fallback on the desk's provider,
    # which pays with its own key kind (a desk's own model account is BYO)
    meter = U.desk_meter(desk, "ask.cameras", lane="you", overrun=2)
    text_answer = None
    text_byo = False
    if _mode() != "demo":
        from ..providers import ProviderPool
        atlas_agent = next((a for a in configs["agents"] if a["id"] == "atlas"), {})
        prov = ProviderPool(configs["providers"], meter=meter).get(atlas_agent.get("provider") or "")
        text_byo = prov.key_kind == "byo"

        def text_answer(system: str, prompt: str) -> str:   # noqa: F811 - text-only fallback on the desk's Atlas model
            return (prov.chat(system, [prov.user_message(prompt)], [], model=atlas_agent.get("model", "")).text or "").strip()
    vlm = str(d.get("model") or "") or (DS.PAID_VLM if (desk.get("tier") or "free") != "free" else "")
    if _mode() != "demo" and not text_byo:             # S3: the re-look needs Atlas's vision model; JSON first
        V.vlm_precheck(meter, vlm)                     # (with the owner's own key the text answer still works, S31)
    try:
        res = RAG.ask(store, desk["id"], q, hours, str(d.get("camera") or ""), configs["business"], mode=_mode(),
                      text_answer=text_answer, vlm_model=vlm, meter=meter, text_byo=text_byo)
    except U.UsageLimit:
        raise                                          # S3: the 429 body
    except Exception as exc:
        return jsonify({"error": f"model error: {type(exc).__name__}: {str(exc)[:200]}"}), 502
    scores = res.get("scores") or {}
    best = sorted(res["evidence"], key=lambda r: -scores.get(int(r["id"]), 0))       # best match first for the follow-ups
    cams = [c["name"] for c in store.connectors(desk["id"]) if c["kind"] == "camera"]
    win = RAG.parse_time_window(q)          # a time named in the question wins over `hours` in the search: widen it in words
    span = (win[1] - win[0]) / 3600 if win else hours
    steps = NS.camera_answer(q, best, span, cams, pinned=bool(win), camera=str(d.get("camera") or ""))
    return jsonify({"answer": res["answer"], "evidence": [_vev_public(r) for r in res["evidence"]], "mode": _mode(),
                    "events_considered": res["events_considered"], "retrieval": res["retrieval"],
                    "next": NS.normalise([], steps, allow=NS.RUN)})


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
                snap = V.save_snapshot(desk["id"], camera, V.annotate(V._shrink(raw), [], f"{time.strftime('%d %b %H:%M:%S', time.gmtime())} UTC  {camera}: {note[:60]}"))
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
    rid, limited = "", ""
    if trigger:
        when = time.strftime("%A %d %B %H:%M", time.localtime(ts) if ts is not None else time.localtime())
        task = ("Assess this sensor/camera event, log it, and tell the right person only if it matters.\n\n"
                f"EXTERNAL EVENT — {camera} at {when}\n"
                f"Reported: {V.counts_text(counts) if counts else 'no object counts'}" + (f"; note: {note}" if note else "") + "\n"
                f"Event id: {ev['id']}." + (" Snapshot attached." if snap else "") + " Use camera_events for history; camera_look works only for cameras the desk can reach itself.")
        try:
            with U.bind_lane("auto"):
                if C.enabled(desk, "camera_cases"):
                    _preflight(desk)
                    rid = C.camera_alert(store, desk, camera, "CAMERA ALERT (external sensor)\n" + task, note or "external event",
                                         ev["id"], raise_limit=True)
                else:
                    rid = _start_run(desk, task, "auto")
        except U.UsageLimit as exc:                   # S13: the event is kept and the sensor told why no run started;
            rid, limited = "", exc.message            # no deferral (a run hours later is stale; a case's clock may retry)
        dstore.set_vision_run(ev["id"], rid)
    return jsonify({"ok": True, "event_id": ev["id"], "run_id": rid, **({"limited": limited} if limited else {})})


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
    owner = int(desk.get("owner_id") or 0)
    um = _upload_meter(owner, desk, "upload.cyber")
    if request.content_length:                     # L9 and the disk floor before the body is read (429)
        U.check_storage(owner, request.content_length, meter=um)
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
    charged = 0
    try:
        f.save(str(tmp))
        size = tmp.stat().st_size
        # L9 counts the log while it is on disk: it is parsed into events and its file deleted below, which gives the
        # bytes back (the ledger keeps the upload); over the limit or the disk floor -> 429, nothing parsed
        U.take_storage(owner, size, meter=um, surface=um.surface)
        charged = size
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
        if charged:
            U.release_storage(owner, charged)
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
    p = folder / f"{name}.json"
    old = p.stat().st_size if p.is_file() else 0          # a recording saved under its own name replaces it
    owner = int(desk.get("owner_id") or 0)
    um = _upload_meter(owner, desk, "upload.recording")
    if request.content_length:                            # L9 and the disk floor before anything is written (429)
        U.check_storage(owner, max(0, request.content_length - old), meter=um)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size = tmp.stat().st_size
    _charge_upload(owner, tmp, size, um, charge=size - old)
    os.replace(tmp, p)
    if old > size:
        U.release_storage(owner, old - size)
    return jsonify({"ok": True, "name": name, "size": size})


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
    # a start refused at a usage limit holds the step for the case clock, then answers 429 with the limit (§1.6)
    rid = C.kick(store, desk, c, extra=("The owner asks: " + extra) if extra else "The owner asked Atlas to work this step now.",
                 raise_limit=True)
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
    owner = getattr(s, "owner_id", None) if s else None
    if s and owner is not None:                        # a draft is its owner's: another account in the same browser never sees it
        u = current_user()
        if not u or int(u["id"]) != int(owner):
            s = None
    if not s:
        abort(Response(json.dumps({"error": "no_session"}), 404, mimetype="application/json"))
    return s


def _save_design(s: DS.DesignSession) -> None:
    try:
        store.save_design_session(s.id, s.to_dict())
    except Exception as exc:
        print("design session save failed:", exc)


def _design_spend_gate(s) -> None:
    """Paid designer turns stop at the OpenRouter cap, the same rule as paid desk runs. Free-model turns cost nothing.
    Before this, the studio's say / study / build routes never checked the cap at all."""
    if s.mode != "demo" and s.tier != "free":
        blocked = _spend_blocked()
        if blocked:
            abort(Response(json.dumps({"error": blocked}), 402, mimetype="application/json"))


@app.post("/api/design/start")
def api_design_start():
    u = current_user()
    if not u and not OPEN:
        abort(401)
    _require_live()
    d = request.get_json(silent=True) or {}
    tier = d.get("tier") if d.get("tier") in templates.TIERS else "free"
    s = DS.new_session(_mode(), tier)
    s.owner_id = int(u["id"]) if u else None
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
    profile + first-draft blueprint. SSE: {"t":"status","d":...}* then {"t":"done", profile, transcript, next, suggestions,
    blueprint, ready}."""
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
    _design_spend_gate(s)
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
            q.put({"t": "done", "profile": profile, "transcript": s.transcript, "next": s.next, "suggestions": s.suggestions,
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


@app.get("/api/design/mine")
def api_design_mine():
    """This account's newest unfinished draft (not built, at least one turn, saved in the last 30 days): the workspace
    resumes it on any device or tab instead of a blank brief. {} when there is none."""
    u = current_user()
    if not u:
        return jsonify({})
    for d in store.recent_design_sessions():
        if d.get("owner_id") == u["id"] and not d.get("desk_id") and int(d.get("turn") or 0) > 0:
            return jsonify(_design_session(str(d.get("id"))).public())
    return jsonify({})


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
    _design_spend_gate(s)
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
        s.refresh_next()                                 # no "build" step once the canvas has no agents left
        _save_design(s)
    return jsonify({"blueprint": s.blueprint, "ready": s.ready, "next": s.next})


@app.post("/api/design/<sid>/layout")
def api_design_layout(sid):
    """Where the owner dragged the cards: {positions: {agent_id: {x, y}}}. Stored with the blueprint so the next
    designer turn (and the next browser) draws the team where they left it."""
    s = _design_session(sid)
    d = request.get_json(force=True) or {}
    pos = d.get("positions") if isinstance(d.get("positions"), dict) else {}
    if s.blueprint is None:
        return jsonify({"layout": {}})
    bp = dict(s.blueprint)
    bp["layout"] = pos
    s.blueprint = DS.normalise(bp, s.blueprint) or s.blueprint
    _save_design(s)
    return jsonify({"layout": s.blueprint.get("layout", {})})


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
    s.refresh_next()                                     # the paid switch is offered only while the session is on free models
    _save_design(s)
    return jsonify({"tier": s.tier, "label": templates.TIERS[tier]["label"], "next": s.next})


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
    with _build_lock:                                              # a second build of this draft updates the first one's desk
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
        if not src or _camera_source_reason(src, desk.get("owner_id")):
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


def _run_limit(run_id: str) -> dict[str, Any] | None:
    """The usage limit that stopped a run: its done event's 429 body (usage spec §5.5), while this server still holds
    the run's events (an hour after it ends). The stored run keeps only its "Stopped: ..." summary, so then None."""
    h = _runs.get(run_id)
    for e in reversed(list((h or {}).get("events") or [])):
        if e.get("kind") == "done":
            lim = (e.get("data") or {}).get("limit")
            return lim if isinstance(lim, dict) else None
    return None


@app.post("/api/next")
def api_next():
    """Three ways to proceed in run mode, from the desk's real state (no model call): {context: "desk_open"} when a desk
    opens, {context: "run_done", run_id?, status?, task?} after a job. Never a "say" step: typed text is a job here.
    A run_id must be a run of this desk (else 404); once it has finished, its stored status and task win. A run stopped
    at a usage limit gets that limit's steps, from the run's own done event; a limit is never taken from the body."""
    desk = need_desk()
    d = request.get_json(silent=True)
    d = d if isinstance(d, dict) else {}
    ctx = str(d.get("context") or "")
    if ctx not in ("desk_open", "run_done"):
        return jsonify({"error": "context must be desk_open or run_done"}), 400
    cams = [c["name"] for c in store.connectors(desk["id"]) if c["kind"] == "camera"]
    pending = len(store.actions("pending", limit=100000, desk_id=desk["id"]))
    if ctx == "desk_open":
        c = desk_configs(desk)
        agents = [a for a in c["agents"] if a["id"] != "atlas" and a.get("enabled", True)]
        planned = [p["name"] for p in _planned_cameras(desk, [{"name": n} for n in cams])]
        own = (desk.get("config") or {}).get("workflows")        # a built desk's own list, even when empty: desk_configs
        wfs = own if isinstance(own, list) else c["workflows"]   # backs an empty one with a template's, which the owner never made
        steps = NS.desk_open(c["business"].get("name") or desk["name"], agents, cams, pending, (wfs or [None])[0], planned)
    else:
        rid = str(d.get("run_id") or "").strip()
        status, task = str(d.get("status") or ""), str(d.get("task") or "")
        limit = None
        if rid:
            row, live = store.run(rid), _runs.get(rid)
            if row and row.get("desk_id") == desk["id"]:
                status = status if row.get("status") in (None, "", "running") else row["status"]   # finished: the DB has the truth
                task = row.get("task") or task
            elif live and live["desk_id"] == desk["id"]:           # started, DB row not written yet
                task = live.get("task") or task
            else:
                return jsonify({"error": "no such run on this desk"}), 404
            limit = _run_limit(rid)                                # used only when the run stopped at a limit
        steps = NS.run_done(status or "done", task, pending, cams, limit=limit)
    return jsonify({"context": ctx, "next": NS.normalise([], steps, allow=NS.RUN), "pending": pending})


# an ordinary address: RFC 5322 atext in the local part (o'brien@ included), no quoted strings, comments or spaces
_FORM_EMAIL = re.compile(r"(?=.{3,254}$)[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                         r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")


@app.route("/hook/<token>", methods=["GET", "POST"])
def hook(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    if request.method == "GET":
        return jsonify({"ok": True, "desk": desk["name"], "post": "JSON {name, email, phone, company, notes, source}"})
    d = request.get_json(silent=True) or request.form.to_dict() or {}
    name = str(d.get("name") or d.get("full_name") or "").strip()[:200]
    email_ = str(d.get("email") or "").strip()
    notes = str(d.get("notes") or d.get("message") or d.get("enquiry") or "").strip()
    if not name and not email_:
        return jsonify({"error": "name or email required"}), 400
    if email_ and not _FORM_EMAIL.fullmatch(email_):
        # this URL sits in public website forms: the address becomes a contact and a message-thread key, so it has to
        # be an address and nothing else (double quotes, brackets, backticks, spaces... are refused, never stored)
        return jsonify({"error": "email is not a valid address"}), 400
    company = str(d.get("company") or "").strip()[:200]
    phone = str(d.get("phone") or "").strip()[:40]
    dstore = store.for_desk(desk["id"])
    lid = dstore.add_lead(name or email_.split("@")[0], company, email_, phone, str(d.get("source") or "webhook").strip()[:60], notes)
    if email_ or notes:
        dstore.add_message("form", "in", email_ or name, notes or "(no message)", actor=name or email_)
    if email_ and not dstore.contact_for(email_):
        dstore.upsert_contact(email_, {"name": name, "company": company, "email": email_, "phone": phone, "stage": "New", "notes": "Inbound via webhook"})
    with U.bind_lane("auto"):                        # Atlas works what came in on its own (usage spec §1.1)
        rid = _lead_run_or_wait(desk, lid)
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
    return _lead_run_or_wait(desk, lid, extra=how)


def _meta_unsigned(desk_id: int, kind: str, label: str):
    """None when this POST is signed with the app secret of one of the desk's `kind` connectors, else the refusal.
    The Meta hooks share their URL token with the public web-form hook, so the token alone proves nothing: without
    the signature anyone holding the form URL could forge DMs that start runs and land in the owner's threads."""
    conns = [c for c in store.connectors(desk_id) if c["kind"] == kind]
    if not conns:
        return jsonify({"error": f"no {label} connector on this desk"}), 404
    keys = [str((c.get("config") or {}).get("app_secret") or "").strip() for c in conns]
    keys = [k for k in keys if k]
    if not keys:
        return jsonify({"error": f"set app_secret on the {label} connector (Meta app settings → Basic → App secret): "
                                 "inbound messages are accepted only with Meta's signature"}), 403
    body, sig = request.get_data(cache=True), request.headers.get("X-Hub-Signature-256", "")
    if not any(I.meta_signature_ok(k, body, sig) for k in keys):
        return jsonify({"error": "missing or wrong X-Hub-Signature-256"}), 403
    return None


@app.route("/hook/<token>/whatsapp", methods=["GET", "POST"])
def hook_whatsapp(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    conn = next((c for c in store.connectors(desk["id"]) if c["kind"] == "whatsapp"), None)
    if request.method == "GET":                      # Meta verification handshake
        want = (conn or {}).get("config", {}).get("verify_token") or ""
        if request.args.get("hub.mode") == "subscribe" and want and request.args.get("hub.verify_token") == want:
            return Response(request.args.get("hub.challenge", ""), 200, mimetype="text/plain")   # echoed as text, never HTML
        return jsonify({"error": "verify_token mismatch or no WhatsApp connector"}), 403
    refused = _meta_unsigned(desk["id"], "whatsapp", "WhatsApp")
    if refused:
        return refused
    msgs = I.parse_whatsapp_webhook(request.get_json(silent=True) or {})
    with U.bind_lane("auto"):
        runs = [_inbound_message(desk, m["from"], m["name"], m["text"], "whatsapp") for m in msgs if m.get("from")]
    return jsonify({"ok": True, "messages": len(msgs), "runs": runs})


@app.route("/hook/<token>/instagram", methods=["GET", "POST"])
def hook_instagram(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    conn = next((c for c in store.connectors(desk["id"]) if c["kind"] == "instagram"), None)
    if request.method == "GET":                      # Meta verification handshake, same dance as WhatsApp
        want = (conn or {}).get("config", {}).get("verify_token") or ""
        if request.args.get("hub.mode") == "subscribe" and want and request.args.get("hub.verify_token") == want:
            return Response(request.args.get("hub.challenge", ""), 200, mimetype="text/plain")   # echoed as text, never HTML
        return jsonify({"error": "verify_token mismatch or no Instagram connector"}), 403
    refused = _meta_unsigned(desk["id"], "instagram", "Instagram")
    if refused:
        return refused
    msgs = I.parse_instagram_webhook(request.get_json(silent=True) or {})
    dstore = store.for_desk(desk["id"])
    runs = []
    for m in msgs:                                   # each is stored before its run is tried; a refused run waits
        dstore.add_message("instagram", "in", "ig:" + m["from"], m["text"], actor=m["name"] or ("ig " + m["from"]))
        name = m["name"] or f"Instagram user {m['from'][-4:]}"
        lid = dstore.add_lead(name, "", "", "", "instagram", m["text"])
        how = (f"This arrived as an Instagram DM. Reply on the same channel "
               f"(queue_action kind=instagram, to={m['from']}) — keep it short and warm.")
        with U.bind_lane("auto"):
            runs.append(_lead_run_or_wait(desk, lid, extra=how))
    return jsonify({"ok": True, "messages": len(msgs), "runs": runs})


def _twilio_unsigned(desk_id: int):
    """None when this POST carries Twilio's X-Twilio-Signature made with the auth token of one of the desk's Twilio
    connectors, else the refusal. Same reason as the Meta hooks: the URL token is the public form token, and without
    the signature anyone holding it could forge texts that open leads, threads and runs."""
    conns = [c for c in store.connectors(desk_id) if c["kind"] == "twilio"]
    if not conns:
        return jsonify({"error": "no Twilio connector on this desk"}), 404
    keys = [k for k in (str((c.get("config") or {}).get("auth_token") or "").strip() for c in conns) if k]
    if not keys:
        return jsonify({"error": "set auth_token on the Twilio connector: inbound messages are accepted only with "
                                 "Twilio's signature"}), 403
    # the URL Twilio signed is the public one: what this app sees, the same over https (TLS ends at the proxy), or
    # PUBLIC_URL's origin with this path
    tail = request.path + ("?" + request.query_string.decode("latin-1") if request.query_string else "")
    bases = (request.host_url.rstrip("/"), "https://" + request.host, os.environ.get("PUBLIC_URL", "").strip().rstrip("/"))
    urls = list(dict.fromkeys(b + tail for b in bases if b))
    params = request.form.to_dict(flat=False)
    sig = request.headers.get("X-Twilio-Signature", "")
    if not any(I.twilio_signature_ok(k, urls, params, sig) for k in keys):
        return jsonify({"error": "missing or wrong X-Twilio-Signature"}), 403
    return None


@app.post("/hook/<token>/sms")
def hook_sms(token):
    """Twilio inbound webhook (SMS or WhatsApp sandbox): form fields From, Body, ProfileName, signed by Twilio."""
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    refused = _twilio_unsigned(desk["id"])
    if refused:
        return refused
    f = request.form.to_dict()                       # Twilio posts a form; the signature covers exactly these fields
    frm, body = (f.get("From") or f.get("from") or ""), (f.get("Body") or f.get("body") or "")
    if not frm:
        return jsonify({"error": "From required"}), 400
    source = "twilio whatsapp" if frm.startswith("whatsapp:") else "sms"
    with U.bind_lane("auto"):
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
if not OPEN and AUTH.two_factor_problem():
    print("WARNING: " + AUTH.two_factor_problem())
scheduler.LIVE = lambda desk=None: _mode() != "demo" and not _live_reason(desk)
scheduler.DISPATCH = _dispatch
scheduler.LEAD_RUN = _lead_case_run
U.LEAD_RUN = _lead_case_run                      # deferred lead starts (usage.deferred_tick, once per scheduler pass)
C.START_RUN = _start_run
C.RUN_STATUS = _run_status
C.NOTIFY = lambda desk_id, text: I.notify(store.connectors(desk_id), text)
scheduler.BASE_URL = lambda: os.environ.get("PUBLIC_URL", "").strip()
U.OPEN_MODE = lambda: OPEN                        # desks of user 0 count as public only while accounts are on
U.start(store)                                    # the usage ledger + the provider key snapshot thread
scheduler.start(store, _start_run, store.desk)
threading.Thread(target=_watchdog_loop, daemon=True, name="atlas-watchdog").start()


def main():
    port = int(os.environ.get("PORT", "8094"))
    host = os.environ.get("DESK_HOST", "127.0.0.1").strip() or "127.0.0.1"   # this machine only; DESK_HOST=0.0.0.0 to share on the LAN
    print(f"Atlas Desk  mode={_mode()}  accounts={'off' if OPEN else 'on'}  http://localhost:{port}/desk  (bound to {host})")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
