"""The usage ledger and the one check (usage spec §1-§4, §7): T1, T2, T7, T16, T27, T28, plus the step-2 helpers
(refusal dedup, overrun, lanes and minute windows, auto actions, the reply window, SMS segments, storage, deferred
starts, estimates). The text chokepoint in providers.py (§3.2, §4.4): T8, T9, T10, T14, T43, the provider side of T15
and T21, the per-run cap across a pool, Hermes by mode and the BYO key test. Vision, RAG, journal and objects (§1.5-
§1.7, §3.6, S30-S33, S40-S43, S51): T6, T23, T40, the vision chokepoint, the AI-detection gate, journal and object
pauses, and Ask's text-only fallback. Runs and the engine's catch sites (§1.3 L1 headroom, L4, L5, L8, §4.5, §4.6
S1-S9, S16, S20-S27, S53): T11 (run side), T13, T22, T24, T31, T33, T37, T41, T42, the typed LIMIT tool result, the
seed, the browser's limit stop and the site's live run. Background work (§1.6, S12, S13, S44-S52): T5, T18, T25, T26,
T36, T39's case part, a camera alert's refused run, the watch job and a detection's run at a limit, the sensor hook,
the scheduler's deferred starts and the Leads list's note. Channels and uploads (§1.3 W1, L7, L9, §3.4, §4.6
S14-S15): T19, T38, Twilio's segments and price, the approved actions _dispatch counts and the storage
reconcile. Next steps (§5.5): T39's next-steps part, the steps of a run stopped at a limit and /api/next for one. The
Hermes metering proxy (§1.8, §8.2): T32, the attribution rules, a refusal ending the delegation, the operator view's
Hermes block and setup.sh's wiring. The page's API (§5, §6.2): T20, events grouped by action and paged, today's
table, what binds and the rail's brief, the 30-day history, the owner's own key read from the provider, keys, cameras
and the reply window on the page, and the per-address limiters' shape. No model is called and nothing is sent:
provider responses are stubbed (httpx.MockTransport), the key endpoint, the channel APIs and Hermes too."""
import ast
import io
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


# ---------------------------------------------------------------------------- vision, RAG, journal, objects (step 4)
GROQ_URL = "https://api.groq.com/openai/v1"
L2_TEXT = "AI requests Atlas made on its own 125/125"


def _vision_chain(monkeypatch, *entries):
    from atlas import vision as V
    monkeypatch.setattr(V, "vlm_chain", lambda model="": [dict(e) for e in entries])


def _auto_lane_full(U, owner):
    """The owner's own-work allowance (L2 = 125 at P = 1,000) is used up; the owner's half is untouched."""
    U.take([U.Row(f"o:{owner}:auto", "req:free", None, "ai_requests_auto_day"),
            U.Row(f"o:{owner}", "req:free", None, "ai_requests_day")], units=125)


def _camera_setup(store, monkeypatch, tmp_path, email, cam, **cfg):
    """A signed-up owner's desk with one file camera, a fake local detector (one person) and a fresh camera loop."""
    import atlas.desk.scheduler as S
    from atlas import journal as JR
    from atlas import vision as V
    from test_vision import PERSON, FakeDetector, _jpeg
    monkeypatch.setattr(V, "vlm_ready", lambda: True)
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    monkeypatch.setattr(V, "SNAP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(JR, "JOURNAL_DIR", tmp_path / "journal")
    JR.reset()
    for d in (S._last_frame, S._last_seen, S._present, S._noticed):
        d.clear()
    u = store.add_user(email, "O", "O", "x")
    desk = store.add_desk(u["id"], "Cafe", "blank", "free", {})
    src = _jpeg(tmp_path / f"{cam}.jpg", blob=(40, 30, 120, 220))
    conn = store.add_connector(desk["id"], "camera", cam, {"source": src, "watch_for": "car", "journal": "1", "objects": "0",
                                                           **cfg}, False)
    return S, u["id"], desk, conn


def test_vision_chokepoint_meters_every_chain_entry(U, store, monkeypatch):
    """Spec §3.2 item 2: every vision request goes through vision._vlm_post, one metered request per chain entry tried
    (each entry its own pool: Groq never counts in the owner's OpenRouter share), with the provider-reported usage and
    cost, from the last SSE chunk when streaming. A refusal moves the chain on unsent; the last one is raised."""
    from atlas import vision as V
    groq = {"name": "groq", "base": GROQ_URL, "key": "gk-test", "model": "qwen/qwen3.8-27b"}
    orr = {"name": "openrouter", "base": OR_URL, "key": "sk-or-test", "model": FREE}
    sent, down = [], {"api.groq.com": 503}
    block = {"prompt_tokens": 900, "completion_tokens": 30, "cost": 0}

    def handler(req):
        body = json.loads(req.content)
        sent.append(req.url.host)
        assert body["model"] in (FREE, groq["model"]) and req.headers["X-Title"].startswith("Atlas Desk")
        if down.get(req.url.host):
            return httpx.Response(down[req.url.host], json={"error": {"message": "over capacity"}})
        if body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": "Two "}}]}, {"choices": [{"delta": {"content": "people."}}]},
                      {"choices": [], "usage": block}]
            return httpx.Response(200, text=_sse(chunks), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Two people."}}], "usage": block})
    mt = httpx.MockTransport(handler)
    _vision_chain(monkeypatch, groq, orr)
    m = _meter(owner=70, desk=5, surface="camera.look")
    assert V.describe(b"\xff\xd8", "Who is there?", transport=mt, meter=m) == "Two people."
    assert list(V.chat_images_stream("sys", "text", [("NOW", b"\xff\xd8")], transport=mt, meter=m)) == \
        [{"provider": "openrouter", "model": FREE}, "Two ", "people."]
    rows = _events(store, "kind='request' AND owner_id=70")
    assert [(r["provider"], r["pool"], r["status"], r["http_status"]) for r in rows] == \
        [("groq", "free:groq", "error", 503), ("openrouter", "free:openrouter", "ok", 200)] * 2
    assert rows[0]["note"] == "HTTP 503: over capacity"
    for r in (rows[1], rows[3]):                                         # the body's usage, and the last SSE chunk's
        assert (r["tokens_in"], r["tokens_out"], r["cost_usd"], r["cost_basis"]) == (900, 30, 0, "provider")
    assert {(r["surface"], r["key_kind"], r["action_id"]) for r in rows} == {("camera.look", "house", m.action_id)}
    assert U.counter("o:70", "req:free")[0] == 2 and U.counter("o:70", "req:free:groq")[0] == 2
    # the reader stops early: the request still happened, and says so
    _vision_chain(monkeypatch, orr)
    g = V.describe_stream(b"\xff\xd8", "What changed?", transport=mt, meter=_meter(owner=73, surface="theatre.narrate"))
    assert next(g) == {"provider": "openrouter", "model": FREE} and next(g) == "Two "
    g.close()
    row, = _events(store, "kind='request' AND owner_id=73")
    assert (row["status"], row["note"]) == ("ok", "stream closed by the reader")
    # a refusal never sends: the chain moves on to the next entry's own pool, and the last one is raised
    U.set_override(71, "ai_requests_day", 0)
    down.clear()
    sent.clear()
    _vision_chain(monkeypatch, orr, groq)
    m2 = _meter(owner=71, surface="ask.cameras")
    assert V.chat_images("sys", "text", [("NOW", b"\xff\xd8")], transport=mt, meter=m2) == "Two people." and sent == ["api.groq.com"]
    _vision_chain(monkeypatch, orr)
    with pytest.raises(U.UsageLimit) as e:
        V.describe(b"\xff\xd8", "Who?", transport=mt, meter=m2)
    assert e.value.limit_id == "ai_requests_day" and sent == ["api.groq.com"]
    ref, = _events(store, "kind='refusal' AND owner_id=71")
    assert (ref["limit_id"], ref["units"]) == ("ai_requests_day", 2)
    # the provider's daily quota closes the pool (no fallback), as at the text chokepoint
    quota = httpx.MockTransport(lambda req: httpx.Response(429, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}))
    with pytest.raises(U.UsageLimit) as e:
        V.describe(b"\xff\xd8", "Who?", transport=quota, meter=_meter(owner=72))
    assert e.value.limit_id == "pool_free_day" and U.pool_exhausted()


def test_ai_detection_is_gated_and_never_fakes_counts(U, store, monkeypatch, tmp_path):
    """Spec §1.7, S30: without a local detector the AI count is asked for only when it is about to be made; paced,
    paused or refused at the chokepoint it is skipped (backend paced / paused, counts unknown, nothing sent); a count
    that fails is backend error and never raises. An uncounted frame keeps the motion rule; no count rule judges it."""
    from atlas import vision as V
    from test_vision import _jpeg

    class NoDet:
        available, error = False, ""
    calls, status = [], {"code": 200}

    def handler(req):
        calls.append(1)
        if status["code"] != 200:
            return httpx.Response(status["code"], json={"error": {"message": "upstream down"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"person": 2}'}}], "usage": {"cost": 0}})
    mt = httpx.MockTransport(handler)
    real = V.vlm_detect
    monkeypatch.setattr(V, "vlm_detect", lambda jpeg, labels, model="", transport=None, *, meter: real(jpeg, labels, model, mt, meter=meter))
    monkeypatch.setattr(V, "vlm_ready", lambda: True)
    _vision_chain(monkeypatch, {"name": "openrouter", "base": OR_URL, "key": "sk-or-test", "model": FREE})
    src = _jpeg(tmp_path / "door.jpg")
    cfg = {"watch_for": "person", "alert_on_motion": "0.2"}
    m = _meter(owner=80, lane="auto", surface="camera.detect", ref="cam:door")

    def look(gate, prev=None):
        return V.analyse(src, cfg, prev, live_vlm=True, detector=NoDet(), meter=m, detect_vlm=gate)
    asked = []
    assert look(lambda: asked.append(1) or "", prev=V.grab(src))["backend"] == "none" and asked == []   # static: not asked
    for gate in (lambda: "paced", "paused"):
        res = look(gate)
        assert res["counts"] == {} and res["backend"] in ("paced", "paused") and calls == []
    res = look(lambda: "")
    assert (res["backend"], res["counts"], len(calls)) == ("vlm", {"person": 2}, 1)
    row, = _events(store, "kind='request' AND owner_id=80")
    assert (row["surface"], row["lane"], row["ref"], row["pool"]) == ("camera.detect", "auto", "cam:door", "free:openrouter")
    U.set_override(80, "ai_requests_auto_day", 1)                        # refused between the check and the request
    res = look(lambda: "")
    assert res["backend"] == "paused" and len(calls) == 1 and m.held["camera.detect"]["verdict"] == "paused"
    assert m.held["camera.detect"]["limit"]["limit_id"] == "ai_requests_auto_day"
    U.clear_override(80, "ai_requests_auto_day")
    status["code"] = 500
    res = look(lambda: "")
    assert res["backend"] == "error" and res["detector_error"].startswith("vision model: RuntimeError: vision model HTTP 500")
    ok, why = V.evaluate(cfg, [], {}, None, mot=0.5, unknown="AI detection paused")
    assert ok and why.startswith("scene changed")
    ok, why = V.evaluate({"watch_for": "person"}, [], {"person": 2}, None, mot=0.5, unknown="AI detection paused")
    assert not ok and why == "not counted (AI detection paused): count rules wait for a counted frame"


def test_camera_journal_pauses_and_logs_once_per_window(U, store, monkeypatch, tmp_path):
    """T6: with the auto lane full, 50 camera ticks make no VLM request. One `usage` notice and one refusal row
    (units 50) say so; the notice is never embedded; local detection and the camera tile keep working."""
    from atlas import vision as V
    S, owner, desk, conn = _camera_setup(store, monkeypatch, tmp_path, "usage-t6@example.com", "floor")
    sent = []

    def no_vlm(*a, **k):
        sent.append(1)
        raise AssertionError("no vision request may be made")
    monkeypatch.setattr(V, "_vlm_post", no_vlm)
    _auto_lane_full(U, owner)
    for _ in range(50):
        r = S.camera_tick(store, desk, conn, lambda *a: pytest.fail("the rule must not fire"), True)
    assert sent == [] and _events(store, "kind='request' AND owner_id=?", (owner,)) == []
    assert (r["backend"], r["counts"], r["journal"]) == ("yolo", {"person": 1}, "")
    assert r["journal_status"] == f"Journal paused until 00:00 UTC: {L2_TEXT}"
    notes = [e for e in store.vision_events(desk["id"], "floor", 0, "", 200) if e["backend"] == "usage"]
    assert len(notes) == 1 and notes[0]["reason"] == f"Journal paused: {L2_TEXT}. Notes resume after 00:00 UTC."
    assert store._conn.execute("SELECT COUNT(*) FROM vision_vectors WHERE event_id=?", (notes[0]["id"],)).fetchone()[0] == 0
    ref, = _events(store, "kind='refusal' AND owner_id=?", (owner,))
    assert (ref["units"], ref["limit_id"], ref["surface"], ref["ref"]) == (50, "ai_requests_auto_day", "camera.note", "cam:floor")
    assert store.last_vision_event(desk["id"], "floor", exclude_backends=V.NOT_MEASURED)["backend"] == "yolo"


def test_no_detector_camera_keeps_motion_alerts_when_detection_pauses(U, store, monkeypatch, tmp_path):
    """Spec §1.7: without a local detector the AI count is a paced stream of the auto lane. At the limit nothing is
    counted or faked: motion alerts still fire, count rules wait, one notice says so, the tile says what is off, and
    no tick writes an error event."""
    from atlas import vision as V
    from test_vision import _jpeg

    class NoDet:
        available, error = False, ""
    S, owner, desk, conn = _camera_setup(store, monkeypatch, tmp_path, "usage-nodet@example.com", "gate", journal="0",
                                         watch_for="person", alert_on_motion="0.2", cooldown_min="0")
    still = store.add_connector(desk["id"], "camera", "side", {**conn["config"], "alert_on_motion": "0"}, False)
    monkeypatch.setattr(V, "DETECTOR", NoDet())
    sent, runs = [], []
    monkeypatch.setattr(V, "_vlm_post", lambda *a, **k: sent.append(1))
    _auto_lane_full(U, owner)
    src = conn["config"]["source"]
    out = []
    for i in range(4):                                                    # a person walking left and right
        _jpeg(src, blob=(40, 30, 120, 220) if i % 2 == 0 else (200, 30, 280, 220))
        out.append(S.camera_tick(store, desk, conn, lambda d, task, lane: runs.append(task) or f"run{i}", True))
    assert sent == [] and [r["backend"] for r in out] == ["paused"] * 4 and all(r["counts"] == {} for r in out)
    assert not out[0]["triggered"] and all(r["triggered"] and r["reason"].startswith("scene changed") for r in out[1:])
    assert len(runs) == 3 and "Seen: not counted (paused)" in runs[0]
    assert out[-1]["detect_status"] == f"Detection paused until 00:00 UTC: {L2_TEXT}. Count rules are off; motion alerts still fire."
    r = S.camera_tick(store, desk, still, lambda *a: pytest.fail("no alert without a motion rule"), True)
    assert r["detect_status"].endswith("Count rules are off, and with no 'alert on motion' this camera does not alert.")
    evs = store.vision_events(desk["id"], "gate", 0, "", 100)
    notes = [e for e in evs if e["backend"] == "usage"]
    assert len(notes) == 1 and notes[0]["reason"] == (
        f"Detection paused (no local detector): {L2_TEXT}. Count rules are off until 00:00 UTC; motion alerts fire only "
        "where 'alert on motion' is set.")
    assert not [e for e in evs if e["backend"] == "error"] and {e["backend"] for e in evs} == {"paused", "usage"}
    ref = _events(store, "kind='refusal' AND owner_id=? AND surface='camera.detect' AND ref='cam:gate'", (owner,))
    assert len(ref) == 1 and ref[0]["units"] == 4


def test_journal_refusals_pause_the_camera_and_keep_the_draft(U, store, monkeypatch, tmp_path):
    """S42: a refused verify pass keeps the draft, marked not verified. S41: a note refused between the check and its
    request pauses that camera's notes until the reset (no model-error backoff), with one notice."""
    from atlas import journal as JR
    from atlas import vision as V
    lim = U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())

    def verify_refused(system, text, images, model="", max_tokens=500, transport=None, *, meter):
        if system == JR.VERIFY_SYSTEM:
            raise lim
        return "One adult at the counter."
    monkeypatch.setattr(V, "chat_images", verify_refused)
    q = JR.subscribe(91)
    text = JR.write_note((91, "till"), "till", JR.config({"journal": "1"}), b"\xff\xd8", {"person": 1}, stream=False,
                         meter=_meter(owner=91, lane="auto", surface="camera.note"))
    JR.unsubscribe(91, q)
    verify = [e for e in (q.get_nowait() for _ in range(q.qsize())) if e["kind"] == "note_verify"]
    assert text == "One adult at the counter." and verify[0]["error"].startswith(f"not verified: {lim.message}"[:60])

    def refused(*a, **k):
        raise lim
    S, owner, desk, conn = _camera_setup(store, monkeypatch, tmp_path, "usage-s41@example.com", "till", journal_verify="0")
    monkeypatch.setattr(V, "chat_images", refused)
    r = S.camera_tick(store, desk, conn, lambda *a: "x", True)
    assert r["journal_status"] == f"Journal paused until 00:00 UTC: {L2_TEXT}"
    key = (desk["id"], "till")
    assert JR._state[key]["pause_until"] == lim.retry_at and "fail_until" not in JR._state[key]
    assert JR.due(key, JR.config(conn["config"]), time.time(), 0.9, {"person": 5}) == (False, r["journal_status"])
    S.camera_tick(store, desk, conn, lambda *a: "x", True)
    notes = [e for e in store.vision_events(desk["id"], "till", 0, "", 50) if e["backend"] == "usage"]
    assert len(notes) == 1 and notes[0]["reason"] == f"Journal paused: {L2_TEXT}. Notes resume after 00:00 UTC."


def test_object_follow_ups_pace_and_pause(U, store, monkeypatch):
    """Spec §1.5, S51: a called object's follow-ups are a paced stream in the desk owner's auto lane; at a limit the
    camera's follow-ups pause until the reset with no error note; any other failure still leaves its note."""
    import numpy as np
    from atlas import objects as OBJ
    u = store.add_user("usage-obj@example.com", "O", "O", "x")
    desk = store.add_desk(u["id"], "Yard", "blank", "free", {})
    w = OBJ.ObjectWorker(store, desk["id"], "yard", "unused.mp4")
    obj = {"id": 5, "desk_id": desk["id"], "watch": 1, "label": "person", "camera": "yard"}
    w.ds = SimpleNamespace(vision_object=lambda oid: obj)
    track = SimpleNamespace(obj_id=5, last_ts=0.0)
    w.tracker.tracks = [track]
    w._latest[5] = (np.zeros((4, 4, 3), dtype="uint8"), [0, 0, 2, 2])
    seen, done = [], threading.Event()

    def follow(store_, o, frame, box, model="", transport=None, *, meter):
        seen.append(meter)
        done.set()
        return "walking to the gate"
    monkeypatch.setattr(OBJ, "follow", follow)

    def tick(ts):
        track.last_ts = ts
        w._follow_up(ts)
    tick(1000.0)
    assert done.wait(5)
    assert (seen[0].owner_id, seen[0].lane, seen[0].surface, seen[0].ref, seen[0].scope) == (u["id"], "auto", "objects.follow", "obj:5", "owner")
    tick(1000.0 + OBJ.FOLLOW_S)                                           # 125 left for one stream: minutes apart
    assert len(seen) == 1 and w.stats["follow"].startswith("Follow-ups paced: one note every")
    _auto_lane_full(U, u["id"])
    tick(1000.0 + 2 * OBJ.FOLLOW_S)
    assert len(seen) == 1 and w.stats["follow"] == f"Follow-ups paused until 00:00 UTC: {L2_TEXT}"
    assert w._paused_until == U.day_start(U._now()) + 86400
    notes = []
    st = SimpleNamespace(for_desk=lambda d: SimpleNamespace(add_object_note=lambda *a: notes.append(a)))
    w2 = OBJ.ObjectWorker(store, desk["id"], "gate", "unused.mp4")

    def refused(*a, **k):
        raise U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())

    def broken(*a, **k):
        raise RuntimeError("vision model HTTP 502: upstream")
    OBJ._safe(refused, st, obj, on_limit=w2.pause)
    assert notes == [] and w2.stats["follow"] == f"Follow-ups paused until 00:00 UTC: {L2_TEXT}"
    OBJ._safe(broken, st, obj, on_limit=w2.pause)
    assert notes == [(5, "error", "vision model unavailable: vision model HTTP 502: upstream")]


def test_ask_falls_back_to_text_only_on_the_owners_key(U, store, monkeypatch, tmp_path):
    """S31: when a limit refuses the Ask's vision re-look, the text answer stands in only on the desk's own model
    account (BYO) and the grounding says why; on Atlas's account the limit itself is the answer."""
    from PIL import Image
    from atlas import rag as R
    from atlas import vision as V
    snap = tmp_path / "s.jpg"
    Image.new("RGB", (32, 24), (120, 30, 30)).save(snap, "JPEG")
    store.add_vision_event(4, "door", {"person": 1}, reason="person", answer="A courier at the door", snapshot=str(snap),
                           triggered=True)
    lim = U._limit("ai_requests_day", used=250, limit=250, now=U._now())

    def refused(*a, **k):
        raise lim
    monkeypatch.setattr(V, "chat_images", refused)
    monkeypatch.setattr(V, "vlm_ready", lambda: True)
    m = _meter(owner=95, desk=4, surface="ask.cameras", overrun=2)
    res = R.ask(store, 4, "who came to the door?", mode="live", text_answer=lambda s, p: "A courier, from the log.",
                meter=m, text_byo=True)
    assert res["answer"] == "A courier, from the log."
    assert res["retrieval"]["grounding"] == "text only (vision on Atlas's account is at its limit until 00:00 UTC)"
    with pytest.raises(U.UsageLimit):
        R.ask(store, 4, "who came to the door?", mode="live", text_answer=lambda s, p: "x", meter=m)


