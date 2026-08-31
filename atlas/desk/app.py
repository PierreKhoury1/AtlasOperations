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

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from .. import config as cfg
from .. import designer as DS
from .. import integrations as I
from .. import metrics as MX
from .. import templates
from . import scheduler
from ..orchestrator import Event, Orchestrator
from ..store import Store

ROOT = cfg.ROOT
SITE_DIR = ROOT / "site"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_PATH = cfg.DATA_DIR / "desk.db"

app = Flask(__name__, static_folder=None)
store = Store(DB_PATH)


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
    if os.environ.get("DESK_PROVIDER", "").strip():
        return "live"
    prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    name = prov.get("default_provider", "anthropic")
    key = cfg.resolve_api_key(prov["providers"].get(name, {"type": "anthropic"}))
    return "live" if key else "demo"


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
        providers["default_provider"] = prov
        for a in agents:
            a["provider"] = prov
        if prov == "openrouter":
            templates.apply_tier(agents, desk.get("tier") or "free")
        else:
            for a in agents:
                a["model"] = ""
    return {"providers": providers, "orchestration": cfg.load("orchestration", cfg.DEFAULT_ORCHESTRATION),
            "business": business, "agents": agents, "workflows": workflows, "ui": {}, "mode": mode,
            "desk_id": desk["id"]}


def ensure_demo_desk() -> dict[str, Any]:
    d = store.desk(1)
    if d is None:
        d = store.add_desk(0, "Acme Estates", "sales_desk", "free", templates.build_desk("sales_desk", {}))
    return d


# ---------------------------------------------------------------------------- auth + desk context
PUBLIC_API = {"/api/health", "/api/stats", "/api/me"}


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
        nxt = request.args.get("next", "/desk")
        return redirect(nxt if nxt.startswith("/desk") else "/desk")
    if current_user():
        return redirect("/desk")
    return _page("login.html", error="", email="")


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
        return redirect("/desk")
    if current_user():
        return redirect("/desk")
    return _page("signup.html", error="", name="", company="", email="")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/")


@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(u or {"id": None, "name": "Guest", "email": "", "company": "", "open": OPEN})


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "mode": _mode()})


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
    return {"id": d["id"], "name": d["name"], "template": d["template"], "tier": d.get("tier", "free"),
            "business_name": b.get("name", d["name"]), "created": d.get("created")}


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
    store.update_desk(did, **fields)
    return jsonify(_desk_public(store.desk(did)))


