"""Operational safety: stream deadlines, watchdog, restart recovery, job status, health + alerts, demo fault injection."""
import json
import time

import pytest


def J(r):
    return json.loads(r.data)


def test_stream_deadline_kills_stalled_model():
    """OpenRouter keeps a queued request alive with comment lines; the stream must give up on its own."""
    from atlas.providers import OpenAICompatProvider

    class Resp:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_lines(self):
            while True:                      # endless keepalive, never a data line
                time.sleep(0.01)
                yield ": OPENROUTER PROCESSING"

    class Client:
        def stream(self, *a, **k): return Resp()

    with pytest.raises(RuntimeError) as e:
        OpenAICompatProvider._stream(Client(), {}, lambda *a, **k: None, lambda r: None, {"first_token_s": 0.05, "call_max_s": 5})
    assert "HTTP 504" in str(e.value) and "stalled" in str(e.value)


def test_demo_fault_injection_raises_retryable():
    from atlas.providers import DemoProvider
    p = DemoProvider({"type": "demo", "delay": 0, "fault_rate": 1.0, "fault_sleep": 0})   # every call faults: 429 / 500 / stall
    raised = []
    for _ in range(12):
        try:
            p.chat("sys", [p.user_message("hi")], [])
        except RuntimeError as exc:
            raised.append(str(exc))
    assert raised and all(r.startswith("HTTP ") for r in raised)


def test_store_recovery_and_health_helpers(store):
    store.create_run("r-old", "t", "auto", "/tmp/x")
    store._conn.execute("UPDATE runs SET created=? WHERE id='r-old'", (time.time() - 5000,)); store._conn.commit()
    assert [r["id"] for r in store.running_runs(3600)] == ["r-old"]
    assert store.mark_interrupted() == 1
    assert store.run("r-old")["status"] == "interrupted"
    assert store.running_runs() == []
    store.finish_run("r-old", "done", "ok", 10, 5)
    assert store.run("r-old")["status"] == "done" and store.runs_between(time.time() - 6000)[0]["ended"]
    assert store.oldest_pending_action() is None


def test_health_alerts_and_watchdog(app_client, monkeypatch):
    import atlas.desk.app as A
    c = app_client
    c.post("/signup", json={"name": "Ops", "company": "OpsCo", "email": "ops@example.com", "password": "password1"})
    d = J(c.post("/api/desks", json={"name": "OpsCo", "template": "sales_desk", "tier": "free"}))
    h = J(c.get("/api/health/full"))
    assert h["ok"] is True and h["runs"]["total"] == 0 and h["watchdog_s"] > 0
    # a failing automation shows up as an alert
    j = J(c.post("/api/jobs", json={"kind": "task", "name": "broken", "task": "x", "every_min": 60}))
    A.store.update_job(j["id"], last_status="error", last_result="ERROR boom", last_run=time.time())
    h = J(c.get("/api/health/full"))
    assert any(a["key"] == f"job:{j['id']}" for a in h["alerts"]) and h["ok"] is True      # warn, not critical
    # a run that the DB thinks is running but nobody owns -> zombie -> critical -> watchdog marks it failed
    A.store.create_run("zzz", "t", "auto", "/tmp/z")
    A.store._conn.execute("UPDATE runs SET created=?, desk_id=? WHERE id='zzz'", (time.time() - A.RUN_MAX_S - 10, d["id"])); A.store._conn.commit()
    h = J(c.get("/api/health/full"))
    assert h["ok"] is False and any(a["key"] == "zombie" for a in h["alerts"])
    sent = []
    monkeypatch.setattr(A, "_notify_alerts", lambda desk, alerts: sent.append((desk["id"], [a["key"] for a in alerts])))
    monkeypatch.setattr(A.time, "sleep", lambda s: (_ for _ in ()).throw(StopIteration))   # run one watchdog iteration
    with pytest.raises((StopIteration, RuntimeError)):
        A._watchdog_loop()
    assert A.store.run("zzz")["status"] == "failed" and "lost" in A.store.run("zzz")["summary"]
    assert any(d["id"] == did and "watchdog" in keys for did, keys in sent)
    h = J(c.get("/api/health/full"))
    assert h["runs"]["zombies"] == 0 and h["runs"]["by_status"].get("failed") == 1
    # a live run older than the limit is killed by the watchdog
    class FakeThread:
        def is_alive(self): return True
    class FakeOrch:
        tokens_in = tokens_out = 0
        cancelled = False
        def cancel(self): self.cancelled = True
    A.store.create_run("live1", "t", "auto", "/tmp/l")
    A.store._conn.execute("UPDATE runs SET desk_id=? WHERE id='live1'", (d["id"],)); A.store._conn.commit()
    fo = FakeOrch()
    A._runs["live1"] = {"thread": FakeThread(), "orch": fo, "desk_id": d["id"], "started": time.time() - A.RUN_MAX_S - 5, "events": [], "task": "t"}
    with pytest.raises((StopIteration, RuntimeError)):
        A._watchdog_loop()
    assert fo.cancelled and A.store.run("live1")["status"] == "failed" and "watchdog" in A.store.run("live1")["summary"]
    A._runs.pop("live1", None)
    assert J(c.get("/api/health/full"))["last_error"]["run_id"] == "live1"


def test_scheduler_marks_skipped_and_backs_off(store):
    from atlas.desk import scheduler as S
    desk = store.add_desk(0, "D", "sales_desk", "free", {"business": {}})
    j = store.add_job(desk["id"], "inbox_watch", "watch", "", 2, time.time() - 1)
    calls = []
    # one loop iteration: patch _stop.wait to stop the loop after the first pass
    S._stop.clear()
    S._stop.wait = lambda t: S._stop.set()
    S._loop(store, lambda *a, **k: calls.append(a) or "rid", store.desk)
    row = store.job(j["id"])
    assert row["last_status"] == "skipped" and row["last_result"].startswith("no IMAP")
    assert row["next_run"] - time.time() > 50 * 60          # backed off to ~hourly instead of every 2 minutes
    assert calls == []
