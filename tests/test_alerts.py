"""Camera alerts reach a person: one message per firing into the approval queue (or sent at once), a per-hour budget
that turns a storm into one digest, quiet hours, and the daily report queued each morning."""
import json
import time

import atlas.desk.scheduler as S
from atlas import alerts as AL
from atlas import journal as JR
from atlas import vision as V
from test_vision import PERSON, FakeDetector, _jpeg


def _desk(store, notify):
    return store.add_desk(0, "Cafe", "blank", "free", {"business": {"name": "Cafe Lumière"}, "notify": notify})


def _cam(store, desk, src, **cfg):
    return store.add_connector(desk["id"], "camera", "door", {"source": src, "watch_for": "person", "min_count": 1,
                                                              "cooldown_min": 0, "repeat": "always", **cfg}, False)


def _tick(store, desk, conn, runs):
    """force=True: the rule's cooldown cannot be zero, and these tests fire the same camera again and again."""
    return S.camera_tick(store, desk, conn, lambda d, t, m: runs.append(t) or "run1", True, force=True)


def test_config_and_clean():
    assert AL.config({"config": {}}) == {"channel": "", "to": "", "auto": False, "quiet": "", "per_hour": 6, "run": True,
                                          "report": True, "report_time": "07:00"}
    n = AL.clean({"channel": "WhatsApp", "to": "+44 7700 900123", "per_hour": "3", "quiet": "23:00-07:00", "report_time": "06:30"})
    assert n["channel"] == "whatsapp" and n["per_hour"] == 3 and n["run"] is False and n["report_time"] == "06:30"
    assert AL.clean({"channel": "pigeon"}).startswith("channel must be")
    assert AL.clean({"channel": "sms", "to": "07700"}).startswith("to must be an international")
    assert AL.clean({"channel": "email", "to": "nope"}).startswith("to must be an email")
    assert AL.clean({"channel": "slack", "quiet": "late"}).startswith("quiet must be")
    assert AL.clean({"channel": "slack", "per_hour": "many"}) == "per_hour must be a number"
    assert AL.config({"config": {"notify": {"channel": "slack"}}})["run"] is False    # a channel set: no run unless asked


def test_alert_becomes_a_pending_message_not_a_run(store, tmp_path, monkeypatch):
    AL.reset(); S._last_frame.clear(); S._last_seen.clear(); S._present.clear()
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    monkeypatch.setattr(S, "BASE_URL", lambda: "https://desk.example.com")
    desk = _desk(store, {"channel": "whatsapp", "to": "+447700900123", "per_hour": 6})
    conn = _cam(store, desk, _jpeg(tmp_path / "door.jpg", blob=(40, 30, 120, 220)), notes="back door")
    runs = []
    r = _tick(store, desk, conn, runs)
    assert r["triggered"] and r["run_id"] == "" and runs == []                      # no agent run: a person is told
    acts = store.for_desk(desk["id"]).actions()
    assert len(acts) == 1 and acts[0]["kind"] == "whatsapp" and acts[0]["to"] == "+447700900123" and acts[0]["status"] == "pending"
    assert acts[0]["subject"].startswith("door: ") and acts[0]["agent"] == "cameras"
    body = acts[0]["body"]
    assert "Camera door" in body and "Seen: 1 person" in body and "Snapshot: https://desk.example.com/api/vision/snapshot/" in body
    ev = store.last_vision_event(desk["id"], "door")
    assert ev["run_id"] == f"action:{acts[0]['id']}" and ev["triggered"]
    # run: true keeps the old behaviour as well as the message
    store.update_desk(desk["id"], config={**desk["config"], "notify": {"channel": "whatsapp", "to": "+447700900123", "run": True}})
    desk = store.desk(desk["id"])
    r = _tick(store, desk, conn, runs)
    assert r["triggered"] and r["run_id"] == "run1" and len(runs) == 1 and len(store.for_desk(desk["id"]).actions()) == 2


