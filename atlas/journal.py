"""Camera journal: a detailed, continuous written record of what every camera sees.

Alert rules answer "should the desk wake up now?". The journal answers "what happened?" - it is the memory the
RAG log ("ask the cameras") searches. With `journal` on, a camera writes a note:

  * when the scene changes (motion >= journal_motion or the detector's counts changed), at most every
    journal_min_gap_s seconds, and
  * at least every journal_every_s seconds even when nothing changed ("still 2 guests at the window table").

Each note is written by the vision model from TWO frames - the frame at the previous note and the current one - plus
the previous note's text, so it records what changed (arrivals, departures, hand-offs, how long someone has waited)
rather than re-describing a still image. Every journal_rollup_min minutes the notes are condensed into a summary
event (timeline + peak counts) so broad questions ("how busy was lunch?") retrieve one dense row instead of 60.

Notes and summaries are ordinary vision events (source "journal" / "digest"), so they are embedded, retrieved and
cited exactly like alerts. They are also appended to a readable diary, one Markdown file per desk per day:
DATA_DIR/journal/desk<id>/<YYYY-MM-DD>.md.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import vision as V
from .config import DATA_DIR

JOURNAL_DIR = DATA_DIR / "journal"

_state: dict[tuple[int, str], dict[str, Any]] = {}     # (desk_id, camera) -> last note {ts, text, jpeg, counts}, rollup_ts, fail_until
_lock = threading.Lock()

# ---------------------------------------------------------------------------- live bus
# Everything the journal does is published as it happens (tick, note_start, note_delta, note_done, digest) so the
# portal can show the note being written word by word next to the live picture. Subscribers are plain queues.
_subs: dict[int, list["queue.Queue[dict[str, Any]]"]] = {}
_subs_lock = threading.Lock()
_BUS_MAX = 400


def publish(desk_id: int, kind: str, camera: str, **data: Any) -> dict[str, Any]:
    ev = {"kind": kind, "camera": camera, "ts": round(time.time(), 3), **data}
    with _subs_lock:
        subs = list(_subs.get(desk_id, ()))
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass                                          # a stalled viewer loses events, never blocks the camera
    return ev


def subscribe(desk_id: int) -> "queue.Queue[dict[str, Any]]":
    q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_BUS_MAX)
    with _subs_lock:
        _subs.setdefault(desk_id, []).append(q)
    return q


def unsubscribe(desk_id: int, q: "queue.Queue[dict[str, Any]]") -> None:
    with _subs_lock:
        try:
            _subs.get(desk_id, []).remove(q)
        except ValueError:
            pass


def subscribers(desk_id: int) -> int:
    with _subs_lock:
        return len(_subs.get(desk_id, ()))

NOTE_SYSTEM = """You keep the written journal for one camera at a business. Your notes are the ONLY record the owner
will search later ("when did the delivery arrive?", "how long did the couple by the window wait?", "was the back door
left open?"), so be concrete and complete.

You get the previous note and one or two frames: the frame at the previous note (if any) and the current frame, with
the seconds between them. Write the note for the CURRENT frame:
- People: how many, where exactly (door, counter, till, tables, queue, aisle, kitchen pass), what each is doing,
  direction of movement, and neutral visual descriptors that tell them apart (clothing colour, bag, apron or uniform,
  adult or child). Never guess identity, name, ethnicity or age beyond adult/child.
- Staff vs customers when uniform or position makes it clear; otherwise say it is unclear.
- Objects and state: vehicles (type, colour), doors open or closed, items carried, handed over or left behind, tables
  occupied / empty / being cleared, anything spilled, blocked or out of place.
- What CHANGED since the previous note: arrivals, departures, hand-offs, waiting that continues (carry the duration
  forward from the previous note), anything unusual.
- If nothing changed, say so in one line and restate the current state briefly.
Only what is visible. If the frame is dark, blurred or blocked, say so. No guesses about intent. Plain text, no
markdown, no preamble, 50-150 words, most important fact first."""

ROLLUP_SYSTEM = """You condense a window of one camera's journal into a summary the owner can skim and search.
Write: one headline sentence; then the key moments in time order, each starting with its time (HH:MM); then peak
numbers (most people at once, arrivals, departures, vehicles); then anything unusual or still unresolved at the end of
the window. Use only what the notes say; the frame shows the state at the end of the window. Plain text, no markdown,
80-220 words."""


def _num(c: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(c.get(key) if c.get(key) not in (None, "") else default)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def config(c: dict[str, Any]) -> dict[str, Any]:
    return {"on": str(c.get("journal", "")).strip().lower() in ("1", "true", "yes", "on"),
            "every_s": _num(c, "journal_every_s", 60, 10, 3600),
            "min_gap_s": _num(c, "journal_min_gap_s", 8, 2, 600),
            "motion": _num(c, "journal_motion", 0.03, 0.0, 1.0),
            "rollup_min": _num(c, "journal_rollup_min", 15, 1, 1440),
            "focus": str(c.get("journal_focus") or "").strip()[:400]}


def reset() -> None:
    with _lock:
        _state.clear()


def due(key: tuple[int, str], jc: dict[str, Any], now: float, motion: float, counts: dict[str, int]) -> tuple[bool, str]:
    """Should this tick write a note? Returns (due, why)."""
    st = _state.get(key) or {}
    if now < st.get("fail_until", 0):
        return False, "journal backing off after a model error"
    last = st.get("note")
    if not last:
        return True, "first note"
    gap = now - last["ts"]
    if gap >= jc["every_s"]:
        return True, f"{gap:.0f}s since last note"
    if gap < jc["min_gap_s"]:
        return False, f"last note {gap:.0f}s ago"
    if counts != last.get("counts"):
        return True, f"counts {V.counts_text(last.get('counts') or {}) or 'nothing'} -> {V.counts_text(counts) or 'nothing'}"
    if motion >= jc["motion"]:
        return True, f"scene changed (motion {motion:.2f})"
    return False, "no change"


def write_note(key: tuple[int, str], camera: str, jc: dict[str, Any], jpeg: bytes, counts: dict[str, int],
               notes: str = "", model: str = "", now: float | None = None, transport=None, why: str = "",
               stream: bool | None = None) -> str:
    """One journal note from the frame at the previous note + the current frame. Updates the camera's state.
    When anyone is listening on the live bus (or stream=True) the model is streamed and every delta is published."""
    now = now or time.time()
    desk_id = key[0]
    if stream is None:
        stream = subscribers(desk_id) > 0
    st = _state.get(key) or {}
    last = st.get("note")
    gap = now - last["ts"] if last else 0
    frames: list[tuple[str, bytes]] = []
    if last and last.get("jpeg"):
        frames.append((f"EARLIER ({time.strftime('%H:%M:%S', time.localtime(last['ts']))}, {gap:.0f}s ago)", last["jpeg"]))
    frames.append((f"NOW ({time.strftime('%H:%M:%S', time.localtime(now))})", jpeg))
    prompt = (f"Camera: {camera}." + (f" About this camera: {notes}." if notes else "")
              + f"\nTime now: {time.strftime('%A %d %B %H:%M:%S', time.localtime(now))}."
              + f"\nObject detector counts for the current frame (small model, may miss or miscount): {V.counts_text(counts) or 'nothing detected'}."
              + (f"\nPay special attention to: {jc['focus']}." if jc.get("focus") else "")
              + (f"\nPrevious note ({gap:.0f}s ago): {last['text']}" if last else "\nThis is the first note for this camera: describe the full scene.")
              + "\n\nWrite the journal note for NOW.")
    publish(desk_id, "note_start", camera, why=why, counts=dict(counts), frames=len(frames), gap_s=round(gap))
    t_start = time.time()
    if stream:
        parts: list[str] = []
        try:
            for delta in V.chat_images_stream(NOTE_SYSTEM, prompt, frames, model=model, max_tokens=380, transport=transport):
                if isinstance(delta, dict):
                    publish(desk_id, "note_model", camera, **delta)
                    continue
                parts.append(delta)
                publish(desk_id, "note_delta", camera, text=delta)
        except Exception as exc:
            publish(desk_id, "note_error", camera, error=str(exc)[:200])
            raise
        text = "".join(parts).strip()
    else:
        try:
            text = V.chat_images(NOTE_SYSTEM, prompt, frames, model=model, max_tokens=380, transport=transport).strip()
        except Exception as exc:
            publish(desk_id, "note_error", camera, error=str(exc)[:200])
            raise
    text = re.sub(r"\s+\n", "\n", text).replace(" — ", ", ").replace("—", ", ")
    if not text:
        publish(desk_id, "note_error", camera, error="vision model returned an empty note")
        raise RuntimeError("vision model returned an empty note")
    with _lock:
        st = _state.setdefault(key, {})
        st["note"] = {"ts": now, "text": text, "jpeg": jpeg, "counts": dict(counts)}
        st.setdefault("rollup_ts", now)
        st.pop("fail_until", None)
    publish(desk_id, "note_done", camera, text=text, why=why, counts=dict(counts), took_s=round(time.time() - t_start, 2))
    return text


def failed(key: tuple[int, str], backoff_s: float = 30.0) -> None:
    with _lock:
        _state.setdefault(key, {})["fail_until"] = time.time() + backoff_s


def maybe_rollup(ds, desk_id: int, camera: str, jc: dict[str, Any], jpeg: bytes | None, snapshot: str = "",
                 model: str = "", now: float | None = None, transport=None) -> dict[str, Any] | None:
    """When rollup_min has passed since the last summary, condense the notes in between into one `digest` event."""
    now = now or time.time()
    key = (desk_id, camera)
    st = _state.get(key) or {}
    since = st.get("rollup_ts")
    if since is None or now - since < jc["rollup_min"] * 60:
        return None
    rows = [r for r in ds.vision_events(camera, since - 0.001, "", 500) if r.get("source") == "journal" and r.get("answer")]
    rows.sort(key=lambda r: r["ts"])
    with _lock:
        _state.setdefault(key, {})["rollup_ts"] = now
    if not rows:
        return None
    peak: dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("counts") or {}).items():
            peak[k] = max(peak.get(k, 0), int(v or 0))
    lines = "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} [#{r['id']}] {r['answer']}" for r in rows)
    span = f"{time.strftime('%H:%M', time.localtime(rows[0]['ts']))}-{time.strftime('%H:%M', time.localtime(now))}"
    prompt = (f"Camera: {camera}. Window: {span} on {time.strftime('%A %d %B', time.localtime(now))}. "
              f"Peak detector counts in the window: {V.counts_text(peak) or 'nothing'}.\n\nJournal notes (oldest first):\n{lines[:14000]}")
    frames = [("end of window", jpeg)] if jpeg else []
    text = V.chat_images(ROLLUP_SYSTEM, prompt, frames, model=model, max_tokens=500, transport=transport).strip()
    if not text:
        return None
    ev = ds.add_vision_event(camera, peak, backend="vlm", reason=f"summary {span} ({len(rows)} notes)", question="",
                             answer=text, snapshot=snapshot, source="digest")
    diary_append(desk_id, now, camera, f"Summary {span}", text, ev.get("id"))
    publish(desk_id, "digest", camera, text=text, span=span, notes=len(rows), event_id=ev.get("id"))
    return ev


# --------------------------------------------------------------------------- diary (one Markdown file per desk per day)
def diary_path(desk_id: int, day: str = "") -> Path:
    day = day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "") else time.strftime("%Y-%m-%d")
    return JOURNAL_DIR / f"desk{int(desk_id)}" / f"{day}.md"


def diary_append(desk_id: int, ts: float, camera: str, title: str, text: str, event_id: Any = None) -> None:
    p = diary_path(desk_id, time.strftime("%Y-%m-%d", time.localtime(ts)))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        head = "" if p.exists() else f"# Camera journal, {time.strftime('%A %d %B %Y', time.localtime(ts))}\n\n"
        ref = f" [#{event_id}]" if event_id else ""
        with _lock, p.open("a", encoding="utf-8") as f:
            f.write(f"{head}### {time.strftime('%H:%M:%S', time.localtime(ts))} · {camera} · {title}{ref}\n{text.strip()}\n\n")
    except OSError:
        pass


def diary_days(desk_id: int) -> list[str]:
    d = JOURNAL_DIR / f"desk{int(desk_id)}"
    return sorted((p.stem for p in d.glob("*.md")), reverse=True) if d.is_dir() else []


def diary_read(desk_id: int, day: str = "", camera: str = "") -> str:
    p = diary_path(desk_id, day)
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8")
    if not camera:
        return text
    blocks = re.split(r"(?m)^(?=### )", text)
    return "".join(b for b in blocks if not b.startswith("### ") or f" · {camera} · " in b.split("\n", 1)[0])