def test_embeddings_are_aggregated_and_degrade_at_l13(U, store, monkeypatch):
    """T40: the indexer's embeddings are one ledger row per desk per UTC minute (units = requests) and an Ask's one row
    under its own action id, counted at once; with Atlas's paid cap (L13) full nothing is sent, the Ask still answers
    on the hashed embedder and says so, and new events wait unindexed."""
    from atlas import rag as R
    t = _ts(2026, 10, 9, 12, 0, 5)
    clock = _clock(monkeypatch, t)
    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(len(body["input"]))
        assert req.url.path.endswith("/embeddings")
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, float(i), 0.5]} for i in range(len(body["input"]))],
                                         "usage": {"prompt_tokens": 7, "total_tokens": 7, "cost": 0.000002}})
    api = R.ApiEmbedder(base_url=OR_URL, api_key="sk-or-test", model="openai/text-embedding-3-small",
                        transport=httpx.MockTransport(handler))
    monkeypatch.setattr(R, "get_embedder", lambda mode="": R.HashEmbedder() if mode == "hash" else api)
    u = store.add_user("usage-embed@example.com", "O", "O", "x")
    desk = store.add_desk(u["id"], "Shop", "blank", "free", {})
    o, did = u["id"], desk["id"]
    for i in range(100):
        store.add_vision_event(did, "door", {"person": 1}, reason=f"visit {i}", answer=f"A person at the door, visit {i}")
    assert len(sent) == 100
    row, = _events(store, "surface='embed' AND owner_id=?", (o,))
    assert (row["units"], row["kind"], row["lane"], row["status"], row["pool"], row["provider"]) == (100, "request", "auto", "ok", "paid", "openrouter")
    assert (row["tokens_in"], row["ts"], row["ts_end"], row["desk_id"]) == (700, t, t, did)
    assert row["cost_usd"] == pytest.approx(0.0002) and row["cost_basis"] == "provider"
    assert U.counter(f"o:{o}", "req:embed")[0] == 100 and U.counter(f"o:{o}:d{did}", "req:embed")[0] == 100
    assert U.counter(f"o:{o}", "usd:paid", "M")[0] == 200 and U.counter("house", "usd:paid", "M")[0] == 200   # micro-USD
    assert U.counter(f"o:{o}", "req:free")[0] == 0                       # never in the free allowance
    clock["t"] = t + 60                                                   # the next minute is its own row
    store.add_vision_event(did, "door", {}, reason="empty")
    assert [r["units"] for r in _events(store, "surface='embed' AND owner_id=?", (o,))] == [100, 1]
    ask = U.desk_meter(desk, "ask.cameras", lane="you", overrun=2)
    res = R.ask(store, did, "was anyone at the door?", mode="demo", meter=ask)
    assert res["retrieval"]["embedder"] == api.name and "embedder_note" not in res["retrieval"]
    asked, = _events(store, "surface='embed' AND action_id=?", (ask.action_id,))
    assert (asked["units"], asked["lane"]) == (1, "you")                  # the question; the log was indexed already
    monkeypatch.setattr(U.CFG, "house_paid_usd_month", 0.0)               # Atlas's paid cap is reached
    n = len(sent)
    res = R.ask(store, did, "was anyone at the door?", mode="demo", meter=U.desk_meter(desk, "ask.cameras", lane="you"))
    assert res["answer"].startswith("Demo mode") and res["retrieval"]["embedder"] == "hash-trigram-256"
    assert res["retrieval"]["embedder_note"].startswith("search embeddings paused: Paid models on Atlas's account this month")
    ev = store.add_vision_event(did, "door", {"person": 2}, reason="two at the door")
    assert len(sent) == n and store._conn.execute("SELECT COUNT(*) FROM vision_vectors WHERE event_id=? AND model=?",
                                                  (ev["id"], api.name)).fetchone()[0] == 0
    ref, = _events(store, "kind='refusal' AND owner_id=?", (o,))
    assert (ref["limit_id"], ref["surface"], ref["units"]) == ("house_paid_usd_month", "embed", 2)


# T23: the AST audit of every model path (spec §8.1). The metered functions of §3.2 (plus rag.backfill, which indexes)
# must get `meter=` at every call, never a literal None; ProviderPool / make_provider / Orchestrator calls too; every
# raw model POST sits in one of the chokepoints.
METERED = {"vision": {"describe", "vlm_detect", "chat_images", "chat_images_stream", "describe_video", "describe_stream",
                      "analyse"},
           "rag": {"ask", "retrieve", "index_events", "backfill"},
           "journal": {"write_note", "verify_note", "maybe_rollup"},
           "objects": {"call", "follow", "ask"}}
POST_SITES = {"providers.py": "", "vision.py": "_vlm_post", "rag.py": "ApiEmbedder", "desk/app.py": "_demo_stream",
              "study.py": "_hermes_browse", "desk/hermes_proxy.py": ""}
# ProviderPool / make_provider calls still built without a meter, each with the step that hands one over. The list is
# exact: a site that gets its meter must leave it, and a new unmetered call fails.
POOL_PENDING = {
    ("desk/app.py", "api_design_study.work"): "merge: the study meter (step 8's design-route part)",
    ("designer.py", "_provider_for"): "merge: the designer track rewrites designer.py",
    ("designer.py", "_live_turn"): "merge: the designer track rewrites designer.py",
}


