"""Live camera feeds: real-time decode loop with detections, MJPEG delivery, grab() sharing the feed's frame,
the journal's live bus and token-streamed notes, and the SSE endpoint."""
import json
import queue
import threading
import time

import pytest

from atlas import journal as JR
from atlas import live as LIVE
from atlas import vision as V
from test_vision import PERSON, FakeDetector, _jpeg


@pytest.fixture()
def clip(tmp_path):
    """A tiny 2-second synthetic recording (a moving block), written with OpenCV."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    path = tmp_path / "clip.mp4"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
    assert w.isOpened()
    for i in range(20):
        f = np.full((240, 320, 3), 30, dtype=np.uint8)
        cv2.rectangle(f, (10 + i * 10, 60), (60 + i * 10, 180), (200, 200, 200), -1)
        w.write(f)
    w.release()
    return str(path)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    LIVE.stop_all()
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    JR.reset()
    yield
    LIVE.stop_all()
    JR.reset()


def _wait(pred, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_feed_decodes_in_real_time_and_loops(clip):
    f = LIVE.open(clip, "test cam")
    assert _wait(lambda: f.seq >= 5), f.error
    assert f.running and f.size == (320, 240) and f.error == ""
    seq0, jpeg = f.wait_frame(0)
    assert jpeg[:2] == b"\xff\xd8" and seq0 >= 5
    # playback is paced by the wall clock: ~10 fps source, so well under 100 frames in a second
    a = f.seq
    time.sleep(1.0)
    assert 3 <= f.seq - a <= 30
    # a 2 s clip loops rather than ending
    assert _wait(lambda: f.seq >= 25, timeout=6)
    assert f.running
    assert LIVE.get(clip) is f
    f.stop()
    assert not f.running and LIVE.get(clip) is None


def test_feed_without_detector_still_streams(clip, monkeypatch):
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([]))
    f = LIVE.open(clip)
    assert _wait(lambda: f.seq >= 3), f.error
    assert f.counts == {}


def test_mjpeg_generator_yields_only_new_frames(clip):
    f = LIVE.open(clip)
    assert _wait(lambda: f.seq >= 2), f.error
    gen = LIVE.mjpeg(f)
    parts = []
    t0 = time.time()
    for chunk in gen:
        parts.append(chunk)
        if len(parts) >= 4 or time.time() - t0 > 6:
            break
    gen.close()
    assert len(parts) >= 4
    for p in parts:
        assert p.startswith(b"--atlasframe\r\nContent-Type: image/jpeg\r\nContent-Length: ")
        body = p.split(b"\r\n\r\n", 1)[1]
        assert body[:2] == b"\xff\xd8"
    assert len({p[-2000:] for p in parts}) == len(parts)     # each part is a different frame
    assert f.viewers == 0                                    # generator close detached the viewer


def test_grab_returns_the_live_frame_when_a_feed_runs(clip, monkeypatch):
    calls = []
    real = V._grab_video
    monkeypatch.setattr(V, "_grab_video", lambda p: (calls.append(p), real(p))[1])
    f = LIVE.open(clip)
    assert _wait(lambda: f.seq >= 2), f.error
    jpeg = V.grab(clip)
    assert jpeg[:2] == b"\xff\xd8" and calls == []           # no ffmpeg decode: the feed's frame was used
    f.stop()
    jpeg2 = V.grab(clip)
    assert jpeg2[:2] == b"\xff\xd8" and calls == [clip]      # feed gone: one-off decode as before


def test_idle_feed_stops_itself(clip, monkeypatch):
    monkeypatch.setattr(LIVE, "IDLE_S", 0.5)
    f = LIVE.open(clip)
    assert _wait(lambda: f.seq >= 2), f.error
    assert _wait(lambda: not f.running, timeout=6)
    assert LIVE.get(clip) is None


def test_bad_source_reports_error():
    f = LIVE.open(r"C:\nowhere\missing.mp4")
    assert _wait(lambda: not f.running, timeout=5)
    assert "not found" in f.error


# --------------------------------------------------------------------------- journal live bus
def test_bus_publish_subscribe_and_full_queue():
    q = JR.subscribe(7)
    other = JR.subscribe(8)
    assert JR.subscribers(7) == 1
    ev = JR.publish(7, "tick", "cam", counts={"person": 1})
    assert ev["kind"] == "tick" and q.get_nowait()["counts"] == {"person": 1}
    assert other.empty()
    for i in range(JR._BUS_MAX + 50):                        # a stalled listener never blocks the publisher
        JR.publish(7, "note_delta", "cam", text="x")
    assert q.qsize() == JR._BUS_MAX
    JR.unsubscribe(7, q)
    JR.unsubscribe(8, other)
    assert JR.subscribers(7) == 0
    JR.unsubscribe(7, q)                                     # idempotent


class FakeStream:
    """Stands in for V.chat_images_stream: meta first, then the note in pieces."""
    def __init__(self, text="Two adults at the counter, one in a red coat holding a bag \u2014 waiting."):
        self.text = text
        self.calls = 0

    def __call__(self, system, text, images, model="", max_tokens=500, transport=None):
        self.calls += 1
        yield {"provider": "fake", "model": "fake-vl"}
        for word in self.text.split(" "):
            yield word + " "


def test_write_note_streams_when_someone_listens(tmp_path, monkeypatch):
    fs = FakeStream()
    monkeypatch.setattr(V, "chat_images_stream", fs)
    monkeypatch.setattr(V, "chat_images", lambda *a, **k: pytest.fail("non-streaming path used while a listener exists"))
    q = JR.subscribe(1)
    jpeg = _jpeg(tmp_path / "a.jpg")
    text = JR.write_note((1, "till"), "till", JR.config({"journal": "1"}), jpeg, {"person": 2}, why="first note")
    JR.unsubscribe(1, q)
    assert fs.calls == 1
    assert "\u2014" not in text and "red coat" in text        # em dash scrubbed, content kept
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait())
    ks = [e["kind"] for e in kinds]
    assert ks[0] == "note_start" and kinds[0]["why"] == "first note" and kinds[0]["counts"] == {"person": 2}
    assert ks[1] == "note_model" and kinds[1]["model"] == "fake-vl"
    assert ks.count("note_delta") == len(fs.text.split(" "))
    assert ks[-1] == "note_done" and kinds[-1]["text"] == text and kinds[-1]["took_s"] >= 0
    assert "".join(e["text"] for e in kinds if e["kind"] == "note_delta").strip() == fs.text


def test_write_note_blocks_when_nobody_listens(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "chat_images_stream", lambda *a, **k: pytest.fail("streamed with no listener"))
    monkeypatch.setattr(V, "chat_images", lambda *a, **k: "One adult at the door.")
    jpeg = _jpeg(tmp_path / "a.jpg")
    assert JR.write_note((2, "door"), "door", JR.config({"journal": "1"}), jpeg, {"person": 1}) == "One adult at the door."


def test_write_note_error_is_published(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vision model HTTP 429: slow down")
        yield  # noqa
    monkeypatch.setattr(V, "chat_images_stream", boom)
    q = JR.subscribe(3)
    with pytest.raises(RuntimeError):
        JR.write_note((3, "cam"), "cam", JR.config({"journal": "1"}), _jpeg(tmp_path / "a.jpg"), {}, why="x")
    JR.unsubscribe(3, q)
    kinds = [q.get_nowait()["kind"] for _ in range(q.qsize())]
    assert kinds == ["note_start", "note_error"]


# --------------------------------------------------------------------------- HTTP
def _desk(app_client):
    import atlas.desk.app as A
    A.OPEN = True
    c = app_client
    r = c.get("/api/desks").get_json()
    if not r.get("current"):
        c.post("/api/desks", json={"name": "Live test", "template": "sales_desk"})
    return c


def test_live_endpoints(app_client, clip, monkeypatch):
    c = _desk(app_client)
    cam = c.post("/api/connectors", json={"kind": "camera", "name": "live cam", "config": {"source": clip, "journal": "1"}}).get_json()
    cid = cam["id"]
    st = c.get(f"/api/cameras/{cid}/live").get_json()
    assert st["ok"] and st["live"]["running"] is False and st["url"].endswith(f"/api/cameras/{cid}/live.mjpg")
    st = c.get(f"/api/cameras/{cid}/live?start=1").get_json()
    assert st["live"]["running"] is True
    f = LIVE.get(clip)
    assert _wait(lambda: f.seq >= 2), f.error
    r = c.get(f"/api/cameras/{cid}/live.mjpg?fps=5")
    assert r.status_code == 200 and r.mimetype == "multipart/x-mixed-replace"
    it = r.iter_encoded()
    first = next(it)
    assert first.startswith(b"--atlasframe\r\nContent-Type: image/jpeg")
    r.close()
    r = c.post(f"/api/cameras/{cid}/live", json={"on": False}).get_json()
    assert r["ok"] and r["live"]["running"] is False
    assert _wait(lambda: LIVE.get(clip) is None)
    # unknown camera / other desk
    assert c.get("/api/cameras/9999/live").status_code == 404
    assert c.get("/api/cameras/9999/live.mjpg").status_code == 404


def test_journal_sse_endpoint(app_client):
    c = _desk(app_client)
    desk_id = c.get("/api/desks").get_json()["current"]
    r = c.get("/api/vision/journal/stream?camera=till")
    assert r.status_code == 200 and r.mimetype == "text/event-stream"
    it = r.iter_encoded()
    assert next(it).startswith(b"retry:")
    hello = json.loads(next(it).decode().split("data: ", 1)[1])
    assert hello["kind"] == "hello" and hello["camera"] == "till" and hello["listeners"] >= 1
    JR.publish(desk_id, "tick", "other", counts={})                 # filtered out
    JR.publish(desk_id, "note_delta", "till", text="hello ")
    ev = json.loads(next(it).decode().split("data: ", 1)[1])
    assert ev["kind"] == "note_delta" and ev["text"] == "hello "
    r.close()
    assert _wait(lambda: JR.subscribers(desk_id) == 0)
