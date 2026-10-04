"""Cases: work that lasts longer than one run.

A run is one job ("answer this lead"). Real operations are processes: answer the lead, wait two days, follow up, book
the visit, close. A case is that process, held by the desk:

  * a playbook (enquiry, incident, task, or the desk's own) lists the states and what moves between them
  * work states hand Atlas a step to do (one run each); wait states wait for a reply, the owner, or the clock
  * events move the case: an approved message was sent, the owner rejected a draft, the customer replied, a camera
    fired again, a timer ran out
  * every state can carry a deadline (SLA); a missed deadline is flagged and the owner is told
  * everything lands on the case timeline (atlas/records.py), next to the records the case is about

The engine is deterministic: models do the steps, the playbook decides what happens next unless Atlas (or the owner)
moves the case explicitly. Ticks come from the desk scheduler; runs start through START_RUN (the portal's _start_run).
"""
from __future__ import annotations

import json
import re
import time
import traceback
from typing import Any, Callable

from . import records as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, type TEXT, title TEXT DEFAULT '', state TEXT DEFAULT '',
  priority TEXT DEFAULT 'normal', record_id INTEGER DEFAULT 0, waiting_for TEXT DEFAULT '', wake_at REAL, due_at REAL,
  opened REAL, updated REAL, state_since REAL, closed_at REAL, outcome TEXT DEFAULT '', source TEXT DEFAULT '',
  data TEXT DEFAULT '{}', active_run TEXT DEFAULT '', runs INTEGER DEFAULT 0, breached INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_cases_desk ON cases(desk_id, closed_at);
CREATE INDEX IF NOT EXISTS ix_cases_wake ON cases(closed_at, wake_at);
CREATE TABLE IF NOT EXISTS case_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id INTEGER, case_id INTEGER, record_id INTEGER, role TEXT DEFAULT 'subject'
);
CREATE INDEX IF NOT EXISTS ix_case_records ON case_records(case_id);
CREATE INDEX IF NOT EXISTS ix_case_records_rec ON case_records(record_id);
"""
TABLES = ("cases", "case_records")

PRIORITIES = ("low", "normal", "high", "urgent")
MAX_RUNS_PER_DAY = int(12)          # a case that keeps waking itself up goes to the owner instead of burning tokens
RETRY_AFTER_S = 15 * 60             # a run the desk refused (spend cap, no model key) is retried this much later

START_RUN: Callable[..., str] | None = None        # set by the portal: _start_run(desk, task, mode, lead_id=None, case_id=None)
NOTIFY: Callable[[int, str], None] | None = None   # set by the portal: tell the owner (Slack etc.) about a missed deadline
RUN_STATUS: Callable[[str], str] | None = None     # set by the portal: live status of a run id ("running" / "done" / ...)

_W = ("Messages go through queue_action and are linked to this case automatically; nothing is sent without the owner's "
      "approval.")

PLAYBOOKS: dict[str, dict[str, Any]] = {
    "enquiry": {
        "label": "Enquiry",
        "description": "Someone asked about the business: answer, follow up if they go quiet, book or close.",
        "initial": "new", "max_rejections": 2,
        "states": [
            {"id": "new", "label": "New", "kind": "work", "sla": "2h",
             "task": "A new enquiry. Check the records for who this is and any history, work out exactly what they want, "
                     "and queue the reply for approval. " + _W,
             "on": {"sent": "waiting_reply", "rejected": "revise", "booked": "booked"}, "after_run": "needs_owner"},
            {"id": "revise", "label": "Redraft", "kind": "work", "sla": "2h",
             "task": "The owner rejected the last draft - their note is in the case history. Rewrite it to fix exactly "
                     "that, nothing else, and queue it again.",
             "on": {"sent": "waiting_reply", "rejected": "revise", "booked": "booked"}, "after_run": "needs_owner"},
            {"id": "waiting_reply", "label": "Waiting for their reply", "kind": "wait", "wait": "reply", "timeout": "48h",
             "on_timeout": "follow_up", "on": {"reply": "replied", "booked": "booked"}},
            {"id": "replied", "label": "They replied", "kind": "work", "sla": "2h",
             "task": "They replied - their message is the newest entry in the case history. Answer it properly (book, "
                     "quote within the business's rules, or answer the question) and queue the reply.",
             "on": {"sent": "waiting_reply", "rejected": "revise", "booked": "booked"}, "after_run": "needs_owner"},
            {"id": "follow_up", "label": "Follow up", "kind": "work", "sla": "4h",
             "task": "No reply for two days. Draft ONE short, friendly follow-up with a new angle and a single clear ask, "
                     "and queue it. Do not repeat the first message.",
             "on": {"sent": "last_call", "rejected": "needs_owner", "booked": "booked"}, "after_run": "needs_owner"},
            {"id": "last_call", "label": "Last wait", "kind": "wait", "wait": "reply", "timeout": "72h",
             "on_timeout": "closed", "timeout_outcome": "no reply", "on": {"reply": "replied", "booked": "booked"}},
            {"id": "needs_owner", "label": "Needs you", "kind": "wait", "wait": "owner", "sla": "24h",
             "on": {"reply": "replied", "sent": "waiting_reply", "booked": "booked"}},
            {"id": "booked", "label": "Booked", "kind": "done", "outcome": "booked"},
            {"id": "closed", "label": "Closed", "kind": "done"},
        ],
    },
    "incident": {
        "label": "Incident",
        "description": "Something happened on camera or on site: assess, tell the right person, watch until it is over.",
        "initial": "open",
        "states": [
            {"id": "open", "label": "Assess", "kind": "work", "sla": "15m",
             "task": "Assess this incident from the cameras: what happened, how serious it is, who must know. Record the "
                     "finding with case_update(note=...). Queue a message only if it matters. " + _W,
             "on": {"sent": "monitoring", "rejected": "monitoring"}, "after_run": "monitoring"},
            {"id": "monitoring", "label": "Monitoring", "kind": "wait", "wait": "time", "timeout": "30m",
             "on_timeout": "review", "on": {"alert": "open"}},
            {"id": "review", "label": "Re-check", "kind": "work", "sla": "15m",
             "task": "Thirty minutes on. Look at the camera again (camera_look). If it is over, close the case with "
                     "case_update(state='resolved', outcome=...). If not, case_update(state='escalated') and queue a "
                     "message to the owner.",
             "on": {"sent": "escalated", "alert": "open"}, "after_run": "resolved"},
            {"id": "escalated", "label": "With the owner", "kind": "wait", "wait": "owner", "sla": "1h",
             "on": {"alert": "open"}},
            {"id": "resolved", "label": "Resolved", "kind": "done", "outcome": "resolved"},
        ],
    },
    "task": {
        "label": "Task",
        "description": "A piece of work for the desk that may need several steps or the owner's input.",
        "initial": "todo",
        "states": [
            {"id": "todo", "label": "To do", "kind": "work", "sla": "1d",
             "task": "Do this task. If it needs the owner, say exactly what you need with case_update(note=...) and "
                     "move it to state 'waiting'. When it is finished, case_update(state='done', outcome=...).",
             "on": {"sent": "done", "rejected": "todo"}, "after_run": "waiting"},
            {"id": "waiting", "label": "Waiting on you", "kind": "wait", "wait": "owner", "sla": "2d", "on": {"reply": "todo"}},
            {"id": "done", "label": "Done", "kind": "done", "outcome": "done"},
        ],
    },
}

KINDS = ("work", "wait", "done")
WAITS = ("reply", "owner", "time", "approval")


# ---------------------------------------------------------------------------------------------- helpers
def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def duration(s: Any) -> float:
    """'15m' / '2h' / '3d' / 90 (seconds) -> seconds. 0 when missing or unreadable."""
    if s in (None, "", 0):
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", str(s).lower())
    if not m:
        return 0.0
    n, u = float(m.group(1)), m.group(2) or "h"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[u]


def settings(desk: dict[str, Any] | None) -> dict[str, Any]:
    ops = ((desk or {}).get("config") or {}).get("ops") or {}
    return {"cases": ops.get("cases", True) is not False,
            "lead_cases": ops.get("lead_cases", True) is not False,
            "camera_cases": ops.get("camera_cases", True) is not False}


def enabled(desk: dict[str, Any] | None, what: str = "cases") -> bool:
    s = settings(desk)
    return bool(s["cases"] and s.get(what, True))


def playbooks(desk: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Built-in playbooks + the desk's own (desk config ops.playbooks, same shape; a same-id one replaces the built-in)."""
    out = dict(PLAYBOOKS)
    for pid, pb in ((((desk or {}).get("config") or {}).get("ops") or {}).get("playbooks") or {}).items():
        ok, _errs = validate_playbook(pb)
        if ok:
            out[R.slug(pid)] = pb
    return out