def _model_paths() -> dict[str, list]:
    atlas_dir = Path(usage.__file__).parent
    found: dict[str, list] = {"meter": [], "pool": set(), "post": []}
    for p in sorted(atlas_dir.rglob("*.py")):
        rel = p.relative_to(atlas_dir).as_posix()
        tree = ast.parse(p.read_text(encoding="utf-8"))
        where: dict[int, str] = {}

        def walk(node, stack):
            for ch in ast.iter_child_nodes(node):
                nested = isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                where[id(ch)] = ".".join(stack)
                walk(ch, stack + [ch.name] if nested else stack)
        walk(tree, [])
        alias: dict[str, object] = {}                     # local name -> metered module, or (module, function)
        docs: set[int] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                mod = (n.module or "").split(".")[-1]
                for a in n.names:
                    if mod in ("", "atlas") and a.name in METERED:
                        alias[a.asname or a.name] = a.name
                    elif mod in METERED:
                        alias[a.asname or a.name] = (mod, a.name)
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "attr", "") == "import_module":
                arg = n.value.args[0] if n.value.args else None
                mod = str(getattr(arg, "value", "")).split(".")[-1]
                if mod in METERED:
                    alias.update({t.id: mod for t in n.targets if isinstance(t, ast.Name)})
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body and \
                    isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                docs.add(id(n.body[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and \
                    ("/chat/completions" in n.value or "/embeddings" in n.value):
                found["post"].append((rel, where.get(id(n), "")))
            if not isinstance(n, ast.Call):
                continue
            f, target = n.func, ""
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and isinstance(alias.get(f.value.id), str):
                target = f"{alias[f.value.id]}.{f.attr}" if f.attr in METERED[alias[f.value.id]] else ""
            elif isinstance(f, ast.Name) and p.parent == atlas_dir and p.stem in METERED and f.id in METERED[p.stem]:
                target = f"{p.stem}.{f.id}"
            elif isinstance(f, ast.Name) and isinstance(alias.get(f.id), tuple) and alias[f.id][1] in METERED[alias[f.id][0]]:
                target = ".".join(alias[f.id])
            meter = next((k.value for k in n.keywords if k.arg == "meter"), None)
            unmetered = meter is None or (isinstance(meter, ast.Constant) and meter.value is None)
            if target and unmetered:
                found["meter"].append((rel, where.get(id(n), ""), n.lineno, target))
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name in ("ProviderPool", "make_provider", "Orchestrator") and unmetered:
                found["pool"].add((rel, where.get(id(n), "")))
    return found


def test_every_model_path_takes_a_meter():
    """T23 (spec §8.1)."""
    found = _model_paths()
    assert found["meter"] == []
    assert found["pool"] == set(POOL_PENDING), "update POOL_PENDING: " + repr(sorted(found["pool"] ^ set(POOL_PENDING)))
    stray = [(rel, fn) for rel, fn in found["post"]
             if rel not in POST_SITES or (POST_SITES[rel] and fn.split(".")[0] != POST_SITES[rel])]
    assert stray == [] and {rel for rel, _fn in found["post"]} >= {"vision.py", "rag.py", "providers.py"}
    import inspect
    from atlas import journal, objects, rag, vision
    for mod, names in (("vision", METERED["vision"]), ("rag", METERED["rag"]), ("journal", METERED["journal"]),
                       ("objects", METERED["objects"])):
        for fn in names:                                  # required and keyword-only: a forgotten meter is a TypeError
            prm = inspect.signature(getattr({"vision": vision, "rag": rag, "journal": journal, "objects": objects}[mod], fn)).parameters["meter"]
            assert prm.kind is inspect.Parameter.KEYWORD_ONLY and prm.default is inspect.Parameter.empty, (mod, fn)
    assert inspect.signature(rag.ApiEmbedder.embed_texts).parameters["meter"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------- runs and engine catch sites (step 5)
class _Script(P.Provider):
    """A model that plays a script, metered like any request on Atlas's key (the free pool). Each step is
    ("tool", name, args) or ("text", reply); `forever` is played once the script is used up."""
    usage_provider = "openrouter"

    def __init__(self, steps, forever=None):
        super().__init__({"type": "openai", "default_model": FREE})
        self.steps, self.forever, self.calls, self.seen = list(steps), forever, 0, []

    def chat(self, system, messages, tools, model="", on_token=None):
        tk = usage.before(self.meter, provider="openrouter", model=FREE, key_kind=self.key_kind)
        usage.after(tk, http_status=200, usage={"prompt_tokens": 10, "completion_tokens": 2})
        self.calls += 1
        self.seen.append(messages[-1] if messages else None)
        step = self.steps.pop(0) if self.steps else (self.forever or ("text", "Done."))
        if step[0] == "tool":
            call = P.ToolCall(id=f"c{self.calls}", name=step[1], args=dict(step[2]))
            return P.LLMResponse(text="", tool_calls=[call], assistant_message={"role": "assistant", "content": ""},
                                 input_tokens=10, output_tokens=2, model=FREE)
        return P.LLMResponse(text=step[1], tool_calls=[], assistant_message={"role": "assistant", "content": step[1]},
                             input_tokens=10, output_tokens=2, model=FREE)

    def tool_results(self, results):
        return [{"role": "tool", "tool_call_id": cid, "name": n, "content": ("ERROR: " + out) if err else out}
                for cid, n, out, err in results]


def _orch(meter, tools=("list_agents",), store=None, emit=None, script=None, **orch_cfg):
    """A desk of one (Atlas, no team, so it may finish without delegating) whose model is `script`."""
    from atlas.orchestrator import Orchestrator
    configs = {"providers": {"default_provider": "stub", "providers": {"stub": {"type": "openai", "base_url": OR_URL,
                                                                                 "default_model": FREE}}},
               "orchestration": {"max_iterations": 24, "specialist_max_iterations": 4, "max_delegation_depth": 2, **orch_cfg},
               "business": {"name": "Usage test desk"}, "workflows": [], "ui": {},
               "agents": [{"id": "atlas", "name": "Atlas", "role": "lead", "system_prompt": "You run the desk.",
                           "tools": list(tools), "provider": "stub", "model": FREE}]}
    o = Orchestrator(configs, store, emit or (lambda ev: None), meter=meter)
    if script is not None:
        o.pool._cache["stub"] = script
    return o


def test_run_stops_at_run_request_cap(U, store, monkeypatch):
    """T22 (spec §1.3 L4, §4.6 S20, S26): a run whose model keeps calling a tool stops at the per-run cap: status
    `limit`, a summary naming the limit, RunResult.limit and the done event carry the 429 body, nothing is sent past
    the cap, every request of the run carries its id, and the refusal is on the ledger once."""
    monkeypatch.setattr(U.CFG, "run_max_requests", 5)
    desk = store.add_desk(81, "Cap desk", "blank", "free", {})
    m = U.run_meter(desk, "you")
    assert (m.run_cap, m.surface, m.lane, m.scope, m.desk_id) == (5, "run.step", "you", "owner", desk["id"])
    events = []
    script = _Script([], forever=("tool", "list_agents", {}))
    res = _orch(m, emit=events.append, script=script).run("Keep listing the team")
    assert (res.status, script.calls) == ("limit", 5)
    assert res.summary.startswith("Stopped: This run made 5 AI requests (per-run cap 5). Stopped; nothing retries it")
    assert res.limit["limit_id"] == "run_requests" and res.limit["retry_at"] is None and res.limit["used"] == 5
    done = [e for e in events if e.kind == "done"][-1]
    assert done.data["status"] == "limit" and done.data["limit"]["limit_id"] == "run_requests"
    assert [e.text for e in events if e.kind == "agent_end"][-1].startswith("Atlas stopped at a usage limit: This run made 5")
    assert not [e for e in events if e.kind == "error"]                  # a limit is not an error
    rows = _events(store, "kind='request' AND owner_id=81")
    assert len(rows) == 5 and {(r["ref"], r["surface"], r["lane"]) for r in rows} == {(res.run_id, "run.step", "you")}
    ref, = _events(store, "kind='refusal' AND owner_id=81")
    assert (ref["limit_id"], ref["units"], ref["surface"]) == ("run_requests", 1, "run.step")


def test_a_tool_refused_at_a_limit_is_out_for_the_run(U, store, monkeypatch):
    """S22-S24 (spec §4.6): a tool whose request is refused returns a typed LIMIT result (an error result the model
    reads), is recorded on the run's meter and is not tried again in that run; the run carries on while its own
    model has room, and finishes."""
    from atlas import vision as V
    desk = store.add_desk(82, "Look desk", "blank", "free", {})
    store.add_connector(desk["id"], "camera", "door", {"source": "door.jpg"}, False)
    tried = []

    def refused(*a, **k):
        tried.append(k.get("meter"))
        raise U._limit("ai_requests_day", used=250, limit=250, now=U._now())
    monkeypatch.setattr(V, "analyse", refused)
    m = U.run_meter(desk, "auto")
    look = ("tool", "camera_look", {"camera": "door", "question": "Anyone there?"})
    script = _Script([look, look, ("text", "Checked what I could.")])
    res = _orch(m, tools=("camera_look",), store=store.for_desk(desk["id"]), script=script).run("Look at the door twice")
    assert res.status == "done" and len(tried) == 1 and tried[0] is m
    want = ("ERROR: LIMIT: AI requests today 250/250, resets 00:00 UTC. camera_look is unavailable for the rest of "
            "this run; do not call it again.")
    assert [s["content"] for s in script.seen[1:]] == [want, want]
    assert m.limited["camera_look"]["limit_id"] == "ai_requests_day"
    held = {"limit_id": "ai_requests_day", "label": "AI requests today", "used": 1, "limit": 1, "unit": "requests",
            "resets_at": None, "retry_at": None}
    orch = _orch(U.run_meter(desk, "you"), tools=("delegate",))
    orch._held_tools()["delegate:writer"] = held                      # a delegation is out for that one agent only
    out = orch._execute_tools(orch.agents["atlas"], [P.ToolCall("d1", "delegate", {"agent_id": "writer", "task": "x"})], 0)
    assert out[0][2] == ("LIMIT: AI requests today 1/1. delegate to writer is unavailable for the rest of this run; "
                         "do not call it again.") and out[0][3] is True


class _SlowOrch:
    """Stands in for the Orchestrator in route tests: a run that holds its slot until `gate` opens, and makes no
    model request at all."""
    gate = threading.Event()

    def __init__(self, configs, store, emit, meter=None):
        self.meter, self.run_id, self.case_id, self.tokens_in, self.tokens_out = meter, "", None, 0, 0

    def run(self, task, mode="auto"):
        import secrets
        from atlas.orchestrator import RunResult
        self.run_id = "slow-" + secrets.token_hex(4)
        _SlowOrch.gate.wait(20)
        return RunResult(self.run_id, "done", "ok", [], 0, 0, Path("."))

    def cancel(self):
        pass


def _signed_in(c, monkeypatch, email, template="sales_desk"):
    """A fresh account with one desk, signed in on the shared test client: (app module, desk id, owner id)."""
    from atlas import secure as SEC
    import atlas.desk.app as A
    monkeypatch.setattr(A, "_RL_AUTH", SEC.RateLimiter(10000, 600))
    monkeypatch.setattr(A, "OPEN", False)
    c.get("/logout")
    c.post("/signup", json={"name": "U", "company": "U", "email": email, "password": "password1"})
    did = c.post("/api/desks", json={"name": "Usage " + email.split("@")[0], "template": template, "tier": "free"}).get_json()["id"]
    return A, did, int(A.store.desk(did)["owner_id"])


def _wait_runs(A, did, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with A._runs_lock:
            alive = [h for h in A._runs.values() if h["desk_id"] == did and h["thread"].is_alive()]
        if not alive:
            return True
        time.sleep(0.05)
    return False


def _guard_models(monkeypatch, handler=None):
    """Nothing leaves the machine: the env's model keys are hidden, the vision chain is one fake OpenRouter entry
    (on the model asked for), and every httpx client answers from `handler`; the default refuses and records the
    request, so a test can assert none was sent."""
    from atlas import vision as V
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "VISION_DEMO_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(V, "vlm_chain", lambda model="": [{"name": "openrouter", "base": OR_URL, "key": "sk-or-test",
                                                           "model": model or FREE}])
    sent = []

    def record(req):
        sent.append(str(req.url))
        return (handler or (lambda r: httpx.Response(500, json={"error": {"message": "test: nothing may be sent"}})))(req)
    mt = httpx.MockTransport(record)
    real = httpx.Client                                  # a client given its own (stub) transport keeps it
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": kw.get("transport") or mt}))
    return sent


def _is_limit(r, limit_id):
    b = r.get_json()
    assert r.status_code == 429 and b["error"] == "usage_limit" and b["limit_id"] == limit_id, (r.status_code, b)
    assert b["message"] and b["usage_url"] == "/desk/usage" and abs(int(r.headers["Retry-After"]) - b["retry_after_s"]) <= 1
    return b


def test_runs_at_once_cap_is_atomic(app_client, monkeypatch):
    """T24 (spec §1.3 L5, §4.2): with 3 runs allowed at once, 6 concurrent POST /api/runs start exactly 3 and refuse
    3 with 429 runs_at_once, retry_at about now + 60 s; a finished run gives its slot back. Each start is on the
    ledger under its run id, the refusals once (x3)."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-l5@example.com")
    monkeypatch.setattr(usage.CFG, "demo", "enforce")                   # the scripted desk counts like a house one
    monkeypatch.setattr(usage.CFG, "runs_at_once", 3)
    monkeypatch.setattr(A, "Orchestrator", _SlowOrch)
    uid = A.store.user_by_email("usage-l5@example.com")["id"]
    out, barrier = [], threading.Barrier(6)
    hdr = {"X-Desk": str(did)}

    def go():
        cl = A.app.test_client()
        with cl.session_transaction() as s:
            s["uid"], s["desk"] = uid, did
        barrier.wait()
        r = cl.post("/api/runs", json={"task": "Check the books"}, headers=hdr)
        out.append((r.status_code, r.get_json(), r.headers.get("Retry-After")))
    _SlowOrch.gate.clear()
    t0 = time.time()
    try:
        _threads(6, go)
        ok = [b for st, b, _ra in out if st == 200]
        busy = [(b, ra) for st, b, ra in out if st == 429]
        assert len(ok) == 3 and len(busy) == 3, out
        for b, ra in busy:
            assert (b["limit_id"], b["used"], b["limit"], b["unit"]) == ("runs_at_once", 3, 3, "runs")
            assert abs(b["retry_at"] - (t0 + 60)) <= 5 and 55 <= int(ra) <= 61 and b["resets_at"] is None
            assert b["message"] == "3 runs are going (limit 3). Start this when one finishes."
        assert usage.runs_going(owner) == 3
    finally:
        _SlowOrch.gate.set()
    assert _wait_runs(A, did) and usage.runs_going(owner) == 0
    assert app_client.post("/api/runs", json={"task": "Again"}, headers=hdr).status_code == 200
    assert _wait_runs(A, did)
    starts = _events(A.store, "kind='action' AND surface='run.you' AND desk_id=?", (did,))
    assert len(starts) == 4 and all(s["ref"].startswith("slow-") for s in starts)
    assert usage.counter(f"o:{owner}", "runs")[0] == 4
    ref, = _events(A.store, "kind='refusal' AND owner_id=?", (owner,))
    assert (ref["limit_id"], ref["units"], ref["surface"]) == ("runs_at_once", 3, "run.you")


def test_run_start_needs_headroom(app_client, monkeypatch):
    """T41 (spec §1.3 L1): a run on the free pool starts only with max(2, the desk's 7-day median requests per run)
    left today; the refusal names the median. A desk with no measured run needs 2."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-headroom@example.com")
    monkeypatch.setattr(usage.CFG, "demo", "enforce")
    monkeypatch.setattr(A, "Orchestrator", _SlowOrch)
    _SlowOrch.gate.set()
    hdr = {"X-Desk": str(did)}
    now = time.time()
    with usage._tx() as cn:                                              # three earlier runs: 13, 14 and 30 requests
        for i, n in enumerate((13, 14, 30)):
            aid = f"hist{i}"
            usage._insert_event(cn, {"ts": now - 86400 - 60, "owner_id": owner, "desk_id": did, "surface": "run.you",
                                     "kind": "action", "action_id": aid, "key_kind": "none"})
            for k in range(n):
                usage._insert_event(cn, {"ts": now - 86400 + k, "owner_id": owner, "desk_id": did, "surface": "run.step",
                                         "kind": "request", "action_id": aid, "key_kind": "house", "pool": "free:openrouter"})
            usage._insert_event(cn, {"ts": now - 86400, "owner_id": owner, "desk_id": did, "surface": "embed",
                                     "kind": "request", "action_id": aid, "units": 40})   # embeddings are not AI requests
    assert usage.run_headroom(did) == (14, 14)
    usage.set_override(owner, "ai_requests_day", 10)
    try:
        b = _is_limit(app_client.post("/api/runs", json={"task": "Weekly summary"}, headers=hdr), "ai_requests_day")
        assert (b["used"], b["limit"]) == (0, 10) and b["message"].startswith(
            "AI requests today: 0 of 10 used. A run on this desk usually needs about 14 (median of the last 7 days). "
            "Resets 00:00 UTC")
        did2 = app_client.post("/api/desks", json={"name": "Usage headroom 2", "template": "sales_desk", "tier": "free"}).get_json()["id"]
        assert app_client.post("/api/runs", json={"task": "First job"}, headers={"X-Desk": str(did2)}).status_code == 200
        usage.set_override(owner, "ai_requests_day", 1)
        b = _is_limit(app_client.post("/api/runs", json={"task": "Second job"}, headers={"X-Desk": str(did2)}), "ai_requests_day")
        assert "A run needs at least 2 (Atlas and one specialist)." in b["message"]
    finally:
        usage.clear_override(owner, "ai_requests_day")
    assert _wait_runs(A, did2)


def test_run_now_counts_in_lane_you(app_client, monkeypatch):
    """T42 (spec §3.2): a job's "Run now" is the owner's click, so its run counts in lane you; the same job on its own
    schedule runs in lane auto. Every request of a run carries the run's lane."""
    import atlas.desk.scheduler as S
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-runnow@example.com")
    job = A.store.add_job(did, "task", "Nightly", "Summarise the day for the owner", 60, time.time() + 3600)
    assert app_client.post(f"/api/jobs/{job['id']}/run", headers={"X-Desk": str(did)}).get_json()["ok"]
    assert S.RUN_NOW[job["id"]] == owner
    S._finish_job(A.store, A.store.job(job["id"]), A._start_run, A.store.desk)
    assert job["id"] not in S.RUN_NOW and _wait_runs(A, did)
    S._finish_job(A.store, A.store.job(job["id"]), A._start_run, A.store.desk)       # the schedule's own pass
    assert _wait_runs(A, did)
    starts = _events(A.store, "kind='action' AND desk_id=? AND surface IN ('run.you', 'run.auto')", (did,))
    assert [(s["surface"], s["lane"]) for s in starts] == [("run.you", "you"), ("run.auto", "auto")]
    for s in starts:
        lanes = {r[0] for r in A.store._conn.execute("SELECT DISTINCT lane FROM usage_events WHERE kind='request' "
                                                     "AND action_id=?", (s["action_id"],)).fetchall()}
        assert lanes == {s["lane"]}
    assert usage.counter(f"o:{owner}", "runs:auto")[0] == 1 and usage.counter(f"o:{owner}", "runs")[0] == 2


def test_byo_desk_runs_with_atlas_allowance_spent(app_client, monkeypatch):
    """T11, run side (spec §2): a desk on the owner's own key starts runs with Atlas's allowance and runs-at-once at
    0; its requests are BYO rows with the provider's cost, never limited by Atlas."""
    from atlas import secure as SEC
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-byo-run@example.com")
    _guard_models(monkeypatch)
    sent = []
    u = {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0004}

    def handler(req):
        sent.append(req.url.host)
        if json.loads(req.content).get("stream"):
            return httpx.Response(200, text=_sse([{"choices": [{"delta": {"content": "Nothing needed today."}}]},
                                                  {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": u}]),
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=_body("Nothing needed today.", usage=u))
    monkeypatch.setattr(SEC, "vetted_ips", lambda host, scope="public": ["93.184.216.34"])
    monkeypatch.setattr(SEC, "guarded_transport", lambda scope="public", **kw: httpx.MockTransport(handler))
    assert app_client.post(f"/api/desks/{did}/providers", json={"preset": "openrouter", "api_key": "sk-or-own-2222-3333"}).status_code == 200
    for k in ("DESK_DEFAULT_ENGINE", "DESK_LEAD_ENGINE", "HERMES_AGENT_URL", "DESK_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DESK_MODE", "live")
    usage.set_override(owner, "ai_requests_day", 0)
    usage.set_override(owner, "runs_at_once", 0)
    try:
        r = app_client.post("/api/runs", json={"task": "Anything new?"}, headers={"X-Desk": str(did)})
        assert r.status_code == 200, r.get_json()
        assert _wait_runs(A, did)
    finally:
        usage.clear_override(owner, "ai_requests_day")
        usage.clear_override(owner, "runs_at_once")
        monkeypatch.setenv("DESK_MODE", "demo")
    rows = _events(A.store, "kind='request' AND owner_id=?", (owner,))
    assert rows and sent and {(x["key_kind"], x["pool"], x["provider"]) for x in rows} == {("byo", "byo", "openrouter")}
    assert rows[0]["cost_usd"] == 0.0004 and A.store.run(r.get_json()["run_id"])["status"] == "done"
    assert not _events(A.store, "kind='refusal' AND owner_id=?", (owner,))


def test_seed_keeps_its_leads_when_a_run_is_refused(app_client, monkeypatch):
    """S9 (spec §4.6, §5.5): the sample leads are saved even when their runs are refused; a refused one carries
    `limited` in a 200 (one refusal no longer fails the seed). A model refusal falls back to the stock samples,
    flagged."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-seed@example.com")
    conf = A.store.desk(did).get("config") or {}
    conf.setdefault("ops", {})["cases"] = False                          # plain runs, so the refusal reaches the seed
    A.store.update_desk(did, config=conf)
    monkeypatch.setattr(usage.CFG, "demo", "enforce")
    monkeypatch.setattr(usage.CFG, "runs_at_once", 1)
    monkeypatch.setattr(A, "Orchestrator", _SlowOrch)
    _SlowOrch.gate.clear()
    try:
        r = app_client.post("/api/demo/seed", headers={"X-Desk": str(did)})
        items = r.get_json()
        assert r.status_code == 200 and len(items) == 3, items
        assert items[0]["run_id"].startswith("slow-") and "limited" not in items[0]
        assert [i["run_id"] for i in items[1:]] == ["", ""]
        assert all(i["limited"] == "1 run is going (limit 1). Start this when one finishes." for i in items[1:])
    finally:
        _SlowOrch.gate.set()
    assert _wait_runs(A, did) and len(A.store.for_desk(did).leads()) == 3
    # a customised business asks the model for its samples: refused at the limit, nothing is sent, stock stands in
    _guard_models(monkeypatch)
    conf = A.store.desk(did).get("config") or {}
    conf.setdefault("business", {})["name"] = "Usage Seed Plumbing"
    A.store.update_desk(did, config=conf)
    monkeypatch.setenv("DESK_MODE", "live")
    monkeypatch.setenv("DESK_PROVIDER", "openrouter")
    usage.set_override(owner, "ai_requests_day", 0)
    flags = {}
    try:
        rows = A._sample_leads(A.store.desk(did), flags)
    finally:
        usage.clear_override(owner, "ai_requests_day")
        monkeypatch.setenv("DESK_MODE", "demo")
    from atlas import templates
    assert rows == templates.SAMPLE_LEADS["sales_desk"] and flags == {"samples": "stock (limit)"}
    ref, = _events(A.store, "kind='refusal' AND owner_id=? AND surface='seed.leads'", (owner,))
    assert ref["limit_id"] == "ai_requests_day"


def test_limit_is_a_run_status_of_its_own(app_client, monkeypatch):
    """Spec §5.5: a run stopped at a limit reads status `limit` from the runs API; the health panel counts it as
    finished and `limited`, never as a failure."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-status@example.com")
    ds = A.store.for_desk(did)
    ds.create_run("lim-1", "Weekly summary", "auto", "")
    ds.finish_run("lim-1", "limit", "Stopped: AI requests today: 250 of 250 used.", 0, 0)
    ds.create_run("ok-1", "Daily check", "auto", "")
    ds.finish_run("ok-1", "done", "All good.", 0, 0)
    assert app_client.get("/api/runs/lim-1", headers={"X-Desk": str(did)}).get_json()["status"] == "limit"
    h = A._health(did)
    assert h["runs"]["limited"] == 1 and h["runs"]["failure_rate"] == 0.0 and h["runs"]["by_status"]["limit"] == 1
    assert not [a for a in h["alerts"] if a["key"] in ("failures", "failure_rate")]


def test_paid_model_on_house_key_needs_paid_allowance(app_client, monkeypatch, tmp_path):
    """T13 and T31's theatre part (spec §1.3 L3, L13, §4.1, §4.6 S2): the live view on a paid model needs the
    owner's paid allowance and Atlas's monthly cap, in demo mode too, refused as JSON before the stream; a :free
    model passes and streams, metered for the viewer."""
    import base64
    from test_vision import _jpeg
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t13@example.com")
    sse = _sse([{"choices": [{"delta": {"content": "A van at the gate."}}]},
                {"choices": [], "usage": {"prompt_tokens": 800, "completion_tokens": 9, "cost": 0}}])
    sent = _guard_models(monkeypatch, lambda req: httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"}))
    frame = base64.b64encode(Path(_jpeg(tmp_path / "gate.jpg")).read_bytes()).decode()
    hdr = {"X-Desk": str(did)}

    def live(model):
        return app_client.post("/api/vision/live", json={"image": frame, "model": model, "camera": "gate"}, headers=hdr)
    monkeypatch.setattr(usage.CFG, "owner_paid_usd_month", 0.0)
    b = _is_limit(live("google/gemini-2.5-flash"), "paid_usd_month")
    assert b["unit"] == "usd" and sent == []
    monkeypatch.setattr(usage.CFG, "owner_paid_usd_month", None)
    monkeypatch.setattr(usage.CFG, "house_paid_usd_month", 0.0)
    _is_limit(live("google/gemini-2.5-flash"), "house_paid_usd_month")
    assert sent == []
    r = live(FREE)
    assert r.status_code == 200 and "A van at the gate." in r.get_data(as_text=True) and len(sent) == 1
    row, = _events(A.store, "kind='request' AND owner_id=?", (owner,))
    assert (row["surface"], row["pool"], row["lane"], row["ref"]) == ("theatre.narrate", "free:openrouter", "you", "live:gate")
    # refused between the check and the request (the pool closed meanwhile): a meta line the page reads, no event
    monkeypatch.setattr(A.V, "vlm_precheck", lambda meter, model="": None)
    monkeypatch.setitem(usage._pool, "exhausted_at", time.time())
    body = live(FREE).get_data(as_text=True)
    meta = json.loads(body.splitlines()[0][1:])
    assert body.startswith("\x01") and meta["error"] == "usage_limit" and meta["limit_id"] == "pool_free_day"
    assert len(sent) == 1 and [e["question"] for e in A.store.vision_events(did, "live:gate", 0)] == ["live frame"]


def test_site_demo_stops_at_the_public_share(app_client, monkeypatch, tmp_path):
    """T31 (spec §1.3 L12, §4.6 S16): the website's demo counts in the public share, in demo mode too; at the share
    the stream answers {"error": "usage_limit", message, resets_at} and sends nothing; under it the one request is
    metered as public, lane auto."""
    import base64
    from atlas import secure as SEC
    from test_vision import _jpeg
    import atlas.desk.app as A
    sse = _sse([{"choices": [{"delta": {"content": "One person at the counter."}}]},
                {"choices": [], "usage": {"prompt_tokens": 500, "completion_tokens": 7, "cost": 0}}])
    sent = _guard_models(monkeypatch, lambda req: httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"}))
    monkeypatch.setenv("VISION_DEMO_KEY", "sk-or-demo-test")
    monkeypatch.setattr(A, "_RL_VISION", SEC.RateLimiter(10000, 60))
    frame = base64.b64encode(Path(_jpeg(tmp_path / "counter.jpg")).read_bytes()).decode()

    def demo():
        r = app_client.post("/api/vision/demo", json={"image": frame, "counts": {"person": 1}})
        return [json.loads(x[5:]) for x in r.get_data(as_text=True).splitlines() if x.startswith("data:")]
    before = len(_events(A.store, "kind='request' AND scope='public' AND surface='site.demo'"))
    usage.set_override(0, "public_requests_day", 0)
    try:
        evs = demo()
    finally:
        usage.clear_override(0, "public_requests_day")
    assert len(evs) == 1 and evs[0]["error"] == "usage_limit" and evs[0]["message"].startswith("Website demos today: 0 of 0 used.")
    assert evs[0]["resets_at"] and sent == []
    evs = demo()
    assert evs[-1].get("done") and "".join(e.get("t", "") for e in evs) == "One person at the counter." and len(sent) == 1
    rows = _events(A.store, "kind='request' AND scope='public' AND surface='site.demo'")
    assert len(rows) == before + 1 and (rows[-1]["lane"], rows[-1]["pool"], rows[-1]["tokens_in"]) == ("auto", "free:openrouter", 500)


# T33: every catch site knows UsageLimit (spec §4.6, §8.1). A try whose body calls a metered name and that has a broad
# handler needs an `except UsageLimit` first (or a broad handler that only re-raises). The rest is listed: allowed with
# its reason, or pending with the site id and the step that decides it. The lists are exact.
CATCH_MODULES = ("desk/app.py", "orchestrator.py", "browser.py", "vision.py", "journal.py", "objects.py",
                 "desk/scheduler.py", "study.py", "rag.py", "designer.py", "cases.py", "desk/showrun.py", "alerts.py")
CATCH_NAMES = frozenset({"chat", "describe", "vlm_detect", "chat_images", "chat_images_stream", "describe_video",
                         "describe_stream", "analyse", "embed_texts", "index_events", "retrieve", "ask", "write_note",
                         "verify_note", "maybe_rollup", "follow", "study", "_hermes_browse", "reply", "run_agent",
                         "_run_agent", "_execute_tools", "run_task", "camera_tick", "_start_run", "start_run",
                         "START_RUN", "LEAD_RUN", "_lead_case_run", "_design_team_for", "design_team", "_sample_leads",
                         "deliver", "_dispatch", "_deliver_metered"})
CATCH_ALLOWED = {
    ("orchestrator.py", "Orchestrator._record_tool"): "records.describe: a business-record lookup, not a model call",
}
CATCH_PENDING = {
    ("desk/app.py", "api_design_say.work"): "S10, merge: the designer track's turn and the limit turn (spec §4.7)",
    ("desk/app.py", "api_design_study.work"): "S11, merge: the study's done event carries the limit",
    ("study.py", "study"): "S35, merge: model_error is the limit message",
    ("designer.py", "_live_turn"): "S36, merge: the fixed limit turn, no retry",
}


def _handler_names(h) -> list[str]:
    t = h.type
    if t is None:
        return []
    return [getattr(x, "id", None) or getattr(x, "attr", None) or "" for x in (t.elts if isinstance(t, ast.Tuple) else [t])]


def _broad_catches() -> set[tuple[str, str]]:
    atlas_dir = Path(usage.__file__).parent
    found: set[tuple[str, str]] = set()

    def metered(stmts) -> bool:
        hit = []

        def visit(n):
            for ch in ast.iter_child_nodes(n):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                    continue                          # defined here, called elsewhere
                if isinstance(ch, ast.Try) and any("UsageLimit" in _handler_names(h) for h in ch.handlers):
                    continue                          # a nested try that knows the limit covers its own calls
                if isinstance(ch, ast.Call):
                    f = ch.func
                    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    if name in CATCH_NAMES or (name == "call" and getattr(getattr(f, "value", None), "id", "") == "OBJ"):
                        hit.append(name)
                visit(ch)
        for s in stmts:
            visit(ast.Module(body=[s], type_ignores=[]))
        return bool(hit)

    for rel in CATCH_MODULES:
        tree = ast.parse((atlas_dir / rel).read_text(encoding="utf-8"))

        def walk(node, stack):
            for ch in ast.iter_child_nodes(node):
                sub = stack + [ch.name] if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else stack
                if isinstance(ch, ast.Try):
                    names = [_handler_names(h) for h in ch.handlers]
                    broad = next((i for i, h in enumerate(ch.handlers)
                                  if h.type is None or {"Exception", "BaseException"} & set(names[i])), None)
                    if broad is not None and metered(ch.body):
                        typed = any("UsageLimit" in names[i] for i in range(broad))
                        last = ch.handlers[broad].body[-1] if ch.handlers[broad].body else None
                        reraise = isinstance(last, ast.Raise) and last.exc is None
                        if not (typed or reraise):
                            found.add((rel, ".".join(stack)))
                walk(ch, sub)
        walk(tree, [])
    return found


def test_catch_sites_know_usage_limit(app_client, monkeypatch, tmp_path):
    """T33 (spec §4.6, §8.1): the audit, then the routes. With the owner's allowance full, a video, an Ask, a camera
    look with a question and an object's call and question each answer 429 with the limit body before any request is
    sent; a look refused inside the tick leaves the camera connector's status alone; an object call leaves no
    'vision model unavailable' note and does not turn watching on."""
    found = _broad_catches()
    want = set(CATCH_ALLOWED) | set(CATCH_PENDING)
    assert found == want, "update CATCH_ALLOWED / CATCH_PENDING: " + repr(sorted(found ^ want))
    from test_names import _sighting
    from test_vision import _jpeg
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t33@example.com", template="site_watch")
    sent = _guard_models(monkeypatch)
    hdr = {"X-Desk": str(did)}
    cam = A.store.add_connector(did, "camera", "door", {"source": _jpeg(tmp_path / "door.jpg")}, False)
    status0 = A.store.connector(cam["id"]).get("status")
    oid = _sighting(A.store.for_desk(did), "door")
    usage.set_override(owner, "ai_requests_day", 0)
    try:
        _is_limit(app_client.post("/api/vision/video", json={"url": "https://videos.example.com/a.mp4", "question": "?"},
                                  headers=hdr), "ai_requests_day")
        _is_limit(app_client.post(f"/api/objects/{oid}/call", json={"on": True}, headers=hdr), "ai_requests_day")
        _is_limit(app_client.post(f"/api/objects/{oid}/ask", json={"question": "What is it?"}, headers=hdr), "ai_requests_day")
        monkeypatch.setenv("DESK_MODE", "live")              # the Ask's re-look and a look's question are live-only
        monkeypatch.setenv("DESK_PROVIDER", "openrouter")
        _is_limit(app_client.post("/api/vision/ask", json={"question": "Who came in?"}, headers=hdr), "ai_requests_day")
        _is_limit(app_client.post(f"/api/cameras/{cam['id']}/look", json={"question": "Who is at the door?"}, headers=hdr),
                  "ai_requests_day")
        monkeypatch.setenv("DESK_MODE", "demo")

        def refused(*a, **k):                              # refused inside the tick, after the route's check
            raise usage._limit("ai_requests_day", used=0, limit=0, now=time.time())
        monkeypatch.setattr(A.scheduler, "camera_tick", refused)
        _is_limit(app_client.post(f"/api/cameras/{cam['id']}/look", json={}, headers=hdr), "ai_requests_day")
    finally:
        usage.clear_override(owner, "ai_requests_day")
        monkeypatch.setenv("DESK_MODE", "demo")
    assert sent == [] and _events(A.store, "kind='request' AND owner_id=?", (owner,)) == []
    assert A.store.connector(cam["id"]).get("status") == status0
    ds = A.store.for_desk(did)
    assert ds.object_notes(oid, 60) == [] and not ds.vision_object(oid).get("watch")
    refusals = {(r["surface"], r["limit_id"]) for r in _events(A.store, "kind='refusal' AND owner_id=?", (owner,))}
    assert refusals >= {("video.describe", "ai_requests_day"), ("objects.call", "ai_requests_day"),
                        ("objects.ask", "ai_requests_day"), ("ask.cameras", "ai_requests_day"),
                        ("camera.look", "ai_requests_day")}


def test_auto_actions_inside_tools_respect_l8(U, store, monkeypatch):
    """T37 (spec §1.3 L8, §4.6 S27): at the cap on actions sent without approval, an auto calendar booking, an auto
    MCP write, an auto HTTP POST, an auto Higgsfield generation and a pre-approved browser submit each wait for
    approval with the note, and nothing goes out; MCP reads and HTTP GETs still run. Under the cap the action runs
    and counts once."""
    from atlas import browser as BR
    from atlas import integrations as I
    from atlas import mcp_client as M
    desk = store.add_desk(90, "Auto desk", "blank", "free", {})
    did = desk["id"]
    ds = store.for_desk(did)
    for kind, name in (("gcal", "cal"), ("http", "crm"), ("higgsfield", "hf")):
        store.add_connector(did, kind, name, {"base_url": "https://crm.example.com"}, True)
    mcp = store.add_connector(did, "mcp", "notes", {}, True)
    direct = []
    monkeypatch.setattr(I, "gcal_create_event", lambda *a, **k: direct.append("gcal") or "booked")
    monkeypatch.setattr(I, "http_call", lambda cfg, method, path, params=None, body=None: direct.append(f"http {method}")
                        or {"status": 200, "json": {"ok": True}})
    monkeypatch.setattr(I, "higgsfield_generate", lambda *a, **k: direct.append("hf") or {"status": "done", "outputs": [], "model": "m"})
    monkeypatch.setattr(M.REGISTRY, "get", lambda conn: SimpleNamespace(call=lambda tool, args: direct.append(f"mcp {tool}") or "ok"))
    m = U.run_meter(desk, "auto")
    orch = _orch(m, tools=("calendar_book", "http_request", "generate_media", "mcp", "browse"), store=ds,
                 browser_allow_submit=True)
    orch.run_id = "r-auto"
    orch._mcp_index = {"mcp__notes__create_note": (mcp, "create_note"), "mcp__notes__echo": (mcp, "echo")}
    agent = orch.agents["atlas"]

    def tool(name, args):
        return orch._tool(agent, P.ToolCall("c", name, args), 0)
    U.set_override(90, "auto_sends_day", 0)
    outs = [tool("calendar_book", {"title": "Valuation", "start": "2026-10-12T10:00"}),
            tool("mcp__notes__create_note", {"text": "x"}),
            tool("http_request", {"connector": "crm", "method": "POST", "path": "/leads"}),
            tool("generate_media", {"prompt": "a poster"})]
    note = "auto-send paused: Sent without approval today 0/0; approve to send"
    assert direct == [] and all("queued for approval" in o and o.endswith(note + ".") for o in outs), outs
    pend = ds.actions("pending")
    assert len(pend) == 4 and {p["reason"] for p in pend} == {note}
    assert tool("mcp__notes__echo", {"text": "hi"}) == "ok"
    assert '"ok": true' in tool("http_request", {"connector": "crm", "method": "GET", "path": "/leads"})
    assert direct == ["mcp echo", "http GET"]
    # the browser: a pre-approved submit asks the gate first; held, it stops for approval with the note
    hand = SimpleNamespace(allow_submit=True, submit_gate=orch._submit_gate)
    with pytest.raises(BR.SubmitBlocked) as e:
        BR.BrowserHand._guard_submit(hand, 'click button "Book" on https://shop.example.com', {"action": "click"})
    assert e.value.note == note
    seen = {}

    def run_task(*a, **k):
        seen.update(k)
        return BR.Result(status="needs_approval", answer="Ready to submit", pending={
            "description": "click Book", "action": {"action": "click"}, "url": "https://shop.example.com/book", "note": note})
    monkeypatch.setattr(BR, "run_task", run_task)
    out = tool("browse", {"task": "Book the slot", "url": "https://shop.example.com"})
    assert "Queued for owner approval" in out and seen["submit_gate"] == orch._submit_gate and seen["meter"] is m
    assert next(a for a in ds.actions("pending") if a["kind"] == "browser_action")["reason"] == note
    refusals = _events(store, "kind='refusal' AND owner_id=90")
    assert {(r["surface"], r["limit_id"], r["units"]) for r in refusals} == {
        (f"action.{k}", "auto_sends_day", 1) for k in ("calendar", "mcp", "http", "higgsfield", "browser")}
    U.clear_override(90, "auto_sends_day")
    assert tool("calendar_book", {"title": "Valuation", "start": "2026-10-12T10:00"}) == "booked"
    assert U.counter(f"d{did}", "send:auto")[0] == 1
    BR.BrowserHand._guard_submit(hand, "click Book", {"action": "click"})   # room again: the submit goes ahead
    assert U.counter(f"d{did}", "send:auto")[0] == 2


def test_browser_steps_count_for_the_run_and_stop_at_a_limit(U, store, monkeypatch):
    """S25 (spec §4.6): a browser task whose next model step is refused stops with status limit, not a 'model error'
    the run carries on from; its steps are the run's requests, labelled browse.step. In a run, the browse tool then
    answers with the typed LIMIT result and is not started again."""
    from atlas import browser as BR

    class Hand:                                            # the page side of the browser hand, without Chromium
        allow_submit, submit_gate, macro_steps = False, None, []
        page = SimpleNamespace(url="https://shop.example.com/")

        def observe(self):
            return SimpleNamespace(render=lambda: "[1] link \"Prices\"", digest=lambda: "same", text="Prices", url=self.page.url)
    desk = store.add_desk(91, "Browse desk", "blank", "free", {})
    m = U.run_meter(desk, "you")
    m.run_cap = 1
    prov = _Script([("tool", "extract", {"note": "price"})])
    prov.meter = m
    with U.step_surface("browse.step"):
        res = BR.run_task("Find the boiler price", "", prov, FREE, hand=Hand())
    assert res.status == "limit" and res.limit.limit_id == "run_requests" and prov.calls == 1
    assert res.summary().startswith("STOPPED AT A USAGE LIMIT. This run made 1 AI requests (per-run cap 1).")
    row, = _events(store, "kind='request' AND owner_id=91")
    assert (row["surface"], row["action_id"]) == ("browse.step", m.action_id)
    ref, = _events(store, "kind='refusal' AND owner_id=91")
    assert (ref["surface"], ref["limit_id"]) == ("browse.step", "run_requests")
    started = []

    def run_task(*a, **k):
        started.append(1)
        return BR.Result(status="limit", answer=res.limit.message, limit=res.limit)
    monkeypatch.setattr(BR, "run_task", run_task)
    m2 = U.run_meter(desk, "you")
    orch = _orch(m2, tools=("browse",))
    call = P.ToolCall("b1", "browse", {"task": "Find the boiler price", "url": "https://shop.example.com"})
    first = orch._execute_tools(orch.agents["atlas"], [call], 0)
    again = orch._execute_tools(orch.agents["atlas"], [call], 0)
    assert first == again and first[0][3] is True and len(started) == 1 and "browse" in m2.limited
    assert first[0][2] == ("LIMIT: AI requests per run or link study 1/1. browse is unavailable for the rest of this "
                           "run; do not call it again.")


def test_showrun_needs_two_requests_of_the_public_share(U, store, monkeypatch):
    """S53 (spec §1.3 L12, §1.6): the site's live run starts only with 2 requests left in the website's share; held,
    the feed says why and shows the recorded run until the reset, with one refusal on the ledger. A run that starts
    carries the public meter (lane auto, the per-run cap)."""
    from atlas import templates
    from atlas.desk import showrun as SR
    from atlas.orchestrator import Event
    built = []

    class Rec:
        def __init__(self, configs, store_, emit, meter=None):
            built.append(meter)
            self.run_id, self.emit = "show-1", emit

        def run(self, task, mode="auto"):
            self.emit(Event("done", "system", "ok", {"status": "done"}))

        def cancel(self):
            pass
    monkeypatch.setattr(SR, "Orchestrator", Rec)

    def configs(d):
        return {"agents": [{"id": "atlas", "name": "Atlas", "provider": "demo"}], "providers": {}, "business": {}}
    sr = SR.ShowRunner({"store": store, "desk_configs": configs, "mode": lambda: "live", "blocked": lambda: "",
                        "templates": templates, "template": "sales_desk"})
    U.set_override(0, "public_requests_day", 1)
    sr.touch()
    reason = "The live run has used today's share; next one after 00:00 UTC."
    assert sr.run is None and built == [] and sr.offline_reason() == reason and sr.starts == []
    snap = sr.snapshot()
    assert snap["live"] is False and snap["reason"] == reason
    ref, = _events(store, "kind='refusal' AND scope='public'")
    assert (ref["limit_id"], ref["surface"]) == ("public_requests_day", "show.run")
    U.clear_override(0, "public_requests_day")
    sr.touch()                                             # held: looked at again at most once a minute
    assert built == []
    sr.limited["checked"] = 0
    sr.touch()
    assert len(built) == 1 and sr.limited is None and sr.offline_reason() == ""
    m = built[0]
    assert (m.scope, m.surface, m.lane, m.owner_id, m.run_cap) == ("public", "show.run", "auto", 0, 120)


# ---------------------------------------------------------------------------- background work (step 6)
def _no_cases(A, did):
    conf = A.store.desk(did).get("config") or {}
    conf.setdefault("ops", {})["cases"] = False
    A.store.update_desk(did, config=conf)


def test_background_lane_cannot_eat_the_interactive_half(app_client, monkeypatch):
    """T5 (spec §1.3 L2, §1.6, S47): with Atlas's own share of today spent, a task job's run is refused: the job is
    skipped with a "limit:" result and waits for the reset instead of the hourly backoff, a one-shot job stays on for
    that retry, and a run the owner starts still goes."""
    import atlas.desk.scheduler as S
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t5@example.com")
    monkeypatch.setattr(usage.CFG, "demo", "enforce")                   # the scripted desk counts like a house one
    monkeypatch.setattr(A, "Orchestrator", _SlowOrch)
    _SlowOrch.gate.set()
    hourly = A.store.add_job(did, "task", "Hourly check", "Check the inbox and say what came in", 60, time.time())
    once = A.store.add_job(did, "task", "Once", "Send the weekly summary", 0, time.time())
    usage.set_override(owner, "ai_requests_auto_day", 0)
    try:
        for j in (hourly, once):
            S._finish_job(A.store, A.store.job(j["id"]), A._start_run, A.store.desk)
        reset = usage.day_start(time.time()) + 86400
        for j in (hourly, once):
            row = A.store.job(j["id"])
            assert (row["last_status"], row["enabled"]) == ("skipped", 1), row
            assert row["last_result"] == "limit: AI requests Atlas made on its own 0/0; resumes 00:00 UTC"
            assert row["next_run"] >= reset
        assert A.store.job(once["id"])["next_run"] == reset
        r = app_client.post("/api/runs", json={"task": "What came in today?"}, headers={"X-Desk": str(did)})
        assert r.status_code == 200, r.get_json()
    finally:
        usage.clear_override(owner, "ai_requests_auto_day")
        for j in (hourly, once):
            A.store.delete_job(j["id"])
    assert _wait_runs(A, did)
    starts = _events(A.store, "kind='action' AND desk_id=? AND surface IN ('run.you', 'run.auto')", (did,))
    assert [s["surface"] for s in starts] == ["run.you"]
    ref, = _events(A.store, "kind='refusal' AND owner_id=?", (owner,))
    assert (ref["limit_id"], ref["surface"], ref["units"]) == ("ai_requests_auto_day", "run.auto", 2)


def test_auto_send_over_cap_waits_for_approval(U, store):
    """T18 (spec §1.3 L8, §1.6, S52): with one send without approval allowed today, the second auto alert stays
    pending with the "auto-send paused" note: neither sent nor failed. A send the channel's own rule holds (the reply
    window, the day's SMS) stays pending with that rule's words and gives its unit back; a send that fails was an
    attempt and counts."""
    from atlas import alerts as AL
    desk = store.add_desk(92, "Alert desk", "site_watch", "free", {})
    did = desk["id"]
    sent = []

    def dispatch(d, row):
        sent.append(row["id"])
        return "[slack ok]"

    def alert(kind, to, dispatcher):
        return AL.queue(store, desk, kind, to, "door: person", "Camera door: 1 person.", "camera alert on door: person",
                        True, dispatcher)
    U.set_override(92, "auto_sends_day", 1)
    a1, a2 = alert("slack", "", dispatch), alert("slack", "", dispatch)
    note = "auto-send paused: Sent without approval today 1/1; approve to send"
    assert a1["status"] == "sent" and sent == [a1["id"]]
    assert (a2["status"], a2["held"], a2["reason"]) == ("pending", note, "camera alert on door: person · " + note)
    assert store.action(a2["id"])["status"] == "pending" and not store.action(a2["id"])["decided_by"]
    assert U.counter(f"d{did}", "send:auto")[0] == 1
    ref, = _events(store, "kind='refusal' AND owner_id=92")
    assert (ref["limit_id"], ref["surface"], ref["units"]) == ("auto_sends_day", "send.slack", 1)
    U.set_override(92, "auto_sends_day", 10)

    def window(d, row):
        raise U.ChannelWindow("whatsapp")

    def sms_cap(d, row):
        raise U._limit("sms_segments_day", used=50, limit=50, now=U._now())
    a3, a4 = alert("whatsapp", "+447700900123", window), alert("sms", "+447700900123", sms_cap)
    assert a3["status"] == "pending" and a3["held"] == ("Outside WhatsApp's 24-hour reply window: a template message "
                                                       "is needed, which Atlas can't send yet.")
    assert a4["status"] == "pending" and a4["held"] == "auto-send paused: SMS sent today 50/50; resets 00:00 UTC"
    assert U.counter(f"d{did}", "send:auto")[0] == 1                    # nothing went out: their units came back
    acts = _events(store, "kind='action' AND owner_id=92")
    assert [(a["surface"], a["status"]) for a in acts] == [("send.slack", "ok"), ("send.whatsapp", "not_sent"),
                                                            ("send.sms", "not_sent")]
    assert store.for_desk(did).messages("+447700900123") == []          # nothing out on the customer's thread

    def boom(d, row):
        raise RuntimeError("webhook 500")
    a5 = alert("slack", "", boom)
    assert a5["status"] == "failed" and "webhook 500" in a5["note"] and U.counter(f"d{did}", "send:auto")[0] == 2
    assert [a["status"] for a in store.for_desk(did).actions()].count("pending") == 3


def test_hook_lead_is_saved_and_deferred_when_the_run_is_refused(app_client, monkeypatch):
    """T25 (spec §1.6, S12): with Atlas's own share spent and cases off, the web-form hook still answers 200 ok. The
    lead is stored, waiting, with no run; a deferred start is on the ledger and the Leads list says why. After the
    reset the scheduler's deferred pass starts its run, in lane auto."""
    import atlas.desk.scheduler as S
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t25@example.com")
    _no_cases(A, did)
    monkeypatch.setattr(usage.CFG, "demo", "enforce")
    monkeypatch.setattr(A, "Orchestrator", _SlowOrch)
    _SlowOrch.gate.set()
    token = A.store.ensure_hook_token(did)
    usage.set_override(owner, "ai_requests_auto_day", 0)
    try:
        r = app_client.post(f"/hook/{token}", json={"name": "Rae", "email": "rae@example.com",
                                                     "notes": "A quote for a new boiler?"})
        b = r.get_json()
        assert r.status_code == 200 and b["ok"] is True and b["run_id"] == "", b
        lead = A.store.lead(b["lead_id"])
        assert lead["status"] == "waiting" and not lead["run_id"]
        row, = usage.deferred(owner)
        reset = usage.day_start(time.time()) + 86400
        assert (row["ref_id"], row["kind"], row["limit_id"], row["retry_at"]) == (b["lead_id"], "lead",
                                                                                 "ai_requests_auto_day", reset)
        listed = next(x for x in app_client.get("/api/leads", headers={"X-Desk": str(did)}).get_json()
                      if x["id"] == b["lead_id"])
        assert listed["waiting"] == "Not worked yet: AI requests Atlas made on its own 0/0; runs after 00:00 UTC"
        assert S.deferred_pass(A.store, time.time()) == 0 and len(usage.deferred(owner)) == 1   # not before the reset
    finally:
        usage.clear_override(owner, "ai_requests_auto_day")
    assert S.deferred_pass(A.store, reset + 1) == 1                      # the reset: Atlas's share has room again
    assert _wait_runs(A, did)
    lead = A.store.lead(b["lead_id"])
    assert lead["status"] == "processed" and lead["run_id"].startswith("slow-")
    assert usage.deferred(owner) == [] and usage.deferred(owner, "started")[0]["tries"] == 1
    start, = _events(A.store, "kind='action' AND desk_id=? AND surface='run.auto'", (did,))
    assert (start["ref"], start["lane"]) == (lead["run_id"], "auto")


def test_case_waits_for_retry_at_and_keeps_its_run_budget(U, store, monkeypatch):
    """T26 (spec §1.6, S49): a case step refused at a limit wakes within 5 minutes after the limit's retry time, uses
    none of the case's daily run budget, says so once on its timeline and keeps the lead and instructions it was
    started with; the clock's start after the reset carries them, in lane auto."""
    from atlas import cases as C
    from atlas import records as R
    desk = store.add_desk(93, "Case desk", "sales_desk", "free", {})
    tried = []

    def refused(desk_, task, mode, lead_id=None, case_id=None):
        tried.append((lead_id, case_id, U.bound_lane()))
        raise U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())
    monkeypatch.setattr(C, "START_RUN", refused)
    monkeypatch.setattr(C, "RUN_STATUS", lambda rid: "done")
    c = C.open_case(store, desk["id"], "enquiry", "Boiler quote", 0)
    assert C.kick(store, desk, c, extra="Reply by email (queue_action kind=email).", lead_id=7) == ""
    with pytest.raises(U.UsageLimit):                                  # refused again, raised for a caller that reports it
        C.kick(store, desk, C.get(store, c["id"]), raise_limit=True)
    held = C.get(store, c["id"])
    retry = U.day_start(U._now()) + 86400
    assert retry <= held["wake_at"] <= retry + 300 and (held["waiting_for"], held["active_run"]) == ("atlas", "")
    assert not held["data"].get("run_starts") and tried == [(7, c["id"], ""), (7, c["id"], "")]
    assert held["data"]["limit"]["limit_id"] == "ai_requests_auto_day"
    assert C.held_leads(store, desk["id"]) == {7: held["data"]["limit"]}
    lines = [t["text"] for t in R.timeline_for(store, case_ids=[c["id"]]) if t["kind"] == "paused"]
    assert lines == ["Paused: AI requests Atlas made on its own 125/125; resumes after 00:00 UTC"]
    started = []

    def start(desk_, task, mode, lead_id=None, case_id=None):
        started.append((task, lead_id, U.bound_lane()))
        return "r-1"
    monkeypatch.setattr(C, "START_RUN", start)
    C._set(store, c["id"], wake_at=time.time() - 1)                      # the reset
    assert C.tick(store)["woke"] == 1
    task, lead_id, lane = started[0]
    assert (lead_id, lane) == (7, "auto") and "Reply by email (queue_action kind=email)." in task
    data = C.get(store, c["id"])["data"]
    assert len(data["run_starts"]) == 1 and not {"lead_id", "limit", "held_extra"} & set(data)


def test_busy_runs_and_inbox_lose_nothing(app_client, monkeypatch):
    """T36 (spec §1.6, S12, S46, S49): with no room for another run at once (L5), a web-form lead's case waits about
    a minute (the busy retry), not until 00:00, and keeps its run budget; the owner pressing Run on it gets the 429.
    An inbox pass of 5 mails stores all 5 messages and leads before any run, and with cases off defers every run
    after one refused try."""
    import atlas.desk.scheduler as S
    from atlas import cases as C
    from atlas import integrations as I
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t36@example.com")
    monkeypatch.setattr(usage.CFG, "demo", "enforce")
    token = A.store.ensure_hook_token(did)
    hdr = {"X-Desk": str(did)}
    usage.set_override(owner, "runs_at_once", 0)
    case, job = None, None
    t0 = time.time()
    try:
        b = app_client.post(f"/hook/{token}", json={"name": "Ivo", "email": "ivo@example.com",
                                                     "notes": "Can you fit a boiler this week?"}).get_json()
        assert b["ok"] is True and b["run_id"] == "" and A.store.lead(b["lead_id"])["status"] == "waiting", b
        case = next(c for c in C.list_cases(A.store, did, open_only=True) if c["data"].get("lead_id") == b["lead_id"])
        assert t0 + 60 <= case["wake_at"] <= time.time() + 61 + 300 and case["waiting_for"] == "atlas"
        assert not case["data"].get("run_starts") and usage.deferred(owner) == []
        listed = next(x for x in app_client.get("/api/leads", headers=hdr).get_json() if x["id"] == b["lead_id"])
        assert listed["waiting"] == "Not worked yet: 0 runs are going; starts when one finishes"
        _is_limit(app_client.post(f"/api/cases/{case['id']}/run", json={}, headers=hdr), "runs_at_once")
        again = C.get(A.store, case["id"])
        assert not again["data"].get("run_starts") and again["wake_at"] >= t0 + 60 and again["active_run"] == ""
        # the inbox, cases off: every mail is stored before any run; the first refusal defers all five
        _no_cases(A, did)
        mails = [{"from_email": f"mail{i}@example.com", "from_name": f"Mail {i}", "subject": f"Quote {i}",
                  "body": f"Hello, a quote please ({i})"} for i in range(5)]
        A.store.add_connector(did, "imap", "inbox", {"host": "imap.example.com", "user": "u", "password": "p"}, False)
        monkeypatch.setattr(I, "fetch_unseen", lambda cfg, limit=5: [dict(m) for m in mails])
        job = A.store.add_job(did, "inbox_watch", "Inbox", "", 5, time.time())
        S._finish_job(A.store, A.store.job(job["id"]), A._start_run, A.store.desk)
        row = A.store.job(job["id"])
        assert row["last_status"] == "ok", row
        assert row["last_result"].startswith("5 new email(s); 5 waiting: Runs at the same time 0/0, runs after ")
        leads = [x for x in A.store.for_desk(did).leads() if x["source"] == "email"]
        assert len(leads) == 5 and {x["status"] for x in leads} == {"waiting"}
        n_in = A.store._conn.execute("SELECT COUNT(*) FROM messages WHERE desk_id=? AND channel='email' AND dir='in'",
                                     (did,)).fetchone()[0]
        assert n_in == 5
        rows = usage.deferred(owner)
        assert sorted(r["ref_id"] for r in rows) == sorted(x["id"] for x in leads)
        assert {r["limit_id"] for r in rows} == {"runs_at_once"}
        assert all(t0 + 60 <= r["retry_at"] <= time.time() + 61 for r in rows)
        refusals = {(r["surface"], r["limit_id"]): r["units"]
                    for r in _events(A.store, "kind='refusal' AND owner_id=?", (owner,))}
        assert refusals == {("run.auto", "runs_at_once"): 2, ("run.you", "runs_at_once"): 1}   # one try each, no more
    finally:
        usage.clear_override(owner, "runs_at_once")
        for r in usage.deferred(owner):                                 # keep the session's scheduler off this desk
            usage._set_deferred(r["id"], status="dropped")
        if case:
            C._set(A.store, case["id"], wake_at=None)
        if job:
            A.store.delete_job(job["id"])


def test_case_run_stopped_at_a_limit(U, store, monkeypatch):
    """T39's case part (spec §1.6, §5.5, S50): a step whose run stopped at the per-run cap goes to the owner and the
    clock never re-runs it; one stopped at the day's allowance runs again after the reset. The run's line on the
    timeline is a limit, not a failure."""
    from atlas import cases as C
    from atlas import records as R
    runs = []

    def start(desk_, task, mode, lead_id=None, case_id=None):
        runs.append(case_id)
        return f"r{len(runs)}"
    monkeypatch.setattr(C, "START_RUN", start)
    monkeypatch.setattr(C, "RUN_STATUS", lambda rid: "limit")
    desk = store.add_desk(94, "Limit desk", "sales_desk", "free", {})
    capped = C.open_case(store, desk["id"], "task", "Reconcile the invoices", 0)
    rid = C.kick(store, desk, capped)
    cap = U._limit("run_requests", used=120, limit=120, now=U._now()).body()
    C.run_finished(store, capped["id"], rid, "limit", "Stopped: " + cap["message"], limit=cap)
    c = C.get(store, capped["id"])
    assert (c["waiting_for"], c["wake_at"], c["active_run"]) == ("owner", None, "")
    tl = R.timeline_for(store, case_ids=[capped["id"]], limit=2)
    assert tl[0]["text"] == "the step stopped at the per-run cap (120 AI requests): check it or press Run again"
    assert tl[1]["kind"] == "run_limit" and tl[1]["text"].startswith("Stopped: This run made 120 AI requests")
    daily = C.open_case(store, desk["id"], "task", "Chase the supplier", 0)
    rid = C.kick(store, desk, daily)
    day = U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now()).body()
    C.run_finished(store, daily["id"], rid, "limit", "Stopped: " + day["message"], limit=day)
    d = C.get(store, daily["id"])
    reset = U.day_start(U._now()) + 86400
    assert d["waiting_for"] == "atlas" and reset <= d["wake_at"] <= reset + 300
    assert C.tick(store)["woke"] == 0 and runs == [capped["id"], daily["id"]]     # neither is re-run now
    assert (C.lane(c), C.lane(d)) == ("you", "atlas")


def _limit_steps(limit, now, task="Chase the Jones invoice"):
    from atlas import nextsteps as NS
    steps = NS.normalise([], NS.run_done("limit", task, 2, ["till"], limit=limit, now=now), allow=NS.RUN)
    assert len(steps) == 3 and all(s["action"] in NS.RUN for s in steps)
    assert steps[0]["href"] == usage.USAGE_URL and steps[1]["action"] == "job" and steps[1]["job"] == task
    assert steps[1]["detail"] == "The last run ended at a usage limit; prefilled, runs only when you press Deploy"
    return steps


def test_run_done_at_a_limit_gives_the_limits_own_steps(U, monkeypatch):
    """T39's next-steps part (spec §5.5): a run stopped at a limit gets three RUN steps of its own (never a say step,
    never the approvals or camera steps of a failed run): what was used on /desk/usage, the job again after the limit's
    UTC reset (rounded up to the minute; "Run it again" alone when nothing resets it, as at the per-run cap, or the
    reset has passed), and the owner's own key on /desk#setup only for a limit that key lifts. The numbers come from
    the limit's own message, without its relative reset time."""
    from atlas import nextsteps as NS
    now = _ts(2026, 10, 9, 14, 5, 10)
    _clock(monkeypatch, now)
    assert NS._ENDED["limit"] == "at a usage limit" and NS._KEY_LIFTS <= set(U.LIMITS) and "run_requests" not in NS._KEY_LIFTS
    bare = NS.normalise([], NS.run_done("limit"), allow=NS.RUN)          # the spec's T39 call, as it stands
    assert len(bare) == 3 and "/desk/usage" in [s.get("href") for s in bare] and not any(s["action"] == "say" for s in bare)
    day = _limit_steps(U._limit("ai_requests_day", used=250, limit=250, now=now).body(), now)
    assert [s["label"] for s in day] == ["See what was used", "Run it again after 00:00 UTC", "Use your own model key"]
    assert day[0]["detail"] == "AI requests today: 250 of 250 used" and day[2] == {
        "label": "Use your own model key", "detail": "The desk's agents then run and bill on your own account instead of Atlas's",
        "action": "open", "href": "/desk#setup"}
    month = _limit_steps(U._limit("paid_usd_month", used=2.5, limit=2.5, now=now).body(), now)
    assert month[1]["label"] == "Run it again after 00:00 UTC on 1 Nov"
    assert month[0]["detail"] == "Paid models this month: $2.50 of $2.50 used"
    minute = U._limit("pool_free_min", used=20, limit=20, now=now, retry_at=now + 40)    # 14:05:50: not "after 14:05"
    assert _limit_steps(minute.body(), now)[1]["label"] == "Run it again after 14:06 UTC"
    assert _limit_steps(minute.body(), now + 120)[1]["label"] == "Run it again"         # that time has passed
    cap = _limit_steps(U._limit("run_requests", used=120, limit=120, now=now).body(), now)
    assert [s["label"] for s in cap] == ["See what was used", "Run it again", "See how far it got"]
    assert cap[0]["detail"] == "This run made 120 AI requests (per-run cap 120)" and cap[2]["href"] == "/desk#runs"
    unknown = _limit_steps(None, now, task="")                       # no body (the client said "limit"): nothing claimed
    assert [s["label"] for s in unknown] == ["See what was used", "Run it again", "See how far it got"]
    assert unknown[0]["detail"] == "Every limit, what used it and when it resets" and unknown[1]["job"] == ""
    for junk in ({"retry_at": "soon", "message": 5, "limit_id": ["ai_requests_day"]}, {"resets_at": float("nan")},
                 {"retry_at": float("inf")}, {"resets_at": 1e300}, {"retry_at": True}, "ai_requests_day"):
        assert [s["label"] for s in _limit_steps(junk, now)][1:] == ["Run it again", "See how far it got"], junk
    assert NS.run_done("error", "x", 0, [], limit=U._limit("ai_requests_day", used=1, limit=1, now=now).body())[0][
        "label"] == "Run the job again"                              # a limit only changes a run stopped at one


def test_api_next_after_a_run_stopped_at_a_limit(app_client, monkeypatch):
    """T39's next-steps part, end to end (spec §5.5): a desk run that stops at the owner's daily allowance halfway
    is stored with status `limit`; POST /api/next for it answers the limit's three steps, built from the run's own
    done event (the reset, the numbers, the owner's key). A client's word alone ("limit", no run id) gets the steps
    that claim nothing."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-next-limit@example.com")
    monkeypatch.setattr(usage.CFG, "demo", "enforce")                   # the scripted desk counts like a house one
    hdr = {"X-Desk": str(did)}
    task = "Draft a two-line hello to a new customer"
    usage.set_override(owner, "ai_requests_day", 2)                     # the start's headroom, then a stop halfway
    try:
        rid = app_client.post("/api/runs", json={"task": task}, headers=hdr).get_json()["run_id"]
        assert _wait_runs(A, did)
    finally:
        usage.clear_override(owner, "ai_requests_day")
    run = A.store.run(rid)
    assert run["status"] == "limit" and run["summary"].startswith("Stopped: AI requests today: 2 of 2 used."), run
    assert A._run_limit(rid)["limit_id"] == "ai_requests_day"
    r = app_client.post("/api/next", json={"context": "run_done", "run_id": rid, "status": "done", "task": "ignored"},
                        headers=hdr).get_json()
    assert r["next"] == [
        {"label": "See what was used", "detail": "AI requests today: 2 of 2 used", "action": "open", "href": "/desk/usage"},
        {"label": "Run it again after 00:00 UTC", "detail": "The last run ended at a usage limit; prefilled, runs only when "
         "you press Deploy", "action": "job", "job": task},
        {"label": "Use your own model key", "detail": "The desk's agents then run and bill on your own account instead of "
         "Atlas's", "action": "open", "href": "/desk#setup"}]
    said = app_client.post("/api/next", json={"context": "run_done", "status": "limit", "task": task}, headers=hdr).get_json()
    assert [s["label"] for s in said["next"]] == ["See what was used", "Run it again", "See how far it got"]


def test_a_refused_alert_run_is_one_event_per_window(U, store, monkeypatch, tmp_path):
    """S44 (spec §1.6): a camera alert whose run is refused at a limit keeps its own event (the alert needs no AI);
    one more event per camera and limit window says why no run started, and later refusals in that window update it
    instead of adding events. Nothing retries the run."""
    import atlas.desk.scheduler as S
    from atlas import alerts as AL
    from atlas import vision as V
    from test_vision import PERSON, FakeDetector, _jpeg
    AL.reset(); S._last_frame.clear(); S._last_seen.clear(); S._present.clear(); S._run_held.clear()
    monkeypatch.setattr(V, "DETECTOR", FakeDetector([PERSON]))
    monkeypatch.setattr(V, "vlm_ready", lambda: False)
    desk = store.add_desk(95, "Gate desk", "blank", "free", {"ops": {"camera_cases": False}})
    conn = store.add_connector(desk["id"], "camera", "gate", {"source": _jpeg(tmp_path / "gate.jpg", blob=(40, 30, 120, 220)),
                                                             "watch_for": "person", "min_count": 1, "cooldown_min": 0,
                                                             "repeat": "always"}, False)
    tries = []

    def refused(d, task, mode):
        tries.append(task)
        raise U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())
    for _ in range(3):
        r = S.camera_tick(store, desk, conn, refused, True, force=True)
        assert r["triggered"] and r["run_id"] == ""
    evs = store.for_desk(desk["id"]).vision_events(camera="gate", limit=50)
    why = [e for e in evs if "desk refused" in (e.get("reason") or "")]
    assert len(tries) == 3 and len(why) == 1 and len([e for e in evs if e["triggered"]]) == 3
    assert why[0]["reason"].startswith("desk refused 3 alert runs since ")
    assert "AI requests Atlas made on its own: 125 of 125 used." in why[0]["reason"]


def test_watch_job_and_detection_runs_hold_at_a_limit(U, store, monkeypatch):
    """S45, S48 (spec §1.6): a limit that escapes a camera tick is one refusal row and no "grab failed" event; a
    security detection's run refused at a limit stays queued, says why, and is tried again at the limit's retry time
    (not a minute later), started in lane auto."""
    import atlas.desk.scheduler as S
    from test_cyber_engine import _det
    desk = store.add_desk(96, "Watch desk", "site_watch", "free", {})
    did = desk["id"]
    store.add_connector(did, "camera", "gate", {"source": "gate.jpg"}, False)

    def tick(*a, **k):
        raise U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())
    monkeypatch.setattr(S, "camera_tick", tick)
    job = store.add_job(did, "camera_watch", "Watch the gate", json.dumps({"connector": "gate"}), 0, time.time())
    for _ in range(2):
        out = S._run_job(store, store.job(job["id"]), lambda *a: "", store.desk)
        assert out == "gate: paused: AI requests Atlas made on its own 125/125"
    assert not [e for e in store.for_desk(did).vision_events(camera="gate") if "grab failed" in (e.get("reason") or "")]
    ref, = _events(store, "kind='refusal' AND owner_id=96")
    assert (ref["surface"], ref["ref"], ref["units"], ref["lane"]) == ("camera.watch", "cam:gate", 2, "auto")
    store.for_desk(did).upsert_sec_detection(_det("ssh_bruteforce:203.0.113.9", severity="high"))
    lanes = []

    def start(d, task, mode):
        lanes.append(U.bound_lane())
        raise U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())
    S.cyber_forget(did)
    try:
        assert S._cy_trigger(store, desk, start, ["ssh_bruteforce:203.0.113.9"], "", None) == ""
        assert lanes == ["auto"] and S.CYBER_PENDING[did] == ["ssh_bruteforce:203.0.113.9"]
        reset = U.day_start(U._now()) + 86400
        assert S._cy_retry[did]["after"] == reset
        assert S.CYBER_STATUS[did]["text"].startswith("desk refused the run: AI requests Atlas made on its own: 125 of 125 used.")
        store.for_desk(did).upsert_sec_detection(_det("scan:198.51.100.4", rule="scan", severity="high"))
        assert S._cy_trigger(store, desk, start, ["scan:198.51.100.4"], "", None) == ""   # a later pass queues, no try
        assert len(lanes) == 1 and len(S.CYBER_PENDING[did]) == 2 and S._cy_retry[did]["after"] == reset
        S._cy_retry[did]["after"] = 0.0                                  # the retry time came: the flush tries again
        S.cyber_flush(store, start, store.desk)
        assert len(lanes) == 2 and S._cy_retry[did]["after"] == reset and len(S.CYBER_PENDING[did]) == 2
    finally:
        S.cyber_forget(did)


def test_sensor_event_is_kept_when_its_run_is_refused(app_client, monkeypatch):
    """S13 (spec §1.6, §4.6): an external sensor's event at a limit is stored and answered 200 with `limited` (the
    sensor is not made to retry); with camera cases on, the incident case is open and held for its clock."""
    from atlas import cases as C
    from atlas import records as R
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-s13@example.com", template="site_watch")
    monkeypatch.setattr(usage.CFG, "demo", "enforce")
    token = A.store.ensure_hook_token(did)
    usage.set_override(owner, "ai_requests_auto_day", 0)
    case = None
    try:
        r = app_client.post(f"/hook/{token}/vision", json={"camera": "gate", "labels": {"person": 1}, "note": "PIR at the gate"})
        b = r.get_json()
        assert r.status_code == 200 and b["ok"] is True and b["run_id"] == "", b
        assert b["limited"].startswith("AI requests Atlas made on its own: 0 of 0 used.")
        ev = A.store.vision_event(b["event_id"])
        assert ev["triggered"] and ev["reason"] == "PIR at the gate" and not ev["run_id"]
        case = next(c for c in C.list_cases(A.store, did, open_only=True) if c["type"] == "incident")
        reset = usage.day_start(time.time()) + 86400
        assert case["waiting_for"] == "atlas" and reset <= case["wake_at"] <= reset + 300
        assert R.timeline_for(A.store, case_ids=[case["id"]], limit=1)[0]["text"] == (
            "Paused: AI requests Atlas made on its own 0/0; resumes after 00:00 UTC")
    finally:
        usage.clear_override(owner, "ai_requests_auto_day")
        if case:
            C._set(A.store, case["id"], wake_at=None)


def test_scheduler_starts_deferred_leads_for_its_own_ledger_only(U, store, monkeypatch):
    """Deferred starts (spec §1.6): the scheduler's pass starts due rows only for the ledger its own store holds (the
    app's scheduler never starts a test's or a tool's rows), and a lead its case took over meanwhile leaves the
    queue instead of being retried."""
    import atlas.desk.scheduler as S
    t = time.time()
    desk = store.add_desk(97, "Defer desk", "sales_desk", "free", {})
    m = _meter(owner=97, desk=desk["id"], lane="auto", surface="run.auto")
    busy = U._limit("runs_at_once", used=3, limit=3, now=t)
    lid = store.add_lead("Ana", "", "ana@example.com", "", "web form", "Need a quote", desk_id=desk["id"])
    lid2 = store.add_lead("Bo", "", "bo@example.com", "", "web form", "Need a quote", desk_id=desk["id"])
    U.defer(m, "lead", lid, busy)
    U.defer(m, "lead", lid2, busy)
    calls = []

    def start(d, lead_id):
        calls.append(lead_id)
        if lead_id == lid2:                                              # its case is open now and holds the step
            exc = U._limit("ai_requests_auto_day", used=125, limit=125, now=U._now())
            exc.case_id = 5
            raise exc
        return "r-1"
    monkeypatch.setattr(U, "LEAD_RUN", start)
    assert S.deferred_pass(object(), t + 120) == 0 and calls == []
    assert S.deferred_pass(store, t + 120) == 1 and calls == [lid, lid2]
    assert U.deferred(97) == [] and [r["ref_id"] for r in U.deferred(97, "dropped")] == [lid2]


# ---------------------------------------------------------------------------- channels and uploads (step 7)
def test_recipients_match_their_inbound_thread_and_errors_stay_out_of_the_ledger(U):
    """W1's lookup (spec §1.3 W1, §3.7): a number is matched the way the sender dials it (UK national and 00 forms
    become +44..., as the hooks store inbound numbers), then by its plain digits; Instagram ids keep their ig: key. A
    failed send keeps only the error's type and HTTP status: provider errors echo numbers and account ids."""
    assert U._contact_keys("whatsapp", "07700 900123") == ("+447700900123", "+07700900123")
    assert U._contact_keys("sms", "0044 7700 900123") == ("+447700900123", "+00447700900123")
    assert U._contact_keys("whatsapp", "whatsapp:+44 7700 900123") == ("+447700900123",)
    assert U._contact_keys("instagram", "778899") == U._contact_keys("instagram", "ig:778899") == ("ig:778899",)
    assert U._contact_keys("email", "Guest@Example.com") == ("guest@example.com",)
    assert U.recipient_ref("whatsapp", "07700 900123") == U.recipient_ref("sms", "+447700900123")
    err = RuntimeError("POST https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json → HTTP 400: The 'To' number "
                       "+447700900123 is not a valid phone number.")
    assert U.send_error_note(err) == "RuntimeError: HTTP 400" and U.send_error_note(TimeoutError()) == "TimeoutError"


def test_twilio_reports_segments_and_price():
    """Spec §3.4 (Twilio row): deliver's meta carries num_segments and the price Twilio reported, as an absolute
    amount and only in USD; a price in another currency is kept as a note, never converted; no price yet is None."""
    from atlas import integrations as I
    meta = {}
    I.twilio_meta({"sid": "SM1", "num_segments": "3", "price": "-0.0237", "price_unit": "USD"}, meta)
    assert meta == {"provider": "twilio", "segments": 3, "cost_usd": 0.0237}
    meta = {}
    I.twilio_meta({"num_segments": "0", "price": None, "price_unit": "USD"}, meta)    # a Messaging Service
    assert (meta["segments"], meta["cost_usd"]) == (0, None)
    meta = {}
    I.twilio_meta({"num_segments": "1", "price": "-0.04", "price_unit": "GBP"}, meta)
    assert meta["cost_usd"] is None and meta["price_note"] == "Twilio price 0.04 GBP"
    I.twilio_meta({}, None)                                                           # no meta asked for: nothing


def _send_stubs(monkeypatch):
    """Meta (WhatsApp, Instagram) and Twilio answer from a stub the way their APIs do; every send is recorded and
    nothing leaves the machine. Twilio refuses +15550009999 (HTTP 400, its error echoing the number)."""
    from atlas import integrations as I
    sent = []

    def handler(req):
        url, body = str(req.url), req.content.decode("utf-8", "replace")
        if "api.twilio.com" in url and "15550009999" in body:
            return httpx.Response(400, json={"message": "The 'To' number +15550009999 is not a valid phone number."})
        sent.append(url)
        if "api.twilio.com" in url:
            return httpx.Response(201, json={"sid": "SM38", "num_segments": "2", "price": "-0.0158", "price_unit": "USD"})
        if "graph.facebook.com" in url:
            return httpx.Response(200, json={"messages": [{"id": "wamid.T38"}], "message_id": "mid.T38"})
        return httpx.Response(500, json={"error": "test: unexpected host"})
    monkeypatch.setattr(I, "_TRANSPORT", httpx.MockTransport(handler))
    return sent


def _decide(c, did, aid, status="approved"):
    return c.post(f"/api/actions/{aid}/decide", json={"status": status}, headers={"X-Desk": str(did)})


def test_reply_window_holds_whatsapp_and_instagram(app_client, monkeypatch):
    """T38 (spec §1.3 W1 and L7, §4.5, §4.6 S14-S15): approving a WhatsApp message to a number that has not written in
    24 h answers 409 outside_window, sends nothing and leaves the action pending; once the customer wrote 2 h ago (the
    action in UK national form, the inbound as the hook stores it) it goes out. The same for Instagram (ig: keys), a
    Twilio WhatsApp sender and the owner's direct reply (nothing stored on the thread). SMS is not gated: Twilio's
    segments and price are on the ledger, the day's segments refuse the next approval with 429, and a send Twilio
    rejects gives its segment back. An auto alert through the real dispatcher stays pending with the W1 copy. The
    ledger keeps hashed recipients only."""
    from atlas import alerts as AL
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t38@example.com")
    hdr = {"X-Desk": str(did)}
    sent = _send_stubs(monkeypatch)
    A.store.add_connector(did, "whatsapp", "wa", {"phone_number_id": "1066", "access_token": "tok"}, False)
    A.store.add_connector(did, "instagram", "ig", {"page_id": "4242", "access_token": "tok"}, False)
    ds = A.store.for_desk(did)
    wa = ds.add_action("r-t38", "host", "whatsapp", "07700 900123", "", "Your table is ready.", "")
    r = _decide(app_client, did, wa)
    b = r.get_json()
    assert r.status_code == 409 and "Retry-After" not in r.headers, b
    assert (b["error"], b["limit_id"], b["channel"], b["retry_at"], b["usage_url"]) == (
        "outside_window", "reply_window", "whatsapp", None, "/desk/usage")
    assert b["message"] == ("Outside WhatsApp's 24-hour reply window: a template message is needed, which Atlas "
                            "can't send yet.")
    assert ds.action(wa)["status"] == "pending" and not ds.action(wa)["decided_by"] and sent == []
    ds.add_message("whatsapp", "in", "+447700900123", "Table for two at 8?", ts=time.time() - 2 * 3600)
    row = _decide(app_client, did, wa).get_json()
    assert row["status"] == "sent" and "wamid.T38" in row["note"] and len(sent) == 1
    ig = ds.add_action("r-t38", "host", "instagram", "778899", "", "Yes, walk-ins are welcome.", "")
    r = _decide(app_client, did, ig)
    assert r.status_code == 409 and r.get_json()["channel"] == "instagram" and r.get_json()["message"].startswith(
        "Outside Instagram's 24-hour reply window: Instagram allows replies only within 24 hours")
    ds.add_message("instagram", "in", "ig:778899", "Do you take walk-ins?", ts=time.time() - 3600)
    assert _decide(app_client, did, ig).get_json()["status"] == "sent" and len(sent) == 2
    # the owner's own reply (S15): outside the window nothing is sent or stored; inside it goes out
    msg = {"channel": "whatsapp", "to": "+15550002222", "body": "Hello"}
    r = app_client.post("/api/messages/send", json=msg, headers=hdr)
    assert r.status_code == 409 and r.get_json()["error"] == "outside_window" and len(sent) == 2
    assert ds.messages("+15550002222") == []
    ds.add_message("whatsapp", "in", "+15550002222", "Hi, are you open?", ts=time.time() - 600)
    assert app_client.post("/api/messages/send", json=msg, headers=hdr).get_json()["ok"] and len(sent) == 3
    # a Twilio WhatsApp sender on a second desk: the same rule
    did2 = app_client.post("/api/desks", json={"name": "T38 Twilio", "template": "sales_desk", "tier": "free"}).get_json()["id"]
    A.store.add_connector(did2, "twilio", "tw", {"account_sid": "AC38", "auth_token": "t", "from_number": "+15550000000",
                                                  "whatsapp_from": "+14155238886"}, False)
    ds2 = A.store.for_desk(did2)
    tw = ds2.add_action("r-t38", "host", "whatsapp", "+447700900456", "", "Booked for Friday.", "")
    assert _decide(app_client, did2, tw).status_code == 409 and len(sent) == 3
    ds2.add_message("whatsapp", "in", "+447700900456", "Can I book Friday?", ts=time.time() - 3600)
    assert _decide(app_client, did2, tw).get_json()["status"] == "sent" and "api.twilio.com" in sent[-1]
    # SMS: no reply window; Twilio's segments count in L7 and its price is the row's cost
    sms = ds2.add_action("r-t38", "host", "sms", "+15550001111", "", "Reminder: your table is at 8pm.", "")
    assert _decide(app_client, did2, sms).get_json()["status"] == "sent" and len(sent) == 5
    assert usage.counter(f"d{did2}", "seg:sms")[0] == 2
    bad = ds2.add_action("r-t38", "host", "sms", "+15550009999", "", "Reminder.", "")
    row = _decide(app_client, did2, bad).get_json()
    assert row["status"] == "failed" and usage.counter(f"d{did2}", "seg:sms")[0] == 2      # its segment came back
    usage.set_override(owner, "sms_segments_day", 2)
    try:
        sms2 = ds2.add_action("r-t38", "host", "sms", "+15550001111", "", "See you soon.", "")
        b = _is_limit(_decide(app_client, did2, sms2), "sms_segments_day")
        assert b["message"].startswith("SMS sent today: 2 of 2 used. Resets 00:00 UTC")
        assert ds2.action(sms2)["status"] == "pending" and len(sent) == 5
    finally:
        usage.clear_override(owner, "sms_segments_day")
    # an auto camera alert through the real dispatcher: held by the window, pending with the rule's words
    alert = AL.queue(A.store, A.store.desk(did2), "whatsapp", "+447700900999", "door: person", "Camera door: 1 person.",
                     "camera alert on door: person", True, A._dispatch)
    assert alert["status"] == "pending" and alert["held"].startswith("Outside WhatsApp's 24-hour reply window")
    sends = _events(A.store, "kind='send' AND owner_id=?", (owner,))
    assert [(s["surface"], s["provider"], s["status"], s["units"], s["lane"]) for s in sends] == [
        ("send.whatsapp", "meta", "ok", 1, "you"), ("send.instagram", "meta", "ok", 1, "you"),
        ("send.whatsapp", "meta", "ok", 1, "you"), ("send.whatsapp", "twilio", "ok", 1, "you"),
        ("send.sms", "twilio", "ok", 2, "you"), ("send.sms", "twilio", "not_sent", 0, "you")]
    assert sends[4]["cost_usd"] == pytest.approx(0.0158) and sends[4]["cost_basis"] == "provider"
    assert sends[5]["note"] == "RuntimeError: HTTP 400" and sends[0]["ref"] == usage.recipient_ref("whatsapp", "+447700900123")
    held = _events(A.store, "kind='refusal' AND owner_id=? AND limit_id='reply_window'", (owner,))
    assert sorted((h["surface"], h["status"], h["lane"]) for h in held) == [
        ("send.instagram", "held", "you"), ("send.whatsapp", "held", "auto"), ("send.whatsapp", "held", "you"),
        ("send.whatsapp", "held", "you"), ("send.whatsapp", "held", "you")]
    auto_held = next(h for h in held if h["lane"] == "auto")
    l8, = _events(A.store, "kind='action' AND owner_id=? AND surface='send.whatsapp'", (owner,))
    assert l8["status"] == "not_sent" and l8["action_id"] == auto_held["action_id"]      # one alert, one action id
    assert usage.counter(f"o:{owner}", "held:window")[0] == 5 and usage.counter(f"o:{owner}", "send:whatsapp")[0] == 3
    ledger = json.dumps(sends + held)
    assert "447700900" not in ledger and "15550001111" not in ledger and "+15550009999" not in ledger


def test_approved_actions_are_counted_and_simulated_sends_are_not(app_client, monkeypatch):
    """Spec §3.2 (action hooks), §1.4 action.*: app._dispatch counts every approved action that really runs (an HTTP
    call, a calendar booking) under the approver's lane and the action's id; a failed one is counted as an error with
    its type and HTTP status only; a message with no connector is simulated and is no send."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-dispatch@example.com")
    ds = A.store.for_desk(did)
    A.store.add_connector(did, "http", "crm", {"base_url": "https://crm.example.com", "auth_type": "none"}, False)
    A.store.add_connector(did, "gcal", "cal", {"client_id": "c", "client_secret": "s", "refresh_token": "r"}, False)

    def http_call(cfg, method, path, params=None, body=None, **kw):
        if path == "/fail":
            raise RuntimeError("POST https://crm.example.com/fail → HTTP 503: {\"to\": \"+447700900123\"}")
        return {"status": 201, "url": "https://crm.example.com" + path}
    monkeypatch.setattr(A.I, "http_call", http_call)
    monkeypatch.setattr(A.I, "gcal_create_event", lambda cfg, title, start, end, attendee="", desc="": "booked (evt1)")
    ok = ds.add_action("r-d", "ops", "api_call", "crm", "POST /x", json.dumps({"method": "POST", "path": "/x", "body": {}}), "")
    bad = ds.add_action("r-d", "ops", "api_call", "crm", "POST /fail", json.dumps({"method": "POST", "path": "/fail"}), "")
    book = ds.add_action("r-d", "ops", "booking", "hannah@x.com", "Consultation",
                         json.dumps({"title": "Consultation", "start": "2030-01-07T11:00", "end": "2030-01-07T11:30"}), "")
    mail = ds.add_action("r-d", "closer", "email", "buyer@example.com", "Quote", "Here it is.", "")
    assert _decide(app_client, did, ok).get_json()["status"] == "sent"
    assert _decide(app_client, did, bad).get_json()["status"] == "failed"
    assert "evt1" in _decide(app_client, did, book).get_json()["note"]
    assert "simulated" in _decide(app_client, did, mail).get_json()["note"]
    acts = _events(A.store, "kind='action' AND owner_id=?", (owner,))
    assert [(a["surface"], a["status"], a["lane"], a["ref"]) for a in acts] == [
        ("action.http", "ok", "you", f"action:{ok}"), ("action.http", "error", "you", f"action:{bad}"),
        ("action.calendar", "ok", "you", f"action:{book}")]
    assert acts[1]["note"] == "RuntimeError: HTTP 503" and "447700900123" not in json.dumps(acts)
    assert _events(A.store, "kind IN ('send', 'refusal') AND owner_id=?", (owner,)) == []


def _multipart(field, filename, data):
    b = "t19-boundary"
    head = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + data + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


_NO_LENGTH = {"wsgi.input_terminated": True, "CONTENT_LENGTH": ""}     # a chunked body: the route sees no length


def _refused(r, limit_id):
    b = r.get_json()
    assert r.status_code == 429 and b["error"] == "usage_limit" and b["limit_id"] == limit_id, (r.status_code, b)
    assert b["retry_at"] is None and "Retry-After" not in r.headers and b["usage_url"] == "/desk/usage"
    return b


def test_upload_refused_over_quota_and_disk_floor(app_client, monkeypatch):
    """T19 (spec §1.3 L9, §4.2), at 1/8 of the spec's sizes: with L9 = 0.125 MB, a 0.25 MB camera recording is refused
    with 429 storage_mb and no file is left, both when its Content-Length is checked before the body is read and for
    a chunked body with no length, which is charged after the write and deleted. The same for a watched video, a
    saved cyber recording and a cyber log. A file that fits is charged; a log is given back once parsed; a recording
    saved again under its name is charged its growth. With the disk at 5% free, uploads get 429 disk_floor."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-t19@example.com", template="site_watch")
    hdr = {"X-Desk": str(did)}
    gb = 1 << 30
    disk = {"free_pct": 50}
    monkeypatch.setattr(usage, "_disk_usage", lambda p: (100 * gb, (100 - disk["free_pct"]) * gb, disk["free_pct"] * gb))
    monkeypatch.setattr(usage.CFG, "disk_floor_pct", 10.0)
    _guard_models(monkeypatch)
    cams, vids, recs = A._upload_dir(owner), A.VIDEO_DIR / f"desk{did}", A._rec_dir(did)
    logs = A.cfg.DATA_DIR / "cyber" / "uploads"

    def files(d):
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def stored():
        return usage.counter(f"o:{owner}", "storage", "T", 0)[0]

    def post(url, field, name, data, chunked=False):
        if chunked:
            body, ctype = _multipart(field, name, data)
            return app_client.post(url, input_stream=io.BytesIO(body), content_type=ctype, headers=hdr,
                                   environ_overrides=_NO_LENGTH)
        return app_client.post(url, data={field: (io.BytesIO(data), name)}, content_type="multipart/form-data", headers=hdr)
    big = b"\0" * (usage.MB // 4)
    line = b"Dec  6 08:23:28 ip-172-31-27-153 sshd[6239]: Invalid user zhangyan from 61.197.203.243\n"
    big_log = line * (usage.MB // 4 // len(line) + 1)
    logs0 = files(logs)
    assert A.VIDEO_DIR == A.cfg.DATA_DIR / "videos"                    # watched videos are under ATLAS_DATA_DIR now
    usage.set_override(owner, "storage_mb", 0.125)
    try:
        for chunked in (False, True):
            b = _refused(post("/api/cameras/upload", "file", "yard.png", big, chunked), "storage_mb")
            assert b["unit"] == "MB" and b["message"].startswith("Files you uploaded: 0 of 0.1 MB used. This file is")
            _refused(post("/api/vision/video", "video", "clip.mp4", big, chunked), "storage_mb")
            _refused(post("/api/cyber/upload", "file", "auth.log", big_log, chunked), "storage_mb")
        rec = {"kind": "atlas-cyber-recording", "version": 1, "pad": "x" * (usage.MB // 4)}
        _refused(app_client.post("/api/cyber/recordings", json={"name": "big", "bundle": rec}, headers=hdr), "storage_mb")
        raw = json.dumps({"name": "big", "bundle": rec}).encode()
        _refused(app_client.post("/api/cyber/recordings", input_stream=io.BytesIO(raw), content_type="application/json",
                                 headers=hdr, environ_overrides=_NO_LENGTH), "storage_mb")
        assert files(cams) == files(vids) == files(recs) == [] and files(logs) == logs0 and stored() == 0
        # a file that fits is charged; a log is charged while it is parsed and given back with its file
        usage.set_override(owner, "storage_mb", 1)
        door = b"\x89PNG" + b"\0" * 1000
        up = post("/api/cameras/upload", "file", "door.png", door)
        assert up.status_code == 200 and stored() == len(door)
        three = (b"Dec  6 08:23:28 ip-172-31-27-153 sshd[6239]: Invalid user zhangyan from 61.197.203.243\n"  # SecRepo
                 b"Dec  6 08:23:30 ip-172-31-27-153 sshd[6241]: Invalid user dff from 61.197.203.243\n"  # auth.log,
                 b"Dec  6 08:24:27 ip-172-31-27-153 sshd[6309]: Invalid user oracle from 61.197.203.243\n")  # CC BY 4.0
        lg = app_client.post("/api/cyber/upload", data={"file": (io.BytesIO(three), "auth.log"), "fmt": "sshd",
                                                        "trigger": "0"}, content_type="multipart/form-data", headers=hdr)
        assert lg.status_code == 200 and lg.get_json()["inserted"] == 3 and stored() == len(door)
        small = {"kind": "atlas-cyber-recording", "version": 1}
        r1 = app_client.post("/api/cyber/recordings", json={"name": "a", "bundle": small}, headers=hdr).get_json()
        assert stored() == len(door) + r1["size"]
        r2 = app_client.post("/api/cyber/recordings", json={"name": "a", "bundle": {**small, "pad": "y" * 100}},
                             headers=hdr).get_json()
        assert r2["size"] > r1["size"] and stored() == len(door) + r2["size"]
        r3 = app_client.post("/api/cyber/recordings", json={"name": "a", "bundle": small}, headers=hdr).get_json()
        assert stored() == len(door) + r3["size"] and A._storage_walk()[owner] == stored()
        # the disk floor: 5% free refuses before the body is read and after the write
        disk["free_pct"] = 5
        for chunked in (False, True):
            b = _refused(post("/api/cameras/upload", "file", "gate.png", b"\0" * 100, chunked), "disk_floor")
            assert b["message"].startswith("Atlas's disk is nearly full: an upload must leave 10% of it free")
        assert len(files(cams)) == 1 and stored() == len(door) + r3["size"]
    finally:
        usage.clear_override(owner, "storage_mb")
    ups = _events(A.store, "kind='upload' AND owner_id=?", (owner,))
    assert [(u["surface"], u["units"], u["lane"]) for u in ups] == [
        ("upload.camera", len(door), "you"), ("upload.cyber", len(three), "you"), ("upload.recording", r1["size"], "you"),
        ("upload.recording", r2["size"], "you"), ("upload.recording", r3["size"], "you")]
    refusals = {(x["surface"], x["limit_id"], x["units"]) for x in _events(A.store, "kind='refusal' AND owner_id=?", (owner,))}
    assert refusals == {("upload.camera", "storage_mb", 2), ("upload.video", "storage_mb", 2),
                        ("upload.cyber", "storage_mb", 2), ("upload.recording", "storage_mb", 2),
                        ("upload.camera", "disk_floor", 2)}


def test_storage_reconcile_measures_the_owners_files(app_client, monkeypatch):
    """L9 step 3 (spec §1.3): the daily reconcile sets each owner's total to what their upload folders hold on disk
    (camera uploads by account, watched videos and saved recordings by desk), whatever the counter had drifted to;
    an owner whose files are gone goes back to 0."""
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-reconcile@example.com", template="site_watch")
    made = []
    for folder, name, n in ((A._upload_dir(owner), "a.png", 300), (A.VIDEO_DIR / f"desk{did}", "1_clip.mp4", 500),
                            (A._rec_dir(did), "r.json", 70)):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"\0" * n)
        made.append(folder / name)
    usage.set_storage(owner, 5)                                         # a total that drifted
    sizes = A._storage_walk()
    assert sizes[owner] == 870
    assert usage.reconcile_storage(sizes)[owner] == 870 and usage.counter(f"o:{owner}", "storage", "T", 0)[0] == 870
    for p in made:
        p.unlink()
    assert A._storage_walk().get(owner, 0) == 0
    usage.reconcile_storage(A._storage_walk())
    assert usage.counter(f"o:{owner}", "storage", "T", 0)[0] == 0


# ---------------------------------------------------------------------------- the Hermes metering proxy (step 9)
HP_TOKEN = "hp-test-token-0123456789abcdef"              # setup.sh writes a random 43-character one
HP_URL = "/internal/llm/v1/chat/completions"
HERMES = "http://127.0.0.1:8642"


def _hp_sse(model):
    return _sse([{"choices": [{"delta": {"content": "step"}}], "model": model},
                 {"choices": [{"delta": {}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0}}])


@pytest.fixture()
def hp(app_client, U, monkeypatch):
    """The metering proxy in proxy mode with a token, its ledger on a fresh store. OpenRouter is a stub that keeps every
    request in `hp.sent` and answers with `hp.answer(req)` when set (a step with usage otherwise): nothing leaves the
    machine. `hp.call(...)` is Hermes making one model call: a client with no session, on loopback, with the token."""
    from atlas.desk import hermes_proxy as HP
    import atlas.desk.app as A
    monkeypatch.setattr(U.CFG, "hermes_meter", "proxy")
    monkeypatch.setattr(U.CFG, "hermes_proxy_token", HP_TOKEN)
    monkeypatch.setattr(U, "house_openrouter_key", lambda: "sk-or-house-test")
    ns = SimpleNamespace(sent=[], answer=None, A=A)

    def upstream(req):
        ns.sent.append({"url": str(req.url), "headers": dict(req.headers), "body": req.content})
        if ns.answer:
            return ns.answer(req)
        body = json.loads(req.content)
        if body.get("stream"):
            return httpx.Response(200, text=_hp_sse(body["model"]), headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=_body("step", usage={"prompt_tokens": 100, "completion_tokens": 10, "cost": 0},
                                              model=body["model"]))
    monkeypatch.setattr(HP, "TRANSPORT", httpx.MockTransport(upstream))

    def call(model=FREE, stream=False, headers=None, token=HP_TOKEN, **kw):
        payload = {"model": model, "messages": [{"role": "user", "content": "next step"}], **({"stream": True} if stream else {})}
        r = A.app.test_client().post(HP_URL, json=payload, headers={"Authorization": f"Bearer {token}", **(headers or {})}, **kw)
        data = r.get_data()
        r.close()
        return r.status_code, data, r.headers
    ns.call = call
    return ns


def _hermes(meter, handler, session_key="atlas:desk8:writer", model=FREE):
    """Atlas's own Hermes instance (house; proxy mode) whose API server is `handler`: Hermes's own tool loop."""
    return _stub(P.make_provider({"type": "hermes_agent", "base_url": HERMES, "api_key": "hk", "default_model": model,
                                  "session_key": session_key}, meter=meter), handler)


def _hp_rows(store, where="1=1", args=()):
    return _events(store, f"surface IN ('hermes.inner', 'hermes.unattributed') AND {where}", args)


def test_hermes_off_keeps_specialists_on_the_built_in_engine(app_client, monkeypatch):
    """T32.1 (spec §1.8): USAGE_HERMES_METER defaults to off, and then DESK_DEFAULT_ENGINE=hermes_agent (and
    DESK_LEAD_ENGINE) moves nobody onto Atlas's own Hermes instance: no hermes_agent_* providers, the engine note on
    every agent it would have moved, and neither team design nor /api/health offers it. proxy moves them; a desk's
    own Hermes instance is the owner's key and is never held back."""
    from atlas import config as C
    assert usage.load_config({}).hermes_meter == "off" and usage.load_config({"USAGE_HERMES_METER": "on"}).hermes_meter == "off"
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-hermes-off@example.com")
    monkeypatch.setattr(C, "load", lambda name, default: json.loads(json.dumps(default)))
    for k, v in (("DESK_MODE", "live"), ("DESK_DEFAULT_ENGINE", "hermes_agent"), ("DESK_LEAD_ENGINE", "hermes_agent"),
                 ("HERMES_AGENT_URL", HERMES), ("HERMES_AGENT_KEY", "hk")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(usage.CFG, "hermes_meter", "off")
    try:
        conf = A.desk_configs(A.store.desk(did))
        assert not [n for n in conf["providers"]["providers"] if n.startswith("hermes_agent")]
        assert len(conf["agents"]) == 5 and all(a.get("engine_note") == usage.HERMES_OFF_NOTE for a in conf["agents"])
        assert A._team_context(A.store.desk(did))[2] is False and app_client.get("/api/health").get_json()["hermes_agent"] is False
        monkeypatch.setattr(usage.CFG, "hermes_meter", "proxy")
        conf = A.desk_configs(A.store.desk(did))
        provs = conf["providers"]["providers"]
        assert all(provs[f"hermes_agent_{a['id']}"]["url_guard"] is False and not a.get("engine_note") for a in conf["agents"])
        assert A._team_context(A.store.desk(did))[2] is True and app_client.get("/api/health").get_json()["hermes_agent"] is True
        monkeypatch.setattr(usage.CFG, "hermes_meter", "off")
        A.store.add_connector(did, "hermes_agent", "hermes", {"base_url": "https://hermes.owner.test:8642", "api_key": "own"})
        conf = A.desk_configs(A.store.desk(did))
        provs = conf["providers"]["providers"]
        assert all(provs[f"hermes_agent_{a['id']}"]["url_guard"] == "internal" and not a.get("engine_note") for a in conf["agents"])
    finally:
        monkeypatch.setenv("DESK_MODE", "demo")


def test_hermes_proxy_charges_inner_requests_to_the_run(hp, U, store, capsys):
    """T32.2 (spec §1.8): a stub Hermes makes 3 inner requests through /internal/llm while Atlas's call to it is open.
    All 3 count for the run that delegated: its owner's allowance (L1), its per-run cap (L4), its lane and action id,
    with OpenRouter's usage and cost (one streamed: its usage from the last chunk). OpenRouter sees Atlas's key, never
    the proxy token or Hermes's session header; Hermes gets OpenRouter's answers byte for byte; the delegation is one
    hermes.call action, not a request; no self-check warning."""
    m = _meter(owner=70, desk=8, surface="run.step", run_cap=10)
    got = []

    def hermes(req):
        assert str(req.url) == HERMES + "/v1/chat/completions" and req.headers["X-Hermes-Session-Key"] == "atlas:desk8:writer"
        got.extend([hp.call(), hp.call(stream=True), hp.call()])
        return httpx.Response(200, json=_body("Done in three steps.", model="hermes-agent"))
    capsys.readouterr()
    assert _ask(_hermes(m, hermes)).text == "Done in three steps."
    assert [g[0] for g in got] == [200, 200, 200] and json.loads(got[0][1])["choices"][0]["message"]["content"] == "step"
    assert got[1][1].decode() == _hp_sse(FREE) and got[1][2]["Content-Type"].startswith("text/event-stream")
    rows = _hp_rows(store, "owner_id=70")
    assert len(rows) == 3 and {(r["surface"], r["kind"], r["action_id"], r["desk_id"], r["lane"], r["key_kind"], r["provider"],
                                r["pool"], r["model"]) for r in rows} == {
        ("hermes.inner", "request", m.action_id, 8, "you", "house", "openrouter", "free:openrouter", FREE)}
    assert [(r["status"], r["http_status"], r["tokens_in"], r["tokens_out"], r["cost_usd"], r["cost_basis"]) for r in rows] == \
        [("ok", 200, 100, 10, 0.0, "provider")] * 3
    assert U.counter("o:70", "req:free")[0] == 3 and U.counter("o:70:d8", "req:free")[0] == 3 and m.requests == 3
    act, = _events(store, "kind='action' AND owner_id=70")
    assert (act["surface"], act["status"], act["note"]) == ("hermes.call", "ok", "3 inner requests through the metering proxy")
    assert [s["url"] for s in hp.sent] == [OR_URL + "/chat/completions"] * 3
    for s in hp.sent:
        assert s["headers"]["authorization"] == "Bearer sk-or-house-test" and "x-hermes-session-key" not in s["headers"]
        assert HP_TOKEN not in json.dumps(s["headers"])
    assert json.loads(hp.sent[1]["body"]) == {"model": FREE, "messages": [{"role": "user", "content": "next step"}], "stream": True}
    assert U.hermes_warning() == {} and U.HERMES_SELF_CHECK not in capsys.readouterr().out
    assert U._hermes == {} and U._hermes_stats["inflight"] == 0


def test_a_byo_desks_hermes_specialists_count_in_runs_at_once(app_client, monkeypatch):
    """T32.2, L5 (spec §1.3 L5, §2): a desk on the owner's own key whose specialists run on Atlas's Hermes instance
    (DESK_DEFAULT_ENGINE=hermes_agent, proxy) takes one of the owner's runs at once; with Hermes off the same desk's run
    is on the owner's key only and takes none."""
    from atlas import config as C
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-hermes-l5@example.com")
    assert app_client.post(f"/api/desks/{did}/providers", json={"preset": "openrouter", "api_key": "sk-or-own-4444-5555"}).status_code == 200
    monkeypatch.setattr(C, "load", lambda name, default: json.loads(json.dumps(default)))
    for k, v in (("DESK_MODE", "live"), ("DESK_DEFAULT_ENGINE", "hermes_agent"), ("HERMES_AGENT_URL", HERMES)):
        monkeypatch.setenv(k, v)
    for k in ("DESK_LEAD_ENGINE", "DESK_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(A, "Orchestrator", _SlowOrch)
    _SlowOrch.gate.set()
    usage.set_override(owner, "runs_at_once", 0)
    hdr = {"X-Desk": str(did)}
    try:
        monkeypatch.setattr(usage.CFG, "hermes_meter", "proxy")
        assert A._run_pools(A.desk_configs(A.store.desk(did)))["house"] is True
        _is_limit(app_client.post("/api/runs", json={"task": "Chase the Jones invoice"}, headers=hdr), "runs_at_once")
        monkeypatch.setattr(usage.CFG, "hermes_meter", "off")
        assert A._run_pools(A.desk_configs(A.store.desk(did)))["house"] is False
        r = app_client.post("/api/runs", json={"task": "Chase the Jones invoice"}, headers=hdr)
        assert r.status_code == 200, r.get_json()
    finally:
        usage.clear_override(owner, "runs_at_once")
        monkeypatch.setenv("DESK_MODE", "demo")
    assert _wait_runs(A, did)


def test_hermes_proxy_answers_only_hermes_on_this_machine(hp, U, store, monkeypatch):
    """T32.3 (spec §1.8): without the token, with a wrong one, through Caddy (X-Forwarded-For, Forwarded) or from
    another machine: 404, as if the route did not exist; the same while the token is unset or too short. With the
    token, any mode but proxy answers 403 and sends nothing. The model list passes through, unmetered."""
    payload = {"model": FREE, "messages": [{"role": "user", "content": "hi"}]}
    c = hp.A.app.test_client()
    ok = {"Authorization": f"Bearer {HP_TOKEN}"}

    def post(headers=None, **kw):
        return c.post(HP_URL, json=payload, headers=headers or {}, **kw).status_code
    assert post() == 404 and post({"Authorization": "Bearer not-the-token-0123456789"}) == 404
    assert post({"Authorization": HP_TOKEN}) == 404                                   # no Bearer scheme
    assert post({**ok, "X-Forwarded-For": "203.0.113.9"}) == 404
    assert post({**ok, "Forwarded": "for=203.0.113.9;proto=https"}) == 404
    assert post(ok, environ_base={"REMOTE_ADDR": "203.0.113.9"}) == 404
    for tok in ("", "short-token"):
        monkeypatch.setattr(U.CFG, "hermes_proxy_token", tok)
        assert post({"Authorization": f"Bearer {tok}"}) == 404
    monkeypatch.setattr(U.CFG, "hermes_proxy_token", HP_TOKEN)
    for mode in ("off", "unmetered"):
        monkeypatch.setattr(U.CFG, "hermes_meter", mode)
        r = c.post(HP_URL, json=payload, headers=ok)
        assert r.status_code == 403 and r.get_json()["error"]["code"] == 403
        assert f"USAGE_HERMES_METER={mode}" in r.get_json()["error"]["message"]
        assert c.get("/internal/llm/v1/models", headers=ok).status_code == 403
    assert hp.sent == [] and _hp_rows(store) == [] and U._hermes_stats["seen"] == 0
    monkeypatch.setattr(U.CFG, "hermes_meter", "proxy")
    assert c.get("/internal/llm/v1/models").status_code == 404
    hp.answer = lambda req: httpx.Response(200, json={"data": [{"id": FREE}]})
    r = c.get("/internal/llm/v1/models?supported_parameters=tools", headers=ok)
    assert r.status_code == 200 and r.get_json() == {"data": [{"id": FREE}]}
    assert hp.sent[-1]["url"] == OR_URL + "/models?supported_parameters=tools" and _hp_rows(store) == []
    hp.answer = None
    assert post(ok, environ_base={"REMOTE_ADDR": "::1"}) == 200                       # loopback over IPv6
    assert c.post(HP_URL, data="not json", headers={**ok, "Content-Type": "application/json"}).status_code == 400
    assert len(_hp_rows(store, "kind='request'")) == 1 and len(hp.sent) == 2


def test_hermes_proxy_caps_requests_in_flight(hp, U, store):
    """T32.4 (spec §1.8, C8): with 8 proxied requests in flight the 9th gets 429 and Retry-After 5 at once; it sends
    nothing and leaves no ledger row. Once they finish the next one goes through."""
    gate, inside = threading.Event(), threading.Semaphore(0)

    def slow(req):
        inside.release()
        gate.wait(20)
        return httpx.Response(200, json=_body("step", usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0}, model=FREE))
    hp.answer = slow
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(hp.call()[0])) for _ in range(8)]
    for t in ts:
        t.start()
    try:
        assert all(inside.acquire(timeout=20) for _ in range(8))
        status, body, headers = hp.call()
        err = json.loads(body)["error"]
        assert (status, headers["Retry-After"], err["code"], err["type"]) == (429, "5", 429, "proxy_busy")
        assert len(hp.sent) == 8 and U._hermes_stats["inflight"] == 8
    finally:
        gate.set()
        for t in ts:
            t.join(20)
    assert codes == [200] * 8 and U._hermes_stats["inflight"] == 0 and U._hermes_stats["busy"] == 1
    assert hp.call()[0] == 200 and len(_hp_rows(store, "kind='request'")) == 9


def test_hermes_proxy_attribution_rules(hp, U, store, monkeypatch):
    """Spec §1.8 rules 1-4: no call in flight: the house row hermes.unattributed (the pool only, no owner); one run's
    parallel calls share its meter (exact); calls of two owners overlap: the earliest pays and the row says
    'attributed: overlap', the provider's cost kept; a session key naming a call wins. The operator view's Hermes block
    counts each, and requests charged to an overlapping call are no self-check warning for the other."""
    clock = _clock(monkeypatch, _ts(2026, 10, 10, 9, 0))
    assert hp.call()[0] == 200                                            # 4: no call in flight
    row, = _hp_rows(store)
    assert (row["surface"], row["scope"], row["owner_id"], row["lane"], row["cost_basis"]) == \
        ("hermes.unattributed", "house", 0, "auto", "provider")
    assert row["pool"] == "free:openrouter" and row["key_kind"] == "house"
    a, b = _meter(owner=71, desk=9, surface="run.step"), _meter(owner=72, desk=10, surface="run.step")
    ta1 = U.hermes_begin(a, "atlas:desk9:writer")
    clock["t"] += 1
    ta2 = U.hermes_begin(a, "atlas:desk9:qa")
    assert hp.call()[0] == 200                                            # 2: one run in flight, two of its calls
    clock["t"] += 1
    tb = U.hermes_begin(b, "atlas:desk10:writer")
    hp.answer = lambda req: httpx.Response(200, json=_body("step", usage={"prompt_tokens": 50, "completion_tokens": 5,
                                                                          "cost": 0.002}, model=PAID))
    assert hp.call(model=PAID)[0] == 200                                  # 3: two owners overlap: the earliest pays
    hp.answer = None
    assert hp.call(headers={"X-Hermes-Session-Key": "atlas:desk10:writer"})[0] == 200    # 1: the session key names b's
    rows = _hp_rows(store, "kind='request' AND owner_id IN (71, 72)")
    assert [(r["owner_id"], r["action_id"], r["pool"], r["cost_usd"], r["cost_basis"]) for r in rows] == [
        (71, a.action_id, "free:openrouter", 0.0, "provider"),
        (71, a.action_id, "paid", 0.002, "provider; attributed: overlap"),
        (72, b.action_id, "free:openrouter", 0.0, "provider")]
    assert U.counter("o:71", "usd:paid", "M")[0] == 2_000 and U.counter("o:72", "req:free")[0] == 1
    assert (U.hermes_end(ta1), U.hermes_end(ta2), U.hermes_end(tb)) == (2, 0, 1)
    assert U.hermes_warning() == {}                                       # nothing charged to ta2, but the proxy was used
    v = U.hermes_view()
    assert v["today"] == {"inner": 4, "overlap": 1, "unattributed": 1, "refused": 0}
    assert (v["mode"], v["proxy_ready"], v["calls_in_flight"], v["requests_in_flight"], v["max_in_flight"]) == ("proxy", True, 0, 0, 8)
    last = v["last_request"]
    assert (last["rule"], last["model"], last["stream"]) == ("session", FREE, False)
    assert "x-hermes-session-key" in last["headers"] and "authorization" not in last["headers"]


def test_hermes_proxy_refuses_at_a_limit_and_the_delegation_ends_there(hp, U, store, monkeypatch):
    """Spec §1.8, §4.6 S22: a limit refuses an inner request with an OpenAI-style 429 at once, Retry-After to the
    reset, nothing sent; the call it belongs to ends at that limit whatever Hermes answered (the parent's typed LIMIT
    result), its delegation row saying so. The run's cap (L4) refuses with no Retry-After. The provider's own daily
    quota reaches Hermes unchanged and closes the pool; the retry is refused without a request. A per-minute refusal
    Hermes got past is no limit for the call."""
    from collections import deque
    _no_sleep(monkeypatch)
    _clock(monkeypatch, _ts(2026, 10, 10, 22, 0))
    replies = []

    def hermes_twice(req):                                # two model calls, then whatever Hermes makes of it
        replies.extend([hp.call(), hp.call()])
        return httpx.Response(200, json=_body("I could not finish: my model call failed.", model="hermes-agent"))
    U.set_override(73, "ai_requests_day", 1)              # room for the pre-check and one inner request
    m = _meter(owner=73, desk=11, surface="run.step", run_cap=10)
    with pytest.raises(U.UsageLimit) as e:
        _ask(_hermes(m, hermes_twice, session_key="atlas:desk11:writer"))
    assert e.value.limit_id == "ai_requests_day" and len(hp.sent) == 1
    (s1, _b1, _h1), (s2, b2, h2) = replies
    err = json.loads(b2)["error"]
    assert (s1, s2, err["code"], err["type"], h2["Retry-After"]) == (200, 429, 429, "usage_limit", "7200")
    assert err["message"] == "AI requests today: 1 of 1 used. Resets 00:00 UTC (in 2 h)."
    assert (err["metadata"]["limit_id"], err["metadata"]["used"], err["metadata"]["limit"]) == ("ai_requests_day", 1, 1)
    ref, = _hp_rows(store, "owner_id=73 AND kind='refusal'")
    assert (ref["surface"], ref["limit_id"], ref["action_id"]) == ("hermes.inner", "ai_requests_day", m.action_id)
    act, = _events(store, "kind='action' AND owner_id=73")
    assert (act["surface"], act["status"], act["limit_id"]) == ("hermes.call", "limit", "ai_requests_day")
    replies.clear()                                       # L4: the run's cap, nothing resets it
    m4 = _meter(owner=74, desk=12, surface="run.step", run_cap=1)
    with pytest.raises(U.UsageLimit) as e:
        _ask(_hermes(m4, hermes_twice, session_key="atlas:desk12:writer"))
    s, b, h = replies[1]
    assert e.value.limit_id == "run_requests" and m4.requests == 1 and len(hp.sent) == 2
    assert s == 429 and json.loads(b)["error"]["metadata"]["limit_id"] == "run_requests" and "Retry-After" not in h
    replies.clear()                                       # the per-minute window: refused at once, then got past

    def hermes_waits(req):
        U._minute.setdefault("free:openrouter", deque()).extend([U._now()] * 20)
        replies.append(hp.call())
        U._minute["free:openrouter"].clear()              # Hermes waited its Retry-After out
        replies.append(hp.call())
        return httpx.Response(200, json=_body("Done.", model="hermes-agent"))
    m5 = _meter(owner=75, desk=13, surface="run.step", run_cap=10)
    assert _ask(_hermes(m5, hermes_waits, session_key="atlas:desk13:writer")).text == "Done."
    (s1, b1, h1), (s2, _b, _h) = replies
    assert (s1, json.loads(b1)["error"]["metadata"]["limit_id"], h1["Retry-After"], s2) == (429, "pool_free_min", "60", 200)
    assert [a["status"] for a in _events(store, "kind='action' AND owner_id=75")] == ["ok"] and len(hp.sent) == 3
    replies.clear()                                       # the provider's daily quota
    quota = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock "
                                               "1000 free model requests per day"}}
    hp.answer = lambda req: httpx.Response(429, json=quota)

    def hermes_fails(req):
        replies.append(hp.call())
        return httpx.Response(500, json={"error": {"message": "model call failed"}})
    m6 = _meter(owner=76, desk=14, surface="run.step", run_cap=10)
    with pytest.raises(U.UsageLimit) as e:
        _ask(_hermes(m6, hermes_fails, session_key="atlas:desk14:writer"))
    assert e.value.limit_id == "pool_free_day" and U.pool_exhausted() and len(hp.sent) == 4
    (s1, b1, _h1), (s2, b2, _h2) = replies                # Atlas's provider retried the 500 once
    assert (s1, json.loads(b1)) == (429, quota)           # OpenRouter's answer, unchanged
    assert s2 == 429 and json.loads(b2)["error"]["metadata"]["limit_id"] == "pool_free_day"
    row, = _hp_rows(store, "owner_id=76 AND kind='request'")
    assert (row["status"], row["limit_id"], row["http_status"]) == ("provider_limit", "pool_free_day", 429)
    assert [a["status"] for a in _events(store, "kind='action' AND owner_id=76")] == ["limit"]


def test_hermes_proxy_streams_are_one_ledger_row_however_they_end(hp, U, store):
    """A streamed inner request is one ledger row whatever happens to the stream: an answer that came back as one JSON
    body in a 200 is read like a plain answer (its daily-quota text closes the pool), and a stream Hermes stops reading
    halfway is an error row with its slot freed."""
    quota = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}}
    hp.answer = lambda req: httpx.Response(200, json=quota)
    status, body, _h = hp.call(stream=True)
    assert (status, json.loads(body)) == (200, quota) and U.pool_exhausted()
    row, = _hp_rows(store, "kind='request'")
    assert (row["status"], row["limit_id"], row["note"]) == ("provider_limit", "pool_free_day",
                                                             "provider returned no choices: Rate limit exceeded: free-models-per-day")
    chunks = [{"choices": [{"delta": {"content": "half"}}], "model": PAID}, {"choices": [{"delta": {"content": " an answer"}}]}]
    hp.answer = lambda req: httpx.Response(200, text=_sse(chunks), headers={"Content-Type": "text/event-stream"})
    r = hp.A.app.test_client().post(HP_URL, json={"model": PAID, "messages": [], "stream": True}, buffered=False,
                                    headers={"Authorization": f"Bearer {HP_TOKEN}"})
    assert r.status_code == 200 and U._hermes_stats["inflight"] == 1      # the first bytes are on their way
    r.close()                                                             # Hermes stops reading
    row, = _hp_rows(store, "kind='request' AND pool='paid'")
    assert (row["status"], row["note"], row["cost_usd"]) == ("error", "the client closed the stream", None)
    assert U._hermes_stats["inflight"] == 0 and len(hp.sent) == 2