# ---------------------------------------------------------------------------- api: overview
@app.get("/api/config")
def api_config():
    desk = current_desk()
    if not desk:
        return jsonify({"mode": _mode(), "needs_desk": True, "protected": not OPEN})
    c = desk_configs(desk)
    return jsonify({
        "mode": c["mode"], "template": desk["template"], "tier": desk.get("tier", "free"), "business": c["business"],
        "desk": _desk_public(desk),
        "agents": [{"id": a["id"], "name": a["name"], "role": a.get("role", ""), "color": a.get("color", ""),
                    "tools": a.get("tools", []), "model": a.get("model", "") or "(provider default)"} for a in c["agents"]],
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


def _start_run(desk: dict[str, Any], task: str, mode: str, lead_id: int | None = None) -> str:
    configs = desk_configs(desk)
    dstore = store.for_desk(desk["id"])
    events: list[dict[str, Any]] = []
    orch: Orchestrator

    def emit(ev: Event):
        if ev.kind != "token":                     # deltas are live-only; the full text arrives as a `log` event
            events.append({"ts": ev.ts, "kind": ev.kind, "agent": ev.agent, "text": ev.text, "data": ev.data})
        if ev.kind == "error":
            _last_error[desk["id"]] = {"ts": ev.ts, "run_id": orch.run_id, "text": (ev.text or "")[:400]}
        _push_feed(orch.run_id, desk["id"], ev)

    orch = Orchestrator(configs, dstore, emit)
    holder: dict[str, Any] = {"events": events, "orch": orch, "task": task, "desk_id": desk["id"], "started": time.time()}

    def work():
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
        run_id = _start_run(desk, _lead_task(dstore.lead(lid)), d.get("mode", "auto"), lid)
    return jsonify({"id": lid, "run_id": run_id})


@app.post("/api/leads/<int:lid>/run")
def api_run_lead(lid):
    desk = need_desk()
    lead = store.lead(lid)
    if not lead or lead.get("desk_id") != desk["id"]:
        abort(404)
    mode = (request.get_json(silent=True) or {}).get("mode", "auto")
    return jsonify({"run_id": _start_run(desk, _lead_task(lead), mode, lid)})


@app.post("/api/runs")
def api_run_task():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    task = (d.get("task") or "").strip()
    if not task:
        return jsonify({"error": "task required"}), 400
    return jsonify({"run_id": _start_run(desk, task, d.get("mode", "auto"))})


@app.get("/api/runs")
def api_runs():
    rows = ds().runs(100)
    for r in rows:
        r["active"] = r["id"] in _runs and _runs[r["id"]]["thread"].is_alive()
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
                contact = dstore.upsert_contact(row["to"], {"stage": "Contacted", "next_action": "Follow up in 3 days if no reply"})
                for m in I.crm_sync(dstore.connectors(), contact):
                    dstore.add_event(row["run_id"], "tool", "owner", f"crm sync → {m}")
            I.notify(dstore.connectors(), f":outbox_tray: *Sent* — {row['kind']} → {row['to']}: {row['subject'] or (row['body'] or '')[:80]} {result}")
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:300]}"
            row = dstore.decide_action(aid, "failed", by=by, note=(note + " send failed: " + err).strip())
            dstore.add_event(row["run_id"], "error", "owner", f"{row['kind']} → {row['to']} failed: {err}")
    else:
        dstore.add_event(row["run_id"], "rejected", "owner", f"{row['kind']} to {row['to']} rejected — {d.get('note','')}")
    return jsonify(row)


