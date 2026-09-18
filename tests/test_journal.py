"""Camera journal: detailed notes on change / max gap, two-frame continuity, summaries, diary, RAG wiring."""
import time

import pytest

from atlas import journal as JR
from atlas import rag as RAG
from atlas import vision as V
from test_vision import PERSON, FakeDetector, _jpeg


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    JR.reset()
    monkeypatch.setattr(JR, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(V, "SNAP_DIR", tmp_path / "snaps")
    yield
    JR.reset()


class FakeVLM:
    """Stands in for V.chat_images: records what the journal sent, returns a canned note."""
    def __init__(self):
        self.calls = []

    def __call__(self, system, text, images, model="", max_tokens=500, transport=None):
        self.calls.append({"system": system, "text": text, "labels": [l for l, _ in images]})
        if system == JR.ROLLUP_SYSTEM:
            return "Quiet window. 12:00 one adult in a red coat waited at the counter, served after 2 minutes."
        return f"note {len(self.calls)}: one adult in a red coat stands at the counter holding a bag."


def test_config_defaults_and_bounds():
    jc = JR.config({})
    assert jc == {"on": False, "every_s": 60, "min_gap_s": 8, "motion": 0.03, "rollup_min": 15, "focus": ""}
    jc = JR.config({"journal": "1", "journal_every_s": "5", "journal_rollup_min": "x", "journal_focus": "tables"})
    assert jc["on"] and jc["every_s"] == 10 and jc["rollup_min"] == 15 and jc["focus"] == "tables"


def test_due_first_change_motion_gap_and_backoff():
    jc = JR.config({"journal": "1"})
    key = (1, "front")
    now = 1000.0
    assert JR.due(key, jc, now, 0.0, {}) == (True, "first note")
    JR._state[key] = {"note": {"ts": now, "text": "x", "jpeg": b"", "counts": {"person": 1}}}
    assert JR.due(key, jc, now + 3, 0.9, {"person": 2})[0] is False            # inside min gap, even with change
    ok, why = JR.due(key, jc, now + 10, 0.0, {"person": 2})
    assert ok and "counts" in why                                              # count changed
    ok, why = JR.due(key, jc, now + 10, 0.2, {"person": 1})
    assert ok and "scene changed" in why                                       # movement
    assert JR.due(key, jc, now + 10, 0.001, {"person": 1}) == (False, "no change")
    ok, why = JR.due(key, jc, now + 61, 0.0, {"person": 1})
    assert ok and "since last note" in why                                     # max gap: still documented when quiet
    JR._state[key]["fail_until"] = time.time() + 30
    assert JR.due(key, jc, time.time(), 0.9, {"person": 5})[0] is False        # backing off after a model error


def test_note_uses_previous_frame_and_note(monkeypatch, tmp_path):
    vlm = FakeVLM()
    monkeypatch.setattr(V, "chat_images", vlm)
    jc = JR.config({"journal": "1", "journal_focus": "queue at the counter"})
    key = (1, "counter")
    first = JR.write_note(key, "counter", jc, b"\xff\xd8one", {"person": 1}, notes="Till and counter", now=1000.0)
    assert first.startswith("note 1") and vlm.calls[0]["labels"][0].startswith("NOW")
    assert len(vlm.calls[0]["labels"]) == 1 and "first note" in vlm.calls[0]["text"]
    assert "queue at the counter" in vlm.calls[0]["text"] and "Till and counter" in vlm.calls[0]["text"]
    JR.write_note(key, "counter", jc, b"\xff\xd8two", {"person": 2}, now=1030.0)
    c = vlm.calls[1]
    assert c["labels"][0].startswith("EARLIER") and "30s ago" in c["labels"][0] and c["labels"][1].startswith("NOW")
    assert "Previous note (30s ago): note 1" in c["text"] and "2 people" in c["text"]


def test_rollup_condenses_notes_into_a_digest_event(store, monkeypatch):
    vlm = FakeVLM()
    monkeypatch.setattr(V, "chat_images", vlm)
    ds = store.for_desk(7)
    jc = JR.config({"journal": "1", "journal_rollup_min": "1"})
    key = (7, "counter")
    t0 = time.time() - 120
    JR._state[key] = {"rollup_ts": t0}
    for i, n in enumerate((1, 3, 2)):
        ds.add_vision_event("counter", {"person": n}, backend="yolo", answer=f"journal text {i}", source="journal")
    ds.add_vision_event("counter", {"person": 9}, backend="yolo", reason="plain change event")   # not a note: ignored
    ev = JR.maybe_rollup(ds, 7, "counter", jc, b"\xff\xd8end", now=time.time())
    assert ev and ev["source"] == "digest" and ev["counts"] == {"person": 3} and "3 notes" in ev["reason"]
    assert "journal text 0" in vlm.calls[-1]["text"] and "journal text 2" in vlm.calls[-1]["text"]
    assert "plain change event" not in vlm.calls[-1]["text"]
    assert JR.maybe_rollup(ds, 7, "counter", jc, None) is None                 # window just reset
    diary = JR.diary_read(7)
    assert "Summary" in diary and "counter" in diary and f"[#{ev['id']}]" in diary


def test_diary_filters_by_camera():
    JR.diary_append(3, time.time(), "front", "note", "front text", 1)
    JR.diary_append(3, time.time(), "back", "note", "back text", 2)
    assert "front text" in JR.diary_read(3) and "back text" in JR.diary_read(3)
    only = JR.diary_read(3, camera="back")
    assert "back text" in only and "front text" not in only and only.startswith("# Camera journal")
    assert JR.diary_days(3) == [time.strftime("%Y-%m-%d")]
    assert JR.diary_read(3, "1999-01-01") == ""


def test_rag_log_lines_show_journal_in_full():
    long = "x" * 600
    rows = [{"id": 1, "ts": time.time(), "camera": "c", "counts": {}, "motion": 0, "reason": "journal: first note",
             "answer": long, "source": "journal", "triggered": 0},
            {"id": 2, "ts": time.time(), "camera": "c", "counts": {}, "motion": 0, "reason": "", "answer": long,
             "source": "camera", "triggered": 0}]
    a, b = RAG.log_lines(rows)
    assert "journal note: " + long in a
    assert "analyst: " + "x" * 220 in b and long not in b


def test_video_file_plays_as_a_live_camera(tmp_path, monkeypatch):
    assert V.source_kind(str(tmp_path / "clip.MP4")) == "video" and V.source_kind("door.jpg") == "file"
    with pytest.raises(RuntimeError, match="not found"):
        V.grab(str(tmp_path / "missing.mp4"))


def test_camera_tick_writes_journal_notes(store, tmp_path, monkeypatch):
    """The watch loop writes a note on the first tick, stays quiet on an unchanged scene, notes again on change."""
    import atlas.desk.scheduler as S
    vlm = FakeVLM()
    monkeypatch.setattr(V, "chat_images", vlm)
    monkeypatch.setattr(V, "vlm_ready", lambda: True)
    monkeypatch.setattr(V, "describe", lambda *a, **k: "analyst answer")
    fake = FakeDetector([PERSON])
    monkeypatch.setattr(V, "DETECTOR", fake)
    S._last_frame.clear(); S._last_seen.clear(); S._present.clear()
    desk = store.add_desk(0, "Cafe", "blank", "free", {})
    src = _jpeg(tmp_path / "cafe.jpg", blob=(40, 30, 120, 220))
    conn = store.add_connector(desk["id"], "camera", "floor", {"source": src, "watch_for": "car", "journal": "1",
                                                                "journal_every_s": "60", "journal_min_gap_s": "2"}, False)
    runs = []
    r1 = S.camera_tick(store, desk, conn, lambda d, t, m: runs.append(t) or "run1", True)
    assert r1["journal"].startswith("note 1") and not r1["triggered"] and not runs
    ev = store.last_vision_event(desk["id"], "floor")
    assert ev["source"] == "journal" and ev["answer"].startswith("note 1") and ev["reason"] == "journal: first note"
    r2 = S.camera_tick(store, desk, conn, lambda *a: "x", True)                 # same frame, 0 s later: no new note
    assert r2["journal"] == "" and len(vlm.calls) == 1
    key = (desk["id"], "floor")
    JR._state[key]["note"]["ts"] -= 5                                            # past the min gap...
    fake.dets = [PERSON, PERSON]                                                 # ...and a second person arrives
    r3 = S.camera_tick(store, desk, conn, lambda *a: "x", True)
    assert r3["journal"].startswith("note 2") and "counts" in r3["journal_status"]
    assert "EARLIER" in vlm.calls[1]["labels"][0]
    diary = JR.diary_read(desk["id"])
    assert "note 1" in diary and "note 2" in diary and "floor" in diary
    # document-only camera: the rule matches (a car) but the agents are never woken; the note is still written
    conn3 = store.add_connector(desk["id"], "camera", "lot", {"source": src, "watch_for": "person", "journal": "1", "alerts": "0"}, False)
    woke = []
    r4 = S.camera_tick(store, desk, conn3, lambda *a: woke.append(1) or "x", True)
    assert not r4["triggered"] and "alerts off" in r4["reason"] and not woke and r4["journal"].startswith("note 3")
    vlm.calls.pop()
    # journal off (or demo mode): no model calls at all
    conn2 = store.add_connector(desk["id"], "camera", "yard", {"source": src, "watch_for": "car"}, False)
    S.camera_tick(store, desk, conn2, lambda *a: "x", True)
    S.camera_tick(store, desk, conn, lambda *a: "x", False)
    assert len(vlm.calls) == 2


def test_team_agents_about_cameras_get_camera_tools():
    from atlas import team as TM
    raw = {"agents": [
        {"id": "reporter", "role": "Daily report writer", "goal": "Morning report from the camera journal",
         "instructions": ["Use the diary for covers and waits", "Hold for approval"], "tools": ["save_deliverable"]},
        {"id": "writer", "role": "Email writer", "goal": "Draft replies", "instructions": ["Be brief", "Hold"], "tools": ["save_deliverable"]}],
        "workflow": [{"agent": "reporter", "task": "report"}]}
    team, _ = TM.validate_team(raw)
    by = {a["id"]: a["tools"] for a in team["agents"]}
    assert {"camera_ask", "camera_events"} <= set(by["reporter"]) and "camera_ask" not in by["writer"]
