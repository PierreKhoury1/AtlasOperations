"""The Hermes metering proxy (usage spec §1.8): the Hermes runtime's own model requests come here, not to OpenRouter.

  POST /internal/llm/v1/chat/completions   metered; streamed or not; OpenRouter's answer is returned unchanged
  GET  /internal/llm/v1/models             passed through, unmetered

Hermes's ~/.hermes/.env holds USAGE_HERMES_PROXY_TOKEN in place of a model key, and its config.yaml points
model.base_url at http://127.0.0.1:8094/internal/llm/v1. The portal adds Atlas's OpenRouter key when it forwards, so
that key never sits in the Hermes process (which runs shell tools), and a Hermes model call that bypasses the proxy
fails loudly with a 401 instead of spending quietly.

Access: Hermes on this machine only. A loopback peer, `Authorization: Bearer <token>` compared in constant time, and no
X-Forwarded-For / Forwarded header (Caddy adds those to everything it proxies, and its Caddyfile answers /internal/*
with 404 as well). Anything else gets a plain 404, as if the route did not exist. The app's _guard does not cover
/internal, so these checks are the route's own. With a valid token the proxy still answers 403 unless
USAGE_HERMES_METER=proxy: in `off` Atlas does not use its Hermes instance, in `unmetered` Hermes runs on its own key.

Per request: one of USAGE_PROXY_MAX_INFLIGHT slots (none free: 429, Retry-After 5); usage.hermes_claim() attributes
the request to the Atlas->Hermes call in flight; usage.before() checks and takes the limits (a refusal is an
OpenAI-style 429 with Retry-After, answered at once: a proxied request never waits, so it never holds a portal thread);
the request goes to OpenRouter with the house key; usage.after() records the usage and cost OpenRouter reported (in the
last SSE chunk when streaming). A limit that refused a call's inner request is kept on the call (usage.hermes_refused),
so the delegation ends at that limit in the run.

The calls in flight and the slots live in this process: the portal runs as one gunicorn worker with threads
(deploy/hetzner/setup.sh, -w 1 --threads 32), so the call Atlas registered and the inner requests Hermes sends meet.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import threading
import time
from typing import Any, Callable

import httpx
from flask import Blueprint, Response, abort, request

from .. import config as cfg
from .. import usage as U

bp = Blueprint("hermes_proxy", __name__)
PREFIX = "/internal/llm/v1"
OPENROUTER = "https://openrouter.ai/api/v1"
READ_TIMEOUT_S = 600.0          # between bytes from OpenRouter (a long non-streamed answer arrives all at once)
CALL_MAX_S = 900.0              # one streamed request end to end; Atlas gives the whole Hermes call 900 s too
BUSY_RETRY_S = 5
TRANSPORT: httpx.BaseTransport | None = None     # tests: a stub transport for OpenRouter
_FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host", "X-Forwarded-Proto")
_SCAN_MAX = 1 << 20             # an SSE line longer than this is no usage chunk: left out of the scan, still passed on
_busy_log = [0.0]
_token_log = [0.0]


class _Once:
    """Run `fn` once, from whichever path gets there first (the stream's end, the response's close, an error)."""

    def __init__(self, fn: Callable[[], None]):
        self._fn, self._lock, self._done = fn, threading.Lock(), False

    def __call__(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._fn()


def _error(status: int, message: str, *, headers: dict[str, str] | None = None, **extra: Any) -> Response:
    """An OpenAI-style error, the shape Hermes's model client reads."""
    return Response(json.dumps({"error": {"message": message, "code": status, **extra}}), status,
                    mimetype="application/json", headers=headers or {})


def _limited(exc: U.UsageLimit) -> Response:
    """A usage limit refused the request; nothing was sent (spec §1.8): 429 with the limit's message and numbers,
    and Retry-After when something resets it."""
    b = exc.body()
    meta = {k: b.get(k) for k in ("limit_id", "label", "used", "limit", "unit", "resets_at", "retry_at")}
    return _error(429, exc.message, headers=exc.headers(), type="usage_limit", metadata=meta)


def _busy() -> Response:
    t = time.monotonic()
    if t - _busy_log[0] > 60:
        _busy_log[0] = t
        print(f"usage: Hermes metering proxy busy ({U.CFG.proxy_max_inflight} requests in flight): answered 429")
    return _error(429, f"Atlas's metering proxy has {U.CFG.proxy_max_inflight} model requests in flight, its limit. "
                       f"Try again in {BUSY_RETRY_S} s.", headers={"Retry-After": str(BUSY_RETRY_S)}, type="proxy_busy")


