"""Test helpers for the designer's turn (round 2 designer spec section 14): a scripted, fault-injecting provider and an
httpx MockTransport that stands in for OpenRouter under the real OpenAI-compatible provider. No model is ever called and
nothing leaves the machine."""
from __future__ import annotations

import json
import time
import types
from typing import Any

import httpx

import atlas.designer as DS

LIVE = {"providers": {}}                      # a live session's providers_cfg when ProviderPool.get is patched
_CLIENT = httpx.Client                        # the real client, before any test wraps it


def block(d: dict[str, Any]) -> str:
    return DS.TAG_OPEN + json.dumps(d) + DS.TAG_CLOSE


class Scripted:
    """A provider that answers each chat() with the next item of `script` (the last one repeats):
    - a string: the reply, streamed through on_token in `chunk`-character pieces after the `think` reasoning delta;
    - a (string, stop_reason) pair;
    - an exception: raised (a fault);
    - a callable(system, msgs): its result is used as above.
    Every call is recorded with what it was sent, so a test can count requests and read the prompt."""

    def __init__(self, *script: Any, stop_reason: str = "stop", think: str = "", chunk: int = 6,
                 extra: dict[str, Any] | None = None, sleep: float = 0.0):
        self.script = list(script)
        self.stop_reason, self.think, self.chunk, self.extra, self.sleep = stop_reason, think, chunk, extra, sleep
        self.calls: list[dict[str, Any]] = []

    def chat(self, system, msgs, tools, model="", on_token=None):
        self.calls.append({"system": system, "msgs": list(msgs), "model": model, "on_token": on_token})
        item = self.script.pop(0) if len(self.script) > 1 else (self.script[0] if self.script else "")
        if callable(item) and not isinstance(item, BaseException):
            item = item(system, msgs)
        if self.sleep:
            time.sleep(self.sleep)                # the real sleep: tests patch only the designer's
        if isinstance(item, BaseException):
            raise item
        text, stop = item if isinstance(item, tuple) else (item, self.stop_reason)
        if on_token:
            if self.think:
                on_token(self.think, True)
            for i in range(0, len(text), self.chunk):
                on_token(text[i:i + self.chunk])
        return types.SimpleNamespace(text=text, stop_reason=stop, extra=dict(self.extra or {}), output_tokens=len(text) // 4)

    def user_message(self, t):
        return {"role": "user", "content": t}


def install(monkeypatch, provider: Any) -> Any:
    """Every ProviderPool hands out `provider`; the designer's own sleeps are skipped."""
    import atlas.providers as P
    monkeypatch.setattr(P.ProviderPool, "get", lambda self, name="": provider)
    monkeypatch.setattr(DS, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    return provider


def say(session: DS.DesignSession, text: str, on_token=None, **kw: Any) -> dict[str, Any]:
    """One live turn through DS.reply on a patched provider."""
    return DS.reply(session, text, on_token=on_token, providers_cfg=kw.pop("providers_cfg", LIVE),
                    designer_model=kw.pop("designer_model", "x/y:free"), **kw)


# ---------------------------------------------------------------------------- OpenRouter over MockTransport
OPENROUTER = {"default_provider": "openrouter",
              "providers": {"openrouter": {"type": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-test",
                                           "default_model": "vendor/model:free", "fallback_model": "vendor/model:free",
                                           "max_tokens": 4000, "timeout": 30}}}


def sse(text: str, reasoning: str = "", finish: str = "stop", usage: dict[str, Any] | None = None) -> bytes:
    """An OpenAI-style SSE body: one reasoning delta, the reply in small content deltas, then the finish and usage."""
    chunks = [{"choices": [{"delta": {"reasoning": reasoning}}]}] if reasoning else []
    chunks += [{"choices": [{"delta": {"content": text[i:i + 9]}}], "model": "vendor/model:free"} for i in range(0, len(text), 9)]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}],
                   "usage": usage or {"prompt_tokens": 100, "completion_tokens": 40,
                                      "completion_tokens_details": {"reasoning_tokens": 25}}})
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


class Router:
    """OpenRouter's /chat/completions, scripted: each request gets the next response (the last repeats), either an
    (HTTP status, error message) pair or a reply text served as SSE. Keeps every request's JSON payload."""

    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.seen: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.seen.append(payload)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, tuple):
            status, message = item
            return httpx.Response(status, json={"error": {"message": message}})
        if not payload.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"content": item}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 100, "completion_tokens": 40}, "model": payload.get("model")})
        return httpx.Response(200, content=sse(item), headers={"content-type": "text/event-stream"})


def mock_http(monkeypatch, handler: Any) -> None:
    """Every httpx.Client the providers open talks to `handler` instead of the network; the provider's 5xx back-off
    sleep is skipped."""
    import atlas.providers as P

    def client(*a: Any, **kw: Any) -> httpx.Client:
        kw["transport"] = httpx.MockTransport(handler)
        return _CLIENT(*a, **kw)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