def validate_playbook(pb: Any) -> tuple[bool, list[str]]:
    errs: list[str] = []
    if not isinstance(pb, dict) or not isinstance(pb.get("states"), list) or not pb["states"]:
        return False, ["a playbook needs a non-empty list of states"]
    ids = [s.get("id") for s in pb["states"] if isinstance(s, dict)]
    if len(ids) != len(set(ids)) or not all(ids):
        errs.append("state ids must be unique and non-empty")
    for s in pb["states"]:
        if not isinstance(s, dict):
            errs.append("every state must be an object"); continue
        if s.get("kind") not in KINDS:
            errs.append(f"state {s.get('id')}: kind must be one of {KINDS}")
        targets = list((s.get("on") or {}).values()) + [s.get("on_timeout"), s.get("after_run")]
        for t in targets:
            if t and t not in ids:
                errs.append(f"state {s.get('id')}: unknown target state {t!r}")
        if s.get("kind") == "wait" and s.get("wait") not in WAITS:
            errs.append(f"state {s.get('id')}: wait must be one of {WAITS}")
    if pb.get("initial") and pb["initial"] not in ids:
        errs.append(f"initial state {pb['initial']!r} is not a state")
    return not errs, errs


def _pb(desk, ctype: str) -> dict[str, Any]:
    return playbooks(desk).get(ctype) or PLAYBOOKS["task"]


def _state(pb: dict[str, Any], sid: str) -> dict[str, Any]:
    return next((s for s in pb["states"] if s["id"] == sid), pb["states"][0])


