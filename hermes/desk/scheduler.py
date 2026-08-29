"""Always-on automations for every desk: a small scheduler thread that runs due jobs.

Job kinds
  task         run a task through the desk (once, or every N minutes)
  inbox_watch  poll an IMAP connector; every unread email becomes a lead + run
  followups    contacts stuck at Contacted for N days with nothing pending → follow-up run
  http_poll    call an HTTP connector and hand the result to the desk as a task

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

TICK = 20.0
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
                try:
                    result = _run_job(store, job, start_run, desk_for)
                except Exception as exc:
                    result = f"ERROR {type(exc).__name__}: {str(exc)[:300]}"
                    traceback.print_exc()
                every = int(job.get("every_min") or 0)
                fields = {"last_run": time.time(), "last_result": result}
                if every > 0:
                    fields["next_run"] = time.time() + every * 60
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
    _thread = threading.Thread(target=_loop, args=(store, start_run, desk_for), daemon=True, name="hermes-scheduler")
    _thread.start()


def stop() -> None:
    _stop.set()
