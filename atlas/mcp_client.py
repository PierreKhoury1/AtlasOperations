"""Minimal MCP (Model Context Protocol) client over stdio — lets a desk plug in any MCP server
(Gmail, Slack, Notion, GitHub, Postgres, filesystem, browser, ...) and exposes each server tool to
the agents as `mcp__<server>__<tool>`.

Own JSON-RPC implementation (newline-delimited over the server's stdin/stdout) so it runs inside
the synchronous orchestrator threads without an event loop. One process per connector, started
lazily, restarted if it dies.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Any

PROTOCOL = "2025-06-18"
WRITE_HINT = re.compile(r"(create|send|delete|remove|update|write|post|put|patch|insert|upload|move|rename|execute|run|set_|add_|reply|publish|pay|transfer|book|cancel)", re.I)


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_")[:24] or "srv"


class MCPServer:
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.slug = _slug(name)
        self.config = config
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict[str, Any]] = []
        self._id = 0
        self._lock = threading.Lock()
        self.last_error = ""

    # ---------------------------------------------------------------- process
    def _spawn(self) -> None:
        cmd = self.config.get("command") or ""
        if not cmd:
            raise RuntimeError("MCP connector needs a command, e.g. npx -y @modelcontextprotocol/server-filesystem C:/data")
        argv = shlex.split(cmd, posix=(os.name != "nt"))
        if os.name == "nt" and argv and argv[0].lower() in ("npx", "npm", "node") and not argv[0].lower().endswith(".cmd"):
            argv[0] = argv[0] + ".cmd" if argv[0].lower() != "node" else argv[0]
        env = dict(os.environ)
        raw = self.config.get("env")
        if isinstance(raw, str) and raw.strip():
            try:
                env.update({str(k): str(v) for k, v in json.loads(raw).items()})
            except Exception:
                for line in raw.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
        elif isinstance(raw, dict):
            env.update({str(k): str(v) for k, v in raw.items()})
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=self.config.get("cwd") or None, env=env, text=True, encoding="utf-8",
                                     bufsize=1, shell=(os.name == "nt" and argv[0].lower().endswith(".cmd")))
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._id = 0
        res = self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                           "clientInfo": {"name": "atlas-desk", "version": "0.2"}}, timeout=60)
        self._notify("notifications/initialized", {})
        self.server_info = res.get("serverInfo", {})
        self.tools = self._request("tools/list", {}, timeout=60).get("tools", [])

    def _drain_stderr(self):
        try:
            assert self.proc and self.proc.stderr
            for line in self.proc.stderr:
                self.last_error = (self.last_error + line)[-2000:]
        except Exception:
            pass

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def ensure(self) -> None:
        if not self.alive():
            self._spawn()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    # ---------------------------------------------------------------- json-rpc
    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            assert self.proc and self.proc.stdout
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    if self.proc.poll() is not None:
                        raise RuntimeError(f"MCP server exited (code {self.proc.returncode}). stderr: {self.last_error[-400:]}")
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == rid:
                    if "error" in msg:
                        e = msg["error"]
                        raise RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                    return msg.get("result", {})
                # notifications / other ids: ignore
            raise RuntimeError(f"MCP request {method} timed out after {timeout}s")

    # ---------------------------------------------------------------- tools
    def schemas(self) -> list[dict[str, Any]]:
        out = []
        for t in self.tools:
            params = t.get("inputSchema") or {"type": "object", "properties": {}}
            if params.get("type") != "object":
                params = {"type": "object", "properties": {}}
            out.append({"name": f"mcp__{self.slug}__{t['name']}",
                        "description": f"[{self.name} via MCP] " + (t.get("description") or t["name"])[:900],
                        "parameters": params})
        return out

    def call(self, tool: str, args: dict[str, Any]) -> str:
        self.ensure()
        res = self._request("tools/call", {"name": tool, "arguments": args or {}}, timeout=180)
        parts = []
        for c in res.get("content") or []:
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource" and isinstance(c.get("resource"), dict):
                parts.append(c["resource"].get("text", "") or c["resource"].get("uri", ""))
            else:
                parts.append(json.dumps(c)[:2000])
        text = "\n".join(parts).strip() or json.dumps(res)[:4000]
        if res.get("isError"):
            text = "ERROR: " + text
        return text[:12000]


class MCPRegistry:
    """One live MCPServer per connector id, shared across runs."""

    def __init__(self):
        self._servers: dict[int, MCPServer] = {}
        self._lock = threading.Lock()

    def get(self, connector: dict[str, Any]) -> MCPServer:
        with self._lock:
            s = self._servers.get(connector["id"])
            if s is None or s.config != (connector.get("config") or {}):
                if s:
                    s.stop()
                s = MCPServer(connector["name"], connector.get("config") or {})
                self._servers[connector["id"]] = s
        s.ensure()
        return s

    def drop(self, connector_id: int) -> None:
        with self._lock:
            s = self._servers.pop(connector_id, None)
        if s:
            s.stop()

    def schemas_for(self, connectors: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, tuple[dict, str]]]:
        """Returns (schemas, index) where index maps exposed tool name -> (connector, mcp tool name)."""
        schemas, index = [], {}
        for c in connectors:
            if c.get("kind") != "mcp":
                continue
            try:
                s = self.get(c)
            except Exception as exc:
                schemas.append({"name": f"mcp__{_slug(c['name'])}__unavailable",
                                "description": f"[{c['name']}] MCP server failed to start: {str(exc)[:200]}",
                                "parameters": {"type": "object", "properties": {}}})
                continue
            for sc in s.schemas():
                schemas.append(sc)
                index[sc["name"]] = (c, sc["name"].split("__", 2)[2])
        return schemas, index


def is_write(tool_name: str) -> bool:
    return bool(WRITE_HINT.search(tool_name))


REGISTRY = MCPRegistry()


def test_server(config: dict[str, Any]) -> str:
    s = MCPServer("test", config)
    try:
        s.ensure()
        names = [t["name"] for t in s.tools]
        info = s.server_info or {}
        return f"{info.get('name', 'server')} {info.get('version', '')}: {len(names)} tool(s) — {', '.join(names[:12])}{'…' if len(names) > 12 else ''}"
    finally:
        s.stop()