def _peer_ok(addr: str | None) -> bool:
    """The TCP peer is this machine (Hermes calls 127.0.0.1:8094 directly)."""
    try:
        ip = ipaddress.ip_address(str(addr or "").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _token_ok() -> bool:
    if not U.proxy_token_ok():                       # unset or too short: the proxy refuses everything
        return False
    scheme, _, given = request.headers.get("Authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), U.CFG.hermes_proxy_token.encode())


def _gate() -> Response | None:
    """None: Hermes on this machine, with the token, in proxy mode. Otherwise the answer (404 aborts)."""
    if any(h in request.headers for h in _FORWARDED) or not _peer_ok(request.remote_addr):
        abort(404)
    if not _token_ok():
        t = time.monotonic()
        if U.CFG.hermes_meter == "proxy" and t - _token_log[0] > 60:     # a local caller: most likely Hermes itself
            _token_log[0] = t
            print("usage: the Hermes metering proxy refused a local request without the right token: "
                  "~/.hermes/.env OPENROUTER_API_KEY must equal USAGE_HERMES_PROXY_TOKEN (16+ characters)")
        abort(404)
    mode = U.CFG.hermes_meter
    if mode != "proxy":
        return _error(403, f"The metering proxy is not in use on this server (USAGE_HERMES_METER={mode}): it sends no "
                           "model request.", type="proxy_off")
    return None


def _upstream() -> tuple[str, str]:
    """The house OpenRouter entry the portal itself runs on: its base URL and Atlas's key (never written here)."""
    try:
        conf = cfg.load("providers", cfg.DEFAULT_PROVIDERS) if (cfg.CONFIG_DIR / "providers.json").exists() \
            else cfg.DEFAULT_PROVIDERS
        base = str((conf["providers"].get("openrouter") or {}).get("base_url") or OPENROUTER)
    except Exception:
        base = OPENROUTER
    return base.rstrip("/"), U.house_openrouter_key()


def _error_text(status: int, body: Any) -> str:
    """The ledger's error line for an upstream answer ('' = a good one). OpenRouter can answer 200 with an error and
    no choices; its daily free-model quota says free-models-per-day either way, and usage.after() closes the pool."""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    if status >= 400:
        return f"HTTP {status}" + (f": {str(msg)[:300]}" if msg else "")
    if isinstance(body, dict) and not body.get("choices") and msg:
        return f"provider returned no choices: {str(msg)[:200]}"
    return ""


def _scan(seen: dict[str, Any], chunk: bytes) -> None:
    """Pick the usage (OpenRouter sends it in the last SSE chunk) and any error chunk out of the bytes passing by. The
    first bytes are kept too, for an answer that came back as one JSON body instead of a stream."""
    if not seen["sse"] and len(seen["head"]) < _SCAN_MAX:
        seen["head"] += chunk[:_SCAN_MAX - len(seen["head"])]
    lines = (seen["buf"] + chunk).split(b"\n")
    tail = lines.pop()
    seen["buf"] = tail if len(tail) <= _SCAN_MAX else b""
    for line in lines:
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        seen["sse"] = True
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("usage"), dict) and obj["usage"]:
            seen["usage"] = obj["usage"]
        if obj.get("error"):
            err = obj["error"]
            seen["error"] = "stream error: " + str((err.get("message") if isinstance(err, dict) else err) or "")[:200]


def _settle(seen: dict[str, Any], status: int) -> None:
    """An answer to a streamed request that was no stream (one JSON body, e.g. an error in a 200): read its usage and
    error the way a plain answer is read, so the daily-quota text still closes the pool."""
    if seen["sse"] or not seen["head"].strip():
        return
    try:
        body = json.loads(seen["head"])
    except ValueError:
        return
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        seen["usage"] = seen["usage"] or body["usage"]
    seen["error"] = seen["error"] or _error_text(status, body)


@bp.post(PREFIX + "/chat/completions")
def chat_completions():
    refused = _gate()
    if refused is not None:
        return refused
    if not U.proxy_enter():
        return _busy()
    release = _Once(U.proxy_exit)
    try:
        return _forward(release)
    except BaseException:
        release()
        raise


def _forward(release: _Once) -> Response:
    """One inner request: attribute, check, forward, record. Frees its slot on return, or when its stream closes."""
    streaming = False
    try:
        raw = request.get_data(cache=False)
        try:
            payload = json.loads(raw or b"null")
        except ValueError:
            return _error(400, "The request body is not JSON.")
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or not model.strip():
            return _error(400, "model is required.")
        model, stream = model.strip(), bool(payload.get("stream"))
        claim = U.hermes_claim(request.headers.get("X-Hermes-Session-Key", ""), model=model, stream=stream,
                               headers=list(request.headers.keys()))
        base, key = _upstream()
        if not key:
            return _error(503, "Atlas has no OpenRouter key to forward with (OPENROUTER_API_KEY).")
        try:
            with U.step_surface(claim.surface):
                ticket = U.before(claim.meter, provider="openrouter", model=model, key_kind="house")
        except U.UsageLimit as exc:
            U.hermes_refused(claim, exc)
            return _limited(exc)
        U.hermes_counted(claim)
        meta = {"attribution": claim.basis} if claim.basis else None
        client = httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT_S, connect=15.0), transport=TRANSPORT)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Accept": "text/event-stream" if stream else "application/json"}
        try:
            r = client.send(client.build_request("POST", base + "/chat/completions", content=raw, headers=headers),
                            stream=True)
        except Exception as exc:
            client.close()
            U.after(ticket, error=f"{type(exc).__name__}: {str(exc)[:160]}", meta=meta)
            return _error(502, "OpenRouter could not be reached from Atlas.")
        if stream and r.status_code < 400:
            streaming = True
            return _stream_back(client, r, ticket, claim, meta, release)
        try:
            r.read()
            try:
                body = r.json()
            except ValueError:
                body = None
            usage = body.get("usage") if isinstance(body, dict) else None
            U.after(ticket, http_status=r.status_code, usage=usage, error=_error_text(r.status_code, body), meta=meta)
            if ticket.limit is not None:                   # the provider's daily quota: the call stops at it
                U.hermes_refused(claim, ticket.limit)
            return Response(r.content, r.status_code, content_type=r.headers.get("content-type") or "application/json")
        except Exception as exc:
            U.after(ticket, http_status=r.status_code, error=f"{type(exc).__name__}: {str(exc)[:160]}", meta=meta)
            return _error(502, "OpenRouter's answer could not be read.")
        finally:
            r.close()
            client.close()
    finally:
        if not streaming:
            release()