def _decode(c: dict[str, Any]) -> dict[str, Any]:
    c = dict(c)
    try:
        c["data"] = json.loads(c.get("data") or "{}")
    except (TypeError, json.JSONDecodeError):
        c["data"] = {}
    return c


def _desk(store, desk_id: int) -> dict[str, Any] | None:
    s, _ = R._base(store)
    return s.desk(desk_id)


# ---------------------------------------------------------------------------------------------- reads
def get(store, cid: int) -> dict[str, Any] | None:
    s, did = R._base(store)
    rows = _rows(s._conn.execute("SELECT * FROM cases WHERE id=?", (int(cid),)))
    if not rows or (did is not None and rows[0]["desk_id"] != did):
        return None
    return _decode(rows[0])


def list_cases(store, desk_id: int | None = None, open_only: bool = False, ctype: str = "", state: str = "",
               record_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    s, did = R._base(store)
    did = did if did is not None else desk_id
    where, args = ["desk_id=?"], [did]
    if open_only:
        where.append("closed_at IS NULL")
    if ctype:
        where.append("type=?"); args.append(ctype)
    if state:
        where.append("state=?"); args.append(state)
    if record_id:
        where.append("(record_id=? OR id IN (SELECT case_id FROM case_records WHERE record_id=?))"); args += [record_id, record_id]
    rows = _rows(s._conn.execute(f"SELECT * FROM cases WHERE {' AND '.join(where)} ORDER BY updated DESC LIMIT ?", (*args, limit)))
    return [_decode(r) for r in rows]


def for_record(store, rid: int) -> list[dict[str, Any]]:
    s, did = R._base(store)
    rec = R.get(store, rid)
    if not rec:
        return []
    return list_cases(s, rec["desk_id"], record_id=rid, limit=50)


def case_records(store, cid: int) -> list[dict[str, Any]]:
    s, _ = R._base(store)
    out = []
    for r in _rows(s._conn.execute("SELECT record_id, role FROM case_records WHERE case_id=? ORDER BY id", (cid,))):
        rec = R.get(s, r["record_id"])
        if rec:
            out.append({**rec, "role": r["role"]})
    return out


def pending_actions(store, cid: int) -> list[dict[str, Any]]:
    s, _ = R._base(store)
    return _rows(s._conn.execute("SELECT * FROM actions WHERE case_id=? AND status='pending' ORDER BY created", (cid,)))


def case_actions(store, cid: int) -> list[dict[str, Any]]:
    s, _ = R._base(store)
    return _rows(s._conn.execute("SELECT * FROM actions WHERE case_id=? ORDER BY created DESC", (cid,)))


def lane(c: dict[str, Any]) -> str:
    """Board column: atlas (working) / you (owner or approval) / customer (waiting on a reply) / scheduled / closed."""
    if c.get("closed_at"):
        return "closed"
    w = c.get("waiting_for") or ""
    if c.get("active_run") or w == "atlas":
        return "atlas"
    if w in ("owner", "approval"):
        return "you"
    if w == "reply":
        return "customer"
    return "scheduled"


def public(store, c: dict[str, Any], desk: dict[str, Any] | None = None, detail: bool = False) -> dict[str, Any]:
    """API shape: the row plus labels, lane, clocks and the subject record."""
    desk = desk or _desk(store, c["desk_id"])
    pb = _pb(desk, c["type"])
    st = _state(pb, c["state"])
    now = time.time()
    subj = R.get(store, c["record_id"]) if c.get("record_id") else None
    out = {k: v for k, v in c.items() if k != "data"}
    out.update({
        "type_label": pb.get("label", c["type"]), "state_label": st.get("label", c["state"]), "state_kind": st.get("kind"),
        "lane": lane(c), "due_in_s": round(c["due_at"] - now) if c.get("due_at") and not c.get("closed_at") else None,
        "wake_in_s": round(c["wake_at"] - now) if c.get("wake_at") and not c.get("closed_at") else None,
        "subject": {"id": subj["id"], "type": subj["type"], "title": subj["title"]} if subj else None,
        "rejections": int(c["data"].get("rejections") or 0), "brief": (c["data"].get("brief") or "")[:4000 if detail else 240],
        "facts": c["data"].get("facts") or [],
    })
    if detail:
        out["states"] = [{"id": s["id"], "label": s.get("label", s["id"]), "kind": s.get("kind")} for s in pb["states"]]
        out["step"] = st.get("task") or ""
        out["next"] = next_moves(pb, st)
        out["records"] = [{"id": r["id"], "type": r["type"], "title": r["title"], "role": r["role"], "props": r["props"]}
                          for r in case_records(store, c["id"])]
        out["timeline"] = R.timeline_for(store, case_ids=[c["id"]], limit=200)
        out["actions"] = case_actions(store, c["id"])
    return out


EVENT_LABELS = {"sent": "message sent", "rejected": "draft rejected", "reply": "they reply", "booked": "booking confirmed",
                "alert": "camera fires again"}


def next_moves(pb: dict[str, Any], st: dict[str, Any]) -> list[dict[str, str]]:
    """What moves this case out of its current state, as data (the Operations page shows this instead of prose)."""
    label = {s["id"]: s.get("label", s["id"]) for s in pb["states"]}
    out = [{"when": EVENT_LABELS.get(ev, ev), "to": to, "to_label": label.get(to, to)} for ev, to in (st.get("on") or {}).items()]
    if st.get("kind") == "wait" and st.get("on_timeout"):
        out.append({"when": f"no change in {st.get('timeout')}", "to": st["on_timeout"], "to_label": label.get(st["on_timeout"], st["on_timeout"])})
    if st.get("kind") == "work" and st.get("after_run"):
        out.append({"when": "step done, nothing sent", "to": st["after_run"], "to_label": label.get(st["after_run"], st["after_run"])})
    return out


def counts(store, desk_id: int) -> dict[str, int]:
    out = {"open": 0, "atlas": 0, "you": 0, "customer": 0, "scheduled": 0, "breached": 0}
    for c in list_cases(store, desk_id, open_only=True, limit=1000):
        out["open"] += 1
        out[lane(c)] = out.get(lane(c), 0) + 1
        if c.get("breached"):
            out["breached"] += 1
    return out


# ---------------------------------------------------------------------------------------------- writes
def _set(store, cid: int, **fields) -> None:
    s, _ = R._base(store)
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)[:60000]
    fields["updated"] = time.time()
    with s._lock:
        s._conn.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), cid))
        s._conn.commit()


