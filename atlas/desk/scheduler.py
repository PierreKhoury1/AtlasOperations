"""Always-on automations for every desk: a small scheduler thread that runs due jobs.

Job kinds
  task         run a task through the desk (once, or every N minutes)
  inbox_watch  poll an IMAP connector; every unread email becomes a lead + run
  followups    contacts stuck at Contacted for N days with nothing pending → follow-up run
  http_poll    call an HTTP connector and hand the result to the desk as a task
  camera_watch grab a frame from each camera connector, detect, log, and wake the desk when the rule fires

`start(store, start_run, desk_for)` is called once by the Flask app. Everything the jobs do goes
through the normal orchestrator, so approvals, policy and the audit log apply exactly as for a lead.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any, Callable

from .. import integrations as I
from .. import vision as V

TICK = 20.0
LIVE = lambda: True                                       # replaced by the app: is this desk on live models?
_last_frame: dict[tuple[int, str], bytes] = {}          # (desk_id, camera) -> last raw frame (motion baseline)
_last_seen: dict[tuple[int, str], dict[str, Any]] = {}  # (desk_id, camera) -> last analysis (portal "live" tile)


def last_seen(desk_id: int, camera: str) -> dict[str, Any] | None:
    return _last_seen.get((desk_id, camera))


def camera_tick(store, desk: dict[str, Any], conn: dict[str, Any], start_run: Callable, live: bool,
                question: str = "", force: bool = False) -> dict[str, Any]:
    """One camera pass: grab → motion → detect → rule → event → (maybe) run. Returns the analysis + event + run id."""
    ds = store.for_desk(desk["id"])
    key = (desk["id"], conn["name"])
    cfg = conn["config"]
    rule = V.rule_config(cfg)
    prev_jpeg = _last_frame.get(key)
    res = V.analyse(str(cfg.get("source", "")), cfg, prev_jpeg, live_vlm=live, question="")
    _last_frame[key] = res["jpeg"]
    prev_ev = ds.last_vision_event(conn["name"])
    last_alert = ds.last_vision_event(conn["name"], triggered_only=True)
    triggered, reason = V.evaluate(cfg, res["detections"], (prev_ev or {}).get("counts"), (last_alert or {}).get("ts"))
    if force:
        triggered, reason = True, "manual trigger"
    changed = (prev_ev or {}).get("counts") != res["counts"] or res["motion"] >= max(rule["motion_min"], 0.08)
    answer = ""
    q = question or (rule["question"] if triggered else "")
    if q and live and V.vlm_ready():
        try:
            answer = V.describe(res["jpeg"], q, model=str(cfg.get("vlm_model") or ""), context=str(cfg.get("notes") or ""))
        except Exception as exc:
            answer = f"(vision model unavailable: {str(exc)[:120]})"
    elif q and not live:
        answer = "demo mode: " + V.counts_text(res["counts"]) + " in frame"
    res["answer"] = answer
    event = None
    if triggered or changed or prev_ev is None or question:
        snap = V.save_snapshot(desk["id"], conn["name"], res["annotated"])
        event = ds.add_vision_event(conn["name"], res["counts"], motion=res["motion"], backend=res["backend"], reason=reason,
                                    question=q, answer=answer, snapshot=snap, triggered=triggered)
    rid = ""
    if triggered and event:
        when = time.strftime("%A %d %B %H:%M")
        task = (rule["task"] or "Assess this camera alert, log it, and tell the right person only if it matters.") + "\n\n"
        task += (f"CAMERA ALERT — {conn['name']} at {when}\n"
                 f"Seen: {V.counts_text(res['counts'])} (detector: {res['backend']}); motion {res['motion']:.2f}; rule: {reason}.\n"
                 + (f"Watching for: {', '.join(rule['labels'])}" + (f" during {rule['hours']}" if rule["hours"] else "") + ".\n")
                 + (f"Analyst answer to '{q}': {answer}\n" if answer else "")
                 + (f"Camera notes: {cfg.get('notes')}\n" if cfg.get("notes") else "")
                 + f"Event id: {event['id']}. Snapshot: {event['snapshot']}. Use camera_look for a fresh frame and camera_events for history.")
        rid = start_run(desk, task, "auto")
        ds.set_vision_run(event["id"], rid)
    seen = {"ts": time.time(), "counts": res["counts"], "motion": res["motion"], "backend": res["backend"], "reason": reason,
            "triggered": triggered, "answer": answer, "event_id": (event or {}).get("id"), "run_id": rid, "size": res["size"],
            "detections": res["detections"], "detector_error": res["detector_error"]}
    _last_seen[key] = {**seen, "annotated": res["annotated"]}
    return {**seen, "annotated": res["annotated"]}
_thread: threading.Thread | None = None
_stop = threading.Event()


def _run_job(store, job: dict[str, Any], start_run: Callable, desk_for: Callable) -> str:
    desk = desk_for(job["desk_id"])
    if not desk:
        return "desk missing"
    ds = store.for_desk(desk["id"])
    kind = job["kind"]
    if kind == "task":
        rid = start_run(desk, job["task"], "auto")
        return f"run {rid}"
    if kind == "inbox_watch":
        conn = next((c for c in ds.connectors() if c["kind"] == "imap"), None)
        if not conn:
            return "no IMAP connector"
        mails = I.fetch_unseen(conn["config"], limit=5)
        started = []
        for m in mails:
            lid = ds.add_lead(m["from_name"], "", m["from_email"], "", "email",
                              f"Subject: {m['subject']}\n\n{m['body']}")
            ds.upsert_contact(m["from_email"], {"name": m["from_name"], "email": m["from_email"], "stage": "New", "notes": "Inbound email"})
            task = (f"New inbound email — handle end to end.\nName: {m['from_name']}\nEmail: {m['from_email']}\n"
                    f"Source: email\nSubject: {m['subject']}\n\nMessage:\n{m['body']}")
            rid = start_run(desk, task, "auto", lid)
            ds.set_lead(lid, status="running", run_id=rid)
            started.append(rid)
        return f"{len(mails)} new email(s)" + (f", runs {', '.join(started)}" if started else "")
    if kind == "followups":
        days = 3
        try:
            days = int(json.loads(job["task"] or "{}").get("days", 3))
        except Exception:
            pass
        cutoff = time.time() - days * 86400
        pending_to = {a["to"] for a in ds.actions("pending")}
        due = [c for c in ds.contacts() if c["stage"] == "Contacted" and (c.get("updated") or 0) < cutoff
               and c.get("email") and c["email"] not in pending_to]
        started = []
        for c in due[:5]:
            task = (f"Follow-up needed. {c['name']} ({c['email']}, {c.get('company') or 'individual'}) was contacted "
                    f"{days}+ days ago and has not replied. Notes: {c.get('notes','')}. Next action on file: {c.get('next_action','')}.\n"
                    f"Draft a short, friendly follow-up (new angle, one clear ask) and queue it for approval; update the CRM next action.")
            started.append(start_run(desk, task, "auto"))
            ds.upsert_contact(c["email"], {"next_action": "Follow-up drafted (awaiting approval)"})
        return f"{len(due)} due, {len(started)} follow-up run(s) started"
    if kind == "camera_watch":
        try:
            spec = json.loads(job["task"] or "{}") if (job["task"] or "").strip().startswith("{") else {"connector": job["task"] or ""}
        except Exception:
            spec = {}
        cams = [c for c in ds.connectors() if c["kind"] == "camera"]
        if spec.get("connector"):
            cams = [c for c in cams if c["name"] == spec["connector"]]
        if not cams:
            return "no camera connector"
        live = bool(LIVE())
        out = []
        for c in cams:
            try:
                r = camera_tick(store, desk, c, start_run, live)
                out.append(f"{c['name']}: {V.counts_text(r['counts'])}" + (f" → run {r['run_id']}" if r["run_id"] else ""))
            except Exception as exc:
                out.append(f"{c['name']}: ERROR {str(exc)[:120]}")
                ds.add_vision_event(c["name"], {}, reason=f"grab failed: {str(exc)[:160]}", backend="error")
        return "; ".join(out)
    if kind == "http_poll":
        try:
            spec = json.loads(job["task"] or "{}")
        except Exception:
            return "task must be JSON {connector, path, prompt}"
        conn = ds.connector_by_name(spec.get("connector", ""))
        if not conn:
            return f"connector {spec.get('connector')!r} not found"
        res = I.http_call(conn["config"], spec.get("method", "GET"), spec.get("path", ""), spec.get("params"))
        payload = json.dumps(res.get("json", res.get("text", "")), ensure_ascii=False)[:6000]
        rid = start_run(desk, f"{spec.get('prompt') or 'Review this API result and act on it.'}\n\nAPI result from {conn['name']}:\n{payload}", "auto")
        return f"HTTP {res['status']} → run {rid}"
    return f"unknown job kind {kind}"


def _loop(store, start_run, desk_for):
    while not _stop.is_set():
        try:
            now = time.time()
            for job in store.due_jobs(now):
                status = "ok"
                try:
                    result = _run_job(store, job, start_run, desk_for)
                    if result.startswith(("no IMAP", "desk missing", "connector ", "task must", "unknown job", "no camera")):
                        status = "skipped"
                except Exception as exc:
                    result = f"ERROR {type(exc).__name__}: {str(exc)[:300]}"
                    status = "error"
                    traceback.print_exc()
                every = int(job.get("every_min") or 0)
                fields = {"last_run": time.time(), "last_result": result, "last_status": status}
                every_s = 0
                if job["kind"] == "camera_watch":
                    try:
                        every_s = int((json.loads(job["task"] or "{}") or {}).get("every_s") or 0)
                    except Exception:
                        every_s = 0
                if every_s > 0 and status != "skipped":
                    fields["next_run"] = time.time() + max(every_s, int(TICK))
                elif every > 0:
                    # a skipped job (nothing to poll) backs off to hourly instead of hammering every tick
                    fields["next_run"] = time.time() + (max(every, 60) if status == "skipped" else every) * 60
                else:
                    fields["enabled"] = 0
                store.update_job(job["id"], **fields)
        except Exception:
            traceback.print_exc()
        _stop.wait(TICK)


def start(store, start_run: Callable, desk_for: Callable) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(store, start_run, desk_for), daemon=True, name="atlas-scheduler")
    _thread.start()


def stop() -> None:
    _stop.set()