# ---------------------------------------------------------------------------- api: crm / audit / report
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
    return {
        "ok": not any(a["level"] == "critical" for a in alerts), "window_h": window_h, "uptime_s": round(now - _BOOT_TS),
        "runs": {"total": len(runs), "by_status": by, "failure_rate": round(len(bad) / len(finished), 3) if finished else 0.0,
                 "p50_s": p(0.5), "p90_s": p(0.9), "active": len(live), "stalled": len(stalled), "zombies": len(zombies)},
        "tokens": tokens, "cost_gbp": round(tokens / 1e6 * 5 * 0.78, 2),
        "queue": {"pending": len(store.actions("pending", desk_id=desk_id)) if desk_id is not None else None,
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


def _watchdog_loop() -> None:
    """Every 15s: kill runs past RUN_MAX_S, mark orphaned DB rows failed, push critical alerts."""
    while True:
        try:
            now = time.time()
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
        ids.append({"id": lid, "run_id": _start_run(desk, _lead_task(dstore.lead(lid)), "auto", lid)})
        time.sleep(0.05)
    return jsonify(ids)


@app.post("/api/demo/reset")
def api_reset():
    desk = need_desk()
    if any(r["desk_id"] == desk["id"] and r["thread"].is_alive() for r in _runs.values()):
        return jsonify({"error": "runs in progress"}), 409
    store.for_desk(desk["id"]).reset()
    for rid in [k for k, v in _runs.items() if v["desk_id"] == desk["id"]]:
        _runs.pop(rid, None)
    return jsonify({"ok": True})


def _dispatch(desk: dict[str, Any], row: dict[str, Any]) -> str:
    """Perform an approved action for real when a connector exists; otherwise simulate and say so."""
    dstore = store.for_desk(desk["id"])
    kind = row["kind"]
    if kind in I.CHANNELS:
        conn = I.outbound_connector(dstore.connectors(), kind)
        if not conn:
            return f"[simulated {kind} — no {' / '.join(I.CHANNELS[kind])} connector; add one under Integrations]"
        return "[" + I.deliver(conn, kind, row["to"], row["subject"] or "", row["body"] or "") + "]"
    if kind == "api_call":
        conn = dstore.connector_by_name(row["to"])
        if not conn:
            return "[failed — connector missing]"
        spec = json.loads(row["body"] or "{}")
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
    c = store.add_connector(desk["id"], kind, name, d.get("config") or {}, bool(d.get("auto")))
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
}


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
    nxt = time.time() + (delay * 60 if delay else (every * 60 if every and not d.get("run_now") else 0))
    task = d.get("task") or ""
    if kind == "followups" and not task:
        task = json.dumps({"days": int(d.get("days") or 3)})
    if kind == "http_poll" and isinstance(task, dict):
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
    return jsonify({"ok": True})


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
        abort(Response(json.dumps({"error": "no_session"}), 404, mimetype="application/json"))
    return s


@app.post("/api/design/start")
def api_design_start():
    u = current_user()
    if not u and not OPEN:
        abort(401)
    d = request.get_json(silent=True) or {}
    tier = d.get("tier") if d.get("tier") in templates.TIERS else "free"
    s = DS.new_session(_mode(), tier)
    if u and u.get("company"):
        s.transcript[0]["text"] = s.transcript[0]["text"].replace("Tell me what your business does", f"Tell me what {u['company']} does")
    return jsonify(s.public())


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
    return jsonify({"blueprint": s.blueprint, "ready": s.ready})


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
    conf = DS.blueprint_to_desk(bp, tier)
    name = (d.get("name") or (bp.get("business") or {}).get("name") or "New desk").strip()
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
    return jsonify({"desk": _desk_public(desk), "connect": _connect_plan(desk, bp), "jobs": jobs})


def _connect_plan(desk: dict[str, Any], bp: dict[str, Any]) -> dict[str, Any]:
    have = store.connectors(desk["id"])
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for c in have:
        by_kind.setdefault(c["kind"], []).append(c)
    items = []
    for c in bp.get("connectors") or []:
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
    if email_:
        dstore.upsert_contact(email_, {"name": name, "company": d.get("company", ""), "email": email_, "phone": d.get("phone", ""), "stage": "New", "notes": "Inbound via webhook"})
    rid = _start_run(desk, _lead_task(dstore.lead(lid)), "auto", lid)
    return jsonify({"ok": True, "lead_id": lid, "run_id": rid})


def _inbound_message(desk: dict[str, Any], phone: str, name: str, text: str, source: str) -> str:
    """A WhatsApp / SMS message from a prospect: new lead + run (existing contact keeps its name/company)."""
    dstore = store.for_desk(desk["id"])
    phone = "+" + I._digits(phone) if phone else ""
    known = next((c for c in dstore.contacts(phone) if c.get("phone") == phone), None) if phone else None
    name = name or (known or {}).get("name") or (phone or "unknown")
    lid = dstore.add_lead(name, (known or {}).get("company", ""), (known or {}).get("email", "") or "", phone, source, text)
    key = (known or {}).get("email") or name
    dstore.upsert_contact(key, {"name": name, "phone": phone, "stage": (known or {}).get("stage") or "New",
                                "notes": f"Inbound via {source}: {text[:200]}"})
    task = _lead_task(dstore.lead(lid)) + f"\n\nThis arrived by {source}. Reply on the same channel (queue_action kind={'whatsapp' if 'whatsapp' in source else 'sms'}, to={phone}) — keep it short."
    return _start_run(desk, task, "auto", lid)


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
scheduler.start(store, _start_run, store.desk)
threading.Thread(target=_watchdog_loop, daemon=True, name="atlas-watchdog").start()


def main():
    port = int(os.environ.get("PORT", "8094"))
    print(f"Atlas Desk  mode={_mode()}  accounts={'off' if OPEN else 'on'}  http://localhost:{port}/desk")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