def attach(store, desk_id: int, cid: int, record_id: int, role: str = "related") -> None:
    s, _ = R._base(store)
    with s._lock:
        if s._conn.execute("SELECT 1 FROM case_records WHERE case_id=? AND record_id=?", (cid, record_id)).fetchone():
            return
        s._conn.execute("INSERT INTO case_records(desk_id,case_id,record_id,role) VALUES(?,?,?,?)", (desk_id, cid, record_id, role))
        s._conn.commit()


def open_case(store, desk_id: int, ctype: str, title: str, record_id: int = 0, priority: str = "normal", source: str = "",
              brief: str = "", actor: str = "system", run_id: str = "", desk: dict[str, Any] | None = None) -> dict[str, Any]:
    """Open a case in its playbook's initial state. Does NOT start a run - call kick() (or let the scheduler do it)."""
    s, _ = R._base(store)
    desk = desk or s.desk(desk_id)
    pbs = playbooks(desk)
    ctype = R.slug(ctype) if R.slug(ctype) in pbs else "task"
    pb = pbs[ctype]
    first = _state(pb, pb.get("initial") or pb["states"][0]["id"])
    now = time.time()
    priority = priority if priority in PRIORITIES else "normal"
    data = {"brief": (brief or "")[:12000], "rejections": 0}
    with s._lock:
        cur = s._conn.execute(
            "INSERT INTO cases(desk_id,type,title,state,priority,record_id,waiting_for,wake_at,due_at,opened,updated,state_since,"
            "source,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (desk_id, ctype, (title or pb.get("label", ctype)).strip()[:200], first["id"], priority, int(record_id or 0),
             _waiting(first), _wake(first, now), (now + duration(first.get("sla"))) if first.get("sla") else None,
             now, now, now, source[:60], json.dumps(data, ensure_ascii=False)))
        cid = cur.lastrowid or s._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        s._conn.commit()
    if record_id:
        attach(s, desk_id, cid, int(record_id), "subject")
    R.add_timeline(s, desk_id, case_id=cid, record_id=0, kind="opened", actor=actor,
                   text=f"{pb.get('label', ctype)} opened" + (f" from {source}" if source else "") + f" - {first.get('label', first['id'])}",
                   run_id=run_id)
    return get(s, cid)  # type: ignore[return-value]


def _waiting(st: dict[str, Any]) -> str:
    if st.get("kind") == "work":
        return "atlas"
    if st.get("kind") == "wait":
        return st.get("wait") or "owner"
    return ""


def _wake(st: dict[str, Any], now: float) -> float | None:
    if st.get("kind") == "work":
        return now
    if st.get("kind") == "wait" and st.get("timeout"):
        return now + duration(st["timeout"])
    return None


def note(store, cid: int, text: str, actor: str = "owner", run_id: str = "", kind: str = "note") -> None:
    c = get(store, cid)
    if c and (text or "").strip():
        R.add_timeline(store, c["desk_id"], case_id=cid, kind=kind, actor=actor, text=text.strip(), run_id=run_id)
        _set(store, cid)


def enter(store, cid: int, state_id: str, reason: str = "", actor: str = "system", outcome: str = "", kick_now: bool = True,
          extra: str = "") -> str:
    """Move a case into a state and do what that state says. Returns the run id when a work state started one."""
    c = get(store, cid)
    if not c:
        return ""
    desk = _desk(store, c["desk_id"])
    pb = _pb(desk, c["type"])
    ids = [s["id"] for s in pb["states"]]
    if state_id not in ids:
        raise ValueError(f"unknown state {state_id!r} for a {c['type']} case (states: {', '.join(ids)})")
    st = _state(pb, state_id)
    old = _state(pb, c["state"])
    now = time.time()
    fields: dict[str, Any] = {"state": state_id, "state_since": now, "waiting_for": _waiting(st), "wake_at": _wake(st, now),
                              "due_at": (now + duration(st.get("sla"))) if st.get("sla") else None, "breached": 0}
    if st.get("kind") == "done":
        fields.update({"closed_at": now, "outcome": (outcome or st.get("outcome") or reason or state_id)[:200], "wake_at": None,
                       "due_at": None, "waiting_for": ""})
    elif c.get("closed_at"):
        fields.update({"closed_at": None, "outcome": ""})       # reopened by the owner
    _set(store, cid, **fields)
    R.add_timeline(store, c["desk_id"], case_id=cid, kind="state", actor=actor,
                   text=f"{old.get('label', c['state'])} → {st.get('label', state_id)}" + (f": {reason}" if reason else ""),
                   data={"from": c["state"], "to": state_id, "outcome": fields.get("outcome", "")})
    if st.get("kind") == "work" and kick_now:
        return kick(store, desk, get(store, cid), extra=extra)
    return ""


