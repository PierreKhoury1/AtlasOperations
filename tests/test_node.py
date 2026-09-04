"""Atlas Vision Node: the track -> event state machine, config loading, the poster's token bucket and the hook
payload shape. No YOLO, no camera, no network - the node module imports ultralytics/cv2/httpx lazily."""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from test_vision import cam_client  # noqa: F401  (fixture: logged-in client on a site_watch desk)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "node"))
import atlas_node as N  # noqa: E402


def cam(**kw):
    c = dict(N.CAMERA_DEFAULTS, name="counter", watch_for=["person"], confirm_s=1.0, gone_s=3.0, cooldown_s=60, heartbeat_min=0)
    c.update(kw)
    return c


def dets(*ids, label="person"):
    return [{"id": i, "label": label, "conf": 0.9, "box": [0, 0, 10, 10]} for i in ids]


def test_entered_requires_confirmation_then_left_after_gone():
    s = N.CameraState(cam(min_count=5))
    assert s.update(0.0, dets(1)) == []                      # first sighting: not confirmed yet
    assert s.update(0.5, dets(1)) == []
    ev = s.update(1.2, dets(1))
    assert [e.kind for e in ev] == ["entered"] and ev[0].trigger is False and ev[0].counts == {"person": 1}
    assert s.update(2.0, []) == []                           # missing for a moment: still tracked
    ev = s.update(6.0, [])
    assert [e.kind for e in ev] == ["left"] and s.tracks == {}


def test_flicker_never_becomes_an_event():
    s = N.CameraState(cam())
    s.update(0.0, dets(7))
    assert s.update(5.0, []) == []                           # gone before confirm_s: silent


def test_count_threshold_rising_edge_and_cooldown():
    s = N.CameraState(cam(min_count=2, cooldown_s=60))
    s.update(0.0, dets(1, 2))
    ev = s.update(1.5, dets(1, 2))
    kinds = [e.kind for e in ev]
    assert kinds.count("count") == 1 and "entered" in kinds
    c = next(e for e in ev if e.kind == "count")
    assert c.trigger and c.counts == {"person": 2} and "2 person" in c.note
    assert [e.kind for e in s.update(10.0, dets(1, 2))] == []           # still 2, inside cooldown: quiet
    ev = s.update(70.0, dets(1, 2))                                     # cooldown passed and still holding: re-alert
    assert [e.kind for e in ev] == ["count"] and "(still)" in ev[0].note


def test_unwatched_labels_do_not_count():
    s = N.CameraState(cam(min_count=1))
    s.update(0.0, dets(1, label="chair"))
    assert s.update(2.0, dets(1, label="chair")) == []
    assert s.counts() == {"chair": 1}                        # still reported in counts, just not alerting


def test_dwell_fires_once_per_track():
    s = N.CameraState(cam(min_count=9, dwell_s=30))
    s.update(0.0, dets(3))
    s.update(1.5, dets(3))
    assert [e.kind for e in s.update(20.0, dets(3))] == []
    ev = s.update(31.0, dets(3))
    assert [e.kind for e in ev] == ["dwell"] and ev[0].trigger and ev[0].track_id == 3
    assert s.update(60.0, dets(3)) == []


def test_after_hours_uses_window_and_cooldown():
    s = N.CameraState(cam(min_count=9, hours="08:00-20:00", cooldown_s=300))
    night, day = datetime(2026, 9, 4, 23, 30), datetime(2026, 9, 4, 12, 0)
    s.update(0.0, dets(1), when=night)
    ev = s.update(1.5, dets(1), when=night)
    assert "after_hours" in [e.kind for e in ev]
    assert "after_hours" not in [e.kind for e in s.update(100.0, dets(1), when=night)]       # cooldown
    assert "after_hours" not in [e.kind for e in s.update(400.0, dets(1), when=day)]         # daytime: fine
    assert "after_hours" in [e.kind for e in s.update(700.0, dets(1), when=night)]


def test_overnight_hours():
    assert N.in_hours("22:00-06:00", datetime(2026, 1, 1, 23, 0))
    assert N.in_hours("22:00-06:00", datetime(2026, 1, 1, 3, 0))
    assert not N.in_hours("22:00-06:00", datetime(2026, 1, 1, 12, 0))
    assert N.in_hours("", datetime(2026, 1, 1, 12, 0)) and N.in_hours("garbage", None)


def test_heartbeat_cadence():
    s = N.CameraState(cam(min_count=9, heartbeat_min=1))
    assert [e.kind for e in s.update(0.0, [])] == ["heartbeat"]
    assert s.update(30.0, []) == []
    hb = s.update(61.0, [])
    assert [e.kind for e in hb] == ["heartbeat"] and hb[0].trigger is False and hb[0].note == "nothing tracked"


def test_config_defaults_and_env(tmp_path, monkeypatch):
    p = tmp_path / "node.json"
    p.write_text(json.dumps({"cameras": [{"source": "0", "watch_for": ["Person", " car "]}]}))
    monkeypatch.setenv("ATLAS_HOOK", "https://x.test/hook/t/vision")
    monkeypatch.setenv("ATLAS_HEALTH_BIND", "0.0.0.0")
    cfg = N.load_config(str(p))
    assert cfg["hook"] == "https://x.test/hook/t/vision" and cfg["device"] == "openvino" and cfg["health_bind"] == "0.0.0.0"
    c = cfg["cameras"][0]
    assert c["name"] == "camera1" and c["watch_for"] == ["person", "car"] and c["cooldown_s"] == 120
    assert N.load_config(str(tmp_path / "missing.json"))["cameras"] == []


def test_poster_bucket_drops_log_events_but_keeps_triggers():
    p = N.Poster("", per_min=2, dry=True)             # not started: nothing drains, so the bucket state is deterministic
    p._stamps = [N.time.time(), N.time.time()]        # bucket full
    assert p.submit({"trigger": False, "camera": "c", "note": "n", "labels": {}}) is False and p.dropped == 1
    assert p.submit({"trigger": True, "camera": "c", "note": "n", "labels": {}}) is True and p.q.qsize() == 1


def test_payload_shape():
    ev = N.Event("count", "3 person at once", True, {"person": 3})
    pl = N.payload({"name": "counter"}, ev, b"\xff\xd8jpeg", "node/openvino")
    assert pl["camera"] == "counter" and pl["labels"] == {"person": 3} and pl["trigger"] is True
    assert pl["note"].startswith("count: ") and pl["backend"] == "node/openvino" and pl["image"]


def test_hook_records_node_backend(cam_client):
    """The desk hook keeps the node's backend string (sanitised) instead of the generic 'external'."""
    c = cam_client[0]
    hook = json.loads(c.get("/api/cameras").data)["hook_url"].replace("http://localhost", "")
    r = c.post(hook, json={"camera": "node-counter", "labels": {"person": 3}, "note": "count: 3 person at once",
                           "trigger": False, "backend": "node/openvino <script>", "kind": "count"})
    assert r.status_code == 200 and json.loads(r.data)["run_id"] == ""
    e = json.loads(c.get("/api/vision/events?camera=node-counter").data)[0]
    assert e["backend"] == "node/openvinoscript" and e["counts"] == {"person": 3} and e["reason"].startswith("count:")
