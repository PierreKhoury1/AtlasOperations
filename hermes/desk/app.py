"""Hermes Desk portal (Flask).

  /            marketing site (hermes/site)
  /desk        client portal SPA
  /api/...     JSON API used by the portal

Run:  py -m hermes.desk            (PORT env, default 8094)
Env:  DESK_TEMPLATE=sales_desk     business-model template to run the desk on
      DESK_MODE=demo|live|auto     demo = scripted provider (no API key needed); auto = live if key present
      DESK_SECRET=...              session-cookie secret (auto-generated into data/secret.key if unset)
      DESK_OPEN=1                  skip accounts entirely (portal public) â€” local dev only
      ANTHROPIC_API_KEY=...        live mode
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import json

from flask import Flask, Response, abort, jsonify, make_response, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from .. import config as cfg
from .. import templates
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
_runs: dict[str, dict[str, Any]] = {}          # run_id -> {"events": [...], "thread":..., "orch":...}
_runs_lock = threading.Lock()
_feed: list[dict[str, Any]] = []               # global live feed (all runs, incl. token deltas) for /api/stream
_feed_lock = threading.Lock()
_feed_seq = 0
_FEED_MAX = 30000


def _push_feed(run_id: str, ev: Event):
    global _feed_seq
    with _feed_lock:
        _feed_seq += 1
        _feed.append({"seq": _feed_seq, "run_id": run_id, "ts": ev.ts, "kind": ev.kind,
                      "agent": ev.agent, "text": ev.text, "data": ev.data})
        if len(_feed) > _FEED_MAX:
            del _feed[: _FEED_MAX // 3]


def _feed_after(seq: int) -> list[dict[str, Any]]:
    with _feed_lock:
        if not _feed or _feed[-1]["seq"] <= seq:
            return []
        lo = max(0, len(_feed) - (_feed[-1]["seq"] - seq))   # seqs are contiguous, so index by offset
        return _feed[lo:]
TEMPLATE = os.environ.get("DESK_TEMPLATE", "sales_desk")


# ---------------------------------------------------------------------------- config / mode
def _mode() -> str:
    m = os.environ.get("DESK_MODE", "auto").lower()
    if m in ("demo", "live"):
        return m
    if os.environ.get("DESK_PROVIDER", "").strip():
        return "live"
    prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    key = cfg.resolve_api_key(prov["providers"].get("anthropic", {"type": "anthropic"}))
    return "live" if key else "demo"


def build_configs() -> dict[str, Any]:
    t = templates.get(TEMPLATE)
    mode = _mode()
    if mode == "demo":
        providers = {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": float(os.environ.get("DEMO_DELAY", "0.6"))}}}
        for a in t["agents"]:
            a["provider"] = "demo"
            a["model"] = ""
    else:
        providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
        for name, pc in cfg.DEFAULT_PROVIDERS["providers"].items():   # backfill presets added later
            providers.setdefault("providers", {}).setdefault(name, dict(pc))
        prov = os.environ.get("DESK_PROVIDER", "").strip()
        if prov:
            providers["default_provider"] = prov
            model = os.environ.get("DESK_MODEL", "").strip()
            if model:
                providers["providers"][prov]["default_model"] = model
            for a in t["agents"]:
                a["provider"] = prov
                a["model"] = ""
    return {
        "providers": providers,
        "orchestration": cfg.load("orchestration", cfg.DEFAULT_ORCHESTRATION),
        "business": t["business"], "agents": t["agents"], "workflows": t["workflows"],
        "ui": {}, "mode": mode,
    }


# ---------------------------------------------------------------------------- auth (accounts)
PUBLIC_API = {"/api/health", "/api/stats", "/api/me"}


def current_user() -> dict[str, Any] | None:
    uid = session.get("uid")
    return store.user(int(uid)) if uid else None


@app.before_request
def _guard():
    if OPEN:
        return None
    p = request.path
    if p.startswith("/api") and p not in PUBLIC_API:
        if not current_user():
            abort(401)
    elif p.startswith("/desk") and not p.startswith("/desk/static/") and p != "/desk/login":
        if not current_user():
            return redirect("/login?next=" + p)
    return None


def _esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


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
            err = "An account with that email already exists â€” log in instead."
        if err:
            if request.is_json:
                return jsonify({"error": err}), 400
            return _page("signup.html", error=err, name=name, company=company, email=email), 400
        u = store.add_user(email, name, company, generate_password_hash(pw))
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        store.add_event("", "signup", "system", f"{name} ({email}) created an account")
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
@app.route("/")
def site_index():
    return send_from_directory(SITE_DIR, "index.html")


@app.route("/site/<path:path>")
def site_files(path):
    return send_from_directory(SITE_DIR, path)


@app.route("/<path:path>")
def site_root_files(path):
    if (SITE_DIR / path).is_file():
        return send_from_directory(SITE_DIR, path)
    abort(404)


@app.route("/desk")
@app.route("/desk/")
def desk_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/desk/static/<path:path>")
def desk_static(path):
    return send_from_directory(STATIC_DIR, path)


# ---------------------------------------------------------------------------- api: overview
@app.get("/api/config")
def api_config():
    c = build_configs()
    return jsonify({
        "mode": c["mode"], "template": TEMPLATE, "business": c["business"],
        "agents": [{"id": a["id"], "name": a["name"], "role": a.get("role", ""), "color": a.get("color", ""),
                    "tools": a.get("tools", [])} for a in c["agents"]],
        "workflows": [{"id": w["id"], "name": w["name"], "description": w.get("description", "")} for w in c["workflows"]],
        "protected": not OPEN,
    })


@app.get("/api/stats")
def api_stats():
    s = store.stats()
    s["active_runs"] = sum(1 for r in _runs.values() if r["thread"].is_alive())
    return jsonify(s)


# ---------------------------------------------------------------------------- api: leads + runs
def _lead_task(lead: dict[str, Any]) -> str:
    return (f"New inbound lead â€” handle end to end.\n"
            f"Name: {lead['name']}\nCompany: {lead['company'] or '(individual)'}\nEmail: {lead['email']}\n"
            f"Phone: {lead['phone'] or '-'}\nSource: {lead['source']}\n\nEnquiry:\n{lead['notes']}")


def _start_run(task: str, mode: str, lead_id: int | None = None) -> str:
    configs = build_configs()
    events: list[dict[str, Any]] = []

    orch: Orchestrator

    def emit(ev: Event):
        if ev.kind != "token":                     # deltas are live-only; the full text arrives as a `log` event
            events.append({"ts": ev.ts, "kind": ev.kind, "agent": ev.agent, "text": ev.text, "data": ev.data})
        _push_feed(orch.run_id, ev)

    orch = Orchestrator(configs, store, emit)
    holder: dict[str, Any] = {"events": events, "orch": orch, "task": task}

    def work():
        res = orch.run(task, mode)
        if lead_id is not None:
            store.set_lead(lead_id, status=("processed" if res.status == "done" else res.status), run_id=res.run_id)

    th = threading.Thread(target=work, daemon=True)
    holder["thread"] = th
    th.start()
    # wait briefly for run_id to exist
    for _ in range(50):
        if orch.run_id:
            break
        time.sleep(0.02)
    with _runs_lock:
        _runs[orch.run_id] = holder
    if lead_id is not None:
        store.set_lead(lead_id, status="running", run_id=orch.run_id)
    return orch.run_id


@app.get("/api/leads")
def api_leads():
    return jsonify(store.leads())


@app.post("/api/leads")
def api_add_lead():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip()
    if not name or not email:
        return jsonify({"error": "name and email required"}), 400
    lid = store.add_lead(name, (d.get("company") or "").strip(), email, (d.get("phone") or "").strip(),
                         (d.get("source") or "web form").strip(), (d.get("notes") or "").strip())
    store.upsert_contact(email, {"name": name, "company": d.get("company", ""), "email": email,
                                 "phone": d.get("phone", ""), "stage": "New", "notes": "Inbound lead"})
    run_id = None
    if d.get("run", True):
        run_id = _start_run(_lead_task(store.lead(lid)), d.get("mode", "auto"), lid)
    return jsonify({"id": lid, "run_id": run_id})


@app.post("/api/leads/<int:lid>/run")
def api_run_lead(lid):
    lead = store.lead(lid)
    if not lead:
        abort(404)
    mode = (request.get_json(silent=True) or {}).get("mode", "auto")
    return jsonify({"run_id": _start_run(_lead_task(lead), mode, lid)})


@app.post("/api/runs")
def api_run_task():
    d = request.get_json(force=True) or {}
    task = (d.get("task") or "").strip()
    if not task:
        return jsonify({"error": "task required"}), 400
    return jsonify({"run_id": _start_run(task, d.get("mode", "auto"))})


@app.get("/api/runs")
def api_runs():
    rows = store.runs(100)
    for r in rows:
        r["active"] = r["id"] in _runs and _runs[r["id"]]["thread"].is_alive()
    return jsonify(rows)


@app.get("/api/runs/<run_id>")
def api_run(run_id):
    row = next((r for r in store.runs(500) if r["id"] == run_id), None)
    live = _runs.get(run_id)
    if not row and live:   # thread started but DB row not written yet
        row = {"id": run_id, "created": time.time(), "task": "", "mode": "", "status": "running",
               "summary": "", "tokens_in": 0, "tokens_out": 0, "run_dir": ""}
    if not row:
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
    """Server-sent events: every orchestrator event from every run, including token deltas.
    ?since=<seq> resumes after a sequence number; ?since=now (default) starts from the present."""
    arg = request.args.get("since", "now")
    run_filter = request.args.get("run", "")
    with _feed_lock:
        start = _feed_seq if arg == "now" else max(0, min(int(arg or 0), _feed_seq))

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
                    if run_filter and e["run_id"] != run_filter:
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
    """Runs that are executing right now, with the events so far (no token deltas) for a late-joining viewer."""
    out = []
    with _runs_lock:
        items = list(_runs.items())
    for rid, h in items:
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
    live = _runs.get(run_id)
    if live:
        live["orch"].cancel()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: approvals
@app.get("/api/actions")
def api_actions():
    return jsonify(store.actions(request.args.get("status", "")))


@app.post("/api/actions/<int:aid>/decide")
def api_decide(aid):
    d = request.get_json(force=True) or {}
    status = d.get("status")
    if status not in ("approved", "rejected"):
        return jsonify({"error": "status must be approved|rejected"}), 400
    row = store.decide_action(aid, status, by=d.get("by", "owner"), note=d.get("note", ""),
                              body=d.get("body"), subject=d.get("subject"))
    if not row:
        abort(404)
    if status == "approved":
        # Simulated dispatch. Wire a real sender (SMTP / WhatsApp API) here when a client goes live.
        row = store.decide_action(aid, "sent", by=d.get("by", "owner"), note=(d.get("note", "") + " [simulated send]").strip())
        store.add_event(row["run_id"], "sent", "owner", f"{row['kind']} sent to {row['to']} â€” {row['subject']}")
        if "@" in (row["to"] or ""):
            store.upsert_contact(row["to"], {"stage": "Contacted", "next_action": "Follow up in 3 days if no reply"})
    else:
        store.add_event(row["run_id"], "rejected", "owner", f"{row['kind']} to {row['to']} rejected â€” {d.get('note','')}")
    return jsonify(row)


# ---------------------------------------------------------------------------- api: crm / audit / report
@app.get("/api/contacts")
def api_contacts():
    return jsonify(store.contacts(request.args.get("q", "")))


@app.post("/api/contacts")
def api_upsert_contact():
    d = request.get_json(force=True) or {}
    contact = d.get("contact") or d.get("email") or d.get("name")
    if not contact:
        return jsonify({"error": "contact required"}), 400
    return jsonify(store.upsert_contact(contact, d.get("fields") or d))


@app.get("/api/audit")
def api_audit():
    return jsonify(store.all_events(400))


@app.get("/api/report")
def api_report():
    s = store.stats()
    acts = store.actions()
    decided = [a for a in acts if a["status"] in ("sent", "approved", "rejected")]
    approval_rate = round(100 * sum(1 for a in decided if a["status"] != "rejected") / len(decided)) if decided else None
    runs = store.runs(500)
    done = [r for r in runs if r["status"] == "done"]
    mins_saved = len(done) * 35  # assumption: ~35 min of research + drafting + CRM per lead
    return jsonify({
        "period": time.strftime("%B %Y"),
        "leads_handled": s["leads"], "runs_done": len(done), "runs_failed": len([r for r in runs if r["status"] == "error"]),
        "actions_queued": len(acts), "approval_rate": approval_rate,
        "sent": sum(1 for a in acts if a["status"] == "sent"), "rejected": s["rejected"], "pending": s["pending"],
        "contacts": s["contacts"], "qualified": s["qualified"],
        "hours_saved": round(mins_saved / 60, 1), "assumption": "35 min saved per processed lead (research + draft + CRM)",
        "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"],
        "est_model_cost_gbp": round((s["tokens_in"] * 5 + s["tokens_out"] * 25) / 1_000_000 * 0.78, 2),
        "changes": ["Outreach tone tightened after 2 owner edits", "Added 'no written valuation' rule to QA agent"],
    })


# ---------------------------------------------------------------------------- demo helpers
SAMPLE_LEADS = [
    {"name": "Priya Raman", "company": "", "email": "priya.raman@example.com", "phone": "+44 7700 900101", "source": "website form",
     "notes": "Landlord with 3 flats in SE17, current agent underperforming. Wants a lettings management quote and a valuation for one flat."},
    {"name": "Tom Okafor", "company": "Okafor Property Ltd", "email": "tom@okaforproperty.example.com", "phone": "+44 7700 900102", "source": "Rightmove enquiry",
     "notes": "Thinking of selling a 2-bed in Walworth in the next 3 months. Asked for a rough price range."},
    {"name": "Hannah Weiss", "company": "", "email": "hannah.weiss@example.com", "phone": "", "source": "referral",
     "notes": "Relocating to London in October, needs a 1-bed rental near Elephant & Castle, budget ~Â£1,600."},
]


@app.post("/api/demo/seed")
def api_seed():
    ids = []
    for L in SAMPLE_LEADS:
        lid = store.add_lead(L["name"], L["company"], L["email"], L["phone"], L["source"], L["notes"])
        store.upsert_contact(L["email"], {"name": L["name"], "company": L["company"], "email": L["email"], "phone": L["phone"], "stage": "New", "notes": "Inbound lead"})
        ids.append({"id": lid, "run_id": _start_run(_lead_task(store.lead(lid)), "auto", lid)})
        time.sleep(0.05)
    return jsonify(ids)


@app.post("/api/demo/reset")
def api_reset():
    if any(r["thread"].is_alive() for r in _runs.values()):
        return jsonify({"error": "runs in progress"}), 409
    with store._lock:
        for t in ("events", "runs", "actions", "leads", "contacts"):
            store._conn.execute(f"DELETE FROM {t}")
        store._conn.commit()
    _runs.clear()
    return jsonify({"ok": True})


def main():
    port = int(os.environ.get("PORT", "8094"))
    print(f"Hermes Desk  mode={_mode()}  template={TEMPLATE}  http://localhost:{port}/desk")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