def fire(store, cid: int, event: str, text: str = "", actor: str = "system", run_id: str = "", extra: str = "") -> str:
    """An event happened on a case. Logs it, then follows the playbook. Returns a run id if one started."""
    c = get(store, cid)
    if not c or c.get("closed_at"):
        if c and text:
            R.add_timeline(store, c["desk_id"], case_id=cid, kind=event, actor=actor, text=text, run_id=run_id)
        return ""
    desk = _desk(store, c["desk_id"])
    pb = _pb(desk, c["type"])
    st = _state(pb, c["state"])
    if text:
        R.add_timeline(store, c["desk_id"], case_id=cid, kind=event, actor=actor, text=text, run_id=run_id)
    data = c["data"]
    if event == "reply" and c.get("active_run") and _run_alive(c["active_run"]):
        data["pending_reply"] = True                     # Atlas is mid-step; it gets the reply the moment it finishes
        _set(store, cid, data=data)
        return ""
    if event == "rejected":
        data["rejections"] = int(data.get("rejections") or 0) + 1
        _set(store, cid, data=data)
        limit = int(pb.get("max_rejections") or 0)
        if limit and data["rejections"] > limit and any(s["id"] == "needs_owner" for s in pb["states"]):
            return enter(store, cid, "needs_owner", f"{data['rejections']} drafts rejected - over to you", actor="system")
    target = (st.get("on") or {}).get(event)
    if event in ("sent", "rejected") and pending_actions(store, cid):
        target = None                                    # more drafts still waiting: decide on the last one first
    if not target:
        if event in ("sent", "rejected") and c["waiting_for"] == "approval" and not pending_actions(store, cid):
            _set(store, cid, waiting_for="owner")
        return ""
    reason = {"sent": "message sent", "rejected": "draft rejected", "reply": "they replied", "booked": "booking confirmed",
              "alert": "the camera fired again"}.get(event, event)
    return enter(store, cid, target, reason, actor=actor, extra=extra)


# ---------------------------------------------------------------------------------------------- runs
def _run_alive(run_id: str) -> bool:
    if not run_id:
        return False
    if run_id == "starting":                           # START_RUN in flight (bind_run follows within the second)
        return True
    try:
        return (RUN_STATUS(run_id) if RUN_STATUS else "") == "running"
    except Exception:
        return False


def build_task(store, c: dict[str, Any], desk: dict[str, Any] | None = None, extra: str = "") -> str:
    desk = desk or _desk(store, c["desk_id"])
    pb = _pb(desk, c["type"])
    st = _state(pb, c["state"])
    lines = [f"Case #{c['id']} · {pb.get('label', c['type'])} · step: {st.get('label', c['state'])} · priority {c['priority']}",
             f"Title: {c['title']}", "", "YOUR STEP NOW: " + (st.get("task") or "Move this case forward.")]
    if extra:
        lines += ["", extra.strip()]
    recs = case_records(store, c["id"])
    if recs:
        lines += ["", "Records on this case (record_get <id> for full history):"]
        lines += [f"- [{r['role']}] {R.brief(r)}" for r in recs[:12]]
    if c["data"].get("brief"):
        lines += ["", "Original request:", c["data"]["brief"].strip()]
    facts = c["data"].get("facts") or []
    if facts:
        lines += ["", "Facts recorded on this case:"] + [f"- {f}" for f in facts[-12:]]
    tl = R.timeline_for(store, case_ids=[c["id"]], limit=14)
    if tl:
        lines += ["", "Case history (newest first):"]
        for t in tl:
            lines.append(f"- {time.strftime('%a %d %b %H:%M', time.localtime(t['ts']))} {t['kind']} ({t['actor'] or '-'}): {t['text'][:500]}")
    pend = pending_actions(store, c["id"])
    if pend:
        lines += ["", "Still waiting for the owner's approval on this case:"]
        lines += [f"- #{a['id']} {a['kind']} → {a['to']}: {a['subject'] or (a['body'] or '')[:80]}" for a in pend]
    states = ", ".join(f"{s['id']} ({s.get('label', s['id'])})" for s in pb["states"])
    lines += ["", "Case rules: " + _W + " Record what you found with case_update(note=...). When this step is finished and "
              f"the case should move, case_update(state=...). States: {states}. To look again later instead, "
              "case_update(wait_hours=N). Save facts about people, bookings and orders with record_save."]
    return "\n".join(lines)


