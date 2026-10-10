"""Usage ledger and limits: every model request, send, upload and refusal Atlas makes for an account, and the one
check that stops work at a limit (round2 usage spec, sections 1-4 and 7).

The binding constraint is one shared OpenRouter account: P free-model requests a UTC day and 20 a minute for
everything this key does. Owners get shares of P, Atlas's own background work at most half of an owner's share, the
public website a share of its own, and paid models a dollar cap per UTC month. Every limit is a number from the ledger,
from the provider's own key endpoint, or from a labelled formula over those; nothing here invents a figure.

Write path. `before()` classifies one request, checks the limits in order and takes the limited counters atomically;
a refusal raises `UsageLimit` and nothing is sent. `after()` records the request with the provider-reported tokens and
cost. `allow()` is the background form: it never raises, it answers "" / "paced" / "paused" and logs one deduplicated
refusal per window. `action()`, `auto_action()`, `channel_send()` and `take_storage()` meter sends, unattended actions
and uploads. `run_begin()` / `run_end()` gate a run's start (its headroom on the free pool, the paid allowances, the
owner's runs at once) and `run_started()` records it. Nothing else writes the usage_* tables.

Windows are UTC with no local-time arithmetic: a day starts at floor(now / 86400) * 86400; a month on the 1st, 00:00.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import statistics
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from . import config as cfg

DAY = 86400
MB = 1 << 20
KEY_URL = "https://openrouter.ai/api/v1/key"     # works with a normal key; /api/v1/credits needs a management key
USAGE_URL = "/desk/usage"


# ---------------------------------------------------------------------------- configuration (read at boot)
@dataclass
class Config:
    enforce: bool = True                       # USAGE_ENFORCE=0: meter only, no refusals (incident escape hatch)
    demo: str = "meter"                        # USAGE_DEMO=enforce: owner limits apply to scripted DemoProvider requests
    pool_free_day: int = 1000                  # P when GET /api/v1/key cannot be read
    pool_min: dict[str, int | None] = field(default_factory=lambda: {"free:openrouter": 20, "free:groq": 30, "free:gemini": None})
    pool_min_reserve: int | None = None        # None: designer.DESIGN_TURN_MAX_REQUESTS (6 before the designer change)
    owner_share: float = 0.25                  # L1 = floor(P x share)            [proposed default, Q1]
    auto_share: float = 0.5                    # L2 = floor(L1 x share)           [proposed default, Q1]
    public_share: float = 0.10                 # L12 = floor(P x share)           [proposed default, Q1]
    house_paid_usd_month: float = 10.0         # L13; SPEND_CAP_USD when only that is set   [Q2]
    owner_paid_share: float = 0.25             # L3 = L13 x share                 [Q2]
    owner_paid_usd_month: float | None = None  # L3 set directly
    price_table: dict[str, tuple[float, float, str]] = field(default_factory=dict)   # model -> ($/Mtok in, out, date)
    run_max_requests: int = 120                # L4 runaway guard [decision]
    runs_at_once: int = 3                      # L5 [decision]
    busy_retry_s: int = 60                     # L5 retry_at
    window_margin_s: int = 300                 # W1 margin on the 24 h reply window
    sms_segments_day: int = 50                 # L7
    auto_sends_day: int = 144                  # L8: alerts 6/h x 24
    storage_mb: int = 4096                     # L9: 2 x the 2,048 MB camera upload maximum
    disk_floor_pct: float = 10.0               # uploads must leave this much of the data disk free
    hermes_meter: str = "off"                  # off | proxy | unmetered
    hermes_proxy_token: str = ""
    proxy_max_inflight: int = 8
    key_poll_s: int = 300                      # provider key snapshot period; 0 = no polling thread
    events_keep_days: int = 35
    counters_keep_days: int = 400
    operator_emails: frozenset[str] = frozenset()
    unlimited_emails: frozenset[str] = frozenset()


def _env_num(env, name: str, default, *, cast=float, lo: float | None = None):
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        v = cast(float(raw)) if cast is int else cast(raw)
    except ValueError:
        print(f"usage: ignoring {name}={raw!r} (not a number)")
        return default
    if lo is not None and v < lo:
        print(f"usage: ignoring {name}={raw!r} (below {lo:g})")
        return default
    return v


def _emails(raw: str) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in str(raw or "").split(",") if e.strip())


def parse_price_table(raw: str) -> dict[str, tuple[float, float, str]]:
    """USAGE_PRICE_TABLE: `<model>=<usd_per_mtok_in>:<usd_per_mtok_out>[@<date set>],...`. Model ids hold '/' and ':'
    but never '=' or ','."""
    out: dict[str, tuple[float, float, str]] = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if "=" not in part:
            continue
        model, spec = part.rsplit("=", 1)
        spec, _, date = spec.partition("@")
        try:
            pin, pout = (float(x) for x in spec.split(":", 1))
        except ValueError:
            print(f"usage: ignoring USAGE_PRICE_TABLE entry {part!r} (want model=in:out)")
            continue
        if model.strip() and pin >= 0 and pout >= 0:
            out[model.strip()] = (pin, pout, date.strip())
    return out


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    c = Config()
    c.enforce = str(env.get("USAGE_ENFORCE", "1")).strip().lower() not in ("0", "false", "no", "off")
    c.demo = "enforce" if str(env.get("USAGE_DEMO", "")).strip().lower() == "enforce" else "meter"
    c.pool_free_day = _env_num(env, "USAGE_POOL_FREE_DAY", 1000, cast=int, lo=1)
    c.pool_min = {"free:openrouter": _env_num(env, "USAGE_POOL_MIN_OPENROUTER", 20, cast=int, lo=1),
                  "free:groq": _env_num(env, "USAGE_POOL_MIN_GROQ", 30, cast=int, lo=1),
                  "free:gemini": None}          # Gemini shows its limits only in AI Studio; its 429 moves the chain on
    c.pool_min_reserve = _env_num(env, "USAGE_POOL_MIN_RESERVE", None, cast=int, lo=0)
    c.owner_share = _env_num(env, "USAGE_OWNER_SHARE", 0.25, lo=0)
    c.auto_share = _env_num(env, "USAGE_AUTO_SHARE", 0.5, lo=0)
    c.public_share = _env_num(env, "USAGE_PUBLIC_SHARE", 0.10, lo=0)
    house = _env_num(env, "USAGE_HOUSE_PAID_USD_MONTH", None, lo=0)
    if house is None:                           # the old all-time cap's value, now per UTC month; its 0 meant "no cap"
        cap = _env_num(env, "SPEND_CAP_USD", 0.0, lo=0)
        house = cap if cap > 0 else 10.0
    c.house_paid_usd_month = float(house)
    c.owner_paid_share = _env_num(env, "USAGE_OWNER_PAID_SHARE", 0.25, lo=0)
    c.owner_paid_usd_month = _env_num(env, "USAGE_OWNER_PAID_USD_MONTH", None, lo=0)
    c.price_table = parse_price_table(env.get("USAGE_PRICE_TABLE", ""))
    c.run_max_requests = _env_num(env, "USAGE_RUN_MAX_REQUESTS", 120, cast=int, lo=1)
    c.runs_at_once = _env_num(env, "USAGE_RUNS_AT_ONCE", 3, cast=int, lo=1)
    c.busy_retry_s = _env_num(env, "USAGE_BUSY_RETRY_S", 60, cast=int, lo=1)
    c.window_margin_s = _env_num(env, "USAGE_WINDOW_MARGIN_S", 300, cast=int, lo=0)
    c.sms_segments_day = _env_num(env, "USAGE_SMS_SEGMENTS_DAY", 50, cast=int, lo=0)
    c.auto_sends_day = _env_num(env, "USAGE_AUTO_SENDS_DAY", 144, cast=int, lo=0)
    c.storage_mb = _env_num(env, "USAGE_STORAGE_MB", 4096, cast=int, lo=0)
    c.disk_floor_pct = _env_num(env, "USAGE_DISK_FLOOR_PCT", 10.0, lo=0)
    mode = str(env.get("USAGE_HERMES_METER", "off") or "off").strip().lower()
    c.hermes_meter = mode if mode in ("off", "proxy", "unmetered") else "off"
    c.hermes_proxy_token = str(env.get("USAGE_HERMES_PROXY_TOKEN", "") or "").strip()
    c.proxy_max_inflight = _env_num(env, "USAGE_PROXY_MAX_INFLIGHT", 8, cast=int, lo=1)
    c.key_poll_s = _env_num(env, "USAGE_KEY_POLL_S", 300, cast=int, lo=0)
    c.events_keep_days = _env_num(env, "USAGE_EVENTS_KEEP_DAYS", 35, cast=int, lo=1)
    c.counters_keep_days = _env_num(env, "USAGE_COUNTERS_KEEP_DAYS", 400, cast=int, lo=1)
    c.operator_emails = _emails(env.get("USAGE_OPERATOR_EMAILS", ""))
    c.unlimited_emails = _emails(env.get("USAGE_UNLIMITED_EMAILS", ""))
    return c


CFG = load_config()


# ---------------------------------------------------------------------------- the limits catalogue (ids are stable)
# window: D = UTC day, M = UTC month, T = running total, run = one run or study, now = instantaneous, 60s = rolling
# minute, rule = not a counter. disk_floor and paid_unpriced are the two refusals the spec states without an id.
LIMITS: dict[str, dict[str, str]] = {
    "ai_requests_day": {"label": "AI requests today", "unit": "requests", "window": "D", "scope": "owner"},
    "ai_requests_auto_day": {"label": "AI requests Atlas made on its own", "unit": "requests", "window": "D", "scope": "owner"},
    "paid_usd_month": {"label": "Paid models this month", "unit": "usd", "window": "M", "scope": "owner"},
    "run_requests": {"label": "AI requests per run or link study", "unit": "requests", "window": "run", "scope": "owner"},
    "runs_at_once": {"label": "Runs at the same time", "unit": "runs", "window": "now", "scope": "owner"},
    "reply_window": {"label": "Outside the 24-hour reply window", "unit": "messages", "window": "rule", "scope": "desk"},
    "sms_segments_day": {"label": "SMS sent today", "unit": "segments", "window": "D", "scope": "desk"},
    "auto_sends_day": {"label": "Sent without approval today", "unit": "messages", "window": "D", "scope": "desk"},
    "storage_mb": {"label": "Files you uploaded", "unit": "MB", "window": "T", "scope": "owner"},
    "disk_floor": {"label": "Free space on Atlas's disk", "unit": "MB", "window": "now", "scope": "house"},
    "pool_free_day": {"label": "Shared free-model pool today", "unit": "requests", "window": "D", "scope": "house"},
    "pool_free_min": {"label": "Shared per-minute limit", "unit": "requests", "window": "60s", "scope": "house"},
    "public_requests_day": {"label": "Website demos today", "unit": "requests", "window": "D", "scope": "public"},
    "house_paid_usd_month": {"label": "Paid models on Atlas's account this month", "unit": "usd", "window": "M", "scope": "house"},
    "paid_unpriced": {"label": "Paid model with no reported cost", "unit": "usd", "window": "rule", "scope": "house"},
}
_UNLIMITED_IDS = ("ai_requests_day", "ai_requests_auto_day", "paid_usd_month", "runs_at_once")   # USAGE_UNLIMITED_EMAILS
_CHANNELS = ("email", "whatsapp", "sms", "instagram", "slack")
# background surfaces never wait for a per-minute slot: they skip the tick instead (spec §1.3 L11)
_BG_SURFACES = frozenset({"camera.detect", "camera.describe", "camera.note", "camera.rollup", "objects.follow", "embed"})


# ---------------------------------------------------------------------------- time (UTC windows)
def _now() -> float:
    return time.time()


_sleep = time.sleep


def day_start(now: float) -> int:
    return int(now // DAY) * DAY


def month_start(now: float) -> int:
    d = datetime.fromtimestamp(now, timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())


def next_month_start(now: float) -> int:
    d = datetime.fromtimestamp(now, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())


def window_start(period: str, now: float) -> int:
    return day_start(now) if period == "D" else month_start(now) if period == "M" else 0


def windows(now: float | None = None) -> dict[str, dict[str, int]]:
    now = _now() if now is None else now
    ds = day_start(now)
    return {"day": {"start": ds, "resets_at": ds + DAY},
            "month": {"start": month_start(now), "resets_at": next_month_start(now)}}


def _dur(seconds: float) -> str:
    s = max(0, int(math.ceil(seconds)))
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(h, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def reset_phrase(resets_at: float, now: float) -> str:
    d = datetime.fromtimestamp(resets_at, timezone.utc)
    when = d.strftime("%H:%M") + " UTC" + (f" on {d.day} {d.strftime('%b')}" if resets_at - now > DAY else "")
    return f"Resets {when} (in {_dur(resets_at - now)})."


def _fmt(v: Any, unit: str) -> str:
    if unit == "usd":
        return f"${float(v or 0):,.2f}"
    v = float(v or 0)
    return f"{int(v):,}" if v.is_integer() else f"{v:,.1f}"


# ---------------------------------------------------------------------------- the refusal
class UsageLimit(Exception):
    """A limit refused the work; nothing was sent. Deliberately NOT a RuntimeError: providers.chat retries
    RuntimeError and reads "HTTP 429" out of its text, and a limit must never be retried or fall back."""
    http_status = 429
    error = "usage_limit"

    def __init__(self, limit_id: str, *, used: Any, limit: Any, resets_at: float | None = None,
                 retry_at: float | None = None, message: str = "", window_start: int = 0, pool: str = "",
                 now: float | None = None):
        meta = LIMITS.get(limit_id, {})
        self.limit_id = limit_id
        self.label = meta.get("label", limit_id)
        self.unit = meta.get("unit", "requests")
        self.scope = meta.get("scope", "owner")
        self.used, self.limit = used, limit
        self.resets_at = int(resets_at) if resets_at else None
        self.retry_at = int(math.ceil(retry_at)) if retry_at else None
        self.window_start = int(window_start or 0)
        self.pool = pool
        self.now = _now() if now is None else now
        self.message = message or self._default_message()
        super().__init__(self.message)

    def _default_message(self) -> str:
        mb = " MB" if self.unit == "MB" else ""          # "Files you uploaded: 0.9 of 1 MB used."
        text = f"{self.label}: {_fmt(self.used, self.unit)} of {_fmt(self.limit, self.unit)}{mb} used."
        return text + (" " + reset_phrase(self.resets_at, self.now) if self.resets_at else "")

    def _shown(self, v: Any) -> Any:
        if v is None:
            return None
        if self.unit == "usd":
            return round(float(v), 2)
        f = float(v)
        return int(f) if f.is_integer() else round(f, 1)

    @property
    def retry_after_s(self) -> int | None:
        return max(0, int(math.ceil(self.retry_at - _now()))) if self.retry_at else None

    def body(self) -> dict[str, Any]:
        """The JSON of every 429 (spec §4.5)."""
        return {"error": self.error, "limit_id": self.limit_id, "label": self.label, "scope": self.scope,
                "limit": self._shown(self.limit), "used": self._shown(self.used), "unit": self.unit,
                "resets_at": self.resets_at, "retry_at": self.retry_at, "retry_after_s": self.retry_after_s,
                "message": self.message, "usage_url": USAGE_URL}

    def headers(self) -> dict[str, str]:
        ra = self.retry_after_s
        return {"Retry-After": str(ra)} if ra is not None else {}


_WINDOW_COPY = {
    "whatsapp": "Outside WhatsApp's 24-hour reply window: a template message is needed, which Atlas can't send yet.",
    "instagram": "Outside Instagram's 24-hour reply window: Instagram allows replies only within 24 hours of the "
                 "person's last message.",
}


class ChannelWindow(UsageLimit):
    """W1: a WhatsApp or Instagram send outside the 24-hour reply window. A rule, not a counter: 409, never retried
    automatically (the customer has to write first)."""
    http_status = 409
    error = "outside_window"

    def __init__(self, channel: str, *, now: float | None = None):
        now = _now() if now is None else now
        super().__init__("reply_window", used=0, limit=0, message=_WINDOW_COPY.get(channel, _WINDOW_COPY["whatsapp"]),
                         window_start=day_start(now), now=now)
        self.channel = channel

    def body(self) -> dict[str, Any]:
        return {"error": self.error, "limit_id": self.limit_id, "channel": self.channel, "retry_at": None,
                "message": self.message, "usage_url": USAGE_URL}


_AUTO = object()


def _limit(limit_id: str, *, used: Any, limit: Any, now: float, retry_at: Any = _AUTO, message: str = "",
           pool: str = "") -> UsageLimit:
    """A UsageLimit with the window's reset and the retry time of spec §4.5."""
    w = LIMITS[limit_id]["window"]
    resets = day_start(now) + DAY if w == "D" else next_month_start(now) if w == "M" else None
    if retry_at is _AUTO:
        retry_at = resets if w in ("D", "M") else (now + CFG.busy_retry_s if limit_id == "runs_at_once" else None)
    if not message:
        if limit_id == "runs_at_once":
            going = "run is" if used == 1 else "runs are"
            message = f"{_fmt(used, 'runs')} {going} going (limit {_fmt(limit, 'runs')}). Start this when one finishes."
        elif limit_id == "run_requests":
            message = (f"This run made {_fmt(used, 'requests')} AI requests (per-run cap {_fmt(limit, 'requests')}). "
                       "Stopped; nothing retries it automatically.")
        elif limit_id == "pool_free_min":
            message = (f"Shared per-minute limit: {_fmt(used, 'requests')} requests in the last minute. "
                       f"Try again in {max(1, math.ceil((retry_at or now) - now))} s.")
        elif limit_id == "pool_free_day":       # owners see the pool's state, not the whole account's counts (§5.1)
            message = "Shared free-model pool today: full. " + reset_phrase(resets, now)
    ws = month_start(now) if w == "M" else 0 if w == "T" else day_start(now)
    return UsageLimit(limit_id, used=used, limit=limit, resets_at=resets, retry_at=retry_at, message=message,
                      window_start=ws, pool=pool, now=now)