def test_auto_sends_through_the_dispatcher(store, tmp_path, monkeypatch):
    AL.reset(); S._last_frame.clear(); S._last_seen.clear(); S._present.clear()
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    sent = []
    monkeypatch.setattr(S, "DISPATCH", lambda desk, row: sent.append(row) or "[slack ok]")
    desk = _desk(store, {"channel": "slack", "auto": True})
    conn = _cam(store, desk, _jpeg(tmp_path / "d.jpg", blob=(40, 30, 120, 220)))
    _tick(store, desk, conn, [])
    a = store.for_desk(desk["id"]).actions()[0]
    assert a["status"] == "sent" and a["decided_by"] == "auto" and a["note"] == "[slack ok]" and sent[0]["id"] == a["id"]
    # a failing send is recorded, never raised into the camera loop
    monkeypatch.setattr(S, "DISPATCH", lambda desk, row: (_ for _ in ()).throw(RuntimeError("webhook 500")))
    S._last_seen.clear()
    _tick(store, desk, conn, [])
    a = store.for_desk(desk["id"]).actions()[0]
    assert a["status"] == "failed" and "webhook 500" in a["note"]


def test_budget_mutes_a_storm_into_one_digest(store, tmp_path, monkeypatch):
    AL.reset(); S._last_frame.clear(); S._last_seen.clear(); S._present.clear()
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    desk = _desk(store, {"channel": "sms", "to": "+447700900123", "per_hour": 2})
    conn = _cam(store, desk, _jpeg(tmp_path / "d.jpg", blob=(40, 30, 120, 220)))
    ds = store.for_desk(desk["id"])
    for _ in range(5):
        S._last_seen.clear()
        _tick(store, desk, conn, [])
    acts = ds.actions()
    assert len(acts) == 2 and all(a["kind"] == "sms" for a in acts)                 # 2 sent, 3 held
    muted = [e for e in ds.vision_events(camera="door", limit=20) if "(muted: over 2/h)" in (e.get("reason") or "")]
    assert len(muted) == 3
    # an hour later the next tick flushes the digest
    with AL._lock:
        AL._muted[(desk["id"], "door")]["since"] -= 3601
    S._last_seen.clear()
    _tick(store, desk, conn, [])
    acts = ds.actions()
    digest = [a for a in acts if "muted" in a["subject"]]
    assert len(digest) == 1 and "3 more alerts" in digest[0]["subject"] and "held back" in digest[0]["body"]
    assert "Last one: " in digest[0]["body"] and "1 person" in digest[0]["body"]


def test_quiet_hours_log_but_do_not_send(store, tmp_path, monkeypatch):
    AL.reset(); S._last_frame.clear(); S._last_seen.clear(); S._present.clear()
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    monkeypatch.setattr(V, "in_hours", lambda spec, now=None: spec == "22:00-06:00")
    desk = _desk(store, {"channel": "email", "to": "boss@example.com", "quiet": "22:00-06:00"})
    conn = _cam(store, desk, _jpeg(tmp_path / "d.jpg", blob=(40, 30, 120, 220)))
    r = _tick(store, desk, conn, [])
    assert r["triggered"] and store.for_desk(desk["id"]).actions() == []
    assert "(quiet hours: not sent)" in store.last_vision_event(desk["id"], "door")["reason"]