def kick(store, desk: dict[str, Any] | None, c: dict[str, Any] | None, extra: str = "", lead_id: int | None = None) -> str:
    """Start the run for a case's current step (once at a time). Returns the run id, or "" when nothing started."""
    if not c or c.get("closed_at") or START_RUN is None:
        return ""
    if c.get("active_run") and _run_alive(c["active_run"]):
        return ""
    desk = desk or _desk(store, c["desk_id"])
    if not desk:
        return ""
    data = c["data"]
    day_ago = time.time() - 86400
    starts = [t for t in data.get("run_starts") or [] if t > day_ago]
    if len(starts) >= MAX_RUNS_PER_DAY:
        _set(store, c["id"], waiting_for="owner", wake_at=None)
        R.add_timeline(store, c["desk_id"], case_id=c["id"], kind="paused", actor="system",
                       text=f"{len(starts)} runs on this case in 24 h - paused until you look at it")
        return ""
    task = build_task(store, c, desk, extra)
    data["run_starts"] = starts + [time.time()]
    data["run_started"] = time.time()
    data["run_moved"] = False
    data.pop("pending_reply", None)
    # "starting" holds the slot until the run's first event binds its real id (bind_run) - a fast run can finish before
    # START_RUN even returns, and a second kick must not start a twin meanwhile
    _set(store, c["id"], active_run="starting", waiting_for="atlas", wake_at=None, data=data)
    try:
        rid = START_RUN(desk, task, "auto", lead_id, c["id"])
    except BaseException as exc:                         # Flask abort (spend cap 402, no model key 503) or a provider error
        why = getattr(getattr(exc, "response", None), "data", b"") or str(exc) or type(exc).__name__
        if isinstance(why, bytes):
            why = why.decode(errors="replace")
        last = R.timeline_for(store, case_ids=[c["id"]], limit=1)
        msg = f"the desk could not start the step: {str(why)[:200]}"
        if not last or last[0]["text"] != msg:
            R.add_timeline(store, c["desk_id"], case_id=c["id"], kind="error", actor="system", text=msg)
        _set(store, c["id"], active_run="", waiting_for="atlas", wake_at=time.time() + RETRY_AFTER_S)
        return ""
    c2 = get(store, c["id"])
    if c2 and c2.get("active_run") == "starting" and (c2["data"].get("finished_run") != rid):
        bind_run(store, c["id"], rid)                     # START_RUN did not report the run's start itself
    return rid


def bind_run(store, cid: int, run_id: str) -> None:
    """The run for a case's step has its id (called from the run's first event, before any model call)."""
    c = get(store, cid)
    if not c or c.get("active_run") not in ("", "starting"):
        return
    _set(store, cid, active_run=run_id, runs=int(c.get("runs") or 0) + 1)
    R.add_timeline(store, c["desk_id"], case_id=cid, kind="run", actor="atlas", text="Atlas started the step", run_id=run_id)


def run_finished(store, cid: int, run_id: str, status: str, summary: str = "") -> None:
    """Called when a case's run ends (from the run thread, or by the tick after a restart)."""
    c = get(store, cid)
    if not c:
        return
    short = (summary or "").split("\n\n---\nVerified by the desk")[0].strip()
    R.add_timeline(store, c["desk_id"], case_id=cid, kind="run_done" if status == "done" else "run_failed", actor="atlas",
                   text=(short[:1500] or f"run {status}"), run_id=run_id, data={"status": status})
    if c.get("active_run") not in (run_id, "starting"):
        return
    data = c["data"]
    moved = bool(data.get("run_moved"))
    data["run_moved"] = False
    data["finished_run"] = run_id
    _set(store, cid, active_run="", data=data)
    if c.get("closed_at"):
        return
    desk = _desk(store, c["desk_id"])
    if data.get("pending_reply"):
        data.pop("pending_reply", None)
        _set(store, cid, data=data)
        kick(store, desk, get(store, cid), extra="A new reply arrived while you were on the last step - it is in the case history.")
        return
    if moved:
        c2 = get(store, cid)
        if c2 and not c2.get("closed_at") and c2["waiting_for"] == "atlas" and not c2.get("wake_at"):
            _set(store, cid, wake_at=time.time())       # Atlas moved it into another work state: the tick runs that next
        return
    pb = _pb(desk, c["type"])
    st = _state(pb, c["state"])
    if st.get("kind") != "work":
        return
    if pending_actions(store, cid):
        _set(store, cid, waiting_for="approval")
        return
    if status != "done":
        _set(store, cid, waiting_for="owner")
        R.add_timeline(store, c["desk_id"], case_id=cid, kind="attention", actor="system",
                       text=f"the step ended with status {status} - check it or press Run again")
        return
    if st.get("after_run"):
        enter(store, cid, st["after_run"], "Atlas finished the step without sending anything", actor="system")
    else:
        _set(store, cid, waiting_for="owner")


