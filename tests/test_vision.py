"""Cameras as a desk input: rule engine, motion, frame grab, store log, portal API (look / watch / events / ask),
the external-detector hook, the scheduler job and the camera_look / camera_events agent tools. No YOLO, no network:
the detector is faked so the suite runs anywhere (Render has no ultralytics)."""
import io
import json
import time

import pytest
from PIL import Image, ImageDraw

from atlas import vision as V


def J(resp):
    return json.loads(resp.data)


def _jpeg(path, colour=(20, 24, 30), blob=None):
    img = Image.new("RGB", (320, 240), colour)
    if blob:
        ImageDraw.Draw(img).rectangle(blob, fill=(230, 230, 230))
    img.save(path, "JPEG")
    return str(path)


class FakeDetector:
    """Returns whatever the test says, tracks calls; `available` like the real one."""

    def __init__(self, dets=None):
        self.dets = dets or []
        self.error = ""
        self.calls = 0

    @property
    def available(self):
        return True

    def detect(self, jpeg, conf=0.35):
        self.calls += 1
        return list(self.dets)


PERSON = {"label": "person", "conf": 0.91, "box": [40, 30, 120, 220]}
CAR = {"label": "car", "conf": 0.8, "box": [150, 100, 300, 200]}


# ----------------------------------------------------------------------------- pure helpers
def test_rule_evaluate_counts_hours_cooldown():
    cfg = {"watch_for": "person, vehicle", "min_count": 1, "cooldown_min": 10}
    ok, why = V.evaluate(cfg, [PERSON], None, None)
    assert ok and "0 → 1" in why
    # unchanged scene, cooldown not passed → no re-alert
    ok, why = V.evaluate(cfg, [PERSON], {"person": 1}, time.time() - 60)
    assert not ok and "unchanged" in why
    # unchanged scene, cooldown passed → alert again ("still present")
    ok, why = V.evaluate(cfg, [PERSON], {"person": 1}, time.time() - 11 * 60)
    assert ok and "still present" in why
    # count changed but inside cooldown → suppressed, reason says cooldown
    ok, why = V.evaluate(cfg, [PERSON, PERSON], {"person": 1}, time.time() - 60)
    assert not ok and "cooldown" in why
    # below min_count
    assert V.evaluate({"watch_for": "person", "min_count": 2}, [PERSON], None, None)[0] is False
    # synonyms: 'vehicle' → car
    assert V.evaluate(cfg, [CAR], None, None)[0] is True
    # hours window (overnight) — 03:00 inside, 12:00 outside
    from datetime import datetime
    assert V.in_hours("20:00-07:00", datetime(2026, 9, 1, 3, 0)) is True
    assert V.in_hours("20:00-07:00", datetime(2026, 9, 1, 12, 0)) is False
    assert V.in_hours("09:00-17:00", datetime(2026, 9, 1, 12, 0)) is True
    assert V.in_hours("", datetime(2026, 9, 1, 12, 0)) is True
    # alert_on_motion: scene change wakes the desk even with no watched objects (never on the very first frame)
    m = {"watch_for": "person", "alert_on_motion": 0.2}
    assert V.evaluate(m, [], None, None, mot=0.5)[0] is False
    ok, why = V.evaluate(m, [], {}, None, mot=0.5)
    assert ok and "scene changed" in why
    assert V.evaluate(m, [], {}, None, mot=0.1)[0] is False
    assert V.evaluate(m, [], {}, time.time() - 30, mot=0.5)[0] is False        # default 10-min cooldown
    noon = datetime(2026, 9, 1, 12, 0).timestamp()
    ok, why = V.evaluate({"watch_for": "person", "hours": "20:00-07:00"}, [PERSON], None, None, now_ts=noon)
    assert not ok and "outside" in why