def test_daily_report_job(store, monkeypatch):
    desk = _desk(store, {"channel": "whatsapp", "to": "+447700900123", "report_time": "07:00"})
    job = AL.ensure_report_job(store, desk)
    assert job and job["kind"] == "daily_report" and job["every_min"] == 1440 and job["next_run"] > time.time()
    assert time.strftime("%H:%M", time.localtime(job["next_run"])) == "07:00"
    assert AL.ensure_report_job(store, desk)["id"] == job["id"]                      # idempotent
    ds = store.for_desk(desk["id"])
    ds.add_vision_event("door", {"person": 2}, reason="2 people", answer="Two people at the back door", triggered=True)
    out = S._run_job(store, store.job(job["id"]), lambda *a: "x", store.desk)
    assert out.startswith("report ") and "-> whatsapp pending" in out
    a = ds.actions()[0]
    assert a["kind"] == "whatsapp" and a["subject"].startswith("Camera report, Cafe Lumière, ") and "• " not in a["body"][:2]
    assert "Alerts" in a["body"] and "**" not in a["body"] and len(a["body"]) <= 3800
    # reports off -> the job goes away
    store.update_desk(desk["id"], config={**desk["config"], "notify": {"channel": "whatsapp", "to": "+447700900123", "report": False}})
    assert AL.ensure_report_job(store, store.desk(desk["id"])) is None and [j for j in ds.jobs() if j["kind"] == "daily_report"] == []


def test_messages():
    ev = {"id": 12}
    subj, body = AL.alert_message("till", "3 people waiting over 2 min", {"person": 3}, "Queue at the till", "note text", ev,
                                  known=[{"name": "Marco", "sure": True}, {"name": "Aisha", "sure": False}], base_url="http://x", when=0)
    assert subj == "till: 3 people waiting over 2 min" and "Known: Marco, Aisha (likely)." in body
    assert "Analyst: Queue at the till" in body and "Journal: note text" in body and "http://x/api/vision/snapshot/12" in body
    subj, body = AL.digest_message("till", {"since": 0, "count": 4, "last": "2 people"}, 6)
    assert subj.startswith("till: 4 more alerts since") and "fired 6 times" in body and "Last one: 2 people" in body
    md = "# Camera report\n\n## Named people and things\n\n### Marco · person\nFirst seen 09:00\n\n## What the cameras wrote (summaries)\n\nlong\n\n## Alerts\n\n- 09:10 **till** · queue"
    subj, body = AL.report_message({"config": {"business": {"name": "Cafe"}}}, "2026-09-29", md, "whatsapp")
    assert subj == "Camera report, Cafe, 2026-09-29" and "• Marco · person" in body and "long" not in body and "till · queue" in body
    assert AL.report_message({}, "2026-09-29", md, "email")[1] == md
    assert AL.report_due("07:00", now=time.mktime((2026, 9, 29, 8, 0, 0, 0, 0, -1))) == time.mktime((2026, 9, 30, 7, 0, 0, 0, 0, -1))
    assert AL.report_due("07:00", now=time.mktime((2026, 9, 29, 6, 0, 0, 0, 0, -1))) == time.mktime((2026, 9, 29, 7, 0, 0, 0, 0, -1))


def test_notify_api(app_client):
    c = app_client
    c.post("/signup", json={"name": "A", "company": "Alerts Co", "email": "alerts@example.com", "password": "password1"})
    c.post("/login", json={"email": "alerts@example.com", "password": "password1"})
    d = c.post("/api/desks", json={"name": "Alerts Co", "template": "site_watch"}).get_json()
    assert d["notify"]["channel"] == "" and d["notify"]["run"] is True
    r = c.patch(f"/api/desks/{d['id']}", json={"notify": {"channel": "whatsapp", "to": "+447700900123", "per_hour": 4, "report_time": "06:45"}})
    assert r.status_code == 200 and r.get_json()["notify"] == {"channel": "whatsapp", "to": "+447700900123", "auto": False, "quiet": "",
                                                                "per_hour": 4, "run": False, "report": True, "report_time": "06:45"}
    import atlas.desk.app as A
    jobs = [j for j in A.store.jobs(d["id"]) if j["kind"] == "daily_report"]
    assert len(jobs) == 1 and jobs[0]["name"] == "Daily camera report at 06:45"
    assert c.patch(f"/api/desks/{d['id']}", json={"notify": {"channel": "sms", "to": "12345"}}).status_code == 400
    ds = A.store.for_desk(d["id"])
    vid = ds.add_vision_event("door", {"person": 1}, reason="x", snapshot="")
    assert c.get(f"/api/vision/snapshot/{vid}").status_code == 404