def paused_note(exc: UsageLimit) -> str:
    """The note an auto action carries when it waits for approval instead (L8)."""
    return f"auto-send paused: {exc.label} {_fmt(exc.used, exc.unit)}/{_fmt(exc.limit, exc.unit)}; approve to send"


# ---------------------------------------------------------------------------- meters, tickets, rows
@dataclass
class Meter:
    """One per action: a design turn, an Ask, a run, a camera tick, a study, a send. Parallel delegations of a run
    share one meter, so `requests` changes only under its lock."""
    owner_id: int
    desk_id: int
    scope: str                      # owner | public | house
    lane: str                       # you | auto
    surface: str                    # spec §1.4 surface id
    key_kind: str = "house"         # house | byo | demo; a provider may override it per request (before(key_kind=))
    action_id: str = field(default_factory=lambda: secrets.token_hex(6))
    ref: str = ""
    overrun: int = 0                # extra requests an in-flight interactive action may make past a full allowance
    run_cap: int = 0                # L4 for runs and link studies, 0 otherwise
    requests: int = 0
    limited: dict[str, dict] = field(default_factory=dict)   # tool surfaces refused in this run -> limit body
    # what allow() last answered per surface when it held the work: {"verdict": "paced", "why": "pace", "gap_s": ...},
    # {"verdict": "paced", "why": "minute", "retry_s": ...} or {"verdict": "paused", "limit": <limit body>}
    held: dict[str, dict] = field(default_factory=dict, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def count(self, n: int = 1) -> int:
        with self._lock:
            self.requests += n
            return self.requests

    def _reserve(self) -> bool:
        """L4: count this request unless the run's cap is reached (check and count are one step)."""
        with self._lock:
            if self.run_cap and self.requests >= self.run_cap:
                return False
            self.requests += 1
            return True

    def _unreserve(self) -> None:
        with self._lock:
            self.requests = max(0, self.requests - 1)

    def _take_overrun(self) -> bool:
        with self._lock:
            if self.overrun > 0:
                self.overrun -= 1
                return True
            return False


@dataclass(frozen=True)
class Row:
    """One limited counter row for `take()`. `limit` is in counter units (requests, segments, messages, bytes);
    None = no Atlas limit (still counted)."""
    scope_key: str
    metric: str
    limit: int | None
    limit_id: str = ""
    period: str = "D"
    window_start: int | None = None          # None: the period's window at take time


@dataclass
class Ticket:
    """One request from before() to after(). The windows are fixed at before(), so a request taken at 23:59:59 that
    finishes after midnight lands in the day it was taken."""
    meter: Meter
    pool: str
    day_start: int
    month_start: int
    taken: list[tuple[str, str, str, int]]
    provider: str
    model: str
    t0: float
    key_kind: str = "house"
    metric: str = ""
    owner_taken: bool = False                 # the owner request rows were taken in before()
    pool_taken: bool = False
    minute_ts: float | None = None
    done: bool = False
    limit: UsageLimit | None = None           # set by after() on the provider's daily-quota error: raise it
    surface: str = ""                         # step_surface() in force at before(): the ledger row's surface


# ---------------------------------------------------------------------------- module state
_store = None
_mem_lock = threading.RLock()
_dedup_lock = threading.Lock()
_snap: dict[str, Any] = {}
_pool: dict[str, Any] = {}
_minute: dict[str, deque] = {}
_streams: dict[tuple[int, str], dict[str, float]] = {}
_refusals: dict[tuple, int] = {}
_embed_lock = threading.Lock()
_embed_rows: dict[tuple, tuple[int, float]] = {}       # embeddings aggregation key -> (usage_events.id, last write)
_ov_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_email_cache: dict[int, tuple[float, str]] = {}
_reserve_cache: dict[str, int] = {}
_hermes: dict[str, dict[str, Any]] = {}      # Atlas->Hermes calls in flight (proxy mode): token -> meter, start, inner
_hermes_warn: dict[str, Any] = {}
_live_runs: dict[int, dict[str, float]] = {}  # owner -> {run meter's action id: start}: runs going on a house provider (L5)
_poller: dict[str, Any] = {"thread": None}
_unattr_log = [0.0]
LEAD_RUN: Callable[..., str] | None = None   # app._lead_case_run(desk, lead_id); deferred_tick starts runs through it
STREAM_ACTIVE_S = 600                        # a paced stream counts while it was seen in the last 10 min
_OV_TTL = 30.0


def _snap_empty() -> dict[str, Any]:
    return {"ts": 0.0, "ok": False, "error": "", "free_used": None, "free_limit": None, "usage_daily": None,
            "usage_weekly": None, "usage_monthly": None, "limit": None, "limit_remaining": None, "limit_reset": None,
            "is_free_tier": None}


def reset() -> None:
    """Forget the in-memory state: pool snapshot, minute windows, streams, caches, Hermes calls, the refusal map."""
    with _mem_lock:
        _snap.clear()
        _snap.update(_snap_empty())
        _pool.clear()
        _pool.update(since_free=0, since_day=0, since_usd=0.0, since_month=0, exhausted_at=0.0)
        _minute.clear()
        _streams.clear()
        _ov_cache.clear()
        _email_cache.clear()
        _reserve_cache.clear()
        _hermes.clear()
        _hermes_warn.clear()
        _hermes_warn.update(ts=0.0, message="", logged=0.0)
        _live_runs.clear()
    with _dedup_lock:
        _refusals.clear()
    with _embed_lock:
        _embed_rows.clear()


def init(store) -> None:
    """Bind the store the ledger lives in (no polling thread) and start from a clean in-memory state."""
    global _store
    _store = store
    reset()


def start(store) -> None:
    """Boot: check SQLite can upsert, bind the store, start the provider key snapshot thread (replaces _spend_poll)."""
    if getattr(store, "backend", "sqlite") == "sqlite" and sqlite3.sqlite_version_info < (3, 24):
        raise RuntimeError(f"the usage ledger needs SQLite 3.24 or later (upserts); this Python has {sqlite3.sqlite_version}")
    init(store)
    t = _poller.get("thread")
    if CFG.key_poll_s > 0 and (t is None or not t.is_alive()):
        t = threading.Thread(target=_poll_loop, daemon=True, name="atlas-usage-key")
        _poller["thread"] = t
        t.start()


reset()


@contextmanager
def _tx() -> Iterator[Any]:
    """The store lock and connection for one write sequence, committed once. The lock is never held across a
    network call."""
    st = _store
    with st._lock:
        try:
            yield st._conn
        except BaseException:
            try:
                st._conn.rollback()
            except Exception:
                pass
            raise
        st._conn.commit()


_EV_COLS = ("ts", "ts_end", "owner_id", "desk_id", "scope", "lane", "surface", "action_id", "ref", "kind", "units",
            "key_kind", "provider", "model", "pool", "status", "http_status", "tokens_in", "tokens_out", "tokens_cached",
            "cost_usd", "est_usd", "cost_basis", "limit_id", "note")
_EV_SQL = f"INSERT INTO usage_events({','.join(_EV_COLS)}) VALUES({','.join('?' * len(_EV_COLS))})"
_EV_DEFAULTS = {"ts_end": None, "owner_id": 0, "desk_id": 0, "scope": "owner", "lane": "", "action_id": "", "ref": "",
                "units": 1, "key_kind": "", "provider": "", "model": "", "pool": "", "status": "ok", "http_status": 0,
                "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cost_usd": None, "est_usd": None,
                "cost_basis": "", "limit_id": "", "note": ""}
_UPSERT = ("INSERT INTO usage_counters(scope_key,metric,period,window_start,n,refused,lim,updated) VALUES(?,?,?,?,?,0,?,?) "
           "ON CONFLICT(scope_key,metric,period,window_start) DO UPDATE "
           "SET n = usage_counters.n + excluded.n, lim = excluded.lim, updated = excluded.updated")
_PK = "scope_key=? AND metric=? AND period=? AND window_start=?"


def _meter_fields(m: Meter) -> dict[str, Any]:
    return {"owner_id": int(m.owner_id or 0), "desk_id": int(m.desk_id or 0), "scope": m.scope, "lane": m.lane,
            "surface": m.surface, "action_id": m.action_id, "ref": str(m.ref or "")[:120], "key_kind": m.key_kind}


def _insert_event(c, ev: dict[str, Any]) -> int:
    row = {**_EV_DEFAULTS, **ev}
    row["note"] = str(row.get("note") or "")[:200]
    cur = c.execute(_EV_SQL, tuple(row[k] for k in _EV_COLS))
    return int(cur.lastrowid or 0)


def _write(ev: dict[str, Any] | None, counters: list[tuple]) -> int | None:
    """One ledger row plus its counter upserts, under the store lock, committed once. counters:
    [(scope_key, metric, period, window_start, n, lim)]; rows with n == 0 are skipped."""
    if _store is None:
        return None
    now = _now()
    with _tx() as c:
        eid = _insert_event(c, ev) if ev else None
        for sk, metric, period, ws, n, lim in counters:
            if sk and n:
                c.execute(_UPSERT, (sk, metric, period, int(ws), int(n), lim, now))
    return eid


def counter(scope_key: str, metric: str, period: str = "D", window_start_: int | None = None,
            now: float | None = None) -> tuple[int, int, int | None]:
    """(n, refused, lim) of one counter row; (0, 0, None) when it does not exist yet."""
    if _store is None:
        return 0, 0, None
    ws = window_start(period, _now() if now is None else now) if window_start_ is None else window_start_
    r = _store._conn.execute(f"SELECT n, refused, lim FROM usage_counters WHERE {_PK}",
                             (scope_key, metric, period, int(ws))).fetchone()
    return (int(r[0] or 0), int(r[1] or 0), (None if r[2] is None else int(r[2]))) if r else (0, 0, None)


def counter_n(scope_key: str, metric: str, period: str = "D", window_start_: int | None = None,
              now: float | None = None) -> int:
    return counter(scope_key, metric, period, window_start_, now)[0]


# ---------------------------------------------------------------------------- the atomic take (spec §4.2)
def _row_limit(r: Row, used_units: int) -> tuple[Any, Any]:
    """Counter units -> the limit's display units."""
    unit = LIMITS.get(r.limit_id, {}).get("unit", "requests")
    if unit == "MB":
        return round(used_units / MB, 1), (None if r.limit is None else round(r.limit / MB, 1))
    return used_units, r.limit


def take(rows: list[Row], units: int = 1, *, meter: Meter | None = None, force: bool = False,
         now: float | None = None) -> list[tuple[str, str, str, int]]:
    """Take `units` on every row, in order, or on none: the only function that changes limited counters.

    Each row is a conditional UPDATE (`n + units <= limit`), so two takes can never both get the last slot: the store
    lock serialises them in one process; across processes SQLite serialises writers and Postgres re-checks the
    condition against the committed row. A refusal undoes the rows already taken, adds 1 to `refused` on the refusing
    row and raises UsageLimit. `force` takes unconditionally (meter-only mode). A meter with `overrun` left takes the
    unit anyway, once per call (an interactive action already under way finishes)."""
    if _store is None or not rows:
        return []
    now = _now() if now is None else now
    units = int(units)
    taken: list[tuple[str, str, str, int]] = []
    refused: tuple[Row, int] | None = None
    overran = False
    with _tx() as c:
        for r in rows:
            ws = r.window_start if r.window_start is not None else window_start(r.period, now)
            pk = (r.scope_key, r.metric, r.period, int(ws))
            c.execute("INSERT INTO usage_counters(scope_key,metric,period,window_start,n,refused,lim,updated) "
                      "VALUES(?,?,?,?,0,0,?,?) ON CONFLICT(scope_key,metric,period,window_start) DO NOTHING",
                      (*pk, r.limit, now))
            if r.limit is None:      # no Atlas limit: a NULL inside `n + ? <= ?` would refuse everything
                cur = c.execute(f"UPDATE usage_counters SET n = n + ?, lim = NULL, updated = ? WHERE {_PK}", (units, now, *pk))
            elif force:
                cur = c.execute(f"UPDATE usage_counters SET n = n + ?, lim = ?, updated = ? WHERE {_PK}",
                                (units, r.limit, now, *pk))
            else:
                cur = c.execute(f"UPDATE usage_counters SET n = n + ?, lim = ?, updated = ? WHERE {_PK} AND n + ? <= ?",
                                (units, r.limit, now, *pk, units, r.limit))
                if cur.rowcount != 1 and meter is not None and (overran or meter._take_overrun()):
                    overran = True
                    cur = c.execute(f"UPDATE usage_counters SET n = n + ?, lim = ?, updated = ? WHERE {_PK}",
                                    (units, r.limit, now, *pk))
            if cur.rowcount == 1:
                taken.append(pk)
                continue
            for tk in taken:
                c.execute(f"UPDATE usage_counters SET n = n - ? WHERE {_PK}", (units, *tk))
            c.execute(f"UPDATE usage_counters SET refused = refused + 1, lim = ?, updated = ? WHERE {_PK}",
                      (r.limit, now, *pk))
            got = c.execute(f"SELECT n FROM usage_counters WHERE {_PK}", pk).fetchone()
            refused, taken = (r, int(got[0] or 0) if got else 0), []
            break
    if refused:
        r, n = refused
        used, lim = _row_limit(r, n)
        raise _limit(r.limit_id, used=used, limit=lim, now=now)
    return taken


def _untake(taken: list[tuple[str, str, str, int]], units: int = 1) -> None:
    if _store is None or not taken:
        return
    with _tx() as c:
        for pk in taken:
            c.execute(f"UPDATE usage_counters SET n = n - ? WHERE {_PK}", (int(units), *pk))


def _bump_refused(r: Row, now: float) -> None:
    if _store is None:
        return
    ws = r.window_start if r.window_start is not None else window_start(r.period, now)
    with _tx() as c:
        c.execute("INSERT INTO usage_counters(scope_key,metric,period,window_start,n,refused,lim,updated) "
                  "VALUES(?,?,?,?,0,0,?,?) ON CONFLICT(scope_key,metric,period,window_start) DO NOTHING",
                  (r.scope_key, r.metric, r.period, int(ws), r.limit, now))
        c.execute(f"UPDATE usage_counters SET refused = refused + 1, updated = ? WHERE {_PK}",
                  (now, r.scope_key, r.metric, r.period, int(ws)))


# ---------------------------------------------------------------------------- per-owner limits (spec §1.3, §7.2)
def _overrides_for(owner_id: int) -> dict[str, Any]:
    owner_id = int(owner_id or 0)
    t = time.monotonic()
    with _mem_lock:
        hit = _ov_cache.get(owner_id)
        if hit and t - hit[0] < _OV_TTL:
            return hit[1]
    d: dict[str, Any] = {}
    if _store is not None:
        for lid, val in _store._conn.execute("SELECT limit_id, value FROM usage_overrides WHERE owner_id=?",
                                             (owner_id,)).fetchall():
            d[str(lid)] = val
    with _mem_lock:
        _ov_cache[owner_id] = (t, d)
    return d


def set_override(owner_id: int, limit_id: str, value: float | None, *, note: str = "", set_by: str = "") -> None:
    """Operator override for one owner and limit. value None = no Atlas limit (still metered)."""
    if limit_id not in LIMITS:
        raise ValueError(f"unknown limit id {limit_id!r}")
    with _tx() as c:
        c.execute("INSERT INTO usage_overrides(owner_id,limit_id,value,note,set_by,updated) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(owner_id,limit_id) DO UPDATE SET value = excluded.value, note = excluded.note, "
                  "set_by = excluded.set_by, updated = excluded.updated",
                  (int(owner_id), limit_id, None if value is None else float(value), str(note)[:200], str(set_by)[:120], _now()))
    with _mem_lock:
        _ov_cache.pop(int(owner_id), None)


def clear_override(owner_id: int, limit_id: str) -> None:
    with _tx() as c:
        c.execute("DELETE FROM usage_overrides WHERE owner_id=? AND limit_id=?", (int(owner_id), limit_id))
    with _mem_lock:
        _ov_cache.pop(int(owner_id), None)


def overrides(owner_id: int) -> list[dict[str, Any]]:
    if _store is None:
        return []
    cur = _store._conn.execute("SELECT limit_id, value, note, set_by, updated FROM usage_overrides WHERE owner_id=? "
                               "ORDER BY limit_id", (int(owner_id),))
    return [{"limit_id": r[0], "value": r[1], "note": r[2] or "", "set_by": r[3] or "", "updated": r[4]} for r in cur.fetchall()]


def _owner_email(owner_id: int) -> str:
    t = time.monotonic()
    with _mem_lock:
        hit = _email_cache.get(owner_id)
        if hit and t - hit[0] < 60:
            return hit[1]
    u = _store.user(owner_id) if _store is not None and owner_id else None
    email = str((u or {}).get("email") or "").lower()
    with _mem_lock:
        _email_cache[owner_id] = (t, email)
    return email


def is_unlimited(owner_id: int) -> bool:
    """USAGE_UNLIMITED_EMAILS: no Atlas limit on L1-L3 and L5; still metered and still bound by L10, L11, L13."""
    return bool(CFG.unlimited_emails and owner_id) and _owner_email(int(owner_id)) in CFG.unlimited_emails


def is_operator(user: dict[str, Any] | None) -> bool:
    return bool(user) and str(user.get("email") or "").lower() in CFG.operator_emails


def _floor(x: float) -> int:
    return int(math.floor(x + 1e-9))          # 1000 x 0.29 is 289.99999999999997 in floating point


def limit_value(owner_id: int, limit_id: str) -> int | float | None:
    """The limit in force for one owner: an operator override, else no limit for an unlimited owner, else the default
    derived from the live P and the env. Counts are ints, usd and MB floats; None = no Atlas limit."""
    owner_id = int(owner_id or 0)
    unit = LIMITS[limit_id]["unit"]
    ov = _overrides_for(owner_id)
    if limit_id in ov:
        v = ov[limit_id]
        return None if v is None else (float(v) if unit in ("usd", "MB") else int(v))
    if limit_id in _UNLIMITED_IDS and is_unlimited(owner_id):
        return None
    p = pool_size()
    if limit_id == "ai_requests_day":
        return _floor(p * CFG.owner_share)
    if limit_id == "ai_requests_auto_day":
        l1 = limit_value(owner_id, "ai_requests_day")
        return None if l1 is None else _floor(l1 * CFG.auto_share)
    if limit_id == "paid_usd_month":
        return float(CFG.owner_paid_usd_month if CFG.owner_paid_usd_month is not None
                     else round(CFG.house_paid_usd_month * CFG.owner_paid_share, 6))
    if limit_id == "public_requests_day":
        return _floor(p * CFG.public_share)
    if limit_id == "pool_free_day":
        return p
    if limit_id == "pool_free_min":
        return CFG.pool_min.get("free:openrouter")
    if limit_id == "house_paid_usd_month":
        return float(CFG.house_paid_usd_month)
    return {"run_requests": CFG.run_max_requests, "runs_at_once": CFG.runs_at_once,
            "sms_segments_day": CFG.sms_segments_day, "auto_sends_day": CFG.auto_sends_day,
            "storage_mb": float(CFG.storage_mb)}.get(limit_id)


def run_cap(owner_id: int) -> int:
    """L4 for a run or link study meter (0 = no cap)."""
    v = limit_value(owner_id, "run_requests")
    return int(v) if v else 0


# ---------------------------------------------------------------------------- the shared pool (L10) and its snapshot
def pool_size() -> int:
    """P: the provider's free_model_daily_requests.limit at the last poll, else USAGE_POOL_FREE_DAY."""
    v = _snap.get("free_limit")
    return int(v) if isinstance(v, (int, float)) and v > 0 else int(CFG.pool_free_day)


def pool_used(now: float | None = None) -> int:
    """The provider's count at the last poll (if it was read today) plus this server's free requests since then."""
    now = _now() if now is None else now
    ds = day_start(now)
    with _mem_lock:
        base = _snap.get("free_used") if _snap.get("ts") and day_start(_snap["ts"]) == ds else 0
        since = _pool["since_free"] if _pool["since_day"] == ds else 0
    return int(base or 0) + int(since)


def pool_exhausted(now: float | None = None) -> bool:
    now = _now() if now is None else now
    return _pool["exhausted_at"] >= day_start(now) and _pool["exhausted_at"] > 0


def mark_exhausted(now: float | None = None) -> None:
    """The provider said free-models-per-day: no free request is sent again until 00:00 UTC."""
    with _mem_lock:
        _pool["exhausted_at"] = _now() if now is None else now


def _pool_take(now: float, *, force: bool = False) -> None:
    with _mem_lock:
        ds = day_start(now)
        if _pool["since_day"] != ds:
            _pool["since_free"], _pool["since_day"] = 0, ds
        if not force:
            p = pool_size()
            used = pool_used(now)
            if pool_exhausted(now) or used >= p:
                raise _limit("pool_free_day", used=max(used, p), limit=p, now=now)
        _pool["since_free"] += 1


def _pool_release(now: float) -> None:
    with _mem_lock:
        if _pool["since_day"] == day_start(now) and _pool["since_free"] > 0:
            _pool["since_free"] -= 1


def house_openrouter_key() -> str:
    """The house OpenRouter key, resolved the way _spend_poll did it (config, then OPENROUTER_API_KEY). Reading it
    never writes a default config/providers.json."""
    try:
        conf = cfg.load("providers", cfg.DEFAULT_PROVIDERS) if (cfg.CONFIG_DIR / "providers.json").exists() else cfg.DEFAULT_PROVIDERS
        prov = conf["providers"].get("openrouter") or {}
        return cfg.resolve_api_key({**prov, "name": "openrouter"})
    except Exception:
        return ""


def _fnum(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def parse_key_data(data: dict[str, Any]) -> dict[str, Any]:
    """The numeric fields of GET /api/v1/key (plus the reset period and free-tier flag). Never the label, the key,
    creator or workspace ids."""
    fm = data.get("free_model_daily_requests") if isinstance(data.get("free_model_daily_requests"), dict) else {}
    fu, fl = _fnum(fm.get("used")), _fnum(fm.get("limit"))
    lr = data.get("limit_reset")
    return {"free_used": None if fu is None else int(fu), "free_limit": None if fl is None else int(fl),
            "usage_daily": _fnum(data.get("usage_daily")), "usage_weekly": _fnum(data.get("usage_weekly")),
            "usage_monthly": _fnum(data.get("usage_monthly")), "limit": _fnum(data.get("limit")),
            "limit_remaining": _fnum(data.get("limit_remaining")),
            "limit_reset": lr if lr in ("daily", "weekly", "monthly") else None,
            "is_free_tier": data.get("is_free_tier") if isinstance(data.get("is_free_tier"), bool) else None}


def poll_key(*, key: str | None = None, transport=None) -> dict[str, Any]:
    """Read GET /api/v1/key with the house key once: on success store the snapshot (memory + usage_provider), reset the
    "since snapshot" counters and set P; on an error keep the last snapshot and record the error."""
    key = house_openrouter_key() if key is None else key
    now = _now()
    err, data = "", None
    if not key:
        err = "no house OpenRouter key"
    else:
        try:
            import httpx
            with httpx.Client(transport=transport, timeout=15) as client:
                r = client.get(KEY_URL, headers={"Authorization": f"Bearer {key}"})
            if r.status_code >= 400:
                err = f"HTTP {r.status_code}"
            else:
                data = (r.json() or {}).get("data")
                if not isinstance(data, dict):
                    err, data = "no data in the key response", None
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:80]}"
    if data is None:
        with _mem_lock:
            _snap["error"] = err
        return snapshot()
    vals = parse_key_data(data)
    with _mem_lock:
        _snap.update(vals)
        _snap.update(ts=now, ok=True, error="")
        _pool.update(since_free=0, since_day=day_start(now), since_usd=0.0, since_month=month_start(now))
    if _store is not None:
        try:
            with _tx() as c:
                c.execute("INSERT INTO usage_provider(account, ts, data) VALUES(?,?,?)", ("house", now, json.dumps(vals)))
        except Exception as exc:
            print(f"usage: key snapshot not stored: {type(exc).__name__}: {str(exc)[:120]}")
    return snapshot()


