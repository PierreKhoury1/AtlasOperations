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
                           "max_chars": {"type": "integer", "description": "Default 6000 (max 12000). Fetch at most 2-3 pages per task."}},
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
    "list_connectors": {
        "name": "list_connectors",
        "description": "List the external systems this desk is connected to (email sending, inbox, HTTP APIs, webhooks) and whether writes need approval.",
        "parameters": {"type": "object", "properties": {}},
    },
    "http_request": {
        "name": "http_request",
        "description": "Call an external HTTP API through a configured connector (see list_connectors). GET runs immediately and returns the response. POST/PUT/PATCH/DELETE run immediately only if the connector allows writes without approval; otherwise the call is queued for the owner to approve.",
        "parameters": {
            "type": "object",
            "properties": {
                "connector": {"type": "string", "description": "Connector name."},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": "Path relative to the connector's base_url, e.g. /v1/customers"},
                "params": {"type": "object", "description": "Query parameters."},
                "body": {"type": "object", "description": "JSON body for write methods."},
                "reason": {"type": "string", "description": "Why this call is needed (shown to the owner if approval is required)."},
            },
            "required": ["connector", "method", "path"],
        },
    },
    "calendar_free_slots": {
        "name": "calendar_free_slots",
        "description": "List free appointment slots on the connected Google Calendar between two dates (working hours, weekdays). Read-only, runs immediately. Returns ISO start times.",
        "parameters": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "YYYY-MM-DD (or ISO datetime)."},
                "to_date": {"type": "string", "description": "YYYY-MM-DD (or ISO datetime)."},
                "duration_minutes": {"type": "integer", "description": "Slot length, default 30."},
            },
            "required": ["from_date", "to_date"],
        },
    },
    "calendar_book": {
        "name": "calendar_book",
        "description": "Book an appointment on the connected Google Calendar. Goes to the owner's approval queue unless the calendar connector allows writes; the attendee gets a Google invite when it is created.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO datetime, e.g. 2026-09-03T10:30"},
                "end": {"type": "string", "description": "ISO datetime; default start + 30 min."},
                "attendee_email": {"type": "string"},
                "description": {"type": "string"},
                "reason": {"type": "string", "description": "Why this booking / what the owner should check."},
            },
            "required": ["title", "start"],
        },
    },
    "schedule_task": {
        "name": "schedule_task",
        "description": "Schedule work for later: a one-off task in N minutes (e.g. a follow-up check) or a recurring task every N minutes. The task text is handed to Atlas when it fires.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task": {"type": "string", "description": "Full instructions, self-contained (include names, emails, context)."},
                "in_minutes": {"type": "integer", "description": "Run once after this many minutes."},
                "every_minutes": {"type": "integer", "description": "Repeat every N minutes (omit for one-off)."},
            },
            "required": ["name", "task"],
        },
    },
    "run_python": {
        "name": "run_python",
        "description": "Run a short Python 3 script (60s limit) in the run folder. Use for calculations, data transforms, parsing CSV/JSON, generating tables. stdout is returned. Files in the run folder are readable/writable; network is available.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    },
    "generate_media": {
        "name": "generate_media",
        "description": "Generate a cinematic video or a Soul image with the desk's Higgsfield connector (ads, social clips, hero visuals, product shots). Spends the client's credits, so it is queued for owner approval unless the connector allows it automatically. Returns the output URL(s) when done.",
        "parameters": {
            "type": "object",
            "properties": {
                "media": {"type": "string", "enum": ["image", "video", "dop"], "description": "image = Soul still; video = text-to-video (kling/veo/seedance per connector); dop = camera-motion video from an existing image_url"},
                "prompt": {"type": "string", "description": "Cinematic, specific: subject, setting, lighting, camera, mood, brand cues."},
                "image_url": {"type": "string", "description": "Source image for dop / image-to-video."},
                "duration": {"type": "integer", "description": "Seconds (video): 5 or 10 for kling; 4/6/8 for veo."},
                "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
                "purpose": {"type": "string", "description": "What this asset is for (shown to the owner at approval)."},
            },
            "required": ["media", "prompt"],
        },
    },
    "camera_look": {
        "name": "camera_look",
        "description": "Look through one of the desk's cameras RIGHT NOW: grabs a live frame, lists the objects recognised (people, vehicles...) with counts, and — if you pass a question — has the vision analyst answer it from the frame. The annotated snapshot is saved as a deliverable. Read-only, runs immediately.",
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Camera connector name (see the cameras list in your context). Blank = the first camera."},
                "question": {"type": "string", "description": "Optional question about the scene, e.g. 'Is the shutter closed? How many customers are queueing?'"},
            },
        },
    },
    "camera_events": {
        "name": "camera_events",
        "description": "Search what the cameras and sensors have seen: the event log (time, camera, objects counted, motion, alert reason, analyst's answer). Use it to answer 'what happened at the door last night', 'when was the last delivery', 'how busy were we between 12 and 2'. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Restrict to one camera (blank = all)."},
                "hours": {"type": "number", "description": "How far back to look, in hours (default 24)."},
                "query": {"type": "string", "description": "Keyword filter over the log (e.g. 'person', 'truck', 'after hours')."},
                "alerts_only": {"type": "boolean", "description": "Only events that woke the desk (default false)."},
                "limit": {"type": "integer", "description": "Max rows (default 40)."},
            },
        },
    },
    "remember": {
        "name": "remember",
        "description": "Store a durable fact about this business/desk for future runs (client preferences, decisions, recurring facts). Keys are short slugs; re-using a key overwrites.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]},
    },
    "recall": {
        "name": "recall",
        "description": "Search desk memory (facts stored with remember) by keyword. Empty query returns the most recent facts.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
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
ORCHESTRATOR_ONLY = {"delegate", "list_agents", "finish", "queue_action", "crm_lookup", "crm_update",
                     "list_connectors", "http_request", "schedule_task", "remember", "recall",
                     "calendar_free_slots", "calendar_book", "generate_media", "camera_look", "camera_events"}


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

    def web_fetch(self, url: str, max_chars: int = 6000) -> str:
        import httpx
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 12000
        if max_chars <= 0:
            max_chars = 6000
        max_chars = min(max_chars, 12000)
        if not re.match(r"^https?://", url):
            return "error: only http(s) URLs"
        from . import secure as SEC
        reason = SEC.private_url_reason(url)
        if reason:
            return f"error: refused - {reason}. Agent fetches may only target public addresses."
        try:
            r = httpx.get(url, follow_redirects=True, timeout=30,
                          headers={"User-Agent": "Atlas/0.1 (+desktop research agent)"})
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

    def run_python(self, code: str) -> str:
        import subprocess
        import sys as _sys
        code = str(code or "")
        if not code.strip():
            return "error: empty code"
        run_dir = self.run_dir.resolve()
        script = run_dir / f"_snippet_{int(__import__('time').time() * 1000)}.py"
        script.write_text(code, encoding="utf-8")
        try:
            r = subprocess.run([_sys.executable, "-I", str(script)], cwd=str(run_dir), capture_output=True,
                               text=True, timeout=60, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return "error: script exceeded 60s"
        finally:
            try:
                script.unlink()
            except Exception:
                pass
        out = (r.stdout or "")[-8000:]
        err = (r.stderr or "")[-3000:]
        return (out + (("\n[stderr]\n" + err) if err else "") + (f"\n[exit {r.returncode}]" if r.returncode else "")).strip() or "(no output)"

    _ALIASES = {"content": ("markdown", "text", "body", "data", "code"), "filename": ("name", "file", "path_name"),
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