# ---------------------------------------------------------------------------------------------- the clock
def tick(store, now: float | None = None) -> dict[str, int]:
    """Timers, deadlines and lost runs for every desk. Cheap when nothing is due; called by the desk scheduler."""
    s, _ = R._base(store)
    now = now or time.time()
    done = {"woke": 0, "timeouts": 0, "breached": 0, "recovered": 0}
    for c in [_decode(r) for r in _rows(s._conn.execute(
            "SELECT * FROM cases WHERE closed_at IS NULL AND active_run!='' LIMIT 50"))]:
        if c["active_run"] == "starting":                # a start that never reported back (crash mid-start)
            if (c["data"].get("run_started") or 0) < now - 120:
                run_finished(s, c["id"], "starting", "lost", "")
                done["recovered"] += 1
            continue
        st = RUN_STATUS(c["active_run"]) if RUN_STATUS else ""
        if st and st != "running":
            run_finished(s, c["id"], c["active_run"], st, _run_summary(s, c["active_run"]))
            done["recovered"] += 1
    for c in [_decode(r) for r in _rows(s._conn.execute(
            "SELECT * FROM cases WHERE closed_at IS NULL AND wake_at IS NOT NULL AND wake_at<=? ORDER BY wake_at LIMIT 20", (now,)))]:
        if c.get("active_run") and _run_alive(c["active_run"]):
            continue
        desk = s.desk(c["desk_id"])
        if not desk or not enabled(desk):
            continue
        pb = _pb(desk, c["type"])
        st = _state(pb, c["state"])
        try:
            if st.get("kind") == "wait" and st.get("on_timeout"):
                done["timeouts"] += 1
                enter(s, c["id"], st["on_timeout"], f"no change in {st.get('timeout')}", actor="clock",
                      outcome=st.get("timeout_outcome") or "")
            elif st.get("kind") == "work" or c["waiting_for"] == "time":
                done["woke"] += 1
                if not kick(s, desk, c, extra="Scheduled check." if c["waiting_for"] == "time" else ""):
                    if get(s, c["id"]) and get(s, c["id"]).get("wake_at") == c.get("wake_at"):
                        _set(s, c["id"], wake_at=None)
            else:
                _set(s, c["id"], wake_at=None)
        except Exception:
            traceback.print_exc()
            _set(s, c["id"], wake_at=now + RETRY_AFTER_S)
    for c in [_decode(r) for r in _rows(s._conn.execute(
            "SELECT * FROM cases WHERE closed_at IS NULL AND breached=0 AND due_at IS NOT NULL AND due_at<? LIMIT 50", (now,)))]:
        desk = s.desk(c["desk_id"])
        pb = _pb(desk, c["type"])
        st = _state(pb, c["state"])
        pri = "high" if c["priority"] in ("low", "normal") else c["priority"]
        _set(s, c["id"], breached=1, priority=pri)
        msg = f"deadline missed: '{st.get('label', c['state'])}' was due within {st.get('sla')}"
        R.add_timeline(s, c["desk_id"], case_id=c["id"], kind="sla", actor="clock", text=msg)
        if NOTIFY:
            try:
                NOTIFY(c["desk_id"], f":alarm_clock: *Case #{c['id']} {c['title']}* - {msg}")
            except Exception:
                pass
        done["breached"] += 1
    return done


def _run_summary(s, run_id: str) -> str:
    row = s.run(run_id)
    return (row or {}).get("summary") or ""


