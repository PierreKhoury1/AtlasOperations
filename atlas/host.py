"""Hosted sessions: Claude Code on the operator's machine IS the desk's lead.

The house agent (app.house_bridge_owner's connected runner) can answer two kinds of job. A *model call* (the bridge
as before: one `claude -p` per request, the desk's own loop in charge) and a *hosted turn*: the whole task goes to
one long-lived Claude Code session per desk (`--resume <session id>`), and Claude Code's own agent loop drives the
work. The desk's tools (records, cases, approvals, cameras, messages, files) reach it as MCP tools from this server
(`POST /mcp`, a per-run bearer token), so policy, approvals, the audit log and usage metering are unchanged. Claude
Code's built-in tools are off (`--tools ""`): nothing on the operator's machine is read or run, no sandbox needed.

One desk = one session = one folder on the operator's machine (`~/.atlas-host/<desk id>/`): the session remembers
every earlier turn. "Sync session" in the desk is the whole setup on the owner's side.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Callable

from . import tools as T
from . import usage as U
from .providers import ToolCall

PROTOCOL = "2025-06-18"
HOST_MODEL = "claude-code"                     # the ledger's model name for a hosted turn's requests
MAX_LINE = 400_000                             # one stream-json line (a tool result can be large)

_LIVE: dict[str, "HostRun"] = {}                # mcp token -> the run it belongs to
_LOCK = threading.Lock()


class HostRun:
    """One hosted turn in flight: the orchestrator (its tools, store, events, meter) and what the stream said."""

    def __init__(self, orch, agent: dict[str, Any], on_session: Callable[[str], None] | None = None):
        self.orch = orch
        self.agent = agent
        self.token = "ath_" + secrets.token_urlsafe(24)
        self.on_session = on_session
        self.session_id = ""
        self.texts: list[str] = []
        self.result = ""
        self.result_error = ""
        self.summary = ""                      # what `finish` was called with, if it was
        self.tool_calls = 0
        self.messages = 0
        self.limit: U.UsageLimit | None = None
        self.started = time.time()
        self.cost_usd = 0.0

    # ------------------------------------------------------------------ registry
    def register(self) -> "HostRun":
        with _LOCK:
            _LIVE[self.token] = self
        return self

    def unregister(self) -> None:
        with _LOCK:
            _LIVE.pop(self.token, None)


def live(token: str) -> HostRun | None:
    with _LOCK:
        return _LIVE.get(token or "")


def live_count() -> int:
    with _LOCK:
        return len(_LIVE)


# ---------------------------------------------------------------------- the brief Claude Code runs with
PREAMBLE = (
    "You are Atlas, the lead of this operations desk, running as a hosted session. The owner types tasks and reads "
    "what you did. The desk itself - its records, cases, approval queue, CRM, cameras, connected systems, files and "
    "team - is reached ONLY through the atlas MCP tools; nothing else exists here. Never claim a desk action happened "
    "without its tool result. Anything outbound (email, WhatsApp, SMS, a booking) goes through queue_action: the owner "
    "approves it; you never send. Work the task to the end, then call finish with a short factual summary of what was "
    "done, what is queued for approval and what the owner should decide. Keep replies short and concrete."
)


def brief(orch, agent: dict[str, Any]) -> str:
    """The session's system prompt: the desk's lead brief as the orchestrator builds it, minus the text-tool protocol
    (the tools are real MCP tools here), plus the hosted-session rules."""
    a = dict(agent)
    a["text_tools"] = False
    try:
        body = orch.system_prompt(a)
    except Exception:
        body = agent.get("system_prompt", "")
    return PREAMBLE + "\n\n" + body


def tool_names(orch, agent: dict[str, Any]) -> list[str]:
    names = [n for n in orch._agent_tools(agent, 0) if n in T.SCHEMAS]
    if "finish" not in names:
        names.append("finish")
    return names


def mcp_tools(orch, agent: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for n in tool_names(orch, agent):
        s = T.SCHEMAS[n]
        schema = dict(s.get("parameters") or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        out.append({"name": n, "description": str(s.get("description") or "")[:1000], "inputSchema": schema})
    return out


# ---------------------------------------------------------------------- MCP (JSON-RPC over one POST)
def _rpc_ok(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def mcp_handle(hr: HostRun, body: Any) -> dict[str, Any] | None:
    """One JSON-RPC message from Claude Code. None = a notification (no reply body)."""
    if not isinstance(body, dict):
        return _rpc_err(None, -32600, "invalid request")
    method = str(body.get("method") or "")
    mid = body.get("id")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    if method == "initialize":
        return _rpc_ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "atlas", "version": "1"},
                             "instructions": "The desk's tools. Outbound messages are queued for the owner's approval."})
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return _rpc_ok(mid, {})
    if method == "tools/list":
        return _rpc_ok(mid, {"tools": mcp_tools(hr.orch, hr.agent)})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        text, is_error = call_tool(hr, name, args)
        return _rpc_ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if mid is None:
        return None
    return _rpc_err(mid, -32601, f"method not found: {method}")


def call_tool(hr: HostRun, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one desk tool for the session, through the orchestrator's own gate (allow-list, usage holds, policy)."""
    orch, agent = hr.orch, hr.agent
    if name not in tool_names(orch, agent):
        return f"NOT ALLOWED: {name} is not one of this desk's tools.", True
    hr.tool_calls += 1
    preview = json.dumps(args, ensure_ascii=False)[:160]
    orch.emit("tool", agent["id"], f"{name} {preview}".strip(), inst="atlas#host")
    call = ToolCall(f"host-{hr.tool_calls}", name, args)
    try:
        (_cid, _n, out, err), = orch._execute_tools(agent, [call], 0)
    except Exception as exc:                   # Cancelled included: the session is told, the run ends with it
        return f"{type(exc).__name__}: {exc}", True
    if name == "finish" and not err:
        hr.summary = str(args.get("summary") or "")
    return str(out or ""), bool(err)


