"""The usage ledger and the one check (usage spec §1-§4, §7): T1, T2, T7, T16, T27, T28, plus the step-2 helpers
(refusal dedup, overrun, lanes and minute windows, auto actions, the reply window, SMS segments, storage, deferred
starts, estimates). The text chokepoint in providers.py (§3.2, §4.4): T8, T9, T10, T14, T43, the provider side of T15
and T21, the per-run cap across a pool, Hermes by mode and the BYO key test. No model is called: provider responses
are stubbed (httpx.MockTransport), the key endpoint too."""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from atlas import journal as JR
from atlas import providers as P
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
    ev = _events(store, "kind='request' AND owner_id=5")
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
    minute, byo, day = _events(store, "kind='request' AND owner_id=1")
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
    # a demo run on a background thread may write a request row of its own meanwhile (owner 0): only owner 9 is ours
    assert out["events"] >= 2 and {k: out[k] for k in ("counters", "provider", "deferred")} == {"counters": 1, "provider": 1, "deferred": 1}
    assert [e["kind"] for e in _events(store, "owner_id=9")] == ["action"]
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
    rows = _events(store, "kind='refusal' AND owner_id=11")
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
    est, unreported = _events(store, "kind='request' AND owner_id=30")
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


# ---------------------------------------------------------------------------- the text chokepoint (providers.py)
OR_URL = "https://openrouter.ai/api/v1"
ANTHROPIC_MSG = {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5",
                 "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
                 "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}}


def _stub(p, handler):
    """Send a house provider's HTTP to `handler` (nothing leaves the machine)."""
    mt = httpx.MockTransport(handler)
    p._httpx = SimpleNamespace(Client=lambda **kw: httpx.Client(**{**kw, "transport": mt}), URL=httpx.URL,
                               InvalidURL=httpx.InvalidURL)
    return p


def _house(handler, meter=None, **pcfg):
    """A house OpenRouter provider (the operator's key) whose requests go to `handler`."""
    conf = {"type": "openai", "base_url": OR_URL, "api_key": "sk-or-test", "default_model": PAID, **pcfg}
    return _stub(P.make_provider(conf, meter=meter), handler)


def _body(text="OK", usage=None, model=PAID):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage or {}, "model": model}


def _sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _no_sleep(monkeypatch):
    monkeypatch.setattr(P, "time", SimpleNamespace(sleep=lambda s: None, time=time.time))


def _ask(p, **kw):
    return p.chat("sys", [p.user_message("hi")], [], **kw)


def test_every_model_request_lands_in_the_ledger_with_provider_cost(U, store):
    """T8: provider-reported usage and cost, from the body and from the last SSE chunk."""
    usage_block = {"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.0123,
                   "prompt_tokens_details": {"cached_tokens": 200}}
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["Authorization"] == "Bearer sk-or-test"
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": "O"}}], "model": PAID},
                      {"choices": [{"delta": {"content": "K"}, "finish_reason": "stop"}], "model": PAID},
                      {"choices": [], "usage": usage_block, "model": PAID}]
            return httpx.Response(200, text=_sse(chunks), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=_body(usage=usage_block))
    m = _meter(owner=50, desk=7)
    p = _house(handler, meter=m)
    assert (p.meter, p.key_kind, p.usage_provider) == (m, "house", "openrouter")
    r = _ask(p)
    assert r.text == "OK" and (r.input_tokens, r.output_tokens) == (1200, 80)
    toks = []
    assert _ask(p, on_token=lambda t, thinking=False: toks.append(t)).text == "OK" and "".join(toks) == "OK"
    rows = _events(store, "kind='request' AND owner_id=50")
    assert len(rows) == 2 and len(seen) == 2 and seen[1]["stream"] is True
    for row in rows:
        assert (row["cost_usd"], row["cost_basis"], row["status"], row["http_status"]) == (0.0123, "provider", "ok", 200)
        assert (row["tokens_in"], row["tokens_out"], row["tokens_cached"]) == (1200, 80, 200)
        assert (row["provider"], row["model"], row["pool"], row["key_kind"]) == ("openrouter", PAID, "paid", "house")
        assert (row["surface"], row["desk_id"], row["action_id"], row["lane"]) == ("design.say", 7, m.action_id, "you")
    assert U.counter("o:50", "req:paid")[0] == 2 and U.counter("o:50:d7", "req:paid")[0] == 2
    assert U.counter("o:50", "usd:paid", "M")[0] == 24_600 and U.counter("house", "usd:paid", "M")[0] == 24_600
    assert m.requests == 2