def next_wake(store, now: float, horizon: float) -> float | None:
    s, _ = R._base(store)
    row = s._conn.execute("SELECT MIN(wake_at) FROM cases WHERE closed_at IS NULL AND wake_at IS NOT NULL AND wake_at<=?",
                          (now + horizon,)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------------------------------------- intake
def match_open(store, desk_id: int, email: str = "", phone: str = "") -> dict[str, Any] | None:
    """The newest open case about the person behind this email / phone (a reply continues the conversation)."""
    person = R.person_for(store, desk_id, email=email, phone=phone)
    if not person:
        return None
    cs = [c for c in list_cases(store, desk_id, open_only=True, record_id=person["id"], limit=20)]
    return cs[0] if cs else None


def inbound_reply(store, desk: dict[str, Any], c: dict[str, Any], channel: str, text: str, actor: str = "",
                  extra: str = "") -> str:
    """Someone on an open case wrote back. Logged on the case (and their record), then the playbook decides."""
    if c.get("record_id"):
        R.add_timeline(store, desk["id"], record_id=c["record_id"], kind="reply", actor=actor or channel,
                       text=f"via {channel}: {text[:600]}")
    rid = fire(store, c["id"], "reply", f"via {channel}: {text.strip()[:3000]}", actor=actor or channel, extra=extra)
    if not rid:
        c2 = get(store, c["id"])
        if c2 and not c2.get("closed_at") and _state(_pb(desk, c2["type"]), c2["state"]).get("kind") == "work" and not c2.get("active_run"):
            rid = kick(store, desk, c2, extra=(f"New message via {channel} - newest in the case history. " + extra).strip())
    return rid


def camera_alert(store, desk: dict[str, Any], camera: str, task: str, reason: str, event_id: int | None = None,
                 window_h: float = 6.0) -> str:
    """A camera rule fired: open an incident case (or add to the open one for that camera). Returns the run id."""
    cam, _ = R.upsert(store, desk["id"], "camera", {"name": camera}, key=camera, source="cameras", log=False)
    since = time.time() - window_h * 3600
    open_ = [c for c in list_cases(store, desk["id"], open_only=True, ctype="incident", record_id=cam["id"], limit=5)
             if (c.get("opened") or 0) >= since]
    if open_:
        c = open_[0]
        return fire(store, c["id"], "alert", f"{reason}" + (f" (event #{event_id})" if event_id else ""), actor=camera, extra=task)
    c = open_case(store, desk["id"], "incident", f"{camera}: {reason}"[:200], record_id=cam["id"], priority="high",
                  source="camera", brief=task, actor=camera, desk=desk)
    return kick(store, desk, c)


# ---------------------------------------------------------------------------------------------- agent tools
TOOL_NAMES = ("case_open", "case_update", "case_list")


def tool(store, name: str, args: dict[str, Any], agent_id: str, run_id: str, bound_case: int | None) -> tuple[str, int | None]:
    """Run a case_* tool for an agent. Returns (result text, case id the run is now bound to)."""
    s, did = R._base(store)
    if did is None:
        return "no cases in this context", bound_case
    desk = s.desk(did)
    if name == "case_list":
        cs = list_cases(s, did, open_only=not bool(args.get("include_closed")), ctype=str(args.get("type") or ""),
                        state=str(args.get("state") or ""), limit=int(args.get("limit") or 30))
        if not cs:
            return "no matching cases", bound_case
        return "\n".join(f"case #{c['id']} {c['type']} '{c['title']}' state={c['state']} waiting_for={c['waiting_for'] or '-'} "
                         f"priority={c['priority']}" + (" CLOSED " + c['outcome'] if c.get("closed_at") else "")
                         for c in cs), bound_case
    if name == "case_open":
        rec_id = 0
        ref = args.get("record")
        if ref:
            rec = R.resolve_ref(s, did, ref)
            if not rec and isinstance(ref, dict) and ref.get("type"):
                rec, _ = R.upsert(s, did, str(ref["type"]), ref.get("props") or {}, key=str(ref.get("key") or ""),
                                  source="agent", actor=agent_id, run_id=run_id)
            rec_id = (rec or {}).get("id") or 0
        c = open_case(s, did, str(args.get("type") or "task"), str(args.get("title") or ""), rec_id,
                      str(args.get("priority") or "normal"), source=f"agent {agent_id}", brief=str(args.get("note") or ""),
                      actor=agent_id, run_id=run_id, desk=desk)
        if bound_case is None and run_id:              # this run is now working the case it opened
            data = c["data"]
            data["run_started"] = time.time()
            data["run_moved"] = False
            _set(s, c["id"], active_run=run_id, waiting_for="atlas", wake_at=None, data=data)
            return (f"opened case #{c['id']} ({c['type']}, state {c['state']}) and bound it to this run - messages you "
                    f"queue from now on belong to it"), c["id"]
        return f"opened case #{c['id']} ({c['type']}, state {c['state']}) - the desk will work it", bound_case
    if name == "case_update":
        cid = int(str(args.get("case_id") or bound_case or 0).lstrip("#") or 0)
        c = get(s, cid) if cid else None
        if not c:
            return "ERROR: which case? pass case_id (see case_list)", bound_case
        pb = _pb(desk, c["type"])
        out: list[str] = []
        data = c["data"]
        if args.get("note"):
            R.add_timeline(s, did, case_id=cid, kind="note", actor=agent_id, text=str(args["note"])[:3000], run_id=run_id)
            out.append("note added")
        if args.get("fact"):
            data.setdefault("facts", []).append(str(args["fact"])[:300])
            data["facts"] = data["facts"][-40:]
            out.append("fact recorded")
        if args.get("priority") in PRIORITIES and args["priority"] != c["priority"]:
            _set(s, cid, priority=args["priority"])
            out.append(f"priority {args['priority']}")
        if args.get("link_record"):
            rec = R.resolve_ref(s, did, args["link_record"])
            if rec:
                attach(s, did, cid, rec["id"], "related")
                out.append(f"linked {rec['type']} #{rec['id']}")
        if args.get("facts") is None and out and "fact recorded" in out:
            pass
        data["run_moved"] = data.get("run_moved") or False
        target = str(args.get("state") or "").strip()
        if args.get("close") and not target:            # the playbook's first done state (closed / done / resolved...)
            target = next((st["id"] for st in pb["states"] if st.get("kind") == "done"), "")
        if target:
            ids = [st["id"] for st in pb["states"]]
            if target not in ids:
                return f"ERROR: {target!r} is not a state of a {c['type']} case. States: {', '.join(ids)}", bound_case
            data["run_moved"] = True
            _set(s, cid, data=data)
            in_this_run = c.get("active_run") == run_id
            enter(s, cid, target, str(args.get("reason") or args.get("note") or "")[:300], actor=agent_id,
                  outcome=str(args.get("outcome") or ""), kick_now=not in_this_run)
            out.append(f"moved to {target}")
        elif args.get("wait_hours"):
            try:
                hours = max(0.05, min(float(args["wait_hours"]), 24 * 30))
            except (TypeError, ValueError):
                hours = 24.0
            data["run_moved"] = True
            _set(s, cid, data=data, waiting_for="time", wake_at=time.time() + hours * 3600)
            R.add_timeline(s, did, case_id=cid, kind="scheduled", actor=agent_id, text=f"check again in {hours:g} h", run_id=run_id)
            out.append(f"will look again in {hours:g} h")
        else:
            _set(s, cid, data=data)
        return f"case #{cid} updated: " + (", ".join(out) or "nothing changed"), bound_case
    return f"unknown case tool {name}", bound_case