def _stream_back(client: httpx.Client, r: httpx.Response, ticket: U.Ticket, claim: U.HermesClaim,
                 meta: dict[str, Any] | None, release: _Once) -> Response:
    """Pass OpenRouter's SSE stream through byte for byte, watching it for the usage and an error chunk. When the
    stream ends, breaks or the client goes away: one ledger row, and the slot is free."""
    seen: dict[str, Any] = {"usage": None, "error": "", "buf": b"", "head": b"", "sse": False, "started": False}
    t0 = time.monotonic()

    def finish() -> None:
        for close in (r.close, client.close):
            try:
                close()
            except Exception:
                pass
        if not seen["started"]:                            # the client went away before the first byte
            seen["error"] = seen["error"] or "the client closed the stream before it started"
        _settle(seen, r.status_code)
        U.after(ticket, http_status=r.status_code, usage=seen["usage"], error=seen["error"], meta=meta)
        if ticket.limit is not None:
            U.hermes_refused(claim, ticket.limit)
        release()
    done = _Once(finish)

    def body():
        seen["started"] = True
        try:
            for chunk in r.iter_bytes():
                _scan(seen, chunk)
                yield chunk
                if time.monotonic() - t0 > CALL_MAX_S:
                    seen["error"] = seen["error"] or f"HTTP 504: proxied call exceeded {int(CALL_MAX_S)} s"
                    break
            _scan(seen, b"\n")                             # a last line sent without its newline
        except GeneratorExit:                              # Hermes went away mid-stream: still one request, sent
            if not seen["usage"]:                          # (after the usage chunk it had the whole answer)
                seen["error"] = seen["error"] or "the client closed the stream"
            raise
        except Exception as exc:                           # OpenRouter dropped the stream
            seen["error"] = seen["error"] or f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            done()

    resp = Response(body(), r.status_code, content_type=r.headers.get("content-type") or "text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.call_on_close(done)                               # a stream that never started still frees its slot
    return resp


@bp.get(PREFIX + "/models")
def models():
    """The model list, passed through unmetered: no model is called."""
    refused = _gate()
    if refused is not None:
        return refused
    if not U.proxy_enter():
        return _busy()
    try:
        base, key = _upstream()
        qs = request.query_string.decode("latin-1")
        with httpx.Client(timeout=30.0, transport=TRANSPORT) as c:
            r = c.get(base + "/models" + (f"?{qs}" if qs else ""),
                      headers={"Authorization": f"Bearer {key}"} if key else {})
        return Response(r.content, r.status_code, content_type=r.headers.get("content-type") or "application/json")
    except Exception:
        return _error(502, "OpenRouter could not be reached from Atlas.")
    finally:
        U.proxy_exit()