def test_unreported_cost_stays_null(U, store):
    """T9 (the provider side; the API's cost_unreported count comes with /api/usage)."""
    p = _house(lambda req: httpx.Response(200, json=_body(usage={"prompt_tokens": 5, "completion_tokens": 1})),
               meter=_meter(owner=51))
    _ask(p)
    row, = _events(store, "kind='request' AND owner_id=51")
    assert (row["cost_usd"], row["est_usd"], row["cost_basis"], row["tokens_in"]) == (None, None, "", 5)
    assert U.counter("o:51", "req:paid")[0] == 1 and U.counter("o:51", "usd:paid", "M")[0] == 0


def test_retries_and_fallbacks_count_as_requests(U, store, monkeypatch):
    """T10: a 502 then a 200 is two requests; so is a paid 429 that falls back to the free model."""
    _no_sleep(monkeypatch)
    replies = [httpx.Response(502, json={"error": {"message": "Upstream error"}}), httpx.Response(200, json=_body(model=FREE))]
    p = _house(lambda req: replies.pop(0), meter=_meter(owner=52), default_model=FREE)
    assert _ask(p).text == "OK" and replies == []
    rows = _events(store, "kind='request' AND owner_id=52")
    assert [(r["status"], r["http_status"], r["note"]) for r in rows] == [("error", 502, "HTTP 502: Upstream error"), ("ok", 200, "")]
    assert U.counter("o:52", "req:free")[0] == 2
    sent = []

    def handler(req):
        model = json.loads(req.content)["model"]
        sent.append(model)
        if model == PAID:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded: limit_rpm/anthropic"}})
        return httpx.Response(200, json=_body(model=FREE))
    q = _house(handler, meter=_meter(owner=53), fallback_model=FREE)
    assert _ask(q).text == "OK" and sent == [PAID, FREE]
    rows = _events(store, "kind='request' AND owner_id=53")
    assert [(r["model"], r["pool"], r["status"], r["http_status"]) for r in rows] == [(PAID, "paid", "error", 429),
                                                                                      (FREE, "free:openrouter", "ok", 200)]
    assert U.counter("o:53", "req:paid")[0] == 1 and U.counter("o:53", "req:free")[0] == 1


def test_provider_daily_quota_stops_requests_until_reset(U, store, monkeypatch):
    """T14: the provider's free-models-per-day 429 closes the house pool until 00:00 UTC; nothing else is sent today,
    and the chokepoint raises UsageLimit(pool_free_day) instead of falling back."""
    t = _ts(2026, 10, 9, 17, 11)
    clock = _clock(monkeypatch, t)
    sent = []

    def handler(req):
        sent.append(json.loads(req.content)["model"])
        if len(sent) == 1:
            return httpx.Response(429, json={"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. "
                                                       "Add 10 credits to unlock 1000 free model requests per day"}})
        return httpx.Response(200, json=_body(model=FREE))
    p = _house(handler, meter=_meter(owner=54), default_model=FREE, fallback_model="openrouter/another-model:free")
    with pytest.raises(U.UsageLimit) as e:
        _ask(p)
    assert e.value.limit_id == "pool_free_day" and e.value.retry_at == _ts(2026, 10, 10) and len(sent) == 1   # no fallback
    assert U.snapshot()["exhausted_at"] == t and U.pool_exhausted()
    row, = _events(store, "kind='request' AND owner_id=54")
    assert (row["status"], row["limit_id"], row["http_status"]) == ("provider_limit", "pool_free_day", 429)
    assert row["note"].startswith("HTTP 429: Rate limit exceeded: free-models-per-day.") and "[" not in row["note"]
    q = _house(handler, meter=_meter(owner=55), default_model=FREE)
    with pytest.raises(U.UsageLimit) as e:
        _ask(q)
    assert e.value.limit_id == "pool_free_day" and len(sent) == 1         # refused without any HTTP call
    assert _ask(_house(handler, meter=_meter(owner=55))).text == "OK"     # paid models are another pool
    clock["t"] = _ts(2026, 10, 10, 0, 0, 1)
    assert _ask(q).text == "OK" and sent[-1] == FREE and len(sent) == 3   # after 00:00 UTC it is sent again