# ---------------------------------------------------------------------- the stream from the runner
def on_line(hr: HostRun, line: str) -> bool:
    """One stream-json line Claude Code printed. Returns False when the runner must stop the process (a usage limit,
    or the run was cancelled)."""
    orch = hr.orch
    line = (line or "").strip()
    if not line or len(line) > MAX_LINE:
        return True
    try:
        ev = json.loads(line)
    except ValueError:
        return True
    if not isinstance(ev, dict):
        return True
    kind = str(ev.get("type") or "")
    if orch._cancel.is_set():
        return False
    if kind == "system":
        sid = str(ev.get("session_id") or "")
        if ev.get("subtype") == "init" and sid and sid != hr.session_id:
            hr.session_id = sid
            if hr.on_session:
                try:
                    hr.on_session(sid)
                except Exception:
                    pass
        return True
    if kind == "assistant":
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        hr.messages += 1
        # one metered request per assistant message: the account's allowance and USAGE_HOUSE_AGENT_DAY (pool free:bridge)
        try:
            t = U.before(orch.meter, provider="bridge", model=HOST_MODEL, key_kind="house")
            U.after(t, http_status=200, usage=usage or None, meta={"cost_basis": "claude code session (subscription)"})
        except U.UsageLimit as exc:
            hr.limit = exc
            orch.emit("log", "system", f"hosted session stopped at a usage limit: {exc.message}")
            return False
        tin = int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
        tout = int(usage.get("output_tokens") or 0)
        with orch._usage_lock:
            orch.tokens_in += tin
            orch.tokens_out += tout
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                txt = str(block["text"])
                hr.texts.append(txt)
                orch.emit("log", hr.agent["id"], txt, inst="atlas#host")
            elif block.get("type") == "tool_use":
                pass                           # logged when it runs (call_tool), so the feed shows what really happened
        return True
    if kind == "result":
        hr.result = str(ev.get("result") or "")
        if ev.get("is_error") or str(ev.get("subtype") or "").startswith("error"):
            hr.result_error = str(ev.get("subtype") or "error")
        try:
            hr.cost_usd = float(ev.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        return True
    if kind == "rate_limit_event":
        info = ev.get("rate_limit_info") if isinstance(ev.get("rate_limit_info"), dict) else {}
        if str(info.get("status") or "allowed") != "allowed":
            orch.emit("log", "system", f"Claude Code rate limit on the host machine: {info.get('status')} ({info.get('rateLimitType')})")
        return True
    return True


# ---------------------------------------------------------------------- the driver (runs inside Orchestrator.run_external)
def drive(orch, *, task: str, desk_id: int, session_id: str, resume: bool, mcp_url: str, send, timeout: float,
          on_session: Callable[[str], None] | None = None, agent_name: str = "claude") -> str:
    """One hosted turn: queue the job for the house runner, feed every stream line to on_line, return the summary.
    `send(job, on_chunk, timeout)` is the bridge queue (app._bridge_run for the house owner)."""
    agent = orch.agents.get("atlas") or next(iter(orch.agents.values()))
    hr = HostRun(orch, agent, on_session).register()
    try:
        orch.emit("agent_start", agent["id"], f"{agent.get('name') or 'Atlas'} (hosted {agent_name} session"
                  + (", continuing" if resume else ", new") + ")", depth=0, inst="atlas#host")
        job = {"kind": "host", "desk_id": int(desk_id), "run_id": orch.run_id, "session_id": session_id, "resume": bool(resume),
               "system": brief(orch, agent), "task": task, "mcp": {"url": mcp_url, "token": hr.token, "name": "atlas"},
               "tools": tool_names(orch, agent), "messages": [], "model": ""}
        try:
            final = send(job, lambda line: on_line(hr, line), timeout) or ""
        except Exception as exc:
            if hr.limit is not None:
                raise hr.limit
            if orch._cancel.is_set():
                from .orchestrator import Cancelled
                raise Cancelled()
            orch.emit("error", agent["id"], f"hosted session: {exc}")
            return f"ERROR from atlas: {exc}"
        if hr.limit is not None:
            raise hr.limit
        if hr.result_error and not hr.summary:
            orch.emit("error", agent["id"], f"hosted session ended with {hr.result_error}: {(hr.result or final)[:300]}")
        summary = hr.summary or hr.result or final or (hr.texts[-1] if hr.texts else "")
        orch.emit("agent_end", agent["id"], f"{agent.get('name') or 'Atlas'} done - {hr.messages} message(s), {hr.tool_calls} tool call(s)"
                  + (f", session {hr.session_id[:8]}" if hr.session_id else ""), depth=0, inst="atlas#host")
        return summary or "(no output)"
    finally:
        hr.unregister()