def test_setup_wires_new_installs_through_the_metering_proxy():
    """Spec §8.2, §8.3 steps 5-6: a new install's Hermes gets the proxy token where the OpenRouter key would go and
    model.base_url = the proxy; atlas.env gets the same token and USAGE_HERMES_METER, off unless --hermes-proxy (the
    owner's default); Caddy answers /internal/* with 404 on every run; an install from before keeps its own key."""
    sh = (Path(usage.__file__).parents[1] / "deploy" / "hetzner" / "setup.sh").read_text(encoding="utf-8")
    assert "PROXY_URL=http://127.0.0.1:8094/internal/llm/v1" in sh and "  base_url: $PROXY_URL\n" in sh
    assert "openrouter.ai/api/v1" not in sh
    assert 'setenv $HENV OPENROUTER_API_KEY "$PROXY_TOKEN"' in sh and 'PROXY_TOKEN="$(rand 32)"' in sh
    assert 'HERMES_METER="off"' in sh and '--hermes-proxy) HERMES_METER="proxy"' in sh
    assert "\nUSAGE_HERMES_METER=$HERMES_METER\nUSAGE_HERMES_PROXY_TOKEN=$PROXY_TOKEN\n" in sh
    caddy = sh.split("cat > /etc/caddy/Caddyfile <<EOT", 1)[1].split("\nEOT\n", 1)[0]
    assert caddy.index("respond /internal/* 404") < caddy.index("reverse_proxy 127.0.0.1:8094")
    assert "-w 1 --threads 32" in sh                      # one worker: the calls in flight and the proxy share a process


