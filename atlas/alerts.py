"""Camera alerts that reach a person, and do not wear them out.

A rule firing used to start an Atlas run and nothing else; on a sales desk that produced an outreach email. Now a
desk can say where alerts go (config["notify"]) and every firing becomes one message in the approval queue (sent at
once when the desk says so), with the snapshot, the note and the reason:

    notify = {"channel": "whatsapp" | "sms" | "email" | "slack", "to": "+44...", "auto": false,
              "quiet": "23:00-07:00",          # inside this window alerts are logged, not sent (the report has them)
              "per_hour": 6,                    # per camera; beyond it the rest of the hour is one digest message
              "run": false,                     # also wake the agents for every alert (the old behaviour)
              "report": true, "report_time": "07:00"}   # the daily report by name, queued each morning

Nothing here sends by itself: an action is queued; the owner approves it, or `auto` sends it straight away through
the same connectors the agents use (I.deliver). Budget and quiet-hour decisions are recorded on the event's reason,
so the day's report still shows every firing.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import integrations as I
from . import vision as V

CHANNELS = ("whatsapp", "sms", "email", "slack")
DEFAULT_PER_HOUR = 6
WINDOW_S = 3600.0

_budget: dict[tuple[int, str], list[float]] = {}          # (desk, camera) -> alert timestamps in the last hour
_muted: dict[tuple[int, str], dict[str, Any]] = {}        # (desk, camera) -> {"since", "count", "last"} while muted
_lock = threading.Lock()


def config(desk: dict[str, Any]) -> dict[str, Any]:
    n = ((desk.get("config") or {}).get("notify") or {}) if isinstance(desk, dict) else {}
    ch = str(n.get("channel") or "").strip().lower()
    try:
        per_hour = max(1, min(120, int(n.get("per_hour") or DEFAULT_PER_HOUR)))
    except (TypeError, ValueError):
        per_hour = DEFAULT_PER_HOUR
    rt = str(n.get("report_time") or "07:00").strip()
    if not V.parse_hours(rt + "-" + rt) and rt != "00:00":
        rt = "07:00"
    return {"channel": ch if ch in CHANNELS else "", "to": str(n.get("to") or "").strip(), "auto": bool(n.get("auto")),
            "quiet": str(n.get("quiet") or "").strip(), "per_hour": per_hour,
            "run": bool(n.get("run", not ch)),                       # no channel configured -> the old behaviour (a run)
            "report": bool(n.get("report", True)), "report_time": rt}


def clean(n: dict[str, Any]) -> dict[str, Any] | str:
    """Validate what the owner posted; a string is the error."""
    ch = str(n.get("channel") or "").strip().lower()
    if ch and ch not in CHANNELS:
        return "channel must be whatsapp, sms, email or slack"
    to = str(n.get("to") or "").strip()
    if ch in ("whatsapp", "sms") and not to.startswith("+"):
        return "to must be an international number, e.g. +44 7700 900123"
    if ch == "email" and "@" not in to:
        return "to must be an email address"
    quiet = str(n.get("quiet") or "").strip()
    if quiet and not V.parse_hours(quiet):
        return "quiet must be a window like 23:00-07:00"
    rt = str(n.get("report_time") or "07:00").strip()
    if not V.parse_hours(rt + "-" + rt) and rt != "00:00":
        return "report_time must be HH:MM"
    try:
        per_hour = max(1, min(120, int(n.get("per_hour") or DEFAULT_PER_HOUR)))
    except (TypeError, ValueError):
        return "per_hour must be a number"
    return {"channel": ch, "to": to, "auto": bool(n.get("auto")), "quiet": quiet, "per_hour": per_hour,
            "run": bool(n.get("run", False)), "report": bool(n.get("report", True)), "report_time": rt}


# ---------------------------------------------------------------------------- the budget
def decide(desk_id: int, camera: str, per_hour: int, quiet: str, now: float | None = None) -> str:
    """'send' | 'quiet' | 'muted'. Counts the firing either way; the first firing over budget opens a muted window."""
    now = now or time.time()
    if quiet and V.in_hours(quiet):
        return "quiet"
    key = (desk_id, camera)
    with _lock:
        ts = [t for t in _budget.get(key, []) if now - t < WINDOW_S]
        ts.append(now)
        _budget[key] = ts
        if len(ts) <= per_hour:
            return "send"
        m = _muted.setdefault(key, {"since": now, "count": 0, "last": ""})
        m["count"] += 1
        return "muted"


def expired_mutes(now: float | None = None) -> list[tuple[int, str, dict[str, Any]]]:
    """Muted windows whose hour is over: (desk_id, camera, {since, count, last}); cleared on return."""
    now = now or time.time()
    out = []
    with _lock:
        for key, m in list(_muted.items()):
            if now - m["since"] >= WINDOW_S:
                out.append((key[0], key[1], dict(m)))
                _muted.pop(key, None)
                _budget.pop(key, None)
    return out


def note_muted(desk_id: int, camera: str, text: str) -> None:
    with _lock:
        m = _muted.get((desk_id, camera))
        if m:
            m["last"] = text[:300]


def reset() -> None:
    with _lock:
        _budget.clear()
        _muted.clear()


# ---------------------------------------------------------------------------- the messages
def _snapshot_url(base_url: str, event: dict[str, Any]) -> str:
    return f"{base_url.rstrip('/')}/api/vision/snapshot/{event['id']}" if base_url and event.get("id") else ""


def alert_message(camera: str, reason: str, counts: dict[str, int], answer: str, note: str, event: dict[str, Any],
                  known: list[dict[str, Any]] | None = None, base_url: str = "", when: float | None = None) -> tuple[str, str]:
    """(subject, body) for one firing: short enough for a phone, with everything the person needs to decide."""
    when = when or time.time()
    subject = f"{camera}: {reason}"[:120]
    who = ", ".join(k["name"] + ("" if k.get("sure") else " (likely)") for k in (known or [])[:4])
    lines = [f"Camera {camera}, {time.strftime('%H:%M', time.localtime(when))}: {reason}.",
             f"Seen: {V.counts_text(counts) or 'nothing recognised'}." + (f" Known: {who}." if who else "")]
    if answer:
        lines.append("Analyst: " + answer.strip()[:500])
    if note:
        lines.append("Journal: " + note.strip()[:500])
    url = _snapshot_url(base_url, event)
    if url:
        lines.append("Snapshot: " + url)
    lines.append(f"Event #{event.get('id', '?')}. Reply here or open the desk to see the camera live.")
    return subject, "\n".join(lines)


def digest_message(camera: str, muted: dict[str, Any], per_hour: int) -> tuple[str, str]:
    since = time.strftime("%H:%M", time.localtime(muted["since"]))
    subject = f"{camera}: {muted['count']} more alerts since {since} (muted)"
    body = (f"Camera {camera} fired {per_hour} times within an hour, so the next {muted['count']} were held back rather than "
            f"sent one by one (since {since}). All of them are in the journal and today's report."
            + (f"\nLast one: {muted['last']}" if muted.get("last") else "")
            + "\nIf this camera keeps firing, raise its cooldown or dwell time, or its per-hour budget.")
    return subject, body


def queue(store, desk: dict[str, Any], kind: str, to: str, subject: str, body: str, reason: str, auto: bool,
          dispatch=None) -> dict[str, Any]:
    """One outbound action. Pending for approval, or sent at once when `auto` and a dispatcher is given."""
    ds = store.for_desk(desk["id"])
    aid = ds.add_action("", "cameras", kind, to, subject, body, reason)
    row = ds.action(aid)
    if auto and dispatch is not None:
        try:
            result = dispatch(desk, row)
            row = ds.decide_action(aid, "sent", by="auto", note=result)
        except Exception as exc:
            row = ds.decide_action(aid, "failed", by="auto", note=f"send failed: {type(exc).__name__}: {str(exc)[:200]}")
        if kind in ("email", "whatsapp", "sms"):         # auto-sent alerts belong on the customer-facing thread too
            ds.add_message(kind, "out", to, body, subject=subject, actor="cameras", status=row["status"], action_id=aid)
    return row


def report_due(report_time: str, now: float | None = None) -> float:
    """Next occurrence of HH:MM local time, strictly after `now`."""
    now = now or time.time()
    h, m = (int(x) for x in report_time.split(":"))
    lt = time.localtime(now)
    t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, lt.tm_wday, lt.tm_yday, -1))
    if t <= now:
        t += 86400
    return t


def ensure_report_job(store, desk: dict[str, Any]) -> dict[str, Any] | None:
    """One daily_report job per desk, at the configured time; removed when reports are off or no channel is set."""
    n = config(desk)
    ds = store.for_desk(desk["id"])
    existing = [j for j in ds.jobs() if j["kind"] == "daily_report"]
    if not (n["report"] and n["channel"]):
        for j in existing:
            store.delete_job(j["id"])
        return None
    nxt = report_due(n["report_time"])
    if existing:
        store.update_job(existing[0]["id"], next_run=nxt, enabled=1, every_min=1440, name=f"Daily camera report at {n['report_time']}")
        return store.job(existing[0]["id"])
    return ds.add_job("daily_report", f"Daily camera report at {n['report_time']}", "", 1440, nxt)


def report_message(desk: dict[str, Any], date: str, md: str, channel: str) -> tuple[str, str]:
    biz = ((desk.get("config") or {}).get("business") or {}).get("name") or desk.get("name") or "your site"
    subject = f"Camera report, {biz}, {date}"
    if channel in ("whatsapp", "sms", "slack"):                      # a phone-sized version: names, alerts, the rest by link
        keep, skip = [], False
        for line in md.splitlines():
            if line.startswith("## "):
                skip = line.strip() in ("## What the cameras wrote (summaries)",)
            if not skip:
                keep.append(line.replace("**", "").replace("### ", "• ").replace("## ", "\n"))
        body = "\n".join(keep).strip()
        limit = 1500 if channel == "sms" else 3800
        if len(body) > limit:
            body = body[:limit - 40].rstrip() + "\n… (full report on the desk)"
        return subject, body
    return subject, md