def _poll_loop() -> None:
    while True:
        try:
            poll_key()
        except Exception as exc:
            with _mem_lock:
                _snap["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        time.sleep(max(30, CFG.key_poll_s))


def snapshot() -> dict[str, Any]:
    with _mem_lock:
        out = dict(_snap)
        out["P"] = pool_size()
        out["exhausted_at"] = _pool["exhausted_at"] or None
    return out


def house_paid_used(now: float | None = None) -> float:
    """L13 used: max(this month's ledger cost + labelled estimates, the provider's usage_monthly at the last poll plus
    this server's provider-reported cost since then). The provider figure includes other servers on the same key."""
    now = _now() if now is None else now
    ms = month_start(now)
    ledger = (counter_n("house", "usd:paid", "M", ms) + counter_n("house", "usd:est", "M", ms)) / 1e6
    prov = 0.0
    with _mem_lock:
        if _snap.get("ts") and month_start(_snap["ts"]) == ms and _snap.get("usage_monthly") is not None:
            prov = float(_snap["usage_monthly"]) + (_pool["since_usd"] if _pool["since_month"] == ms else 0.0)
    return max(ledger, prov)


def owner_paid_used(owner_id: int, now: float | None = None, *, scope: str = "owner") -> float:
    now = _now() if now is None else now
    ms = month_start(now)
    k = "pub" if scope == "public" else f"o:{int(owner_id or 0)}"
    return (counter_n(k, "usd:paid", "M", ms) + counter_n(k, "usd:est", "M", ms)) / 1e6


def house_paid_blocked(now: float | None = None) -> str:
    """The L13 message when paid house requests are refused, else "" (what app._spend_blocked returns)."""
    if not CFG.enforce:
        return ""
    now = _now() if now is None else now
    lim = limit_value(0, "house_paid_usd_month")
    used = house_paid_used(now)
    return _limit("house_paid_usd_month", used=used, limit=lim, now=now).message if lim is not None and used >= lim else ""


def spend_view(now: float | None = None) -> dict[str, Any]:
    """/api/health/full's `spend` (spec §3.5): the provider snapshot plus L13 as this server sees it."""
    s = snapshot()
    return {"usage_monthly": s["usage_monthly"], "limit": s["limit"], "limit_remaining": s["limit_remaining"],
            "free_used": s["free_used"], "free_limit": s["free_limit"], "error": s["error"], "read_at": s["ts"] or None,
            "house_paid_used": round(house_paid_used(now), 4), "house_paid_limit": limit_value(0, "house_paid_usd_month")}


# ---------------------------------------------------------------------------- per-minute windows (L11)
def _min_reserve() -> int:
    """Per-minute OpenRouter slots background work may not take: one worst-case free design turn."""
    if CFG.pool_min_reserve is not None:
        return int(CFG.pool_min_reserve)
    if "v" not in _reserve_cache:
        try:
            from . import designer
            v = int(getattr(designer, "DESIGN_TURN_MAX_REQUESTS", 0) or 0)
        except Exception:
            v = 0
        _reserve_cache["v"] = v if v > 0 else 6      # 6 = today's worst case per free design turn [calc C2]
    return _reserve_cache["v"]


def _minute_cap(pool: str, lane: str) -> int | None:
    lim = CFG.pool_min.get(pool)
    if not lim:
        return None
    if pool == "free:openrouter" and lane == "auto":
        return max(1, int(lim) - _min_reserve())
    return int(lim)


def _minute_state(pool: str, cap: int, now: float) -> tuple[int, float]:
    """(requests in the last 60 s, seconds until one more fits under `cap`). Caller holds _mem_lock."""
    dq = _minute.setdefault(pool, deque())
    while dq and dq[0] <= now - 60:
        dq.popleft()
    n = len(dq)
    return (n, 0.0) if n < cap else (n, max(0.001, dq[n - cap] + 60 - now))


def _wait_budget(meter: Meter) -> float:
    """How long a request may wait for a per-minute slot: background ticks and Hermes proxy requests never wait, run
    steps up to 90 s, a person's request up to 10 s (spec §1.3 L11)."""
    s = meter.surface or ""
    if s == "hermes.inner" or s in _BG_SURFACES:
        return 0.0
    if s.startswith(("run.", "browse.")) or s == "show.run":
        return 90.0
    return 10.0


def _minute_take(pool: str, meter: Meter) -> float | None:
    cap = _minute_cap(pool, meter.lane)
    if cap is None:
        return None
    budget, waited = _wait_budget(meter), 0.0
    while True:
        now = _now()
        with _mem_lock:
            n, wait = _minute_state(pool, cap, now)
            if wait <= 0:
                _minute[pool].append(now)
                return now
        if waited + wait > budget:
            raise _limit("pool_free_min", used=n, limit=cap, now=now, retry_at=now + wait, pool=pool)
        step = min(1.0, wait)
        _sleep(step)
        waited += step


def _minute_release(pool: str, ts: float | None) -> None:
    if ts is None:
        return
    with _mem_lock:
        try:
            _minute.get(pool, deque()).remove(ts)
        except ValueError:
            pass


# ---------------------------------------------------------------------------- classification and the check (§4.1)
def classify(provider: str, model: str, key_kind: str = "house") -> str:
    """The pool one request draws on: demo | byo | free:openrouter | free:groq | free:gemini | paid."""
    if key_kind == "demo":
        return "demo"
    if key_kind == "byo":
        return "byo"
    p = (provider or "").lower()
    if p == "groq":
        return "free:groq"
    if p == "gemini":
        return "free:gemini"
    return "free:openrouter" if (model or "").strip().lower().endswith(":free") else "paid"


def _okey(m: Meter) -> str | None:
    """The counter key a meter's requests count under: o:<owner>, pub, or none for house work."""
    return f"o:{int(m.owner_id or 0)}" if m.scope == "owner" else "pub" if m.scope == "public" else None


def _owner_rows(m: Meter) -> list[Row]:
    """The limited request rows of one free-pool request: lane row first, then the owner row (L2, L1), or L12."""
    if m.scope == "owner":
        o = int(m.owner_id or 0)
        rows = [Row(f"o:{o}:auto", "req:free", limit_value(o, "ai_requests_auto_day"), "ai_requests_auto_day")] if m.lane == "auto" else []
        return rows + [Row(f"o:{o}", "req:free", limit_value(o, "ai_requests_day"), "ai_requests_day")]
    if m.scope == "public":
        return [Row("pub", "req:free", limit_value(0, "public_requests_day"), "public_requests_day")]
    return []


def _metric(pool: str) -> str:
    return {"free:openrouter": "req:free", "free:groq": "req:free:groq", "free:gemini": "req:free:gemini",
            "paid": "req:paid", "byo": "req:byo"}.get(pool, "req:demo")


def _demo_enforced() -> bool:
    return CFG.enforce and CFG.demo == "enforce"


def _check_paid(m: Meter, provider: str, model: str, key_kind: str, now: float) -> None:
    """L13, then L3 for an owner, then the fail-closed rule for house Anthropic calls without a price (§3.4)."""
    l13 = limit_value(0, "house_paid_usd_month")
    if l13 is not None:
        used = house_paid_used(now)
        if used >= l13:
            raise _limit("house_paid_usd_month", used=used, limit=l13, now=now)
    if m.scope == "owner":
        l3 = limit_value(m.owner_id, "paid_usd_month")
        if l3 is not None:
            used = owner_paid_used(m.owner_id, now)
            if used >= l3:
                raise _limit("paid_usd_month", used=used, limit=l3, now=now)
    if (provider or "").lower() == "anthropic" and key_kind == "house" and model not in CFG.price_table:
        raise _limit("paid_unpriced", used=0, limit=0, now=now,
                     message=f"No reported cost for {model or 'this model'} on Atlas's account: set a price in "
                             "USAGE_PRICE_TABLE or use OpenRouter.")


def _unattributed() -> Meter:
    t = time.monotonic()
    if t - _unattr_log[0] > 60:
        _unattr_log[0] = t
        print("usage: a model request with no meter: recorded as house / unattributed")
    return Meter(owner_id=0, desk_id=0, scope="house", lane="auto", surface="unattributed")


def resolve(meter: Meter | None) -> Meter:
    """The meter a request counts for: the one handed over, else the one bound to this context, else house /
    unattributed (logged at most once a minute)."""
    return meter if meter is not None else (current() or _unattributed())


def before(meter: Meter | None = None, *, provider: str = "", model: str = "", key_kind: str | None = None) -> Ticket:
    """Classify one request, check its limits in order and take the limited counters (spec §4.1). Raises UsageLimit,
    and then nothing may be sent. `key_kind` overrides the meter's for this request (a run mixes house and BYO)."""
    m = resolve(meter)
    kk = key_kind or m.key_kind or "house"
    now = _now()
    pool = classify(provider, model, kk)
    t = Ticket(meter=m, pool=pool, day_start=day_start(now), month_start=month_start(now), taken=[],
               provider=(provider or "").lower(), model=model or "", t0=now, key_kind=kk,
               metric="req:free" if pool == "demo" and _demo_enforced() else _metric(pool), surface=_cur_surface.get())
    if not CFG.enforce or (pool == "demo" and CFG.demo != "enforce"):
        m.count()                                    # 1. meter only: counted, never refused
        if pool == "free:openrouter":
            _pool_take(now, force=True)
            t.pool_taken = True
            t.taken = take(_owner_rows(m), force=True, now=now)
            t.owner_taken = True
        return t
    if not m._reserve():                             # 2. L4, the run's cap, on any key
        exc = _limit("run_requests", used=m.requests, limit=m.run_cap, now=now)
        _refuse_quietly(m, exc, surface=t.surface)
        raise exc
    try:
        if pool == "byo":                            # 3. the owner's own key: metered, never limited by Atlas
            return t
        if pool == "demo":                           # USAGE_DEMO=enforce: the owner limits apply to scripted requests
            t.taken = take(_owner_rows(m), meter=m, now=now)
            t.owner_taken = True
            return t
        if pool.startswith("free:"):                 # 4. L10, L11, then the owner rows
            if pool == "free:openrouter":
                _pool_take(now)
                t.pool_taken = True
            t.minute_ts = _minute_take(pool, m)
            if pool == "free:openrouter":
                t.taken = take(_owner_rows(m), meter=m, now=now)
                t.owner_taken = True
            return t
        _check_paid(m, provider, model, kk, now)     # 5. paid: L13, L3, unpriced Anthropic
        return t
    except UsageLimit as exc:
        m._unreserve()
        if t.pool_taken:
            _pool_release(now)
        _minute_release(pool, t.minute_ts)
        _refuse_quietly(m, exc, surface=t.surface)
        raise


def precheck(meter: Meter, *, pool: str | None = None, provider: str = "", model: str = "",
             key_kind: str | None = None, record: bool = True) -> None:
    """The read-only form of before() for routes: refuse with JSON before an SSE stream starts. Nothing is taken
    (the per-request take still happens at the chokepoint); a refusal is logged like any other (record=False: not
    logged, for a caller trying several pools in turn)."""
    kk = key_kind or meter.key_kind or "house"
    pool = pool or (classify(provider, model, kk) if (provider or model) else ("byo" if kk == "byo" else "free:openrouter"))
    now = _now()
    if not CFG.enforce or (pool == "demo" and CFG.demo != "enforce"):
        return
    try:
        if meter.run_cap and meter.requests >= meter.run_cap:
            raise _limit("run_requests", used=meter.requests, limit=meter.run_cap, now=now)
        if pool == "byo":
            return
        if pool in ("free:openrouter", "demo"):
            if pool == "free:openrouter":
                p, used = pool_size(), pool_used(now)
                if pool_exhausted(now) or used >= p:
                    raise _limit("pool_free_day", used=max(used, p), limit=p, now=now)
            for r in _owner_rows(meter):
                n = counter_n(r.scope_key, r.metric, r.period, None, now)
                if r.limit is not None and n + 1 > r.limit:
                    raise _limit(r.limit_id, used=n, limit=r.limit, now=now)
        if pool.startswith("free:"):
            cap = _minute_cap(pool, meter.lane)
            if cap:
                with _mem_lock:
                    n, wait = _minute_state(pool, cap, now)
                if wait > _wait_budget(meter):
                    raise _limit("pool_free_min", used=n, limit=cap, now=now, retry_at=now + wait, pool=pool)
            return
        if pool == "paid":
            _check_paid(meter, provider, model, kk, now)
    except UsageLimit as exc:
        if record:
            _refuse_quietly(meter, exc)
        raise


# ---------------------------------------------------------------------------- after the request (spec §3.2-§3.4)
def _int0(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _tokens(u: Any) -> tuple[int, int, int]:
    """(tokens_in, tokens_out, tokens_cached) from an OpenAI-style or an Anthropic usage block."""
    if not isinstance(u, dict):
        return 0, 0, 0
    if "input_tokens" in u or "output_tokens" in u:            # Anthropic: cache reads kept apart
        return (_int0(u.get("input_tokens")) + _int0(u.get("cache_creation_input_tokens")),
                _int0(u.get("output_tokens")), _int0(u.get("cache_read_input_tokens")))
    det = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    return _int0(u.get("prompt_tokens")), _int0(u.get("completion_tokens")), _int0(det.get("cached_tokens"))


def _cost(u: Any) -> float | None:
    """The provider-reported cost (OpenRouter `usage.cost`, credits = USD); None when it reported none."""
    if not isinstance(u, dict):
        return None
    c = _fnum(u.get("cost"))
    return c if c is not None and c >= 0 else None


def _micro(usd: float | None) -> int:
    return int(round(usd * 1e6)) if usd else 0


def after(ticket: Ticket | None, *, http_status: int = 0, usage: Any = None, error: str = "",
          meta: dict[str, Any] | None = None) -> None:
    """Record one request: its status, the tokens and the cost as the provider reported them. Never raises (it runs
    in the chokepoint's `finally`). On the provider's daily-quota error it marks the pool exhausted until 00:00 UTC
    and leaves the UsageLimit on `ticket.limit` for the chokepoint to raise."""
    if ticket is None or ticket.done:
        return
    ticket.done = True
    try:
        _after(ticket, int(http_status or 0), usage, str(error or ""), meta or {})
    except Exception as exc:
        print(f"usage: ledger write failed: {type(exc).__name__}: {str(exc)[:160]}")


def _after(t: Ticket, http_status: int, usage: Any, err: str, meta: dict[str, Any]) -> None:
    m, now = t.meter, _now()
    limit_id = ""
    if "free-models-per-day" in err:
        status = "provider_limit"
        if t.key_kind == "house":                 # an owner's own key at its quota says nothing about the house pool
            limit_id = "pool_free_day"
            mark_exhausted(now)
            p = pool_size()
            t.limit = _limit("pool_free_day", used=max(p, pool_used(now)), limit=p, now=now)
    elif err or http_status >= 400:
        status = "error"
    else:
        status = "ok"
    tin, tout, tcached = _tokens(usage)
    cost = _cost(usage)
    est, basis = None, ""
    if cost is not None:
        basis = "provider"
    elif t.pool == "paid" and t.model in CFG.price_table:
        pin, pout, date = CFG.price_table[t.model]
        est = (tin + tcached) / 1e6 * pin + tout / 1e6 * pout     # cache reads at the input price: errs high
        basis = "estimate: operator price table" + (f", set {date}" if date else "")
    if meta.get("cost_basis"):
        basis = str(meta["cost_basis"])[:60]
    D, M = t.day_start, t.month_start
    ok = _okey(m)
    dk = f"{ok}:d{int(m.desk_id or 0)}" if m.scope == "owner" else None
    rows: list[tuple] = []

    def add(sk: str | None, metric: str, period: str, n: int, lim: Any = None) -> None:
        if sk and n:
            rows.append((sk, metric, period, D if period == "D" else M, n, lim))

    if not t.owner_taken:
        add(ok, t.metric, "D", 1)
    add(dk, t.metric, "D", 1)
    if t.pool == "free:openrouter" and status != "provider_limit":   # refused at the provider's cap: not in its count
        add("house", "req:free", "D", 1, pool_size())
    elif t.pool in ("free:groq", "free:gemini", "paid"):
        add("house", t.metric, "D", 1)
    cm, em = _micro(cost), _micro(est)
    if t.pool == "paid":
        l3 = limit_value(m.owner_id, "paid_usd_month") if m.scope == "owner" else None
        l13 = limit_value(0, "house_paid_usd_month")
        for metric, n in (("usd:paid", cm), ("usd:est", em)):
            add(ok, metric, "D", n)
            add(ok, metric, "M", n, _micro(l3) if l3 is not None else None)
            add(dk, metric, "D", n)
            add("house", metric, "D", n)
            add("house", metric, "M", n, _micro(l13) if l13 is not None else None)
        if cost and t.provider in ("openrouter", "hermes") and t.key_kind == "house":
            with _mem_lock:                         # on top of the provider's usage_monthly until the next poll
                if _pool["since_month"] != M:
                    _pool["since_usd"], _pool["since_month"] = 0.0, M
                _pool["since_usd"] += cost
    elif t.pool == "byo":
        add(ok, "usd:byo", "D", cm)
        add(ok, "usd:byo", "M", cm)
        add(dk, "usd:byo", "D", cm)
    ev = {**_meter_fields(m), "ts": t.t0, "ts_end": now, "kind": "request", "units": 1, "key_kind": t.key_kind,
          "provider": t.provider[:40], "model": t.model[:120], "pool": t.pool, "status": status,
          "http_status": http_status, "tokens_in": tin, "tokens_out": tout, "tokens_cached": tcached,
          "cost_usd": cost, "est_usd": est, "cost_basis": basis, "limit_id": limit_id,
          # the error's own line only: the "[upstream: raw]" part providers.py appends may echo request content
          "note": err.split(" [", 1)[0][:200] if err else str(meta.get("note") or "")}
    if t.surface:                                   # e.g. a run's browser steps: browse.step, still the run's request
        ev["surface"] = t.surface
    _write(ev, rows)


# ---------------------------------------------------------------------------- refusals (deduplicated, spec §1.6)
def refuse(meter: Meter | None, exc: UsageLimit, *, background: bool = False, surface: str = "",
           ref: str | None = None) -> None:
    """One `refusal` row per owner, limit, surface, ref and window; every repeat adds 1 to its `units` and moves
    `ts_end`. A background refusal is printed once per window. After a restart the first refusal writes a new row."""
    if _store is None:
        return
    m = meter or _unattributed()
    now = _now()
    surface = surface or m.surface
    ref = str(m.ref if ref is None else ref)[:120]
    key = (int(m.owner_id or 0), exc.limit_id, surface, ref, exc.window_start or day_start(now))
    status = "held" if isinstance(exc, ChannelWindow) else "refused"
    note = exc.message[:200]
    with _dedup_lock:
        rid = _refusals.get(key)
        first = False
        with _tx() as c:
            if rid:
                cur = c.execute("UPDATE usage_events SET units = units + 1, ts_end = ?, note = ? WHERE id = ?", (now, note, rid))
                rid = rid if cur.rowcount == 1 else None
            if not rid:
                first = True
                rid = _insert_event(c, {**_meter_fields(m), "surface": surface, "ref": ref, "ts": now, "ts_end": now,
                                        "kind": "refusal", "units": 1, "pool": exc.pool, "status": status,
                                        "limit_id": exc.limit_id, "note": note})
        _refusals[key] = rid
    if first and background:
        print(f"usage: owner {key[0]} {surface} {ref} paused: {exc.message}")


def _refuse_quietly(m: Meter, exc: UsageLimit, **kw: Any) -> None:
    try:
        refuse(m, exc, background=(m.lane == "auto"), **kw)
    except Exception as e:
        print(f"usage: refusal not recorded: {type(e).__name__}: {str(e)[:120]}")


# ---------------------------------------------------------------------------- background work: allow + pacing (§1.5)
def stream_seen(owner_id: int, stream: str, now: float | None = None) -> None:
    """A paced stream (a camera's journal, a camera's AI detection, a followed object) was active now."""
    with _mem_lock:
        _streams.setdefault((int(owner_id or 0), str(stream)), {})["seen"] = _now() if now is None else now


def active_streams(owner_id: int, now: float | None = None) -> int:
    now = _now() if now is None else now
    o = int(owner_id or 0)
    with _mem_lock:
        return sum(1 for (oid, _s), st in _streams.items() if oid == o and now - st.get("seen", 0) <= STREAM_ACTIVE_S)


def pace_gap(owner_id: int, now: float | None = None, *, per_action: int = 1, scope: str = "owner",
             streams: int | None = None) -> float:
    """Minimum seconds between paced actions of one stream so the free allowance lasts until 00:00 UTC (spec §1.5):

        gap = (resets_at - now) / max(1, floor(remaining / per_action / streams))

    remaining = the auto lane's remaining (capped by the owner's), streams = the owner's paced streams active in the
    last 10 min. 0 when no Atlas limit applies, or with no ledger bound (a CLI tool, a unit test: nothing to spread).
    Counts the free:openrouter pool only."""
    now = _now() if now is None else now
    if not CFG.enforce or _store is None:
        return 0.0
    ds = day_start(now)
    rem: list[int] = []
    if scope == "public":
        lp = limit_value(0, "public_requests_day")
        if lp is not None:
            rem.append(lp - counter_n("pub", "req:free", "D", ds))
    else:
        o = int(owner_id or 0)
        l2, l1 = limit_value(o, "ai_requests_auto_day"), limit_value(o, "ai_requests_day")
        if l2 is not None:
            rem.append(l2 - counter_n(f"o:{o}:auto", "req:free", "D", ds))
        if l1 is not None:
            rem.append(l1 - counter_n(f"o:{o}", "req:free", "D", ds))
    if not rem:
        return 0.0
    remaining = max(0, min(rem))
    k = streams if streams is not None else max(1, active_streams(owner_id, now))
    slots = math.floor(remaining / max(1, per_action) / max(1, k))
    return (ds + DAY - now) / max(1, slots)


def allow(meter: Meter, *, surface: str = "", stream: str = "", per_action: int = 1,
          pool: str = "free:openrouter") -> str:
    """The background check (never raises): "" = go, "paced" = skip this tick (pacing or a full minute window),
    "paused" = a lane, owner, pool or money limit is full; one deduplicated refusal row per window records it.
    Why it held is left on `meter.held[surface]` for the caller's status line (see held_text())."""
    now = _now()
    surface = surface or meter.surface
    if stream:
        stream_seen(meter.owner_id, stream, now)
    kk = meter.key_kind
    if not CFG.enforce or kk == "byo" or (kk == "demo" and CFG.demo != "enforce"):
        return _go(meter, surface, stream, now)
    try:
        if meter.run_cap and meter.requests >= meter.run_cap:
            raise _limit("run_requests", used=meter.requests, limit=meter.run_cap, now=now)
        if pool == "free:openrouter":
            p, used = pool_size(), pool_used(now)
            if pool_exhausted(now) or used >= p:
                raise _limit("pool_free_day", used=max(used, p), limit=p, now=now)
            for r in _owner_rows(meter):
                n = counter_n(r.scope_key, r.metric, r.period, None, now)
                if r.limit is not None and n + per_action > r.limit:
                    _bump_refused(r, now)
                    raise _limit(r.limit_id, used=n, limit=r.limit, now=now)
        elif pool == "paid":
            _check_paid(meter, "", "", kk, now)
    except UsageLimit as exc:
        _refuse_quietly(meter, exc, surface=surface)
        meter.held[surface] = {"verdict": "paused", "limit": exc.body()}
        return "paused"
    if pool.startswith("free:"):
        cap = _minute_cap(pool, meter.lane)
        if cap:
            with _mem_lock:
                wait = _minute_state(pool, cap, now)[1]
            if wait > 0:
                meter.held[surface] = {"verdict": "paced", "why": "minute", "retry_s": int(math.ceil(wait))}
                return "paced"
        if stream and pool == "free:openrouter":
            gap = pace_gap(meter.owner_id, now, per_action=per_action, scope=meter.scope)
            with _mem_lock:
                last = _streams.get((int(meter.owner_id or 0), stream), {}).get("go", 0.0)
            if gap > 0 and last and now - last < gap:
                meter.held[surface] = {"verdict": "paced", "why": "pace", "gap_s": int(round(gap))}
                return "paced"
    return _go(meter, surface, stream, now)


def _go(meter: Meter, surface: str, stream: str, now: float) -> str:
    meter.held.pop(surface, None)
    if stream:
        with _mem_lock:
            _streams.setdefault((int(meter.owner_id or 0), stream), {})["go"] = now
    return ""


def brief(limit: "UsageLimit | dict[str, Any]") -> str:
    """'<label> <used>/<limit>' for a status line: numbers first (spec §4.9)."""
    b = limit.body() if isinstance(limit, UsageLimit) else (limit or {})
    unit = b.get("unit") or "requests"
    return f"{b.get('label', '')} {_fmt(b.get('used'), unit)}/{_fmt(b.get('limit'), unit)}"


def until_text(limit: "UsageLimit | dict[str, Any]", now: float | None = None) -> str:
    """When held work runs again, as 'HH:MM UTC' (plus the date when that is over a day away); '' when nothing
    retries it automatically (the per-run cap, storage)."""
    b = limit.body() if isinstance(limit, UsageLimit) else (limit or {})
    when = b.get("retry_at") or b.get("resets_at")
    if not when:
        return ""
    now = _now() if now is None else now
    d = datetime.fromtimestamp(float(when), timezone.utc)
    return d.strftime("%H:%M") + " UTC" + (f" on {d.day} {d.strftime('%b')}" if float(when) - now > DAY else "")


def held_text(meter: Meter, surface: str, what: str, *, unit: str = "") -> str:
    """The camera tile's line for work allow() held, e.g. "AI counts paced: one every 12 min", "Journal paced: one
    note every 23 min", "Detection paused until 00:00 UTC: AI requests Atlas made on its own 125/125". '' = not held."""
    h = meter.held.get(surface) or {}
    if h.get("verdict") == "paused":
        until = until_text(h.get("limit") or {})
        return f"{what} paused" + (f" until {until}" if until else "") + ": " + brief(h.get("limit") or {})
    if h.get("verdict") == "paced":
        if h.get("why") == "pace":
            return f"{what} paced: one {unit + ' ' if unit else ''}every {max(1, round(h.get('gap_s', 0) / 60))} min"
        return f"{what} paced: the shared per-minute limit is full, next try in {max(1, int(h.get('retry_s') or 1))} s"
    return ""


# ---------------------------------------------------------------------------- actions, sends, uploads
def action(meter: Meter | None, kind: str = "action", units: int = 1, *, metric: str = "", surface: str = "",
           status: str = "ok", provider: str = "", model: str = "", key_kind: str = "none", ref: str | None = None,
           note: str = "", cost_usd: float | None = None, limit_id: str = "") -> int | None:
    """A ledger row for a run start, a delegation, a send or an upload (kind action | send | upload), plus its counter
    on the owner and the desk breakdown. Run starts in lane auto also count in `runs:auto`. Never raises."""
    if _store is None:
        return None
    m = meter or _unattributed()
    now = _now()
    ds = day_start(now)
    rows: list[tuple] = []
    ok = _okey(m)
    if metric and ok:
        rows.append((ok, metric, "D", ds, int(units), None))
        if m.scope == "owner":
            rows.append((f"{ok}:d{int(m.desk_id or 0)}", metric, "D", ds, int(units), None))
        if metric == "runs" and m.lane == "auto":
            rows.append((ok, "runs:auto", "D", ds, int(units), None))
    ev = {**_meter_fields(m), "ts": now, "kind": kind, "units": int(units), "surface": surface or m.surface,
          "key_kind": key_kind, "provider": provider, "model": model, "status": status, "cost_usd": cost_usd,
          "cost_basis": "provider" if cost_usd is not None else "", "limit_id": limit_id, "note": note}
    if ref is not None:
        ev["ref"] = str(ref)[:120]
    try:
        return _write(ev, rows)
    except Exception as exc:
        print(f"usage: action not recorded: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def auto_action(meter: Meter, kind: str) -> UsageLimit | None:
    """L8: one action no person approved (an auto alert or report, a calendar booking, an MCP write, a non-GET HTTP
    call, a Higgsfield generation, a pre-approved browser submit). Reserves 1 on the desk's day; an attempt counts as an
    attempt. Returns the refusal instead of raising: the caller queues the action for approval with paused_note()."""
    now = _now()
    surface = ("send." if kind in _CHANNELS else "action.") + kind
    row = Row(f"d{int(meter.desk_id or 0)}", "send:auto", limit_value(meter.owner_id, "auto_sends_day"), "auto_sends_day")
    try:
        take([row], force=not CFG.enforce, now=now)
    except UsageLimit as exc:
        _refuse_quietly(meter, exc, surface=surface)
        return exc
    action(meter, "action", surface=surface, note="sent without approval")
    return None


def auto_undo(meter: Meter, kind: str) -> None:
    """The unattended action auto_action() let through did not go out after all: the channel's own rule held it (the
    reply window, the day's SMS segments). Its L8 unit goes back and its ledger row (this meter's) reads not_sent.
    Never raises."""
    if _store is None:
        return
    now = _now()
    surface = ("send." if kind in _CHANNELS else "action.") + kind
    try:
        with _tx() as c:
            c.execute(f"UPDATE usage_counters SET n = n - 1, updated = ? WHERE {_PK} AND n > 0",
                      (now, f"d{int(meter.desk_id or 0)}", "send:auto", "D", day_start(now)))
            row = c.execute("SELECT MAX(id) FROM usage_events WHERE kind='action' AND surface=? AND action_id=? "
                            "AND status='ok'", (surface, meter.action_id)).fetchone()
            if row and row[0]:
                c.execute("UPDATE usage_events SET status='not_sent', note=? WHERE id=?",
                          ("held before sending: approval needed", int(row[0])))
    except Exception as exc:
        print(f"usage: auto action not given back: {type(exc).__name__}: {str(exc)[:120]}")


def send_held_note(exc: UsageLimit) -> str:
    """Why an unattended send waits in the approval queue (spec §1.6): L8's paused note, the reply-window copy (W1),
    or a channel limit with its reset (L7: approving it before then is refused too)."""
    if isinstance(exc, ChannelWindow):
        return exc.message
    if exc.limit_id == "auto_sends_day":
        return paused_note(exc)
    until = until_text(exc)
    return f"auto-send paused: {brief(exc)}" + (f"; resets {until}" if until else "")


def _contact_keys(kind: str, to: str) -> tuple[str, ...]:
    """The `messages.contact_key`s a recipient may be stored under, the canonical one first: ig:<id> for Instagram;
    for a phone, + and the number the sender actually dials (integrations._digits: 0044... and UK 07... become 44...,
    as the hooks store inbound numbers), then store.message_key's plain digits; else the lowercased address."""
    if kind == "instagram":
        a = str(to or "").strip().lower()
        return (a if a.startswith("ig:") else "ig:" + "".join(ch for ch in a if ch.isdigit()),)
    from .store import Store
    plain = Store.message_key(to)
    if "@" in str(to or "") or kind == "email":
        return (plain,)
    from . import integrations as I
    dialled = "+" + I._digits(str(to or "").replace("whatsapp:", ""))
    return (dialled, plain) if dialled != "+" and dialled != plain else (plain,)


def _contact_key(kind: str, to: str) -> str:
    return _contact_keys(kind, to)[0]


def recipient_ref(kind: str, to: str) -> str:
    """A recipient as the ledger stores it: to:<16 hex of HMAC-SHA256(desk secret, normalised address)>."""
    from . import secure as SEC
    return "to:" + hmac.new(SEC._secret_material(), _contact_key(kind, to).encode(), hashlib.sha256).hexdigest()[:16]


def last_inbound(desk_id: int, kind: str, to: str) -> float | None:
    """When this recipient last wrote to the desk on this channel (one indexed lookup in `messages`)."""
    if _store is None:
        return None
    keys = _contact_keys(kind, to)
    r = _store._conn.execute(f"SELECT MAX(ts) FROM messages WHERE desk_id=? AND contact_key IN ({','.join('?' * len(keys))}) "
                             "AND channel=? AND dir='in'", (int(desk_id), *keys, kind)).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def send_error_note(exc: BaseException) -> str:
    """A failed send as the ledger may keep it: the error's type and the provider's HTTP status, never its text
    (provider errors echo the recipient's number, account ids and message content; spec §3.7)."""
    m = re.search(r"\bHTTP (\d{3})\b", str(exc))
    return type(exc).__name__ + (f": HTTP {m.group(1)}" if m else "")


@dataclass
class Reservation:
    """A send cleared by W1 and holding its L7 segment. Send outside the store lock, then confirm() or release()."""
    desk_id: int
    owner_id: int
    kind: str
    ref: str
    meter: Meter
    taken: list[tuple[str, str, str, int]]
    t0: float
    done: bool = False

    def confirm(self, segments: int | None = None, *, provider: str = "", cost_usd: float | None = None,
                note: str = "") -> None:
        """Sent. For SMS, Twilio's num_segments (0 or missing counts as 1); the extra segments are added past the
        reservation unconditionally, a bounded overshoot of the last message."""
        if self.done:
            return
        self.done = True
        n = max(1, int(segments or 1)) if self.kind == "sms" else 1
        try:
            if self.taken and n > 1 and _store is not None:
                with _tx() as c:
                    c.execute(f"UPDATE usage_counters SET n = n + ?, updated = ? WHERE {_PK}", (n - 1, _now(), *self.taken[0]))
            ds = day_start(self.t0)
            m = self.meter
            _write({**_meter_fields(m), "ts": _now(), "kind": "send", "units": n, "surface": f"send.{self.kind}",
                    "ref": self.ref, "key_kind": "channel", "provider": provider, "cost_usd": cost_usd,
                    "cost_basis": "provider" if cost_usd is not None else "", "note": note},
                   [(f"d{self.desk_id}", f"send:{self.kind}", "D", ds, 1, None),
                    (f"o:{self.owner_id}", f"send:{self.kind}", "D", ds, 1, None)])
        except Exception as exc:
            print(f"usage: send not recorded: {type(exc).__name__}: {str(exc)[:120]}")

    def release(self, *, error: str = "", provider: str = "") -> None:
        """Nothing was sent: give the reserved segment back (and record the failed attempt when `error`: system text
        only, see send_error_note)."""
        if self.done:
            return
        self.done = True
        try:
            if self.taken:
                _untake(self.taken)
            if error:
                _write({**_meter_fields(self.meter), "ts": _now(), "kind": "send", "units": 0, "surface": f"send.{self.kind}",
                        "ref": self.ref, "key_kind": "channel", "provider": provider, "status": "not_sent",
                        "note": error}, [])
        except Exception as exc:
            print(f"usage: release not recorded: {type(exc).__name__}: {str(exc)[:120]}")


def channel_send(desk: dict[str, Any], kind: str, to: str, *, meter: Meter | None = None) -> Reservation:
    """Before one outbound message: the W1 reply-window read (WhatsApp, Instagram) and the L7 SMS reservation.
    Raises ChannelWindow (409) or UsageLimit (429); nothing is sent then. W1 mirrors the channel's own rule, so it
    holds even with USAGE_ENFORCE=0."""
    now = _now()
    did, oid = int(desk["id"]), int(desk.get("owner_id") or 0)
    ref = recipient_ref(kind, to)
    m = meter or Meter(owner_id=oid, desk_id=did, scope=desk_scope(oid), lane=bound_lane() or "you",
                       surface=f"send.{kind}", key_kind="channel", ref=ref)
    if kind in ("whatsapp", "instagram"):
        last = last_inbound(did, kind, to)
        if last is None or now - last > DAY - CFG.window_margin_s:
            exc = ChannelWindow(kind, now=now)
            ds = day_start(now)
            try:
                _write(None, [(f"d{did}", "held:window", "D", ds, 1, None), (f"o:{oid}", "held:window", "D", ds, 1, None)])
            except Exception:
                pass
            _refuse_quietly(m, exc, surface=f"send.{kind}", ref=ref)
            raise exc
    taken: list[tuple[str, str, str, int]] = []
    if kind == "sms":
        try:
            taken = take([Row(f"d{did}", "seg:sms", limit_value(oid, "sms_segments_day"), "sms_segments_day")],
                         force=not CFG.enforce, now=now)
        except UsageLimit as exc:
            _refuse_quietly(m, exc, surface="send.sms", ref=ref)
            raise
    return Reservation(desk_id=did, owner_id=oid, kind=kind, ref=ref, meter=m, taken=taken, t0=now)


def _disk_usage(path: str):
    return shutil.disk_usage(path)


def _floor_check(nbytes: int, written: bool, now: float) -> None:
    """Refuse an upload that would leave less than USAGE_DISK_FLOOR_PCT of the data disk free. It protects the
    server, so it holds even with USAGE_ENFORCE=0."""
    pct = float(CFG.disk_floor_pct or 0)
    if pct <= 0:
        return
    try:
        total, _used, free = _disk_usage(str(cfg.DATA_DIR))
    except OSError:
        return
    left = free - (0 if written else nbytes)
    if total and left < total * pct / 100:
        raise _limit("disk_floor", used=round((total - left) / MB, 1), limit=round(total * (1 - pct / 100) / MB, 1),
                     now=now, message=f"Atlas's disk is nearly full: an upload must leave {pct:g}% of it free, and this "
                                      f"one would leave {max(0.0, 100 * left / total):.1f}%.")


def _storage_row(owner_id: int) -> Row:
    lim = limit_value(owner_id, "storage_mb")
    return Row(f"o:{int(owner_id or 0)}", "storage", None if lim is None else int(lim * MB), "storage_mb", "T", 0)


def check_storage(owner_id: int, nbytes: int, *, meter: Meter | None = None) -> None:
    """The pre-check with a Content-Length, before anything is written (L9 and the disk floor). A refusal is on the
    ledger like any other (the meter's surface: upload.camera, upload.video, ...); nothing is taken."""
    now = _now()
    nbytes = max(0, int(nbytes or 0))
    m = meter or Meter(owner_id=int(owner_id or 0), desk_id=0, scope=desk_scope(int(owner_id or 0)), lane="you",
                       surface="upload.file")
    try:
        _floor_check(nbytes, False, now)
        r = _storage_row(owner_id)
        if CFG.enforce and r.limit is not None:
            n = counter_n(r.scope_key, r.metric, "T", 0)
            if n + nbytes > r.limit:
                _bump_refused(r, now)
                used, lim = _row_limit(r, n)
                raise _sized(_limit("storage_mb", used=used, limit=lim, now=now), nbytes)
    except UsageLimit as exc:
        _refuse_quietly(m, exc, surface=m.surface)
        raise


def _sized(exc: UsageLimit, nbytes: int) -> UsageLimit:
    """An L9 refusal says how big the refused file is: the numbers alone ("0.9 of 1 MB used") do not."""
    if exc.limit_id == "storage_mb" and nbytes:
        exc.message += f" This file is {_fmt(round(nbytes / MB, 1), 'MB')} MB."
        exc.args = (exc.message,)
    return exc


def take_storage(owner_id: int, nbytes: int, *, written: bool = True, meter: Meter | None = None,
                 surface: str = "upload.file", charge: int | None = None) -> None:
    """Charge an upload's saved size (os.path.getsize after the write). Raises UsageLimit over L9 or the disk floor;
    the caller then deletes the file. A body without a Content-Length is charged here all the same. `charge`: what
    the running total grows by when that is not the saved size (a file replacing one of the owner's own: the net
    growth); 0 or less checks and takes nothing, and the upload row still records the saved size."""
    now = _now()
    nbytes = max(0, int(nbytes or 0))
    grow = nbytes if charge is None else int(charge)
    m = meter or Meter(owner_id=int(owner_id or 0), desk_id=0, scope=desk_scope(int(owner_id or 0)), lane="you",
                       surface=surface)
    if grow > 0:
        try:
            _floor_check(grow, written, now)
            take([_storage_row(owner_id)], units=grow, force=not CFG.enforce, now=now)
        except UsageLimit as exc:
            _refuse_quietly(m, _sized(exc, nbytes), surface=surface)
            raise
    action(m, "upload", nbytes, surface=surface, note=f"{nbytes / MB:.1f} MB")


def release_storage(owner_id: int, nbytes: int) -> None:
    """A stored upload was deleted."""
    if _store is None or not nbytes:
        return
    n = max(0, int(nbytes))
    with _tx() as c:
        c.execute(f"UPDATE usage_counters SET n = CASE WHEN n > ? THEN n - ? ELSE 0 END, updated = ? WHERE {_PK}",
                  (n, n, _now(), f"o:{int(owner_id or 0)}", "storage", "T", 0))


def set_storage(owner_id: int, nbytes: int) -> None:
    """The daily reconcile: the owner's files on disk, measured."""
    if _store is None:
        return
    lim = _storage_row(owner_id).limit
    with _tx() as c:
        c.execute("INSERT INTO usage_counters(scope_key,metric,period,window_start,n,refused,lim,updated) VALUES(?,?,?,?,?,0,?,?) "
                  "ON CONFLICT(scope_key,metric,period,window_start) DO UPDATE SET n = excluded.n, lim = excluded.lim, "
                  "updated = excluded.updated", (f"o:{int(owner_id or 0)}", "storage", "T", 0, max(0, int(nbytes)), lim, _now()))


def reconcile_storage(sizes: dict[int, int]) -> dict[int, int]:
    """The daily reconcile (spec §1.3 L9 step 3): each owner's running total becomes what the caller's walk of their
    upload folders measured on disk; an owner with a total but no files left goes to 0. Returns {owner: bytes} set."""
    if _store is None:
        return {}
    known: set[int] = set()
    for (sk,) in _store._conn.execute("SELECT scope_key FROM usage_counters WHERE metric='storage' AND period='T'").fetchall():
        try:
            known.add(int(str(sk).split(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    out: dict[int, int] = {}
    for oid in sorted(known | {int(o) for o in sizes}):
        out[oid] = max(0, int(sizes.get(oid, 0) or 0))
        set_storage(oid, out[oid])
    return out


# ---------------------------------------------------------------------------- runs (spec §1.3 L1 headroom, L2, L3, L5)
RUN_START_SURFACES = ("run.you", "run.auto")   # the ledger row of a run start; the run's own requests are run.step


def demo_enforced() -> bool:
    """USAGE_DEMO=enforce: scripted DemoProvider requests count against the owner limits (tests, the limit UX)."""
    return _demo_enforced()


def run_meter(desk: dict[str, Any] | None, lane: str) -> Meter:
    """The meter of one desk run. Every model request of the run counts for it: Atlas, the specialists, and their
    tools' vision, search and browser steps, in the run's lane and under the per-run cap (L4)."""
    m = desk_meter(desk, "run.step", lane=lane)
    m.run_cap = run_cap(m.owner_id)
    return m


def run_headroom(desk_id: int, now: float | None = None) -> tuple[int, float | None]:
    """A run's start headroom on the free pool (spec §1.3 L1): max(2, the median AI requests per run of this desk
    over the last 7 UTC days, from the ledger), and that median (None when no run of the desk was measured yet). 2 is
    Atlas plus one specialist. A run counts through its start row (run.you / run.auto) and every request under its
    meter; embeddings are not AI requests, and runs still going are left out so they do not pull the median down."""
    if _store is None:
        return 2, None
    now = _now() if now is None else now
    since = day_start(now) - 6 * DAY
    did = int(desk_id or 0)
    marks = ",".join("?" * len(RUN_START_SURFACES))
    rows = _store._conn.execute(
        "SELECT e.action_id, SUM(e.units) FROM usage_events e WHERE e.desk_id=? AND e.ts>=? AND e.kind='request' "
        "AND e.surface<>'embed' AND e.action_id IN (SELECT a.action_id FROM usage_events a WHERE a.desk_id=? AND "
        f"a.ts>=? AND a.kind='action' AND a.surface IN ({marks})) GROUP BY e.action_id",
        (did, since, did, since, *RUN_START_SURFACES)).fetchall()
    with _mem_lock:
        going = {aid for runs in _live_runs.values() for aid in runs}
    counts = sorted(int(n or 0) for aid, n in rows if aid not in going and n)
    if not counts:
        return 2, None
    med = statistics.median(counts)
    return max(2, int(math.ceil(med))), med


def _check_headroom(meter: Meter, now: float, need: int | None = None) -> None:
    """Refuse a free-pool run that would stop after a step or two: fewer requests left today than its headroom, in
    the shared pool (L10), then the lane and the owner's allowance (L2, L1), or the website's share (L12)."""
    if need is None:
        need, med = run_headroom(meter.desk_id, now)
        why = (f"A run on this desk usually needs about {need} (median of the last 7 days)." if med is not None
               else "A run needs at least 2 (Atlas and one specialist).")
    else:
        why = f"A run needs at least {need} (Atlas and one specialist)."
    resets = reset_phrase(day_start(now) + DAY, now)
    p = pool_size()
    used = max(pool_used(now), p if pool_exhausted(now) else 0)
    if p - used < need:                     # owners see the pool's state, not the whole account's counts (§5.1)
        raise _limit("pool_free_day", used=used, limit=p, now=now,
                     message=f"Shared free-model pool today: not enough left for a run. {why} {resets}")
    for r in _owner_rows(meter):
        if r.limit is None:
            continue
        n = counter_n(r.scope_key, r.metric, r.period, None, now)
        if r.limit - n < need:
            raise _limit(r.limit_id, used=n, limit=r.limit, now=now,
                         message=f"{LIMITS[r.limit_id]['label']}: {_fmt(n, 'requests')} of {_fmt(r.limit, 'requests')} "
                                 f"used. {why} {resets}")


def _take_run_slot(meter: Meter, now: float) -> None:
    """L5: one of the owner's runs-at-once, checked and taken in one step."""
    o = int(meter.owner_id or 0)
    lim = limit_value(o, "runs_at_once")
    with _mem_lock:
        going = _live_runs.setdefault(o, {})
        if CFG.enforce and lim is not None and len(going) >= lim:
            n = len(going)
            if not going:
                _live_runs.pop(o, None)
            raise _limit("runs_at_once", used=n, limit=lim, now=now)
        going[meter.action_id] = now


def run_begin(meter: Meter, *, house: bool = True, free: bool = True, paid: bool = False, need: int | None = None,
              surface: str = "") -> None:
    """Before a run starts (spec §1.3 L1 headroom, L2, L3, L5, §4.2). A run that would draw on the free pool needs
    its headroom left today (the lane's too, in lane auto: L2); one with paid house agents needs the paid allowances
    (L13, L3); and a run with any agent on a house provider takes one of the owner's runs-at-once (L5). Raises
    UsageLimit, and then nothing starts; each refusal is on the ledger once per window. run_end() gives the slot
    back. A run whose every agent is on the owner's own key takes no slot and no headroom (spec §2)."""
    now = _now()
    surface = surface or f"run.{meter.lane or 'you'}"
    try:
        if CFG.enforce and free:
            _check_headroom(meter, now, need)
        if CFG.enforce and paid:
            _check_paid(meter, "", "", "house", now)
        if house and meter.scope == "owner":
            _take_run_slot(meter, now)
    except UsageLimit as exc:
        _refuse_quietly(meter, exc, surface=surface)
        raise


def run_end(meter: Meter | None) -> None:
    """The run's thread ended: its runs-at-once slot is free again (idempotent)."""
    if meter is None:
        return
    o = int(meter.owner_id or 0)
    with _mem_lock:
        going = _live_runs.get(o)
        if going is not None:
            going.pop(meter.action_id, None)
            if not going:
                _live_runs.pop(o, None)


def runs_going(owner_id: int) -> int:
    """The owner's runs going now that hold a runs-at-once slot (L5)."""
    with _mem_lock:
        return len(_live_runs.get(int(owner_id or 0)) or {})


def run_started(meter: Meter, run_id: str = "") -> None:
    """The run is going: its start on the ledger (metric runs, and runs:auto in lane auto), under the run's id."""
    if run_id:
        meter.ref = str(run_id)[:120]
    action(meter, "action", metric="runs", surface=f"run.{meter.lane or 'you'}", ref=meter.ref, note="run started")


def limit_state(owner_id: int, limit_id: str, now: float | None = None) -> tuple[Any, Any]:
    """(used, limit) of one of the limits a run start can meet, in the limit's own unit, as the ledger and the
    provider snapshot read now; (None, limit) for a limit without a single counter here."""
    now = _now() if now is None else now
    o = int(owner_id or 0)
    lim = limit_value(o, limit_id)
    ds = day_start(now)
    if limit_id == "ai_requests_day":
        used: Any = counter_n(f"o:{o}", "req:free", "D", ds)
    elif limit_id == "ai_requests_auto_day":
        used = counter_n(f"o:{o}:auto", "req:free", "D", ds)
    elif limit_id == "public_requests_day":
        used = counter_n("pub", "req:free", "D", ds)
    elif limit_id == "pool_free_day":
        used = max(pool_used(now), int(lim or 0) if pool_exhausted(now) else 0)
    elif limit_id == "paid_usd_month":
        used = owner_paid_used(o, now)
    elif limit_id == "house_paid_usd_month":
        used = house_paid_used(now)
    elif limit_id == "runs_at_once":
        used = runs_going(o)
    else:
        used = None
    return used, lim


def lead_note(owner_id: int, limit_id: str, retry_at: float | None, now: float | None = None) -> str:
    """The Leads list's line for a lead whose run waits for a limit (spec §1.6): the numbers as they stand now, then
    when it runs. Owners see the shared pool's state, never the whole account's counts (§5.1)."""
    now = _now() if now is None else now
    if limit_id == "runs_at_once":
        n = runs_going(owner_id)
        return f"Not worked yet: {n} run{' is' if n == 1 else 's are'} going; starts when one finishes"
    meta = LIMITS.get(limit_id) or {"label": limit_id or "a usage limit", "unit": "requests"}
    used, lim = limit_state(owner_id, limit_id, now)
    if limit_id == "pool_free_day":
        head = "Shared free-model pool today: full"
    elif used is None or lim is None:
        head = meta["label"]
    else:
        head = f"{meta['label']} {_fmt(used, meta['unit'])}/{_fmt(lim, meta['unit'])}"
    when = until_text({"retry_at": retry_at}, now) if retry_at else ""
    return f"Not worked yet: {head}" + (f"; runs after {when}" if when else "")


def tool_limit_text(limit: "UsageLimit | dict[str, Any]", tool: str) -> str:
    """The typed result of a tool call refused at a limit (spec §4.6 S22): the model reads that the tool is out for
    the rest of the run, so it stops calling it."""
    until = until_text(limit)
    return (f"LIMIT: {brief(limit)}" + (f", resets {until}" if until else "")
            + f". {tool} is unavailable for the rest of this run; do not call it again.")


# ---------------------------------------------------------------------------- deferred starts (spec §1.6)
def defer(meter: Meter, kind: str, ref_id: int, exc: UsageLimit) -> int | None:
    """A run start refused at a limit, to retry after `retry_at` (ids only; the task is rebuilt from the lead). One
    waiting row per lead: a second refusal moves it. The lead's status becomes `waiting`."""
    if _store is None:
        return None
    now = _now()
    retry = float(exc.retry_at or (now + CFG.busy_retry_s))
    did = int(meter.desk_id or 0)
    with _tx() as c:
        row = c.execute("SELECT id FROM usage_deferred WHERE desk_id=? AND kind=? AND ref_id=? AND status='waiting'",
                        (did, kind, int(ref_id))).fetchone()
        if row:
            c.execute("UPDATE usage_deferred SET retry_at=?, limit_id=? WHERE id=?", (retry, exc.limit_id, row[0]))
            rid = int(row[0])
        else:
            cur = c.execute("INSERT INTO usage_deferred(owner_id,desk_id,kind,ref_id,limit_id,retry_at,created,tries,status) "
                            "VALUES(?,?,?,?,?,?,?,0,'waiting')", (int(meter.owner_id or 0), did, kind, int(ref_id),
                                                                  exc.limit_id, retry, now))
            rid = int(cur.lastrowid or 0)
    if kind == "lead":
        _store.set_lead(int(ref_id), status="waiting")
    return rid


def deferred(owner_id: int | None = None, status: str = "waiting") -> list[dict[str, Any]]:
    if _store is None:
        return []
    sql, args = "SELECT * FROM usage_deferred WHERE status=?", [status]
    if owner_id is not None:
        sql += " AND owner_id=?"
        args.append(int(owner_id))
    cur = _store._conn.execute(sql + " ORDER BY retry_at, id", args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _set_deferred(rid: int, **f: Any) -> None:
    with _tx() as c:
        c.execute(f"UPDATE usage_deferred SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), rid))


def deferred_leads(desk_id: int) -> dict[int, tuple[str, float]]:
    """The desk's leads waiting for a deferred start: lead id -> (limit id, retry_at)."""
    if _store is None:
        return {}
    rows = _store._conn.execute("SELECT ref_id, limit_id, retry_at FROM usage_deferred WHERE desk_id=? AND kind='lead' "
                                "AND status='waiting'", (int(desk_id),)).fetchall()
    return {int(r[0]): (str(r[1] or ""), float(r[2])) for r in rows}


def deferred_tick(now: float | None = None, *, start: Callable[..., Any] | None = None, batch: int = 5,
                  store: Any = None) -> int:
    """Once per scheduler pass: start up to `batch` due deferred leads, oldest first, through LEAD_RUN in lane auto.
    A new refusal moves the row's retry_at on (and that owner's other due rows with it); a lead worked or deleted in
    the meantime drops its row, and so does one whose case now holds it (cases switched on meanwhile: the case clock
    retries it). `store`: the caller's store; nothing runs unless the ledger is bound to it (a test or a tool that
    bound the ledger elsewhere never gets its rows started by the app's scheduler). Returns how many runs started."""
    start = start or LEAD_RUN
    if _store is None or start is None or (store is not None and store is not _store):
        return 0
    now = _now() if now is None else now
    due = _store._conn.execute("SELECT id, owner_id, desk_id, kind, ref_id FROM usage_deferred WHERE status='waiting' "
                               "AND retry_at<=? ORDER BY retry_at, id LIMIT ?", (now, int(batch))).fetchall()
    started, blocked = 0, {}
    for rid, oid, did, kind, ref_id in due:
        if oid in blocked:
            _set_deferred(rid, retry_at=blocked[oid])
            continue
        lead = _store.lead(int(ref_id)) if kind == "lead" else None
        desk = _store.desk(int(did))
        if not lead or not desk or lead.get("status") != "waiting" or lead.get("run_id"):
            _set_deferred(rid, status="dropped")
            continue
        try:
            with bind_lane("auto"):
                start(desk, int(ref_id))
        except UsageLimit as exc:
            if getattr(exc, "case_id", None):        # its case is open and paused now: the case clock retries it
                _set_deferred(rid, status="dropped")
                continue
            blocked[oid] = float(exc.retry_at or (now + CFG.busy_retry_s))
            with _tx() as c:
                c.execute("UPDATE usage_deferred SET retry_at=?, limit_id=?, tries=tries+1 WHERE id=?",
                          (blocked[oid], exc.limit_id, rid))
            continue
        except Exception as exc:
            with _tx() as c:
                c.execute("UPDATE usage_deferred SET status='dropped', tries=tries+1 WHERE id=?", (rid,))
            _store.set_lead(int(ref_id), status="error")
            print(f"usage: deferred lead {ref_id} failed to start: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        with _tx() as c:
            c.execute("UPDATE usage_deferred SET status='started', tries=tries+1 WHERE id=?", (rid,))
        started += 1
    return started


# ---------------------------------------------------------------------------- retention (spec §3.6)
def prune(now: float | None = None) -> dict[str, int]:
    """Drop ledger rows past retention: events 35 days, D counters 400 days, provider snapshots 35 days, finished
    deferred starts 7 days. M and T counter rows and overrides are kept."""
    if _store is None:
        return {}
    now = _now() if now is None else now
    out: dict[str, int] = {}
    with _tx() as c:
        out["events"] = c.execute("DELETE FROM usage_events WHERE ts < ?", (now - CFG.events_keep_days * DAY,)).rowcount
        out["counters"] = c.execute("DELETE FROM usage_counters WHERE period='D' AND window_start < ?",
                                    (day_start(now) - CFG.counters_keep_days * DAY,)).rowcount
        out["provider"] = c.execute("DELETE FROM usage_provider WHERE ts < ?", (now - 35 * DAY,)).rowcount
        out["deferred"] = c.execute("DELETE FROM usage_deferred WHERE status IN ('started','dropped') AND created < ?",
                                    (now - 7 * DAY,)).rowcount
    with _dedup_lock:
        for k in [k for k in _refusals if k[4] < now - 40 * DAY]:
            _refusals.pop(k, None)
    with _mem_lock:
        for k in [k for k, st in _streams.items() if now - st.get("seen", 0) > DAY]:
            _streams.pop(k, None)
    return out


# ---------------------------------------------------------------------------- context: meters and lanes across calls
_cur_meter: ContextVar[Meter | None] = ContextVar("atlas_usage_meter", default=None)
_cur_lane: ContextVar[str] = ContextVar("atlas_usage_lane", default="")
_cur_surface: ContextVar[str] = ContextVar("atlas_usage_surface", default="")


@contextmanager
def step_surface(surface: str) -> Iterator[str]:
    """Requests made in this context (this thread) are recorded under `surface` instead of their meter's: a run's
    browser steps are browse.step. They still count for that meter (its owner, lane and per-run cap)."""
    tok = _cur_surface.set(surface)
    try:
        yield surface
    finally:
        _cur_surface.reset(tok)


@contextmanager
def bind(meter: Meter) -> Iterator[Meter]:
    """Fallback only: new threads and pool workers do not inherit it, so meters are handed over explicitly."""
    tok = _cur_meter.set(meter)
    try:
        yield meter
    finally:
        _cur_meter.reset(tok)


def current() -> Meter | None:
    return _cur_meter.get()


@contextmanager
def bind_lane(lane: str) -> Iterator[str]:
    """A lane-only context for background entry points (jobs, hooks, the case clock, deferred starts)."""
    tok = _cur_lane.set(lane)
    try:
        yield lane
    finally:
        _cur_lane.reset(tok)


def bound_lane() -> str:
    return _cur_lane.get()


def cli_meter(module: str) -> Meter:
    """Command-line tools on the house key: scope house, surface cli:<module>."""
    return Meter(owner_id=0, desk_id=0, scope="house", lane="you", surface=f"cli:{module}")


def house_meter(surface: str, *, lane: str = "auto", ref: str = "") -> Meter:
    """Work Atlas does for no account (the embedder's warm-up): scope house, limited only by the house limits."""
    return Meter(owner_id=0, desk_id=0, scope="house", lane=lane, surface=surface, ref=str(ref or "")[:120])


# Accounts on or off (DESK_OPEN=1: no accounts, everything is owner 0's). The app replaces it so a test that flips
# app.OPEN is seen here too.
OPEN_MODE: Callable[[], bool] = lambda: str(os.environ.get("DESK_OPEN", "")).strip() in ("1", "true", "yes")


def desk_scope(owner_id: int) -> str:
    """owner, or public for a desk owned by user 0 while accounts are on: the demo desk and the showroom count in the
    website's share (spec §7.3)."""
    return "owner" if int(owner_id or 0) or OPEN_MODE() else "public"


def desk_meter(desk: dict[str, Any] | None, surface: str, *, lane: str = "auto", ref: str = "", overrun: int = 0,
               key_kind: str = "house") -> Meter:
    """The meter for one action on a desk: its owner's allowance (or the public share, desk_scope())."""
    owner = int((desk or {}).get("owner_id") or 0)
    return Meter(owner_id=owner, desk_id=int((desk or {}).get("id") or 0), scope=desk_scope(owner), lane=lane,
                 surface=surface, key_kind=key_kind, ref=str(ref or "")[:120], overrun=int(overrun or 0))


def derive(meter: Meter, surface: str) -> Meter:
    """The same action on another surface (a camera tick's detection, note and rollup): owner, desk, scope, lane, key
    kind, action id and ref are shared; its request count is its own and it has no overrun."""
    return Meter(owner_id=meter.owner_id, desk_id=meter.desk_id, scope=meter.scope, lane=meter.lane, surface=surface,
                 key_kind=meter.key_kind, action_id=meter.action_id, ref=meter.ref)


# ---------------------------------------------------------------------------- embeddings (spec §1.3 L3/L13, §3.6)
# Search-index embeddings are paid house requests that only L13 blocks (never L3, never the free pools). They are
# metered per request but written aggregated: the background indexer as one `embed` row per (owner, desk, UTC minute),
# an Ask as one row under its own action id. Each request adds to its row and to the counters at once, so L13 never
# lags. A row per status: a failed request is never folded into an ok one.
@dataclass
class EmbedTicket:
    meter: Meter
    provider: str
    model: str
    pool: str
    t0: float
    day_start: int
    month_start: int
    done: bool = False


def _embed_check(pool: str, now: float) -> None:
    if not CFG.enforce or pool != "paid":
        return
    l13 = limit_value(0, "house_paid_usd_month")
    if l13 is not None:
        used = house_paid_used(now)
        if used >= l13:
            raise _limit("house_paid_usd_month", used=used, limit=l13, now=now)


def embed_precheck(meter: Meter | None, *, provider: str = "", model: str = "") -> None:
    """Would an embeddings request be refused now (L13)? Raises UsageLimit and records the deduplicated refusal; takes
    nothing. Retrieval asks before it starts, so a refused Ask searches with the hashed embedder instead."""
    m = resolve(meter)
    now = _now()
    try:
        _embed_check(classify(provider, model, "house"), now)
    except UsageLimit as exc:
        _refuse_quietly(m, exc, surface="embed")
        raise


def embed_before(meter: Meter | None, *, provider: str = "", model: str = "") -> EmbedTicket:
    """One embeddings request: L13 only (a refusal raises UsageLimit and nothing is sent). Not an AI request in the
    allowance sense: it never counts in L1/L2, L3's cap or a run's cap (L4), only in its own column and in money."""
    m = resolve(meter)
    now = _now()
    pool = classify(provider, model, "house")
    try:
        _embed_check(pool, now)
    except UsageLimit as exc:
        _refuse_quietly(m, exc, surface="embed")
        raise
    return EmbedTicket(meter=m, provider=(provider or "").lower(), model=model or "", pool=pool, t0=now,
                       day_start=day_start(now), month_start=month_start(now))


def embed_after(ticket: EmbedTicket | None, *, http_status: int = 0, usage: Any = None, error: str = "") -> None:
    """Record one embeddings request into its aggregated row and the counters. Never raises."""
    if ticket is None or ticket.done:
        return
    ticket.done = True
    try:
        _embed_after(ticket, int(http_status or 0), usage, str(error or ""))
    except Exception as exc:
        print(f"usage: embedding not recorded: {type(exc).__name__}: {str(exc)[:160]}")


def _embed_after(t: EmbedTicket, http_status: int, usage: Any, err: str) -> None:
    if _store is None:
        return
    m, now = t.meter, _now()
    status = "error" if (err or http_status >= 400) else "ok"
    tin = _tokens(usage)[0]
    cost = _cost(usage)
    D, M = t.day_start, t.month_start
    ok = _okey(m)
    did = int(m.desk_id or 0)
    dk = f"{ok}:d{did}" if m.scope == "owner" else None
    rows: list[tuple] = [(sk, "req:embed", "D", D, 1, None) for sk in (ok, dk, "house") if sk]
    cm = _micro(cost)
    if t.pool == "paid" and cm:
        l3 = limit_value(m.owner_id, "paid_usd_month") if m.scope == "owner" else None
        l13 = limit_value(0, "house_paid_usd_month")
        rows += [(sk, "usd:paid", "D", D, cm, None) for sk in (ok, dk, "house") if sk]
        if ok:
            rows.append((ok, "usd:paid", "M", M, cm, _micro(l3) if l3 is not None else None))
        rows.append(("house", "usd:paid", "M", M, cm, _micro(l13) if l13 is not None else None))
        if t.provider == "openrouter":
            with _mem_lock:                         # on top of the provider's usage_monthly until the next poll
                if _pool["since_month"] != M:
                    _pool["since_usd"], _pool["since_month"] = 0.0, M
                _pool["since_usd"] += cost
    if m.lane == "you":                             # an Ask: one row under the Ask's own action id
        key: tuple = ("act", m.action_id, status)
        action_id = m.action_id
    else:                                           # the background indexer: one row per desk per UTC minute
        minute = int(now // 60) * 60
        key = ("min", int(m.owner_id or 0), did, m.scope, minute, status)
        action_id = f"embed-d{did}-{minute}"
    note = err.split(" [", 1)[0][:200] if err else ""
    with _embed_lock:
        hit = _embed_rows.get(key)
        with _tx() as c:
            rid = None
            if hit:
                sets = "units = units + 1, tokens_in = tokens_in + ?, ts_end = ?, http_status = ?"
                args: list[Any] = [tin, now, http_status]
                if cost is not None:
                    sets += ", cost_usd = COALESCE(cost_usd, 0) + ?, cost_basis = 'provider'"
                    args.append(float(cost))
                if note:
                    sets += ", note = ?"
                    args.append(note)
                cur = c.execute(f"UPDATE usage_events SET {sets} WHERE id = ?", (*args, hit[0]))
                rid = hit[0] if cur.rowcount == 1 else None
            if not rid:
                rid = _insert_event(c, {**_meter_fields(m), "surface": "embed", "action_id": action_id, "ts": now,
                                        "ts_end": now, "kind": "request", "units": 1, "key_kind": "house",
                                        "provider": t.provider[:40], "model": t.model[:120], "pool": t.pool,
                                        "status": status, "http_status": http_status, "tokens_in": tin,
                                        "cost_usd": cost, "cost_basis": "provider" if cost is not None else "",
                                        "note": note})
            for sk, metric, period, ws, n, lim in rows:
                if sk and n:
                    c.execute(_UPSERT, (sk, metric, period, int(ws), int(n), lim, now))
        _embed_rows[key] = (int(rid), now)
        if len(_embed_rows) > 256:                  # minutes and Asks that are over
            for k in [k for k, (_rid, ts) in _embed_rows.items() if now - ts > 180]:
                _embed_rows.pop(k, None)


# ---------------------------------------------------------------------------- Hermes calls in flight (spec §1.8)
HERMES_SELF_CHECK = "Hermes did not call the metering proxy; check ~/.hermes/config.yaml"


def hermes_begin(meter: Meter | None, session_key: str = "") -> str:
    """Register one Atlas->Hermes call before it is sent (USAGE_HERMES_METER=proxy). The registry holds the call's
    meter - owner, desk, run, lane and run cap - so the metering proxy can charge Hermes's inner model requests to it.
    Returns the token hermes_end() takes."""
    token = secrets.token_hex(8)
    with _mem_lock:
        _hermes[token] = {"meter": resolve(meter), "session_key": str(session_key or "")[:120], "t0": _now(), "inner": 0}
    return token


def hermes_end(token: str, *, answered: bool = True) -> int:
    """The Hermes call returned (or failed): forget it, and return how many inner requests the proxy saw for it. In
    proxy mode a Hermes answer needs at least one model call, so an answer with none seen means Hermes bypassed the
    proxy: logged at most every 10 minutes and kept for the operator view (hermes_warning()). A call that failed
    proves nothing either way."""
    with _mem_lock:
        h = _hermes.pop(str(token or ""), None)
    if not h:
        return 0
    inner = int(h.get("inner") or 0)
    if answered and CFG.hermes_meter == "proxy" and not inner:
        now = _now()
        with _mem_lock:
            log = not _hermes_warn.get("logged") or now - _hermes_warn["logged"] > 600
            _hermes_warn.update(ts=now, message=HERMES_SELF_CHECK, **({"logged": now} if log else {}))
        if log:
            print("usage: " + HERMES_SELF_CHECK)
    return inner


def hermes_warning() -> dict[str, Any]:
    """The last self-check warning ({ts, message}), or {} when there was none."""
    with _mem_lock:
        return {"ts": _hermes_warn["ts"], "message": _hermes_warn["message"]} if _hermes_warn.get("ts") else {}