# ---------------------------------------------------------------------------- the page and its API (§5, §6)
def _ev(cn, **f):
    """One ledger row written straight into the table (a test that needs exact times and action ids)."""
    usage._insert_event(cn, {"owner_id": 7, "desk_id": 3, "scope": "owner", "lane": "you", "key_kind": "house",
                             "pool": "free:openrouter", "model": FREE, "kind": "request", **f})


def test_events_group_by_action_fold_alike_and_page(U, store):
    """Spec §5.2: one item per action (a run with its steps and Hermes steps is one line), consecutive alike items
    within 10 minutes fold into one with a count, a deduplicated refusal is one item whose count is its units, keyset
    paging never repeats or skips an item, the Refused and Errors filters, an item's own rows; another owner's rows
    never show."""
    t0 = time.time() - 3600
    with U._tx() as cn:
        _ev(cn, ts=t0, surface="run.you", kind="action", action_id="run1", ref="r-1", key_kind="none", pool="", model="")
        for i in range(4):
            _ev(cn, ts=t0 + 10 + i, surface="run.step", action_id="run1", tokens_in=100, tokens_out=10, cost_usd=0.0,
                cost_basis="provider")
        for i in range(2):
            _ev(cn, ts=t0 + 20 + i, surface="hermes.inner", action_id="run1", pool="paid", model=PAID, cost_usd=0.002,
                cost_basis="provider")
        for i, aid in enumerate(("a1", "a2", "a3")):
            _ev(cn, ts=t0 + 100 * (i + 1), surface="ask.cameras", action_id=aid, cost_usd=0.0, cost_basis="provider")
        _ev(cn, ts=t0 + 500, ts_end=t0 + 1500, surface="camera.note", lane="auto", ref="cam:Door", kind="refusal",
            action_id="tick9", units=50, status="refused", limit_id="ai_requests_auto_day", pool="", model="",
            note="AI requests Atlas made on its own: 125 of 125 used. Resets 00:00 UTC (in 6 h 27 min).")
        _ev(cn, ts=t0 + 2000, surface="ask.cameras", action_id="a4", cost_usd=0.0, cost_basis="provider")
        _ev(cn, ts=t0 + 2500, surface="camera.look", action_id="e1", status="error", http_status=502, ref="cam:Gate")
        _ev(cn, ts=t0 + 2600, owner_id=8, desk_id=9, surface="ask.cameras", action_id="other")
    names = {3: "Harbour Physio"}
    page = U.events_page(7, names=names)
    items = page["events"]
    assert [i["what"] for i in items] == ["Camera look · Gate", "Ask the cameras", "Camera journal paused · Door",
                                          "Ask the cameras", "Run you started · r-1"] and page["next"] is None
    look, ask4, ref, asks, run = items
    assert (look["status"], look["cost_usd"], look["cost_unreported"], look["desk"]) == ("error", None, 1, "Harbour Physio")
    assert (ask4["count"], asks["count"], asks["requests"], asks["ids"]) == (1, 3, 3, ["a3", "a2", "a1"])
    assert (ref["count"], ref["units"], ref["unit"], ref["status"], ref["limit_id"]) == (50, 50, "refusals", "refused",
                                                                                       "ai_requests_auto_day")
    assert ref["note"] == "AI requests Atlas made on its own: 125 of 125 used. Resets 00:00 UTC."   # no stale "(in ...)"
    assert (run["surface"], run["requests"], run["hermes_requests"], run["models"]) == ("run.you", 6, 2, [FREE, PAID])
    assert run["cost_usd"] == pytest.approx(0.004) and run["key"] == "house" and run["lane"] == "you"
    seen, cursor, pages = [], "", 0
    while True:                                                         # keyset paging, two items a page
        p = U.events_page(7, cursor=cursor, limit=2, names=names)
        seen += [i["what"] for i in p["events"]]
        pages += 1
        if not p["next"]:
            break
        cursor = p["next"]
    assert seen == [i["what"] for i in items] and pages == 3
    assert [i["what"] for i in U.events_page(7, status="refused")["events"]] == ["Camera journal paused · Door"]
    assert [i["what"] for i in U.events_page(7, status="error")["events"]] == ["Camera look · Gate"]
    assert U.event_counts(7) == {"refused": 1, "error": 1}
    rows = U.event_detail(7, asks["ids"])
    assert len(rows) == 3 and {r["surface"] for r in rows} == {"ask.cameras"}
    assert U.event_detail(8, asks["ids"]) == [] and U.event_detail(7, ["other"]) == []     # owners read their own rows only
    assert [i["what"] for i in U.events_page(8)["events"]] == ["Ask the cameras"]
    assert U.events_page(7, desk_id=4)["events"] == []


