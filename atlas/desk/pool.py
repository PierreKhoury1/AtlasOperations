"""The session pool: one Claude Code session per account, run by Atlas on its own box.

A person signs in; Atlas starts a runner for them (scripts/atlas_bridge.py --agent claude) as a child process with
its own home folder, bound to a bridge token of their own, so their desks find "a connected agent" without anything
installed on their computer. The runner's Claude Code talks to Anthropic through this module's metering proxy
(ANTHROPIC_BASE_URL -> /pool/<owner>/<ptoken>): the real API key stays in the portal's environment, every request is
written to the usage ledger as a paid house request with its real token cost, and the account's paid_usd_month cap
(admin page / USAGE_OWNER_PAID_USD_MONTH) refuses the request before it is sent.

Env: ANTHROPIC_API_KEY (turns the pool on; SESSION_POOL=0 turns it off anyway), POOL_MODEL (CLI --model, optional),
POOL_MAX (runners at once, 24), POOL_IDLE_S (kill after idle, 1800), POOL_PRICES (model=in:out per MTok, overrides
the built-in Anthropic table), POOL_DIR (homes, default <ATLAS_DATA_DIR>/pool).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .. import usage as U

ANTHROPIC = "https://api.anthropic.com"
ONLINE_WAIT_S = 25.0
# USD per million tokens (input, output); cache reads cost 10% of input, cache writes 125%. Matched by substring, in order.
_DEFAULT_PRICES = (("opus", 15.0, 75.0), ("sonnet", 3.0, 15.0), ("haiku", 1.0, 5.0))
_HOP = {"host", "content-length", "transfer-encoding", "connection", "authorization", "x-api-key", "accept-encoding"}


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()) and os.environ.get("SESSION_POOL", "1").strip() not in ("0", "off", "no")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def prices() -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    for k, (i, o, _d) in U.parse_price_table(os.environ.get("POOL_PRICES", "")).items():
        out.append((k.lower(), i, o))
    return out + [(m, i, o) for m, i, o in _DEFAULT_PRICES]


def price_for(model: str) -> tuple[float, float] | None:
    m = (model or "").lower()
    for key, pin, pout in prices():
        if key in m:
            return pin, pout
    return None


def ensure_priced(model: str) -> bool:
    """The ledger refuses a house Anthropic call whose model has no price (usage spec §3.4, fail closed): put the
    pool's price for it into the live price table so the call is allowed and costed."""
    if not model or model in U.CFG.price_table:
        return model in U.CFG.price_table
    p = price_for(model)
    if p is None:
        return False
    U.CFG.price_table[model] = (p[0], p[1], "pool")
    return True