def test_counts_text_and_motion_and_annotate(tmp_path):
    assert V.counts_text({"person": 1}) == "1 person"
    assert V.counts_text({"person": 3, "car": 1}) == "3 people, 1 car"
    assert V.counts_text({"truck": 2}) == "2 trucks"
    assert V.counts_text({}) == "nothing recognised"
    a = open(_jpeg(tmp_path / "a.jpg"), "rb").read()
    b = open(_jpeg(tmp_path / "b.jpg", blob=(20, 20, 200, 200)), "rb").read()
    assert V.motion(a, a) < 0.01
    assert V.motion(a, b) > 0.15
    assert V.motion(None, a) == 1.0
    out = V.annotate(b, [PERSON], "banner")
    assert out[:2] == b"\xff\xd8" and Image.open(io.BytesIO(out)).size == (320, 240)


def test_grab_file_and_source_kind(tmp_path):
    p = _jpeg(tmp_path / "cam.jpg")
    assert V.source_kind("0") == "webcam" and V.source_kind("rtsp://x") == "rtsp" and V.source_kind("http://x/a.jpg") == "http" and V.source_kind(p) == "file"
    jpeg = V.grab(p)
    assert jpeg[:2] == b"\xff\xd8" and V.frame_size(jpeg) == (320, 240)
    with pytest.raises(RuntimeError):
        V.grab(str(tmp_path / "missing.jpg"))
    # big frames are shrunk to MAX_SIDE
    Image.new("RGB", (2400, 1200)).save(tmp_path / "big.png")
    assert max(V.frame_size(V.grab(str(tmp_path / "big.png")))) == V.MAX_SIDE


def test_vlm_detect_parses_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "describe", lambda *a, **k: 'Sure: {"person": 2, "car": 0, "dog": "x"}')
    dets = V.vlm_detect(b"\xff\xd8", ["person", "car"])
    assert V.counts(dets) == {"person": 2}
    monkeypatch.setattr(V, "describe", lambda *a, **k: "I cannot tell")
    assert V.vlm_detect(b"\xff\xd8", ["person"]) == []


def test_analyse_falls_back_without_detector(monkeypatch, tmp_path):
    p = _jpeg(tmp_path / "cam.jpg")
    d = FakeDetector([PERSON, CAR])
    res = V.analyse(p, {"watch_for": "person"}, None, live_vlm=False, detector=d)
    assert res["backend"] == "yolo" and res["counts"] == {"person": 1, "car": 1} and res["annotated"][:2] == b"\xff\xd8"

    class NoDet(FakeDetector):
        @property
        def available(self):
            return False
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    res = V.analyse(p, {}, None, live_vlm=True, detector=NoDet())
    assert res["backend"] == "none" and res["counts"] == {}
    # with a key, the vision model counts (motion gate passes on first frame)
    monkeypatch.setattr(V, "vlm_ready", lambda: True)
    monkeypatch.setattr(V, "vlm_detect", lambda jpeg, labels, model="", transport=None: [PERSON])
    res = V.analyse(p, {"watch_for": "person"}, None, live_vlm=True, detector=NoDet())
    assert res["backend"] == "vlm" and res["counts"] == {"person": 1}