def test_today_counts_steps_under_their_run_and_embeddings_under_their_action(U, store):
    """Spec §6.2 item 4: a run's own steps and its Hermes specialists' steps count under the run, by lane; an Ask's
    embeddings under the Ask, the background indexer's in Search index; a camera's rows per camera; the owner's own
    key apart from Atlas's account; costs as reported, unreported ones counted."""
    t = time.time()
    with U._tx() as cn:
        for i in range(3):
            _ev(cn, ts=t - 50 + i, surface="run.step", action_id="r1", tokens_in=1000, tokens_out=100, cost_usd=0.0)
        for i in range(2):
            _ev(cn, ts=t - 40 + i, surface="hermes.inner", action_id="r1", cost_usd=0.0)
        _ev(cn, ts=t - 30, surface="run.step", action_id="r2", lane="auto", cost_usd=0.0)
        _ev(cn, ts=t - 20, surface="ask.cameras", action_id="k1", cost_usd=0.0)
        _ev(cn, ts=t - 19, surface="embed", action_id="k1", units=12, pool="paid", model="openai/text-embedding-3-small",
            tokens_in=2400, cost_usd=0.00005)
        _ev(cn, ts=t - 18, surface="embed", action_id="embed-d3-1", lane="auto", units=100, pool="paid", model="e")
        for i in range(2):
            _ev(cn, ts=t - 10 + i, surface="camera.note", action_id=f"n{i}", lane="auto", ref="cam:Door", cost_usd=0.0)
        _ev(cn, ts=t - 5, surface="run.step", action_id="r3", key_kind="byo", pool="byo", model="anthropic/claude", cost_usd=0.01)
        _ev(cn, ts=t - 86400 * 2, surface="ask.cameras", action_id="old")          # two days ago: not today
    rows = {r["label"]: r for r in U.today_view(7)}
    assert set(rows) == {"Runs you started", "Runs Atlas started on its own", "Ask the cameras", "Search index",
                         "Camera journal · Door", "Runs you started · your key"}
    you = rows["Runs you started"]
    assert (you["actions"], you["requests"], you["hermes_requests"], you["tokens_in"], you["cost_usd"]) == (1, 5, 2, 3000, 0.0)
    assert (rows["Ask the cameras"]["requests"], rows["Ask the cameras"]["embed_requests"]) == (1, 12)
    idx = rows["Search index"]
    assert (idx["requests"], idx["embed_requests"], idx["cost_usd"], idx["cost_unreported"]) == (0, 100, None, 100)
    cam = rows["Camera journal · Door"]
    assert (cam["camera"], cam["desk_id"], cam["requests"], cam["actions"]) == ("Door", 3, 2, 2)
    assert rows["Runs you started · your key"]["key"] == "byo" and rows["Runs you started · your key"]["cost_usd"] == 0.01
    assert U.today_view(7, desk_id=4) == []


