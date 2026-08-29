"""Tool definitions (provider-neutral JSON schema) and implementations.

Tools that need orchestration context (delegate, list_agents, finish) are bound by the orchestrator;
the rest operate on the run's workspace directory.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Callable

from .config import INPUTS_DIR

SCHEMAS: dict[str, dict[str, Any]] = {
    "delegate": {
        "name": "delegate",
        "description": "Hand a focused sub-task to a specialist agent. The specialist cannot see this conversation, so include every fact, constraint and prior output it needs in `task`/`context`. Returns the specialist's full reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "ID of the agent (see list_agents)."},
                "task": {"type": "string", "description": "What the agent must produce."},
                "context": {"type": "string", "description": "Background, prior outputs, client facts, constraints."},
            },
            "required": ["agent_id", "task"],
        },
    },
    "list_agents": {
        "name": "list_agents",
        "description": "List the specialist agents available for delegation with their roles.",
        "parameters": {"type": "object", "properties": {}},
    },
    "save_deliverable": {
        "name": "save_deliverable",
        "description": "Save a client-ready deliverable to the run folder (markdown). Overwrites if the name exists.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "e.g. proposal.md"},
                "content": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "Read a text file from workspace/inputs (owner-supplied material) or the current run folder.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative file name/path."}},
            "required": ["path"],
        },
    },
    "list_files": {
        "name": "list_files",
        "description": "List files in workspace/inputs and the current run folder.",
        "parameters": {"type": "object", "properties": {}},
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": "Fetch a public web page (GET) and return its text content, truncated.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"},
                           "max_chars": {"type": "integer", "description": "Default 12000."}},
            "required": ["url"],
        },
    },
    "queue_action": {
        "name": "queue_action",
        "description": "Queue an outbound or sensitive action (email, whatsapp, publish, refund, contract) for HUMAN APPROVAL. Nothing is sent by this call. Returns the queue id.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["email", "whatsapp", "sms", "publish", "refund", "contract", "other"]},
                "to": {"type": "string", "description": "Recipient (email/phone/handle) or target."},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "reason": {"type": "string", "description": "Why this needs approval / what the owner should check."},
            },
            "required": ["kind", "to", "body"],
        },
    },
    "crm_lookup": {
        "name": "crm_lookup",
        "description": "Search CRM contacts by name, company or email. Returns matching contacts with stage and notes.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "crm_update": {
        "name": "crm_update",
        "description": "Create or update a CRM contact. `contact` = email (preferred) or name. Fields may include name, company, email, phone, stage (New|Contacted|Qualified|Proposal|Won|Lost), notes, next_action.",
        "parameters": {
            "type": "object",
            "properties": {"contact": {"type": "string"}, "fields": {"type": "object"}},
            "required": ["contact", "fields"],
        },
    },
    "finish": {
        "name": "finish",
        "description": "Signal the task is complete. Provide the final summary for the owner.",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
}

ALL_TOOL_NAMES = list(SCHEMAS.keys())
ORCHESTRATOR_ONLY = {"delegate", "list_agents", "finish", "queue_action", "crm_lookup", "crm_update"}


def schema_for(names: list[str]) -> list[dict[str, Any]]:
    return [SCHEMAS[n] for n in names if n in SCHEMAS]


def _safe_join(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if base.resolve() not in p.parents and p != base.resolve():
        raise ValueError("path escapes workspace")
    return p


class WorkspaceTools:
    """File + web tools bound to one run directory."""

    def __init__(self, run_dir: Path, on_deliverable: Callable[[Path], None] | None = None):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.on_deliverable = on_deliverable

    def save_deliverable(self, filename: str, content: str) -> str:
        name = re.sub(r"[^\w.\- ]+", "_", filename).strip() or "deliverable.md"
        if "." not in name:
            name += ".md"
        p = _safe_join(self.run_dir, name)
        p.write_text(content, encoding="utf-8")
        if self.on_deliverable:
            self.on_deliverable(p)
        return f"saved {p.name} ({len(content)} chars)"

    def read_file(self, path: str) -> str:
        for base in (self.run_dir, INPUTS_DIR):
            try:
                p = _safe_join(base, path)
            except ValueError:
                continue
            if p.is_file():
                data = p.read_text(encoding="utf-8", errors="replace")
                return data[:60000] + ("\n...[truncated]" if len(data) > 60000 else "")
        return f"not found: {path}"

    def list_files(self) -> str:
        lines = []
        for label, base in (("inputs", INPUTS_DIR), ("run", self.run_dir)):
            files = sorted(x for x in base.rglob("*") if x.is_file())
            lines.append(f"[{label}] {base}")
            lines += [f"  {x.relative_to(base)}  ({x.stat().st_size} B)" for x in files] or ["  (empty)"]
        return "\n".join(lines)

    def web_fetch(self, url: str, max_chars: int = 12000) -> str:
        import httpx
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 12000
        if max_chars <= 0:
            max_chars = 12000
        if not re.match(r"^https?://", url):
            return "error: only http(s) URLs"
        try:
            r = httpx.get(url, follow_redirects=True, timeout=30,
                          headers={"User-Agent": "Hermes/0.1 (+desktop research agent)"})
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}"
        ctype = r.headers.get("content-type", "")
        text = r.text
        if "html" in ctype:
            text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text)
        text = text.strip()
        return f"HTTP {r.status_code} {url}\n\n" + text[:max_chars] + ("\n...[truncated]" if len(text) > max_chars else "")

    _ALIASES = {"content": ("markdown", "text", "body", "data"), "filename": ("name", "file", "path_name"),
                "path": ("file", "filename", "name"), "url": ("link", "href")}

    def call(self, name: str, args: dict[str, Any]) -> str:
        """Dispatch with lenient kwargs: small models often invent near-miss argument names."""
        import inspect
        fn = getattr(self, name, None)
        if fn is None or name in ORCHESTRATOR_ONLY:
            raise ValueError(f"unknown tool {name}")
        params = inspect.signature(fn).parameters
        clean: dict[str, Any] = {}
        for k, v in (args or {}).items():
            if k in params:
                clean[k] = v
        for want, aliases in self._ALIASES.items():
            if want in params and want not in clean:
                for a in aliases:
                    if a in (args or {}):
                        clean[want] = args[a]
                        break
        missing = [k for k, p in params.items() if p.default is inspect._empty and k not in clean]
        if missing:
            raise ValueError(f"{name}: missing argument(s) {', '.join(missing)}; got {sorted(args or {})}")
        return fn(**clean)