def test_describe_uses_openai_image_message(monkeypatch):
    import httpx
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Two people at the door."}}]})
    monkeypatch.setattr(V, "vlm_chain", lambda model="": [
        {"name": "test", "base": "https://api.example/v1", "key": "k123", "model": model or "vision-x"}])
    out = V.describe(b"\xff\xd8\xff", "Who is there?", transport=httpx.MockTransport(handler))
    assert out == "Two people at the door."
    msg = seen["body"]["messages"][1]["content"]
    assert msg[0]["text"] == "Who is there?" and msg[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert seen["auth"] == "Bearer k123" and seen["body"]["model"] == "vision-x"


# ----------------------------------------------------------------------------- store
def test_store_vision_events(store):
    e1 = store.add_vision_event(1, "door", {"person": 2}, motion=0.3, reason="count 0 → 2", answer="two adults", triggered=True)
    e2 = store.add_vision_event(1, "yard", {"truck": 1}, motion=0.5, reason="delivery")
    store.add_vision_event(2, "door", {"person": 9})            # another desk
    assert e1["counts"] == {"person": 2} and e1["triggered"] == 1
    assert [e["camera"] for e in store.vision_events(1)] == ["yard", "door"]
    assert [e["id"] for e in store.vision_events(1, camera="door")] == [e1["id"]]
    assert [e["id"] for e in store.vision_events(1, query="truck")] == [e2["id"]]
    assert [e["id"] for e in store.vision_events(1, triggered_only=True)] == [e1["id"]]
    assert store.last_vision_event(1, "door")["id"] == e1["id"]
    assert store.last_vision_event(1, "nope") is None
    store.set_vision_run(e2["id"], "run-1")
    assert store.vision_event(e2["id"])["run_id"] == "run-1" and store.vision_event(e2["id"])["triggered"] == 1
    assert store.vision_stats(1, 0) == {"events": 2, "triggered": 2}
    ds = store.for_desk(1)
    assert len(ds.vision_events()) == 2 and ds.vision_stats(0)["events"] == 2
    store.delete_desk_data(1)
    assert store.vision_events(1) == [] and len(store.vision_events(2)) == 1


def test_connector_masking_hides_camera_credentials():
    from atlas import integrations as I
    m = I.mask({"source": "rtsp://admin:12345@10.0.0.5:554/Streaming/Channels/101", "watch_for": "person"})
    assert "12345" not in m["source"] and m["source"].startswith("rtsp://admin:") and m["watch_for"] == "person"
    kept = I.merge_secrets({"source": "rtsp://admin:12345@h/x"}, {"source": m["source"], "watch_for": "car"})
    assert kept["source"] == "rtsp://admin:12345@h/x" and kept["watch_for"] == "car"
    assert "camera" in I.KINDS and "source" in I.KINDS["camera"]["fields"]
    assert "camera_look" in I.describe([{"name": "door", "kind": "camera", "config": {"watch_for": "person", "hours": "20:00-07:00"}, "auto": 0}])


# ----------------------------------------------------------------------------- portal flow
@pytest.fixture()
def cam_client(app_client, tmp_path, monkeypatch):
    """Logged-in client on a fresh site_watch desk with one file-backed camera and a fake detector."""
    import atlas.desk.app as A
    import atlas.desk.scheduler as S
    c = app_client
    c.post("/signup", json={"name": "Cam Owner", "company": "Shop", "email": "cam@example.com", "password": "password1"})
    c.post("/login", json={"email": "cam@example.com", "password": "password1"})
    desk = J(c.post("/api/desks", json={"name": "Corner Shop", "template": "site_watch", "tier": "free"}))
    assert desk["template"] == "site_watch"
    fake = FakeDetector([PERSON])
    monkeypatch.setattr(V, "DETECTOR", fake)
    monkeypatch.setattr(V, "SNAP_DIR", tmp_path / "snaps")
    S._last_frame.clear(); S._last_seen.clear()
    src = _jpeg(tmp_path / "door.jpg", blob=(40, 30, 120, 220))
    conn = J(c.post("/api/connectors", json={"kind": "camera", "name": "front-door",
                                             "config": {"source": src, "watch_for": "person", "cooldown_min": "0",
                                                        "question": "Is anyone at the door?", "notes": "Main entrance"}}))
    assert conn["kind"] == "camera"
    return c, desk, conn, fake, src


def _wait_idle(c, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not J(c.get("/api/live"))["runs"]:
            return True
        time.sleep(0.3)
    return False


def test_camera_look_watch_events_ask(cam_client):
    c, desk, conn, fake, src = cam_client
    R = J(c.get("/api/cameras"))
    assert R["cameras"][0]["name"] == "front-door" and R["cameras"][0]["source_kind"] == "file"
    assert R["detector"]["available"] is True and R["hook_url"].endswith("/vision") and R["mode"] == "demo"
    assert R["cameras"][0]["rule"]["labels"] == ["person"] and R["cameras"][0]["seen"] == {}

    # look: first frame → person count 0 → 1 → the rule fires and the desk is woken
    r = J(c.post(f"/api/cameras/{conn['id']}/look", json={}))
    assert r["ok"] and r["counts"] == {"person": 1} and r["backend"] == "yolo" and r["triggered"] and r["run_id"]
    assert "annotated" not in r and r["answer"].startswith("demo mode")
    assert _wait_idle(c)
    run = J(c.get(f"/api/runs/{r['run_id']}"))
    assert "CAMERA ALERT" in run["task"] and "front-door" in run["task"]

    # frame + snapshot + events
    f = c.get(f"/api/cameras/{conn['id']}/frame.jpg")
    assert f.status_code == 200 and f.mimetype == "image/jpeg" and f.data[:2] == b"\xff\xd8"
    E = J(c.get("/api/vision/events?hours=1"))
    assert len(E) == 1 and E[0]["triggered"] == 1 and E[0]["seen"] == "1 person" and E[0]["snapshot_url"]
    snap = c.get(E[0]["snapshot_url"])
    assert snap.status_code == 200 and snap.data[:2] == b"\xff\xd8"
    assert J(c.get("/api/vision/events?alerts=1")) and J(c.get("/api/vision/events?camera=nope")) == []

    # same scene again, cooldown 0 → "still present" alert; a question is answered (demo) and logged
    r2 = J(c.post(f"/api/cameras/{conn['id']}/look", json={"question": "How many at the door?"}))
    assert r2["ok"] and r2["triggered"] and "still present" in r2["reason"] and r2["answer"]
    assert _wait_idle(c)
    assert J(c.get("/api/vision/events?q=door"))[0]["question"] == "How many at the door?"

    # ask the cameras (demo answer is deterministic and cites the log)
    a = J(c.post("/api/vision/ask", json={"question": "Was anyone at the front door?", "hours": 24}))
    assert "front-door" in a["answer"] and a["evidence"] and a["events_considered"] >= 2
    assert "nothing" in J(c.post("/api/vision/ask", json={"question": "x", "camera": "nope"}))["answer"]
    assert c.post("/api/vision/ask", json={}).status_code == 400

    # watch toggle creates / removes a camera_watch job
    w = J(c.post(f"/api/cameras/{conn['id']}/watch", json={"on": True, "every_s": 30}))
    assert w["watching"] and w["job"]["kind"] == "camera_watch" and json.loads(w["job"]["task"])["every_s"] == 30
    assert J(c.get("/api/cameras"))["cameras"][0]["watch_job"]["id"] == w["job"]["id"]
    assert J(c.post(f"/api/cameras/{conn['id']}/watch", json={"on": True}))["job"]["id"] == w["job"]["id"]   # idempotent
    assert J(c.post(f"/api/cameras/{conn['id']}/watch", json={"on": False}))["watching"] is False
    assert J(c.get("/api/cameras"))["cameras"][0]["watch_job"] is None

    # broken source → 400 with a readable error, connector status updated
    c.patch(f"/api/connectors/{conn['id']}", json={"config": {"source": str(src) + ".missing"}})
    bad = c.post(f"/api/cameras/{conn['id']}/look", json={})
    assert bad.status_code == 400 and "not found" in J(bad)["error"]
    assert J(c.get("/api/connectors"))["connectors"][0]["status"].startswith("error")


def test_scheduler_camera_watch_job(cam_client):
    import atlas.desk.app as A
    import atlas.desk.scheduler as S
    c, desk, conn, fake, src = cam_client
    job = A.store.add_job(desk["id"], "camera_watch", "Watch", json.dumps({"connector": "front-door", "every_s": 25}), 1, 0)
    out = S._run_job(A.store, job, A._start_run, A.store.desk)
    assert out.startswith("front-door: 1 person") and "→ run" in out
    assert _wait_idle(c)
    # scene unchanged, cooldown 0 → alert again; then with no person, the count drop is logged but does not wake the desk
    fake.dets = []
    out = S._run_job(A.store, job, A._start_run, A.store.desk)
    assert out == "front-door: nothing recognised"
    ev = J(c.get("/api/vision/events?hours=1"))
    assert ev[0]["counts"] == {} and ev[0]["triggered"] == 0 and "0" in ev[0]["reason"]
    # job pointed at a camera that does not exist → skipped wording
    job2 = A.store.add_job(desk["id"], "camera_watch", "Watch", json.dumps({"connector": "nope"}), 1, 0)
    assert S._run_job(A.store, job2, A._start_run, A.store.desk) == "no camera connector"
    # grab failure is logged as an event, not raised
    fake.dets = [PERSON]
    c.patch(f"/api/connectors/{conn['id']}", json={"config": {"source": str(src) + ".gone"}})
    out = S._run_job(A.store, job, A._start_run, A.store.desk)
    assert "ERROR" in out and J(c.get("/api/vision/events?hours=1"))[0]["backend"] == "error"


def test_external_vision_hook(cam_client):
    c, desk, conn, fake, src = cam_client
    R = J(c.get("/api/cameras"))
    hook = R["hook_url"].replace("http://localhost", "")
    assert J(c.get(hook))["ok"]
    r = J(c.post(hook, json={"camera": "pir-backdoor", "labels": ["person", "person"], "note": "PIR triggered", "motion": 0.4}))
    assert r["ok"] and r["run_id"] and r["event_id"]
    assert _wait_idle(c)
    e = J(c.get("/api/vision/events?camera=pir-backdoor"))[0]
    assert e["counts"] == {"person": 2} and e["source"] == "hook" and e["backend"] == "external" and e["triggered"] == 1
    run = J(c.get(f"/api/runs/{r['run_id']}"))
    assert "EXTERNAL EVENT" in run["task"] and "PIR triggered" in run["task"]
    # trigger=false just logs; dict labels; base64 image becomes a snapshot
    import base64
    img_b64 = base64.b64encode(open(src, "rb").read()).decode()
    r2 = J(c.post(hook, json={"camera": "esp32-cam", "labels": {"car": 1}, "trigger": False, "image": img_b64}))
    assert r2["ok"] and r2["run_id"] == ""
    e2 = J(c.get("/api/vision/events?camera=esp32-cam"))[0]
    assert e2["triggered"] == 0 and e2["counts"] == {"car": 1} and e2["snapshot_url"]
    assert c.get(e2["snapshot_url"]).status_code == 200
    assert c.post("/hook/not-a-token/vision", json={}).status_code == 404


def test_agent_camera_tools(cam_client, tmp_path):
    """camera_look / camera_events through the orchestrator's tool dispatch (what an agent actually calls)."""
    import atlas.desk.app as A
    from atlas.orchestrator import Orchestrator
    from atlas.providers import ToolCall
    c, desk, conn, fake, src = cam_client
    d = A.store.desk(desk["id"])
    configs = A.desk_configs(d)
    orch = Orchestrator(configs, A.store.for_desk(desk["id"]), lambda ev: None)
    orch.run_id = "test-run"
    orch.run_dir = tmp_path / "run"; orch.run_dir.mkdir()
    atlas_agent = orch.agents["atlas"]
    assert "camera_look" in atlas_agent["tools"] and "camera_events" in atlas_agent["tools"]
    assert "front-door" in orch.system_prompt(atlas_agent) and "camera_look" in orch.system_prompt(atlas_agent)

    out = orch._tool(atlas_agent, ToolCall(id="1", name="camera_look", args={"camera": "front-door", "question": "Anyone there?"}), 0)
    assert out.startswith("Camera front-door") and "1 person" in out and "detector: yolo" in out and "Event #" in out
    assert "Analyst:" in out                                         # question asked → analyst line present (demo: unavailable)
    assert list(orch.run_dir.glob("camera-front-door-*.jpg")) and orch.deliverables
    assert "no camera called 'nope'" in orch._tool(atlas_agent, ToolCall(id="2", name="camera_look", args={"camera": "nope"}), 0)

    out = orch._tool(atlas_agent, ToolCall(id="3", name="camera_events", args={"hours": 1}), 0)
    assert "front-door" in out and "1 person" in out and "camera_look by atlas" in out
    assert orch._tool(atlas_agent, ToolCall(id="4", name="camera_events", args={"query": "zebra"}), 0).startswith("No camera events")
    # the specialist 'watcher' has the tools too; the comms agent does not
    assert "camera_look" in orch.agents["watcher"]["tools"] and "camera_look" not in orch.agents["comms"]["tools"]


def test_site_watch_template_and_seed(cam_client):
    from atlas import templates as T
    c, desk, conn, fake, src = cam_client
    assert any(t["id"] == "site_watch" for t in T.DESK_TYPES) and "site_watch" in T.SAMPLE_LEADS
    t = T.get("site_watch")
    assert [a["id"] for a in t["agents"]] == ["atlas", "watcher", "comms"] and [w["id"] for w in t["workflows"]] == ["camera_alert", "daily_digest"]
    ids = J(c.post("/api/demo/seed"))
    assert len(ids) == 3 and _wait_idle(c)
    assert all(r["status"] == "done" for r in J(c.get("/api/runs"))[:3])


def test_site_demo_ask_retrieves_and_streams(app_client, monkeypatch):
    """Public RAG demo: bad input -> 400 with an SSE error; good input -> retrieval picks matching events, the
    model call is made with the log in the system prompt and the answer streams back after a {"used": [...]} line."""
    import atlas.desk.app as A
    c = app_client
    r = c.post("/api/vision/demo/ask", json={"question": "hi"})
    assert r.status_code == 400 and b"ask a question" in r.data
    r = c.post("/api/vision/demo/ask", json={"question": "who was in the stock room?", "events": []})
    assert r.status_code == 400 and b"no events" in r.data
    seen = {}

    def fake_stream(base, headers, payload, model, first=None):
        seen.update(payload=payload, model=model, first=first)
        yield A._sse(first)
        yield A._sse({"t": "Two people [#2].", "model": "m"})
        yield A._sse({"done": True, "model": "m"})
    monkeypatch.setattr(A, "_demo_cfg", lambda: ("https://x", "key", "test/model:free"))
    monkeypatch.setattr(A, "_demo_stream", fake_stream)
    events = [{"n": 1, "time": "10:00:01", "counts": {"person": 1}, "text": "Aisle 4: one shopper at the shelf"},
              {"n": 2, "time": "10:00:07", "counts": {"person": 2}, "text": "Stock room: two people moving boxes"},
              {"n": 3, "time": "10:00:13", "counts": {}, "text": "Fridges: empty aisle"}]
    r = c.post("/api/vision/demo/ask", json={"question": "who was in the stock room?", "events": events})
    assert r.status_code == 200 and r.mimetype == "text/event-stream"
    body = r.data.decode()
    assert '"used": [1, 2, 3]' in body and '"of": 3' in body and "Two people [#2]." in body
    sysmsg = seen["payload"]["messages"][0]["content"]
    assert "[#2] 10:00:07 | 2 person | analyst: Stock room: two people moving boxes" in sysmsg
    assert seen["payload"]["messages"][1]["content"] == "who was in the stock room?"
    assert seen["payload"]["stream"] is True