def test_limits_say_what_binds_and_the_brief_follows(U, store, monkeypatch):
    """Spec §5.1, §5.3: each limit's numbers, state and basis; `binding` names a full limit that stops one below its
    own (the shared pool, the owner's daily allowance, Atlas's paid cap); an override says it is the account's own;
    an unlimited owner's limits are off; the rail's brief tracks the worst; the disk floor reads the disk."""
    clock = _clock(monkeypatch, float(_ts(2026, 10, 9, 17, 33, 0)))
    U.set_override(1, "ai_requests_day", 10, note="pilot until 31 Oct")
    monkeypatch.setattr(U.CFG, "owner_paid_usd_month", 5.0)
    monkeypatch.setattr(U.CFG, "disk_floor_pct", 10.0)
    monkeypatch.setattr(U, "_disk_usage", lambda p: (1000 * U.MB, 450 * U.MB, 550 * U.MB))
    desks = [{"id": 3, "name": "Harbour Physio", "sms": False}, {"id": 4, "name": "Bay Clinic", "sms": True}]
    view = lambda: {(l["id"], l.get("desk_id")): l for l in U.limits_view(1, desks=desks)}
    U.take([U.Row("o:1", "req:free", 10, "ai_requests_day")], units=8)
    L = view()
    l1 = L[("ai_requests_day", None)]
    for k in ("id", "label", "unit", "window", "used", "limit", "resets_at", "retry_at", "basis", "state", "pct", "binding"):
        assert k in l1
    assert (l1["used"], l1["limit"], l1["pct"], l1["state"], l1["window"]) == (8, 10, 80.0, "warn", "utc_day")
    assert l1["override"] == {"value": 10.0, "note": "pilot until 31 Oct"} and l1["basis"]["kind"] == "override"
    assert l1["basis"]["text"] == "Set for your account by the operator: pilot until 31 Oct"
    assert (l1["resets_at"], l1["retry_at"]) == (_ts(2026, 10, 10), _ts(2026, 10, 10))
    assert L[("ai_requests_auto_day", None)]["limit"] == 5 and L[("ai_requests_auto_day", None)]["binding"] is None
    assert L[("runs_at_once", None)]["pct"] is None and L[("run_requests", None)]["limit"] == 120
    assert L[("sms_segments_day", 3)]["state"] == "off" and L[("sms_segments_day", 4)]["state"] == "ok"
    assert L[("auto_sends_day", 3)]["desk"] == "Harbour Physio" and L[("auto_sends_day", 3)]["limit"] == 144
    disk = L[("disk_floor", None)]
    assert (disk["used"], disk["limit"], disk["free_mb"], disk["state"]) == (450.0, 900.0, 550.0, "ok")
    assert L[("pool_free_day", None)]["used"] is None and L[("pool_free_day", None)]["state"] == "ok"   # owners: the state only
    assert U.brief_view(1)["state"] == "warn" and U.brief_view(1)["worst"]["id"] == "ai_requests_day"
    U.mark_exhausted()                                                  # the provider said free-models-per-day
    L = view()
    assert L[("ai_requests_day", None)]["binding"]["id"] == "pool_free_day"
    assert L[("ai_requests_auto_day", None)]["binding"]["label"] == "Shared pool full"
    assert U.brief_view(1)["state"] == "full" and U.brief_view(1)["worst"]["id"] == "pool_free_day"
    clock["t"] += 86400                                                 # a new UTC day: the pool is open again
    U.take([U.Row("o:1", "req:free", 10, "ai_requests_day")], units=10)
    L = view()
    assert L[("ai_requests_day", None)]["state"] == "full" and L[("ai_requests_day", None)]["binding"] is None
    assert L[("ai_requests_auto_day", None)]["binding"]["id"] == "ai_requests_day"
    monkeypatch.setattr(U.CFG, "house_paid_usd_month", 0.0)             # Atlas's paid cap is spent: the owner's $5 is not usable
    assert view()[("paid_usd_month", None)]["binding"]["id"] == "house_paid_usd_month"
    for e in ("owner-one@example.com", "boss@example.com"):         # owner 1 above is the first account; the boss another
        store.add_user(e, "B", "B", "x")
    boss = store.user_by_email("boss@example.com")["id"]
    assert boss != 1
    monkeypatch.setattr(U.CFG, "unlimited_emails", frozenset({"boss@example.com"}))
    b = {(l["id"], l.get("desk_id")): l for l in U.limits_view(boss, desks=[])}
    assert (b[("ai_requests_day", None)]["limit"], b[("ai_requests_day", None)]["state"]) == (None, "off")
    assert b[("ai_requests_day", None)]["basis"]["text"] == "No Atlas limit on this account (operator)"
    assert U.brief_view(boss)["worst"]["id"] != "ai_requests_day"