def cost_of(model: str, usage: dict[str, Any]) -> float | None:
    """USD for one request from Anthropic's usage block, or None when the model is not in any price table."""
    m = (model or "").lower()
    for key, pin, pout in prices():
        if key in m:
            i = int(usage.get("input_tokens") or 0)
            o = int(usage.get("output_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            cw = int(usage.get("cache_creation_input_tokens") or 0)
            return round((i * pin + o * pout + cr * pin * 0.1 + cw * pin * 1.25) / 1e6, 6)
    return None


class Session:
    def __init__(self, owner_id: int, bridge_id: int, ptoken: str, home: Path, proc: subprocess.Popen):
        self.owner_id, self.bridge_id, self.ptoken, self.home, self.proc = owner_id, bridge_id, ptoken, home, proc
        self.started = self.last_used = time.time()
        self.requests = 0
        self.cost_usd = 0.0

    def alive(self) -> bool:
        return self.proc.poll() is None

    def public(self) -> dict[str, Any]:
        return {"running": self.alive(), "pid": self.proc.pid, "started": self.started, "last_used": self.last_used,
                "requests": self.requests, "cost_usd": round(self.cost_usd, 4)}


class Pool:
    """Spawns, finds and reaps runners. `mint` makes the bridge row + token for an owner; `online` says whether the
    owner's bridge has polled recently (both from the app, so this module needs no store of its own)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_owner: dict[int, Session] = {}
        self.mint: Callable[[int], tuple[int, str]] | None = None
        self.online: Callable[[int], bool] | None = None
        self.drop: Callable[[int, int], None] | None = None          # (bridge_id, owner_id): delete the bridge row
        self.base_url = ""                                            # where the runner reaches the desk (loopback)
        self._reaper: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap_loop, name="pool-reaper", daemon=True)
            self._reaper.start()

    def dir(self) -> Path:
        root = os.environ.get("POOL_DIR") or os.path.join(os.environ.get("ATLAS_DATA_DIR") or str(Path.home() / ".atlas"), "pool")
        return Path(root)

    def get(self, owner_id: int) -> Session | None:
        with self._lock:
            s = self._by_owner.get(int(owner_id))
            return s if s and s.alive() else None

    def touch(self, owner_id: int) -> None:
        s = self.get(owner_id)
        if s:
            s.last_used = time.time()

    def ensure(self, owner_id: int, wait: float = 0.0) -> Session | None:
        """The owner's runner, started if needed. With `wait`, block until its bridge is online (or give up)."""
        if not enabled() or not self.mint or not self.base_url:
            return None
        owner_id = int(owner_id)
        with self._lock:
            s = self._by_owner.get(owner_id)
            if s and s.alive():
                s.last_used = time.time()
            else:
                if s:
                    self._forget(s)
                if len([x for x in self._by_owner.values() if x.alive()]) >= _int("POOL_MAX", 24):
                    victim = min((x for x in self._by_owner.values() if x.alive()), key=lambda x: x.last_used, default=None)
                    if victim and time.time() - victim.last_used > 120:
                        self._kill(victim)
                    else:
                        return None
                s = self._spawn(owner_id)
                self._by_owner[owner_id] = s
        if wait and self.online:
            deadline = time.time() + wait
            while time.time() < deadline and s.alive():
                if self.online(owner_id):
                    break
                time.sleep(0.5)
        return s

    def _spawn(self, owner_id: int) -> Session:
        assert self.mint is not None
        bridge_id, token = self.mint(owner_id)
        ptoken = secrets.token_urlsafe(18)
        home = self.dir() / f"u{owner_id}"
        home.mkdir(parents=True, exist_ok=True)
        (home / ".claude").mkdir(exist_ok=True)
        runner = Path(__file__).resolve().parents[2] / "scripts" / "atlas_bridge.py"
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP") if k in os.environ}
        env.update({
            "HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "ANTHROPIC_API_KEY": "atlas-pool-" + ptoken,                       # never the real key: the proxy swaps it in
            "ANTHROPIC_BASE_URL": f"{self.base_url}/pool/{owner_id}/{ptoken}",
            "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1", "ATLAS_HOST_DIR": str(home / "host"),
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        })
        args = [sys.executable, "-I", str(runner), "--url", self.base_url, "--token", token, "--agent", "claude"]
        model = os.environ.get("POOL_MODEL", "").strip()
        if model:
            args += ["--model", model]
        log = open(home / "runner.log", "ab")
        proc = subprocess.Popen(args, cwd=str(home), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        print(f"pool: started runner for account {owner_id} (pid {proc.pid})")
        return Session(owner_id, bridge_id, ptoken, home, proc)

    def _kill(self, s: Session) -> None:
        try:
            s.proc.terminate()
            try:
                s.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                s.proc.kill()
        except Exception:
            pass
        self._forget(s)

    def _forget(self, s: Session) -> None:
        self._by_owner.pop(s.owner_id, None)
        if self.drop:
            try:
                self.drop(s.bridge_id, s.owner_id)
            except Exception:
                pass

    def stop(self, owner_id: int) -> bool:
        with self._lock:
            s = self._by_owner.get(int(owner_id))
            if not s:
                return False
            self._kill(s)
            return True

    def _reap_loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                idle = _int("POOL_IDLE_S", 1800)
                now = time.time()
                with self._lock:
                    for s in list(self._by_owner.values()):
                        if not s.alive() or now - s.last_used > idle:
                            print(f"pool: {'ended' if not s.alive() else 'idle'} runner for account {s.owner_id}")
                            self._kill(s)
            except Exception as exc:
                print("pool reaper:", exc)

    def status(self, owner_id: int) -> dict[str, Any]:
        s = self.get(owner_id)
        on = bool(self.online and self.online(owner_id)) if s else False
        return {"enabled": enabled(), "running": bool(s), "online": on, **({"session": s.public()} if s else {})}

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"owner_id": s.owner_id, **s.public()} for s in self._by_owner.values()]

    # ------------------------------------------------------------ the metering proxy
    def session_for(self, owner_id: int, ptoken: str) -> Session | None:
        s = self.get(owner_id)
        return s if s and secrets.compare_digest(s.ptoken, ptoken or "") else None

    def proxy(self, s: Session, method: str, path: str, headers: dict[str, str], body: bytes, query: str = ""):
        """One request from the runner's Claude Code to Anthropic: metered (U.before / U.after), the real key added,
        the response streamed back. Returns (status, headers, iterator of bytes). Raises U.UsageLimit before sending."""
        s.last_used = time.time()
        s.requests += 1
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        fwd = {k: v for k, v in headers.items() if k.lower() not in _HOP}
        fwd["x-api-key"] = key
        fwd.setdefault("anthropic-version", "2023-06-01")
        url = f"{ANTHROPIC}{path}" + (f"?{query}" if query else "")
        model = ""
        stream = False
        if path.endswith("/messages") and body:
            try:
                j = json.loads(body)
                model = str(j.get("model") or "")
                stream = bool(j.get("stream"))
            except ValueError:
                pass
        ticket = None
        if path.endswith("/messages"):
            ensure_priced(model)
            meter = U.Meter(owner_id=s.owner_id, desk_id=0, scope=U.desk_scope(s.owner_id), lane=U.bound_lane() or "you",
                            surface="pool.claude", key_kind="house")
            ticket = U.before(meter, provider="anthropic", model=model, key_kind="house")   # raises UsageLimit: not sent
        client = httpx.Client(timeout=httpx.Timeout(600.0, connect=20.0))
        try:
            req = client.build_request(method, url, headers=fwd, content=body)
            r = client.send(req, stream=True)
        except Exception as exc:
            client.close()
            U.after(ticket, http_status=0, error=f"{type(exc).__name__}: {exc}")
            raise
        usage: dict[str, Any] = {}

        def body_iter() -> Iterator[bytes]:
            buf = b""
            try:
                for chunk in r.iter_raw():
                    yield chunk
                    if ticket is None:
                        continue
                    if stream:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            _usage_from_sse(line, usage)
                    else:
                        buf += chunk
                if ticket is not None and not stream and buf:
                    try:
                        u = (json.loads(buf) or {}).get("usage") or {}
                        usage.update({k: int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})
                    except ValueError:
                        pass
            finally:
                try:
                    r.close()
                finally:
                    client.close()
                if ticket is not None:
                    c = cost_of(model, usage) if usage else None
                    if c is not None:
                        s.cost_usd += c
                        usage["cost"] = c
                    U.after(ticket, http_status=r.status_code, usage=usage or None,
                            error="" if r.status_code < 400 else f"HTTP {r.status_code}")

        out_headers = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding")}
        return r.status_code, out_headers, body_iter()


def _usage_from_sse(line: bytes, usage: dict[str, Any]) -> None:
    if not line.startswith(b"data:"):
        return
    try:
        ev = json.loads(line[5:].strip() or b"{}")
    except ValueError:
        return
    t = ev.get("type")
    if t == "message_start":
        u = ((ev.get("message") or {}).get("usage") or {})
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[k] = int(u.get(k) or 0)
        usage["output_tokens"] = int(u.get("output_tokens") or 0)
    elif t == "message_delta":
        u = ev.get("usage") or {}
        if u.get("output_tokens") is not None:
            usage["output_tokens"] = int(u.get("output_tokens") or 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            if u.get(k) is not None:
                usage[k] = int(u.get(k) or 0)


POOL = Pool()
