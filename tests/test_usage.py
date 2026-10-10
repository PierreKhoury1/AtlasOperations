"""The usage ledger and the one check (usage spec §1-§4, §7): T1, T2, T7, T16, T27, T28, plus the step-2 helpers
(refusal dedup, overrun, lanes and minute windows, auto actions, the reply window, SMS segments, storage, deferred
starts, estimates). No model is called: provider responses are stubbed, the key endpoint goes through MockTransport."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from atlas import journal as JR
from atlas import usage

PG = bool(os.environ.get("ATLAS_TEST_DATABASE_URL", "").strip())
FREE, PAID = "nvidia/nemotron-3-super-120b-a12b:free", "anthropic/claude-sonnet-4.5"


def _ts(*a) -> int:
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


@pytest.fixture()
def U(store, monkeypatch):
    """The usage module on a fresh store with the default config (whatever this machine's env says) and no sleeps."""
    prev = usage._store
    monkeypatch.setattr(usage, "CFG", usage.load_config({}))
    monkeypatch.setattr(usage, "_sleep", lambda s: None)
    usage.init(store)
    yield usage
    usage.init(prev)


def _clock(monkeypatch, t):
    c = {"t": float(t)}
    monkeypatch.setattr(usage, "_now", lambda: c["t"])
    return c


def _meter(owner=1, desk=1, lane="you", scope="owner", surface="design.say", **kw):
    return usage.Meter(owner_id=owner, desk_id=desk, scope=scope, lane=lane, surface=surface, **kw)


def _events(store, where="1=1", args=()):
    cur = store._conn.execute(f"SELECT * FROM usage_events WHERE {where} ORDER BY id", args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _threads(n, fn):
    ts = [threading.Thread(target=fn) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


# ---------------------------------------------------------------------------- T1 / T28
def _atomic_take(U):
    row = U.Row("o:1", "req:free", 5, "ai_requests_day")
    got = []

    def go():
        try:
            U.take([row])
            got.append("ok")
        except U.UsageLimit as exc:
            got.append(exc.limit_id)
    _threads(20, go)
    assert got.count("ok") == 5 and got.count("ai_requests_day") == 15
    assert U.counter("o:1", "req:free") == (5, 15, 5)
    unlimited = U.Row("o:2", "req:free", None, "ai_requests_day")       # NULL = no Atlas limit: the unconditional form
    _threads(20, lambda: U.take([unlimited]))
    assert U.counter("o:2", "req:free") == (20, 0, None)


def test_take_is_atomic_at_the_last_slot(U):
    _atomic_take(U)
    # the same through before(): an override of 5 lets exactly 5 of 20 concurrent requests through, a NULL override all
    for owner, value, want in ((3, 5, 5), (4, None, 20)):
        U.set_override(owner, "ai_requests_day", value)
        ok = []

        def go():
            try:
                U.before(_meter(owner=owner), provider="openrouter", model=FREE)
                ok.append(1)
            except U.UsageLimit:
                pass
        _threads(20, go)
        assert len(ok) == want and U.counter(f"o:{owner}", "req:free")[0] == want
        U._minute.clear()
    refusals = _events(U._store, "kind='refusal' AND owner_id=3")
    assert len(refusals) == 1 and refusals[0]["units"] == 15 and refusals[0]["limit_id"] == "ai_requests_day"


@pytest.mark.skipif(not PG, reason="T28 runs against PostgreSQL only: set ATLAS_TEST_DATABASE_URL (scripts/test_pg.py)")
def test_usage_counters_upsert_on_postgres(U, store):
    assert store.backend == "postgres"
    _atomic_take(U)


# ---------------------------------------------------------------------------- T2
def test_windows_reset_at_utc_midnight_and_month_start(U, store, monkeypatch):
    t = _ts(2026, 10, 9, 23, 59, 59)
    clock = _clock(monkeypatch, t)
    day9, day10, nov1 = _ts(2026, 10, 9), _ts(2026, 10, 10), _ts(2026, 11, 1)
    assert U.windows() == {"day": {"start": day9, "resets_at": day10}, "month": {"start": _ts(2026, 10, 1), "resets_at": nov1}}
    m = _meter(owner=5)
    U.set_override(5, "ai_requests_day", 0)
    with pytest.raises(U.UsageLimit) as e:
        U.before(m, provider="openrouter", model=FREE)
    b = e.value.body()
    assert (b["limit_id"], b["resets_at"], b["retry_at"], b["retry_after_s"]) == ("ai_requests_day", day10, day10, 1)
    assert b["message"] == "AI requests today: 0 of 0 used. Resets 00:00 UTC (in 1 s)." and e.value.headers() == {"Retry-After": "1"}
    U.set_override(5, "paid_usd_month", 0)
    with pytest.raises(U.UsageLimit) as e:
        U.before(m, provider="openrouter", model=PAID)
    assert e.value.limit_id == "paid_usd_month" and e.value.resets_at == nov1 and e.value.retry_at == nov1
    assert "Resets 00:00 UTC on 1 Nov (in 22 d)." in e.value.message
    U.clear_override(5, "ai_requests_day")
    tk = U.before(m, provider="openrouter", model=FREE)          # taken at 23:59:59 ...
    clock["t"] = t + 2                                            # ... finished at 00:00:01
    U.after(tk, http_status=200, usage={"prompt_tokens": 10, "completion_tokens": 2, "cost": 0})
    for key in ("o:5", "o:5:d1", "house"):
        assert U.counter(key, "req:free", "D", day9)[0] == 1, key
        assert U.counter(key, "req:free", "D", day10)[0] == 0, key
    ev = _events(store, "kind='request'")
    assert len(ev) == 1 and ev[0]["ts"] == t and ev[0]["ts_end"] == t + 2 and ev[0]["cost_usd"] == 0
    assert ev[0]["cost_basis"] == "provider" and (ev[0]["tokens_in"], ev[0]["tokens_out"]) == (10, 2)


# ---------------------------------------------------------------------------- T7
def test_journal_pacing_spreads_notes_until_reset(U, monkeypatch):
    t = _ts(2026, 10, 9, 18, 0, 0)                                # 6 h to 00:00 UTC
    _clock(monkeypatch, t)
    # owner 7 has 10 requests left on its own: L2 = 125 at P = 1,000, 115 used
    U.take([U.Row("o:7:auto", "req:free", None, "ai_requests_auto_day"), U.Row("o:7", "req:free", None, "ai_requests_day")],
           units=115)
    U.stream_seen(7, "journal:door")                              # one paced stream
    gap = U.pace_gap(7, per_action=2)                             # note + verify = 2 requests
    assert gap == 72 * 60
    jc = JR.config({"journal": "1"})
    key = (1, "door-pace-test")
    JR._state[key] = {"note": {"ts": t, "text": "two at the counter", "jpeg": b"", "counts": {}}}
    try:
        ok, why = JR.due(key, jc, t + 71 * 60, 0.9, {"person": 3}, pace=gap)   # even a scene change waits
        assert ok is False and why == "paced: one note every 72 min to last until 00:00 UTC"
        assert JR.due(key, jc, t + 72 * 60, 0.0, {}, pace=gap) == (True, "4320s since last note")
        assert JR.due(key, jc, t + 61, 0.0, {}) == (True, "61s since last note")       # unpaced: the journal's own every_s
    finally:
        JR._state.pop(key, None)
    assert U.pace_gap(8) == pytest.approx(21600 / 125)            # a full allowance and no stream yet: one stream assumed
    U.set_override(9, "ai_requests_day", None)
    assert U.pace_gap(9) == 0.0                                   # no Atlas limit, no pacing


# ---------------------------------------------------------------------------- T16
def test_key_snapshot_sets_pool_and_spend(U, store, monkeypatch):
    t = _ts(2026, 10, 9, 17, 33, 0)
    _clock(monkeypatch, t)
    seen = []

    def handler(req):
        seen.append(req.url.path)
        assert req.headers["Authorization"] == "Bearer sk-test"
        if req.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {
                "label": "sk-or-v1-abc...xyz", "creator_user_id": "user_123", "usage": 19.97, "usage_daily": 0,
                "usage_weekly": 0.97, "usage_monthly": 9.9, "limit": None, "limit_remaining": None, "limit_reset": None,
                "is_free_tier": False, "free_model_daily_requests": {"used": 900, "limit": 1000, "remaining": 100}}})
        return httpx.Response(404)
    snap = U.poll_key(key="sk-test", transport=httpx.MockTransport(handler))
    assert snap["ok"] and (snap["free_used"], snap["free_limit"], snap["P"], snap["usage_monthly"]) == (900, 1000, 1000, 9.9)
    assert U.limit_value(1, "ai_requests_day") == 250 and U.limit_value(1, "ai_requests_auto_day") == 125
    assert U.limit_value(1, "public_requests_day") == 100 and U.limit_value(1, "paid_usd_month") == 2.5
    stored = json.loads(store._conn.execute("SELECT data FROM usage_provider WHERE account='house'").fetchone()[0])
    assert stored["free_used"] == 900 and not {"label", "creator_user_id", "usage"} & set(stored)
    monkeypatch.setitem(U.CFG.pool_min, "free:openrouter", 10_000)     # this test is about the day, not the minute
    m = _meter(owner=1)
    for _ in range(100):                                                # the provider counted 900; 100 more fit
        U.after(U.before(m, provider="openrouter", model=FREE), http_status=200, usage={"cost": 0})
    with pytest.raises(U.UsageLimit) as e:
        U.before(m, provider="openrouter", model=FREE)
    assert e.value.limit_id == "pool_free_day" and e.value.message.startswith("Shared free-model pool today: full.")
    assert U.counter("o:1", "req:free")[0] == 100                      # the owner's own 250 never bound
    # paid: L13 ($10) against the provider's usage_monthly plus this server's reported spend since that read
    tk = U.before(m, provider="openrouter", model=PAID)
    U.after(tk, http_status=200, usage={"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.15})
    assert U.house_paid_used() == pytest.approx(10.05)
    with pytest.raises(U.UsageLimit) as e:
        U.before(m, provider="openrouter", model=PAID)
    assert e.value.limit_id == "house_paid_usd_month" and e.value.resets_at == _ts(2026, 11, 1)
    assert U.house_paid_blocked().startswith("Paid models on Atlas's account this month: $10.05 of $10.00 used.")
    sv = U.spend_view()
    assert (sv["usage_monthly"], sv["free_used"], sv["error"], sv["house_paid_limit"]) == (9.9, 900, "", 10.0)
    # a failed read keeps the last snapshot
    snap = U.poll_key(key="sk-test", transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    assert snap["error"] == "HTTP 500" and snap["P"] == 1000 and snap["free_used"] == 900
    assert not any(p.endswith("/credits") for p in seen)               # GET /api/v1/key only
    assert "/api/v1/credits" not in (Path(U.__file__).parent / "desk" / "app.py").read_text(encoding="utf-8")


def test_provider_daily_quota_closes_the_pool_until_midnight(U, store, monkeypatch):
    t = _ts(2026, 10, 8, 20, 7)
    clock = _clock(monkeypatch, t)
    m = _meter()
    tk = U.before(m, provider="openrouter", model=FREE)                 # a per-minute 429 is not the day
    U.after(tk, http_status=429, error="HTTP 429: Rate limit exceeded: limit_rpm/nvidia")
    assert tk.limit is None and not U.pool_exhausted()
    tk = U.before(_meter(key_kind="byo"), provider="openrouter", model=FREE)   # an owner's own key at its quota
    U.after(tk, http_status=429, error="HTTP 429: Rate limit exceeded: free-models-per-day")
    assert tk.limit is None and not U.pool_exhausted()
    tk = U.before(m, provider="openrouter", model=FREE)
    U.after(tk, http_status=429, error="HTTP 429: Rate limit exceeded: free-models-per-day. Add 10 credits [x: y]")
    assert tk.limit is not None and tk.limit.limit_id == "pool_free_day" and U.snapshot()["exhausted_at"] == t
    with pytest.raises(U.UsageLimit) as e:                              # nothing more is sent today
        U.before(m, provider="openrouter", model=FREE)
    assert e.value.retry_at == _ts(2026, 10, 9)
    minute, byo, day = _events(store, "kind='request'")
    assert (minute["status"], minute["limit_id"]) == ("error", "")
    assert (byo["status"], byo["limit_id"], byo["key_kind"], byo["pool"]) == ("provider_limit", "", "byo", "byo")
    assert day["note"] == "HTTP 429: Rate limit exceeded: free-models-per-day. Add 10 credits"     # no upstream echo
    assert (day["status"], day["limit_id"], day["http_status"]) == ("provider_limit", "pool_free_day", 429)
    assert U.counter("house", "req:free")[0] == 1                      # refused at the cap: not in "this server"'s count
    assert U.counter("o:1", "req:free")[0] == 2                        # but both were requests the owner made
    clock["t"] = _ts(2026, 10, 9, 0, 0, 1)
    assert U.before(m, provider="openrouter", model=FREE).pool == "free:openrouter"


# ---------------------------------------------------------------------------- T27
def test_retention_prunes_events_but_keeps_counters(U, store, monkeypatch):
    t = _ts(2026, 10, 9, 12)
    clock = _clock(monkeypatch, t - 40 * 86400)
    m = _meter(owner=9)
    U.after(U.before(m, provider="openrouter", model=FREE), http_status=200, usage={"cost": 0})
    U.after(U.before(m, provider="openrouter", model=PAID), http_status=200, usage={"cost": 0.01})
    old_day, old_month = U.day_start(clock["t"]), U.month_start(clock["t"])
    U.take([U.Row("o:9", "req:free", None, "ai_requests_day", "D", U.day_start(t - 401 * 86400)),
            U.Row("o:9", "usd:paid", None, "paid_usd_month", "M", U.month_start(t - 401 * 86400))])
    with U._tx() as c:
        c.execute("INSERT INTO usage_provider(account, ts, data) VALUES('house', ?, '{}')", (t - 36 * 86400,))
        c.execute("INSERT INTO usage_deferred(owner_id,desk_id,kind,ref_id,retry_at,created,status) VALUES(9,1,'lead',1,?,?,'started')",
                  (t, t - 8 * 86400))
        c.execute("INSERT INTO usage_deferred(owner_id,desk_id,kind,ref_id,retry_at,created,status) VALUES(9,1,'lead',2,?,?,'waiting')",
                  (t, t - 8 * 86400))
    clock["t"] = t - 34 * 86400
    U.action(m, "action", metric="runs")                                # 34 days old: kept
    clock["t"] = t
    out = U.prune()
    assert out == {"events": 2, "counters": 1, "provider": 1, "deferred": 1}
    assert [e["kind"] for e in _events(store)] == ["action"]
    assert U.counter("o:9", "req:free", "D", old_day)[0] == 1          # D counters: 400 days
    assert U.counter("o:9", "usd:paid", "M", old_month)[0] == 10_000   # micro-USD; M rows forever
    assert U.counter("o:9", "req:free", "D", U.day_start(t - 401 * 86400))[0] == 0
    assert U.counter("o:9", "usd:paid", "M", U.month_start(t - 401 * 86400))[0] == 1
    assert [d["ref_id"] for d in U.deferred(9)] == [2]


# ---------------------------------------------------------------------------- the rest of the check
def test_background_work_paces_pauses_and_logs_once_per_window(U, store, monkeypatch):
    t = _ts(2026, 10, 9, 18)
    clock = _clock(monkeypatch, t)
    cam = _meter(owner=12, lane="auto", surface="camera.detect", ref="cam:Door")
    assert U.allow(cam, stream="detect:Door") == ""
    assert U.allow(cam, stream="detect:Door") == "paced"               # 125 left, 1 stream: one every 172.8 s
    clock["t"] = t + 173
    assert U.allow(cam, stream="detect:Door") == ""
    U.set_override(11, "ai_requests_auto_day", 2)
    door = _meter(owner=11, lane="auto", surface="camera.note", ref="cam:Door")
    for _ in range(2):
        U.after(U.before(door, provider="openrouter", model=FREE), http_status=200, usage={"cost": 0})
    assert [U.allow(door, stream="journal:Door", per_action=2) for _ in range(50)] == ["paused"] * 50
    rows = _events(store, "kind='refusal'")
    assert len(rows) == 1 and rows[0]["units"] == 50 and rows[0]["limit_id"] == "ai_requests_auto_day"
    assert rows[0]["note"].startswith("AI requests Atlas made on its own: 2 of 2 used. Resets 00:00 UTC")
    assert U.counter("o:11:auto", "req:free")[1] == 50                 # refused, counted on the lane row
    you = _meter(owner=11, lane="you")                                  # the owner's own half is untouched
    assert U.before(you, provider="openrouter", model=FREE).pool == "free:openrouter"


def test_overrun_lets_an_inflight_action_finish(U):
    U.set_override(13, "ai_requests_day", 1)
    turn = _meter(owner=13, overrun=2)                                  # a design turn already under way
    for _ in range(3):
        U.before(turn, provider="openrouter", model=FREE)
    with pytest.raises(U.UsageLimit):
        U.before(turn, provider="openrouter", model=FREE)
    assert U.counter("o:13", "req:free")[0] == 3                        # shown plainly: 3 of 1
    with pytest.raises(U.UsageLimit):
        U.precheck(_meter(owner=13, overrun=5))                         # a new action is refused before it starts


def test_lanes_minute_windows_byo_and_demo(U, store, monkeypatch):
    _clock(monkeypatch, _ts(2026, 10, 9, 12))
    monkeypatch.setattr(U.CFG, "pool_min_reserve", 6)
    for _ in range(14):                                                 # background: 20 - 6 reserved for people
        U.before(_meter(owner=20, lane="auto", surface="camera.note"), provider="openrouter", model=FREE)
    with pytest.raises(U.UsageLimit) as e:
        U.before(_meter(owner=20, lane="auto", surface="camera.note"), provider="openrouter", model=FREE)
    assert e.value.limit_id == "pool_free_min" and e.value.retry_at == _ts(2026, 10, 9, 12, 1)
    assert U.allow(_meter(owner=20, lane="auto", surface="camera.note")) == "paced"
    for _ in range(6):                                                  # a person still gets the reserve
        U.before(_meter(owner=21), provider="openrouter", model=FREE)
    with pytest.raises(U.UsageLimit) as e:
        U.before(_meter(owner=21), provider="openrouter", model=FREE)
    assert e.value.message == "Shared per-minute limit: 20 requests in the last minute. Try again in 60 s."
    assert U.before(_meter(owner=21), provider="groq", model="llama-4-scout").pool == "free:groq"   # its own window
    U.set_override(22, "ai_requests_day", 0)
    byo = _meter(owner=22, key_kind="byo")
    U.after(U.before(byo, provider="openrouter", model=PAID), http_status=200, usage={"cost": 0.02})
    assert U.counter("o:22", "req:byo")[0] == 1 and U.counter("o:22", "usd:byo", "M")[0] == 20_000
    demo = _meter(owner=22, key_kind="demo")
    U.after(U.before(demo, provider="demo", model="demo-scripted"), http_status=200)
    assert U.counter("o:22", "req:demo")[0] == 1 and U.counter("o:22", "req:free")[0] == 0
    monkeypatch.setattr(U.CFG, "demo", "enforce")
    with pytest.raises(U.UsageLimit):
        U.before(demo, provider="demo", model="demo-scripted")
    run = _meter(owner=23, surface="run.you", run_cap=2, key_kind="byo")  # L4 applies on any key
    U.before(run, provider="openrouter", model=PAID)
    U.before(run, provider="openrouter", model=PAID)
    with pytest.raises(U.UsageLimit) as e:
        U.before(run, provider="openrouter", model=PAID)
    assert e.value.limit_id == "run_requests" and e.value.retry_at is None
    assert e.value.message == "This run made 2 AI requests (per-run cap 2). Stopped; nothing retries it automatically."
    monkeypatch.setattr(U.CFG, "enforce", False)                       # meter only: counted, never refused
    U.before(_meter(owner=22), provider="openrouter", model=FREE)
    assert U.counter("o:22", "req:free")[0] == 1


def test_paid_estimates_and_unreported_costs(U, store, monkeypatch):
    monkeypatch.setattr(U.CFG, "price_table", U.parse_price_table("claude-opus-5=5:25@2026-10-09"))
    m = _meter(owner=30)
    tk = U.before(m, provider="anthropic", model="claude-opus-5")
    U.after(tk, http_status=200, usage={"input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 100})
    with pytest.raises(U.UsageLimit) as e:                              # no price, no reported cost: fail closed
        U.before(m, provider="anthropic", model="claude-sonnet-5")
    assert e.value.limit_id == "paid_unpriced" and "USAGE_PRICE_TABLE" in e.value.message
    U.after(U.before(m, provider="openrouter", model=PAID), http_status=200, usage={"prompt_tokens": 5, "completion_tokens": 1})
    est, unreported = _events(store, "kind='request'")
    assert est["cost_usd"] is None and est["est_usd"] == pytest.approx(0.0075)
    assert est["cost_basis"] == "estimate: operator price table, set 2026-10-09"
    assert unreported["cost_usd"] is None and unreported["est_usd"] is None and unreported["cost_basis"] == ""
    assert U.counter("o:30", "usd:est", "M")[0] == 7500 and U.counter("house", "usd:est", "M")[0] == 7500
    assert U.owner_paid_used(30) == pytest.approx(0.0075)


def test_auto_actions_reply_window_and_sms_segments(U, store, monkeypatch):
    t = _ts(2026, 10, 9, 15)
    clock = _clock(monkeypatch, t)
    u = store.add_user("usage-owner@example.com", "O", "O", "x")
    desk = store.add_desk(u["id"], "Usage desk", "site_watch", "free", {})
    did = desk["id"]
    m = _meter(owner=u["id"], desk=did, lane="auto", surface="camera.alert", ref="cam:Door")
    U.set_override(u["id"], "auto_sends_day", 1)
    assert U.auto_action(m, "sms") is None
    exc = U.auto_action(m, "calendar")
    assert exc.limit_id == "auto_sends_day" and U.paused_note(exc) == "auto-send paused: Sent without approval today 1/1; approve to send"
    # W1: no inbound from this number in 24 h -> held, 409, nothing taken; the ledger keeps a hash, never the number
    with pytest.raises(U.ChannelWindow) as e:
        U.channel_send(desk, "whatsapp", "+44 7700 900123")
    assert e.value.http_status == 409 and e.value.body()["error"] == "outside_window" and e.value.retry_at is None
    held = _events(store, "kind='refusal' AND status='held'")
    assert len(held) == 1 and held[0]["ref"].startswith("to:") and len(held[0]["ref"]) == 19
    assert "447700900123" not in json.dumps(held) and "7700 900123" not in json.dumps(held)
    assert U.counter(f"d{did}", "held:window")[0] == 1
    store.add_message("whatsapp", "in", "+447700900123", "hi", desk_id=did, ts=t - 2 * 3600)
    r = U.channel_send(desk, "whatsapp", "+44 7700 900123")
    r.confirm(provider="meta")
    assert U.counter(f"d{did}", "send:whatsapp")[0] == 1
    store.add_message("instagram", "in", "ig:178414", "hello", desk_id=did, ts=t - 25 * 3600)
    with pytest.raises(U.ChannelWindow):                                # 25 h ago: outside
        U.channel_send(desk, "instagram", "178414")
    clock["t"] = t - 2 * 3600                                           # 23 h after it: inside
    U.channel_send(desk, "instagram", "178414").release()
    clock["t"] = t
    # L7: reserve one segment, confirm Twilio's num_segments; the next send no longer fits
    U.set_override(u["id"], "sms_segments_day", 2)
    U.channel_send(desk, "sms", "+15550001111").confirm(3, provider="twilio")
    assert U.counter(f"d{did}", "seg:sms")[0] == 3
    with pytest.raises(U.UsageLimit) as e:
        U.channel_send(desk, "sms", "+15550001111")
    assert e.value.limit_id == "sms_segments_day" and e.value.message.startswith("SMS sent today: 3 of 2 used.")
    U.set_override(u["id"], "sms_segments_day", 10)
    res = U.channel_send(desk, "sms", "+15550001111")
    res.release(error="Twilio 21610: unsubscribed")
    assert U.counter(f"d{did}", "seg:sms")[0] == 3


def test_storage_quota_and_disk_floor(U, store, monkeypatch):
    disk = {"free_pct": 50}
    monkeypatch.setattr(U, "_disk_usage", lambda p: (100 * 1024 * U.MB, (100 - disk["free_pct"]) * 1024 * U.MB,
                                                     disk["free_pct"] * 1024 * U.MB))
    U.set_override(40, "storage_mb", 1)
    with pytest.raises(U.UsageLimit) as e:
        U.take_storage(40, 2 * U.MB, surface="upload.video")
    assert e.value.limit_id == "storage_mb" and U.counter("o:40", "storage", "T", 0)[0] == 0
    U.take_storage(40, 512 * 1024, surface="upload.camera")
    assert U.counter("o:40", "storage", "T", 0)[0] == 512 * 1024
    with pytest.raises(U.UsageLimit):
        U.check_storage(40, 600 * 1024)
    U.release_storage(40, 512 * 1024)
    U.check_storage(40, 600 * 1024)
    disk["free_pct"] = 5
    with pytest.raises(U.UsageLimit) as e:
        U.take_storage(41, 1)
    assert e.value.limit_id == "disk_floor" and "would leave 5.0%" in e.value.message
    assert [x["surface"] for x in _events(store, "kind='upload'")] == ["upload.camera"]


def test_deferred_lead_starts_after_retry_at(U, store, monkeypatch):
    t = _ts(2026, 10, 9, 10)
    _clock(monkeypatch, t)
    u = store.add_user("usage-defer@example.com", "O", "O", "x")
    desk = store.add_desk(u["id"], "Defer desk", "site_watch", "free", {})
    lid = store.add_lead("Ana", "", "ana@example.com", "", "web form", "Need a quote", desk_id=desk["id"])
    m = _meter(owner=u["id"], desk=desk["id"], lane="auto", surface="run.auto")
    busy = U._limit("runs_at_once", used=3, limit=3, now=t)
    assert busy.retry_at == t + 60 and busy.message == "3 runs are going (limit 3). Start this when one finishes."
    U.defer(m, "lead", lid, busy)
    assert store.lead(lid)["status"] == "waiting"
    calls = []

    def start(d, lead_id):
        calls.append((d["id"], lead_id, U.bound_lane()))
        if len(calls) == 1:
            raise U._limit("ai_requests_auto_day", used=125, limit=125, now=t + 61)
        store.set_lead(lead_id, status="running", run_id="r-1")
        return "r-1"
    assert U.deferred_tick(t + 30, start=start) == 0 and calls == []    # not due yet
    assert U.deferred_tick(t + 61, start=start) == 0                    # refused again: moved to 00:00 UTC
    row = U.deferred(u["id"])[0]
    assert row["tries"] == 1 and row["retry_at"] == _ts(2026, 10, 10) and row["limit_id"] == "ai_requests_auto_day"
    assert U.deferred_tick(_ts(2026, 10, 10, 0, 0, 5), start=start) == 1
    assert calls[-1] == (desk["id"], lid, "auto") and U.deferred(u["id"]) == []
    lid2 = store.add_lead("Bo", "", "bo@example.com", "", "web form", "", desk_id=desk["id"])
    U.defer(m, "lead", lid2, busy)
    store.set_lead(lid2, status="running", run_id="r-2")                # worked by hand in the meantime
    assert U.deferred_tick(t + 3600, start=start) == 0 and U.deferred(u["id"], "dropped")[0]["ref_id"] == lid2


def test_health_spend_reads_the_key_snapshot(app_client, monkeypatch):
    """/api/health/full keeps its `spend` key, now from GET /api/v1/key and the ledger (spec §3.5)."""
    from atlas import secure as SEC
    import atlas.desk.app as A
    monkeypatch.setattr(A, "_RL_AUTH", SEC.RateLimiter(10000, 600))
    monkeypatch.setattr(A, "OPEN", False)
    c = app_client
    c.get("/logout")
    c.post("/signup", json={"name": "U", "company": "U", "email": "usage-health@example.com", "password": "password1"})
    c.post("/api/desks", json={"name": "Usage health", "template": "site_watch", "tier": "free"})
    sp = json.loads(c.get("/api/health/full").get_data(as_text=True))["spend"]
    assert set(sp) == {"usage_monthly", "limit", "limit_remaining", "free_used", "free_limit", "error", "house_paid_used",
                       "house_paid_limit", "blocked"}
    assert sp["blocked"] is False and sp["house_paid_limit"] == usage.CFG.house_paid_usd_month and A._spend_blocked() == ""
    assert usage._store is A.store
    if usage.CFG.key_poll_s:
        assert usage._poller["thread"].is_alive()


def test_limit_bodies_and_last_real_vision_event(U, store, monkeypatch):
    now = _ts(2026, 10, 9, 17, 32, 43)
    _clock(monkeypatch, now)
    b = U._limit("ai_requests_day", used=250, limit=250, now=now).body()
    assert set(b) == {"error", "limit_id", "label", "scope", "limit", "used", "unit", "resets_at", "retry_at", "retry_after_s",
                      "message", "usage_url"}
    assert b["message"] == "AI requests today: 250 of 250 used. Resets 00:00 UTC (in 6 h 27 min)." and b["retry_after_s"] == 23237
    p = U._limit("paid_usd_month", used=2.5049, limit=2.5, now=now).body()
    assert (p["used"], p["limit"], p["unit"]) == (2.5, 2.5, "usd") and p["message"].startswith("Paid models this month: $2.50 of $2.50 used.")
    assert U._limit("run_requests", used=120, limit=120, now=now).headers() == {}
    assert U.classify("openrouter", FREE) == "free:openrouter" and U.classify("openrouter", PAID) == "paid"
    assert U.classify("gemini", "gemini-2.5-flash") == "free:gemini" and U.classify("openrouter", FREE, "byo") == "byo"
    store.add_vision_event(1, "door", {"person": 2}, backend="yolo", ts=1000.0)
    store.add_vision_event(1, "door", {}, backend="paced", ts=1001.0)
    assert store.last_vision_event(1, "door")["backend"] == "paced"
    assert store.last_vision_event(1, "door", exclude_backends=("paced", "paused", "usage"))["backend"] == "yolo"
    assert store.for_desk(1).last_vision_event("door", exclude_backends=("paced",))["backend"] == "yolo"