def test_history_keeps_each_days_limit(U, store, monkeypatch):
    """Spec §5.1: 30 UTC days from the D counters, yours and Atlas's own, each day's limit as it stood (`lim`)."""
    _clock(monkeypatch, float(_ts(2026, 10, 9, 12, 0, 0)))
    for day, (tot, auto, lim) in {_ts(2026, 10, 7): (40, 10, 250), _ts(2026, 10, 8): (60, 30, 200)}.items():
        U.take([U.Row("o:1", "req:free", lim, "ai_requests_day", window_start=day)], units=tot)
        U.take([U.Row("o:1:auto", "req:free", 125, "ai_requests_auto_day", window_start=day)], units=auto)
    h = U.history(1)
    assert len(h) == 30 and h[-1]["day"] == _ts(2026, 10, 9) and h[0]["day"] == _ts(2026, 9, 10)
    by = {x["day"]: x for x in h}
    assert by[_ts(2026, 10, 7)] == {"day": _ts(2026, 10, 7), "you": 30, "auto": 10, "limit": 250}
    assert by[_ts(2026, 10, 8)] == {"day": _ts(2026, 10, 8), "you": 30, "auto": 30, "limit": 200}
    assert by[_ts(2026, 10, 1)]["limit"] is None and h[-1]["limit"] == 250       # no row on record; today's is the live one


def test_byo_key_view_reads_the_owners_key_at_most_every_five_minutes(U, store, monkeypatch):
    """Spec §2 rule 3: the provider's own figures for an owner's OpenRouter key, read with that key, at most once per
    5 minutes per entry; only the numbers are kept, never the key, its label or account ids; a refusal is shown."""
    clock = _clock(monkeypatch, 1_000_000.0)
    calls = []

    def handler(req):
        calls.append(req.headers["Authorization"])
        return httpx.Response(200, json={"data": {"label": "my key", "creator_user_id": "u_1", "usage": 9.0, "usage_daily": 0.12,
                                                  "usage_monthly": 1.23, "limit": 5.0, "limit_remaining": 3.77,
                                                  "limit_reset": "monthly", "is_free_tier": False,
                                                  "free_model_daily_requests": {"used": 3, "limit": 1000}}})
    t = httpx.MockTransport(handler)
    v = U.byo_key_view("byo:3:openrouter", "sk-or-own-1234", transport=t, wait=True)
    assert (v["usage_monthly"], v["limit"], v["limit_remaining"], v["limit_reset"], v["seen_at"]) == (1.23, 5.0, 3.77, "monthly", 1_000_000.0)
    assert calls == ["Bearer sk-or-own-1234"]
    stored = store._conn.execute("SELECT data FROM usage_provider WHERE account='byo:3:openrouter'").fetchone()[0]
    assert json.loads(stored)["usage_monthly"] == 1.23 and "sk-or-own" not in stored and "creator_user_id" not in stored
    assert "label" not in stored and "my key" not in stored
    clock["t"] += 120
    assert U.byo_key_view("byo:3:openrouter", "sk-or-own-1234", transport=t, wait=True)["usage_monthly"] == 1.23
    assert len(calls) == 1                                              # read at most every 5 minutes
    clock["t"] += 300
    U.byo_key_view("byo:3:openrouter", "sk-or-own-1234", transport=t, wait=True)
    assert len(calls) == 2
    bad = U.byo_key_view("byo:4:openrouter", "sk-or-bad", transport=httpx.MockTransport(lambda r: httpx.Response(401)), wait=True)
    assert bad["error"] == "HTTP 401" and "seen_at" not in bad


def _dname(A, did):
    """A desk's name as every page shows it: its business name, else the desk's own."""
    d = A.store.desk(did)
    return ((d.get("config") or {}).get("business") or {}).get("name") or d["name"]


def test_usage_api_shape_and_owner_isolation(app_client, monkeypatch):
    """T20 (spec §5.1-§5.4): two owners each see only their own rows; every limit carries id, label, unit, window,
    used, limit, resets_at, retry_at, basis and state; events carry ts, desk, what, units and cost_usd (null when
    the provider reported none); an owner's pool is its state and reset only; another owner's usage, desk and rows
    are out of reach (403 / 404 / nothing); the operator reads any account, the pool's counts and the house view;
    the rail's brief; /desk/usage is the page."""
    c = app_client
    A, da, oa = _signed_in(c, monkeypatch, "usage-api-a@example.com")
    usage.reset()                                                       # no minute window left over from other tests
    _guard_models(monkeypatch)

    def rows(did, asks):
        desk = A.store.desk(did)
        for _ in range(asks):
            m = usage.desk_meter(desk, "ask.cameras", lane="you")
            usage.after(usage.before(m, provider="openrouter", model=FREE), http_status=200,
                        usage={"prompt_tokens": 100, "completion_tokens": 10, "cost": 0})
        m = usage.desk_meter(desk, "camera.note", lane="auto", ref="cam:Door")
        usage.after(usage.before(m, provider="openrouter", model=FREE), http_status=200,
                    usage={"prompt_tokens": 50, "completion_tokens": 5})              # no cost reported
    rows(da, 3)
    usage.refuse(usage.desk_meter(A.store.desk(da), "camera.note", lane="auto", ref="cam:Door"),
                 usage._limit("ai_requests_auto_day", used=125, limit=125, now=time.time()), background=True)
    c.get("/logout")
    c.post("/signup", json={"name": "U", "company": "U", "email": "usage-api-b@example.com", "password": "password1"})
    db = c.post("/api/desks", json={"name": "Usage api B", "template": "sales_desk", "tier": "free"}).get_json()["id"]
    ob = int(A.store.desk(db)["owner_id"])
    rows(db, 5)
    rb = c.get("/api/usage").get_json()
    assert rb["owner"] == {"id": ob, "unlimited": False} and {e["desk_id"] for e in rb["events"]} == {db}
    assert {l["id"]: l for l in rb["limits"] if not l.get("desk_id")}["ai_requests_day"]["used"] == 6
    c.get("/logout")
    c.post("/login", json={"email": "usage-api-a@example.com", "password": "password1"})
    r = c.get("/api/usage")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    d = r.get_json()
    for k in ("as_of", "mode", "enforced", "counting_since", "owner", "windows", "limits", "rules", "today", "cameras",
              "history", "pool", "hermes", "deferred", "byo", "house_only", "events", "events_next", "sends", "desks"):
        assert k in d, k
    assert d["owner"]["id"] == oa and d["desks"] == [{"id": da, "name": _dname(A, da)}]
    for lim in d["limits"]:
        for k in ("id", "label", "unit", "window", "used", "limit", "resets_at", "retry_at", "basis", "state"):
            assert k in lim, (lim["id"], k)
        assert lim["state"] in ("ok", "warn", "full", "off")
    L = {l["id"]: l for l in d["limits"] if not l.get("desk_id")}
    assert (L["ai_requests_day"]["used"], L["ai_requests_day"]["limit"], L["ai_requests_auto_day"]["used"]) == (4, 250, 1)
    assert L["ai_requests_day"]["by_desk"] == [{"desk_id": da, "used": 4, "name": _dname(A, da)}]
    assert {s["surface"]: s["used"] for s in L["ai_requests_day"]["by_surface"]} == {"ask.cameras": 3, "camera.note": 1}
    assert set(d["pool"]) == {"state", "resets_at"} and d["pool"]["state"] == "ok"
    assert d["rules"][0]["id"] == "reply_window" and d["rules"][0]["channels"] == [] and d["rules"][0]["basis"]["text"]
    assert d["hermes"]["mode"] == "off" and d["house_only"] == ["Design chat", "Link studies", "Camera vision", "Search index"]
    for e in d["events"]:
        for k in ("ts", "desk", "what", "units", "cost_usd"):
            assert k in e, k
    ev = {e["what"]: e for e in d["events"]}
    assert ev["Camera journal paused · Door"]["count"] == 1 and ev["Camera journal paused · Door"]["status"] == "refused"
    assert ev["Camera journal note · Door"]["cost_usd"] is None and ev["Camera journal note · Door"]["cost_unreported"] == 1
    assert ev["Ask the cameras"]["cost_usd"] == 0.0 and ev["Ask the cameras"]["count"] == 3     # three asks, one line
    assert {e["desk_id"] for e in d["events"]} == {da} and d["event_counts"] == {"refused": 1, "error": 0}
    assert sum(t["requests"] for t in d["today"]) == 4
    b_item = rb["events"][0]["ids"]
    assert c.get("/api/usage/detail?ids=" + ",".join(b_item)).get_json()["rows"] == []       # B's rows: not A's to read
    assert len(c.get("/api/usage/detail?ids=" + ",".join(ev["Ask the cameras"]["ids"])).get_json()["rows"]) == 3
    assert c.get(f"/api/usage?owner={ob}").status_code == 403 and c.get("/api/usage/house").status_code == 403
    assert c.get(f"/api/usage?desk={db}").status_code == 404 and c.get(f"/api/usage/events?desk={db}").status_code == 404
    assert c.get(f"/api/usage?desk={da}").status_code == 200
    page = c.get("/api/usage/events?status=refused").get_json()
    assert [e["what"] for e in page["events"]] == ["Camera journal paused · Door"] and page["next"] is None
    brief = c.get("/api/usage/brief").get_json()
    assert brief["state"] == "ok" and brief["worst"]["id"] in ("ai_requests_day", "ai_requests_auto_day")
    assert c.get("/desk/usage").status_code == 200
    monkeypatch.setattr(usage.CFG, "operator_emails", frozenset({"usage-api-a@example.com"}))
    other = c.get(f"/api/usage?owner={ob}").get_json()
    assert other["owner"]["id"] == ob and {l["id"]: l for l in other["limits"] if not l.get("desk_id")}["ai_requests_day"]["used"] == 6
    assert {"used_provider", "used_here", "elsewhere", "limit"} <= set(other["pool"])
    house = c.get("/api/usage/house").get_json()
    owners = {o["owner_id"]: o for o in house["owners"]}
    assert owners[oa]["ai_requests_day"] == 4 and owners[ob]["ai_requests_day"] == 6 and owners[oa]["refused_today"] == 1
    assert owners[oa]["email"] == "usage-api-a@example.com"
    assert [l["id"] for l in house["limits"]] == ["public_requests_day", "house_paid_usd_month"]
    assert {"mode", "today", "warning"} <= set(house["hermes"]) and "snapshots" in house
    c.get("/logout")
    assert c.get("/api/usage").status_code == 401 and c.get("/desk/usage").status_code == 302


def test_usage_page_lists_keys_cameras_and_the_reply_window(app_client, monkeypatch):
    """Spec §2 rule 4, §1.5, §1.7, §6.2: a desk's own OpenRouter key with today's requests on it and the provider's
    own figures (read in the background with the owner's key), a key whose provider gives none says so; a camera
    without a local detector, with its journal's cadence; WhatsApp alerts and the reply window."""
    from atlas import secure as SEC
    from atlas import vision as V
    A, did, owner = _signed_in(app_client, monkeypatch, "usage-api-keys@example.com")
    usage.reset()
    key_reads = []

    def handler(req):
        if req.url.path == "/api/v1/key":
            key_reads.append(req.headers["Authorization"])
            return httpx.Response(200, json={"data": {"usage_daily": 0.0, "usage_monthly": 1.23, "limit": 5.0,
                                                      "limit_remaining": 3.77, "limit_reset": "monthly"}})
        return httpx.Response(500)
    _guard_models(monkeypatch, handler)
    monkeypatch.setattr(SEC, "vetted_ips", lambda host, scope="public": ["93.184.216.34"])
    hdr = {"X-Desk": str(did)}
    assert app_client.post(f"/api/desks/{did}/providers", json={"preset": "openrouter", "api_key": "sk-or-own-5555-6666"}).status_code == 200
    assert app_client.post(f"/api/desks/{did}/providers", json={"preset": "anthropic", "api_key": "sk-ant-own-7777-8888"}).status_code == 200
    A.store.add_connector(did, "camera", "Door", {"source": "rtsp://cam.local/1", "journal": "on", "journal_every_s": 30})
    A.store.add_connector(did, "whatsapp", "wa", {"phone_number_id": "1", "token": "t"})
    conf = A.store.desk(did).get("config") or {}
    conf["notify"] = {"channel": "whatsapp", "to": "+447700900123"}
    A.store.update_desk(did, config=conf)
    monkeypatch.setattr(type(V.DETECTOR), "available", property(lambda self: False))
    d = app_client.get("/api/usage", headers=hdr).get_json()
    keys = {k["name"]: k for k in d["byo"]}
    assert set(keys) == {"openrouter", "anthropic"} and keys["openrouter"]["key_hint"] == "…6666"
    assert keys["anthropic"]["provider_view"] is None and keys["anthropic"]["provider_note"].startswith("Usage not available from")
    assert keys["openrouter"]["today"] == {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": None, "cost_unreported": 0}
    t0 = time.time()
    while time.time() - t0 < 5 and not (keys["openrouter"]["provider_view"] or {}).get("seen_at"):
        time.sleep(0.05)
        keys = {k["name"]: k for k in app_client.get("/api/usage", headers=hdr).get_json()["byo"]}
    assert keys["openrouter"]["provider_view"]["usage_monthly"] == 1.23 and key_reads == ["Bearer sk-or-own-5555-6666"]
    cam, = d["cameras"]
    assert (cam["name"], cam["detector"], cam["journal"], cam["journal_every_s"], cam["alert_on_motion"]) == ("Door", "vlm", True, 30, False)
    rule, = d["rules"]
    assert rule["channels"] == ["whatsapp"] and rule["alerts_on_whatsapp"] is True and rule["held_today"] == 0


def test_rate_limits_answer_in_the_limit_shape(app_client, monkeypatch):
    """Spec §4.5: the per-address limiters keep their purpose (abuse) and answer like a usage limit: error
    rate_limited, the message, retry_after_s and Retry-After."""
    from atlas import secure as SEC
    import atlas.desk.app as A
    monkeypatch.setattr(A, "_RL_HOOK", SEC.RateLimiter(1, 60))
    app_client.post("/hook/no-such-token", json={})
    r = app_client.post("/hook/no-such-token", json={})
    b = r.get_json()
    assert r.status_code == 429 and b["error"] == "rate_limited" and b["message"] and 1 <= b["retry_after_s"] <= 60
    assert r.headers["Retry-After"] == str(b["retry_after_s"])