def test_per_minute_429_is_not_reported_as_daily_allowance(U, store):
    """T15, provider side: a 429 without "per-day" stays a transient RuntimeError and never closes the pool (the
    designer's reply text for it is the designer track's part)."""
    p = _house(lambda req: httpx.Response(429, json={"error": {"message": "Rate limit exceeded: limit_rpm/google/gemma-3-27b-it"}}),
               meter=_meter(owner=56), default_model=FREE)
    with pytest.raises(RuntimeError) as e:
        _ask(p)
    assert str(e.value).startswith("HTTP 429: Rate limit exceeded: limit_rpm")
    assert not U.pool_exhausted() and U.snapshot()["exhausted_at"] is None
    row, = _events(store, "kind='request' AND owner_id=56")
    assert (row["status"], row["limit_id"], row["http_status"]) == ("error", "", 429)


def test_errors_inside_a_200_are_recorded_as_errors(U, store):
    """OpenRouter can answer 200 with an error body and no choices, or send an error chunk after the stream started:
    the ledger row says error, and the daily-quota text still closes the pool."""
    p = _house(lambda req: httpx.Response(200, json={"error": {"message": "Upstream timed out", "code": 502}}), meter=_meter(owner=63))
    with pytest.raises(RuntimeError, match="HTTP 502: provider returned no choices"):
        _ask(p)
    chunks = [{"choices": [{"delta": {"content": "Hal"}}], "model": PAID},
              {"error": {"code": "server_error", "message": "Provider disconnected unexpectedly"},
               "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]}]
    q = _house(lambda req: httpx.Response(200, text=_sse(chunks), headers={"Content-Type": "text/event-stream"}), meter=_meter(owner=63))
    assert _ask(q, on_token=lambda t, thinking=False: None).text == "Hal"
    rows = _events(store, "kind='request' AND owner_id=63")
    assert [(r["status"], r["http_status"], r["note"]) for r in rows] == [
        ("error", 200, "provider returned no choices: Upstream timed out"),
        ("error", 200, "stream error: Provider disconnected unexpectedly")]
    quota = _house(lambda req: httpx.Response(200, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}),
                   meter=_meter(owner=64), default_model=FREE)
    with pytest.raises(U.UsageLimit) as e:
        _ask(quota)
    assert e.value.limit_id == "pool_free_day" and U.pool_exhausted()


def test_anthropic_house_paid_without_price_is_refused(U, store, monkeypatch):
    """T43: Anthropic reports no cost, so a house request needs an operator price (est_usd, labelled) or it is refused
    before anything is sent; the SDK never retries on its own, the provider retries once and counts both."""
    import anthropic
    import httpx2                                     # the anthropic SDK's own HTTP client package
    _no_sleep(monkeypatch)
    m = _meter(owner=57)
    p = P.make_provider({"type": "anthropic", "api_key": "sk-ant-test", "default_model": "claude-opus-5"}, meter=m)
    assert p._client.max_retries == 0 and (p.key_kind, p.usage_provider, p.meter) == ("house", "anthropic", m)
    sent, replies = [], []

    def handler(req):
        sent.append(req.url.path)
        return replies.pop(0) if replies else httpx2.Response(200, json=ANTHROPIC_MSG)
    p._client = anthropic.Anthropic(api_key="sk-ant-test", max_retries=p._client.max_retries,
                                    http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(U.UsageLimit) as e:
        _ask(p)
    assert e.value.limit_id == "paid_unpriced" and "USAGE_PRICE_TABLE" in e.value.message and sent == []
    monkeypatch.setattr(U.CFG, "price_table", U.parse_price_table("claude-opus-5=5:25@2026-10-09"))
    assert _ask(p).text == "OK" and len(sent) == 1
    replies.append(httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    assert _ask(p).text == "OK" and len(sent) == 3                       # 529, then the provider's one retry
    rows = _events(store, "kind='request' AND owner_id=57")
    assert [(r["status"], r["http_status"]) for r in rows] == [("ok", 200), ("error", 529), ("ok", 200)]
    ok = rows[0]
    assert (ok["cost_usd"], ok["provider"], ok["pool"], ok["tokens_in"], ok["tokens_out"]) == (None, "anthropic", "paid", 1000, 100)
    assert ok["est_usd"] == pytest.approx(0.0075) and ok["cost_basis"] == "estimate: operator price table, set 2026-10-09"
    assert U.counter("o:57", "usd:est", "M")[0] == 15_000 and U.counter("o:57", "req:paid")[0] == 3


def test_demo_mode_exempts_only_scripted_requests(U, store, monkeypatch):
    """T21, provider side: DemoProvider requests are metered as req:demo and never refused unless USAGE_DEMO=enforce;
    a real house-key request is enforced in demo mode like anywhere else (the theatre part comes with vision)."""
    assert os.environ.get("DESK_MODE") == "demo"
    U.set_override(58, "ai_requests_day", 0)
    m = _meter(owner=58, surface="run.you")
    d = P.make_provider({"type": "demo", "delay": 0}, meter=m)
    assert (d.key_kind, d.usage_provider) == ("demo", "demo")
    for _ in range(3):
        _ask(d)
    rows = _events(store, "kind='request' AND owner_id=58")
    assert [(r["pool"], r["key_kind"], r["provider"], r["status"], r["tokens_in"], r["cost_usd"]) for r in rows] == \
        [("demo", "demo", "demo", "ok", 0, None)] * 3                     # no canned token figures in the ledger
    assert U.counter("o:58", "req:demo")[0] == 3 and U.counter("o:58", "req:free")[0] == 0
    monkeypatch.setattr(U.CFG, "demo", "enforce")
    with pytest.raises(U.UsageLimit) as e:
        _ask(d)
    assert e.value.limit_id == "ai_requests_day" and len(_events(store, "kind='request' AND owner_id=58")) == 3
    monkeypatch.setattr(U.CFG, "demo", "meter")
    sent = []
    h = _house(lambda req: sent.append(1) or httpx.Response(200, json=_body(model=FREE)), meter=m, default_model=FREE)
    with pytest.raises(U.UsageLimit) as e:
        _ask(h)
    assert e.value.limit_id == "ai_requests_day" and sent == []
    fault = P.make_provider({"type": "demo", "delay": 0, "fault_rate": 1.0, "fault_sleep": 0}, meter=_meter(owner=59))
    for _ in range(4):                                                   # injected faults are recorded as errors
        try:
            _ask(fault)
        except RuntimeError:
            pass
    rows = _events(store, "kind='request' AND owner_id=59")
    assert len(rows) == 4 and {r["status"] for r in rows} <= {"ok", "error"}


def test_one_meter_and_one_run_cap_across_house_and_byo_providers(U, store, monkeypatch):
    """ProviderPool(meter=) hands every provider the run's meter; each pays with its own key kind, and the per-run cap
    (L4) counts them all, BYO included, refusing before anything is sent."""
    from atlas import secure as SEC
    sent = []

    def handler(req):
        sent.append(req.url.host)
        return httpx.Response(200, json=_body(usage={"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.001}))
    monkeypatch.setattr(SEC, "vetted_ips", lambda host, scope="public": ["93.184.216.34"])
    monkeypatch.setattr(SEC, "guarded_transport", lambda scope="public", **kw: httpx.MockTransport(handler))
    run = _meter(owner=60, desk=3, surface="run.you", run_cap=3)
    pool = P.ProviderPool({"default_provider": "openrouter", "providers": {
        "openrouter": {"type": "openai", "base_url": OR_URL, "api_key": "sk-or-test", "default_model": PAID},
        "desk:openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key": "sk-own-1",
                        "default_model": "gpt-5.4-mini", "url_guard": True, "env_key": False}}}, meter=run)
    house, byo = _stub(pool.get(), handler), pool.get("desk:openai")
    assert (house.meter, byo.meter) == (run, run) and (house.key_kind, byo.key_kind) == ("house", "byo")
    for prov in (house, byo, house):
        _ask(prov)
    with pytest.raises(U.UsageLimit) as e:
        _ask(byo)
    assert e.value.limit_id == "run_requests" and e.value.retry_at is None and len(sent) == 3 and run.requests == 3
    rows = _events(store, "kind='request' AND owner_id=60")
    assert [(r["key_kind"], r["provider"], r["pool"]) for r in rows] == [("house", "openrouter", "paid"), ("byo", "openai", "byo"),
                                                                         ("house", "openrouter", "paid")]
    assert U.counter("o:60", "req:byo")[0] == 1 and U.counter("o:60", "usd:byo", "M")[0] == 1_000
    assert U.counter("o:60", "req:paid")[0] == 2 and U.counter("o:60", "usd:paid", "M")[0] == 2_000


def test_hermes_provider_is_metered_by_mode(U, store, monkeypatch, capsys):
    """Spec §1.8: Atlas's own Hermes instance is off by default (nothing sent), one request when unmetered (no cost:
    it stands for many inner calls), a delegation in proxy mode (pre-checked; the proxy meters the inner requests).
    A desk's own instance is the owner's key: one BYO request, whatever the mode."""
    from atlas import secure as SEC
    sent, down = [], {"on": False}

    def handler(req):
        sent.append(json.loads(req.content)["model"])
        if down["on"]:
            return httpx.Response(401, json={"error": {"message": "Invalid API key"}})
        return httpx.Response(200, json=_body(usage={"prompt_tokens": 40, "completion_tokens": 9, "cost": 0.5}, model="hermes-agent"))
    m = _meter(owner=61, desk=4, surface="run.you", run_cap=10)
    house = _stub(P.make_provider({"type": "hermes_agent", "base_url": "http://127.0.0.1:8642", "api_key": "hk",
                                   "default_model": FREE, "session_key": "atlas:desk4:writer"}, meter=m), handler)
    assert (house.key_kind, house.usage_provider, house.metering()) == ("house", "hermes", "off")
    with pytest.raises(RuntimeError, match="off until its model use can be counted"):
        _ask(house)
    assert sent == [] and _events(store, "owner_id=61") == []
    monkeypatch.setattr(U.CFG, "hermes_meter", "unmetered")
    _ask(house)
    row, = _events(store, "kind='request' AND owner_id=61")
    assert (row["provider"], row["model"], row["pool"], row["key_kind"]) == ("hermes", FREE, "free:openrouter", "house")
    assert (row["cost_usd"], row["cost_basis"], row["tokens_in"], row["note"]) == (None, "", 40, "inner calls not counted")
    assert U.counter("o:61", "req:free")[0] == 1 and m.requests == 1
    monkeypatch.setattr(U.CFG, "hermes_meter", "proxy")
    capsys.readouterr()
    _ask(house)
    assert len(sent) == 2 and len(_events(store, "kind='request' AND owner_id=61")) == 1 and m.requests == 1
    act, = _events(store, "kind='action' AND owner_id=61")
    assert (act["surface"], act["provider"], act["model"], act["status"], act["key_kind"]) == ("hermes.call", "hermes", FREE, "ok", "house")
    assert U.counter("o:61", "hermes:calls")[0] == 1 and U._hermes == {}
    assert U.HERMES_SELF_CHECK in capsys.readouterr().out and U.hermes_warning()["message"] == U.HERMES_SELF_CHECK
    down["on"], U._hermes_warn["logged"] = True, 0.0                     # a call that failed proves nothing either way
    with pytest.raises(RuntimeError, match="HTTP 401"):
        _ask(house)
    assert [a["status"] for a in _events(store, "kind='action' AND owner_id=61")] == ["ok", "error"]
    assert U.HERMES_SELF_CHECK not in capsys.readouterr().out and len(sent) == 3
    down["on"] = False
    U.mark_exhausted()                                                    # the delegation is pre-checked: refused unsent
    with pytest.raises(U.UsageLimit) as e:
        _ask(house)
    assert e.value.limit_id == "pool_free_day" and len(sent) == 3 and U._hermes == {}
    monkeypatch.setattr(U.CFG, "hermes_meter", "off")
    monkeypatch.setattr(SEC, "vetted_ips", lambda host, scope="public": ["192.168.1.20"])
    monkeypatch.setattr(SEC, "guarded_transport", lambda scope="public", **kw: httpx.MockTransport(handler))
    own = P.make_provider({"type": "hermes_agent", "base_url": "http://hermes.owner.test:8642", "api_key": "",
                           "url_guard": "internal", "env_key": False}, meter=m)
    assert (own.key_kind, own.metering()) == ("byo", "byo")
    _ask(own)
    last = _events(store, "kind='request' AND owner_id=61")[-1]
    assert (last["key_kind"], last["pool"], last["provider"], last["model"]) == ("byo", "byo", "hermes", "hermes-agent")
    assert len(sent) == 4


def test_unmetered_provider_requests_are_recorded_as_house(U, store, capsys):
    """A provider nobody gave a meter falls back to the bound one, else house / unattributed (logged)."""
    p = _house(lambda req: httpx.Response(200, json=_body(model=FREE)), default_model=FREE)
    m = _meter(owner=62, surface="ask.cameras")
    with U.bind(m):
        _ask(p)
    assert len(_events(store, "kind='request' AND owner_id=62 AND surface='ask.cameras'")) == 1
    U._unattr_log[0] = 0.0
    _ask(p)
    rows = _events(store, "kind='request' AND surface='unattributed' AND pool='free:openrouter'")
    assert len(rows) == 1 and (rows[0]["owner_id"], rows[0]["scope"]) == (0, "house")
    assert "no meter" in capsys.readouterr().out


def test_byo_key_test_is_metered_on_the_owners_key(app_client, monkeypatch):
    """make_provider(meter=) for the desk's key test (spec §3.2): a BYO request for its owner, never limited."""
    from atlas import secure as SEC
    import atlas.desk.app as A
    monkeypatch.setattr(A, "_RL_AUTH", SEC.RateLimiter(10000, 600))
    monkeypatch.setattr(A, "OPEN", False)
    monkeypatch.setattr(SEC, "vetted_ips", lambda host, scope="public": ["93.184.216.34"])
    monkeypatch.setattr(SEC, "guarded_transport", lambda scope="public", **kw: httpx.MockTransport(
        lambda req: httpx.Response(200, json=_body(usage={"prompt_tokens": 9, "completion_tokens": 1, "cost": 0.00002}))))
    c = app_client
    c.get("/logout")
    c.post("/signup", json={"name": "K", "company": "K", "email": "usage-keytest@example.com", "password": "password1"})
    did = c.post("/api/desks", json={"name": "Key test", "template": "sales_desk", "tier": "free"}).get_json()["id"]
    owner = A.store.desk(did)["owner_id"]
    assert c.post(f"/api/desks/{did}/providers", json={"preset": "openrouter", "api_key": "sk-or-own-0000-1111"}).status_code == 200
    usage.set_override(owner, "ai_requests_day", 0)                      # Atlas's allowance is spent; the owner's key is not
    try:
        r = c.post(f"/api/desks/{did}/providers/openrouter/test", json={}).get_json()
    finally:
        usage.clear_override(owner, "ai_requests_day")
    assert r["ok"], r
    row, = _events(A.store, "kind='request' AND owner_id=? AND surface='byo.test'", (owner,))
    assert (row["key_kind"], row["pool"], row["desk_id"], row["cost_usd"], row["ref"]) == ("byo", "byo", did, 0.00002, "key:openrouter")
    assert "sk-or-own" not in json.dumps(row)
