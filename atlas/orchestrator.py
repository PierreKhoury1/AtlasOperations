"""Atlas orchestration core.

Two modes:
  * auto      — the Atlas agent plans, delegates (tool `delegate`), reviews and finishes.
  * <workflow> — deterministic step pipeline from config/workflows.json, optional Atlas synthesis at the end.

Everything reports through `emit(Event)` so any UI (or CLI) can render it.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import integrations as I
from . import mcp_client as M
from . import policy as P
from . import tools as T
from .config import RUNS_DIR
from .providers import ProviderPool, ToolCall
from .store import Store


@dataclass
class Event:
    kind: str            # log | agent_start | agent_end | tool | deliverable | done | error | usage
    agent: str = "system"
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class RunResult:
    run_id: str
    status: str
    summary: str
    deliverables: list[Path]
    tokens_in: int
    tokens_out: int
    run_dir: Path


class Cancelled(Exception):
    pass


class Orchestrator:
    def __init__(self, configs: dict[str, Any], store: Store | None, emit: Callable[[Event], None]):
        self.configs = configs
        self.store = store
        self._emit = emit
        self.pool = ProviderPool(configs["providers"])
        self.business = configs["business"]
        self.agents: dict[str, dict[str, Any]] = {a["id"]: a for a in configs["agents"] if a.get("enabled", True)}
        self.workflows: dict[str, dict[str, Any]] = {w["id"]: w for w in configs["workflows"]}
        self.orch = configs.get("orchestration", {})
        self._cancel = threading.Event()
        self._usage_lock = threading.Lock()
        self.tokens_in = 0
        self.tokens_out = 0
        self.run_id = ""
        self.run_dir = RUNS_DIR
        self.deliverables: list[Path] = []
        self._ws: T.WorkspaceTools | None = None
        self._policy_hits: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, agent: str = "system", text: str = "", **data):
        # factual counters for the verified-summary footer (kept regardless of what the model claims it did)
        c = getattr(self, "_counts", None)
        if c is not None:
            if kind == "approval":
                c["queued"] += 1
            elif kind == "sent":
                c["sent"] += 1
            elif kind == "tool" and text.startswith("crm_update"):
                c["crm"] += 1
            elif kind == "tool" and text.startswith("schedule_task"):
                c["jobs"] += 1
        ev = Event(kind, agent, text, data)
        if self.store and self.run_id and kind not in ("usage", "token"):
            try:
                self.store.add_event(self.run_id, kind, agent, text)
            except Exception:
                pass
        self._emit(ev)

    def cancel(self):
        self._cancel.set()

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # ------------------------------------------------------------------ prompts
    def business_context(self) -> str:
        b = self.business
        services = "\n".join(f"- {s}" for s in b.get("services", []))
        parts = [
            f"# Business: {b.get('name','')}",
            f"Model: {b.get('model','')}  |  {b.get('tagline','')}",
            b.get("description", ""),
            f"\nServices:\n{services}" if services else "",
            f"\nTarget clients: {b.get('target_clients','')}",
            f"Tone of voice: {b.get('tone','')}",
            f"Currency: {b.get('currency','')}",
            f"Pricing notes: {b.get('pricing_notes','')}",
            f"Sign outbound messages as: {b['sender_name']}" if b.get("sender_name") else "",
            f"Availability for calls/visits: {b['availability']}" if b.get("availability") else "",
            f"Today is {time.strftime('%A %d %B %Y')}.",
        ]
        if b.get("extra_context"):
            parts.append("\nAdditional context:\n" + b["extra_context"])
        return "\n".join(p for p in parts if p is not None).strip()

    def roster_text(self, exclude: str = "atlas") -> str:
        lines = []
        for a in self.agents.values():
            if a["id"] == exclude:
                continue
            lines.append(f"- {a['id']}: {a['name']} — {a.get('role','')}")
        return "\n".join(lines) or "(no specialists configured)"

    def system_prompt(self, agent: dict[str, Any]) -> str:
        parts = [agent.get("system_prompt", "")]
        if agent["id"] == "atlas" or self.orch.get("include_business_context_in_specialists", True):
            parts.append(self.business_context())
        if "delegate" in agent.get("tools", []):
            parts.append("Specialist agents available via delegate(agent_id, ...):\n" + self.roster_text(agent["id"]))
        if "http_request" in agent.get("tools", []) and self.store is not None and hasattr(self.store, "connectors"):
            parts.append("Connected systems (use http_request / list_connectors):\n" + I.describe(self.store.connectors()))
        if "camera_look" in agent.get("tools", []) and self.store is not None and hasattr(self.store, "connectors"):
            cams = [c for c in self.store.connectors() if c["kind"] == "camera"]
            if cams:
                parts.append("Cameras (camera_look to see now, camera_events to search history):\n" + I.describe(cams))
            else:
                parts.append("No cameras connected yet (the owner adds them under Cameras).")
        if "recall" in agent.get("tools", []) and self.store is not None and hasattr(self.store, "recall"):
            mem = self.store.recall("", 15)
            if mem:
                parts.append("Desk memory (most recent facts; use recall for more, remember to add):\n" + "\n".join(f"- {m['key']}: {m['value'][:200]}" for m in mem))
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------ agent loop
    def run_agent(self, agent_id: str, task: str, context: str = "", depth: int = 0) -> str:
        agent = self.agents.get(agent_id)
        if not agent:
            if agent_id in T.SCHEMAS:
                return (f"ERROR: '{agent_id}' is a TOOL, not an agent. Call the {agent_id} tool directly with its own "
                        f"arguments. Agents you can delegate to: {', '.join(a for a in self.agents if a != 'atlas')}")
            return f"ERROR: unknown agent '{agent_id}'. Agents you can delegate to: {', '.join(a for a in self.agents if a != 'atlas')}"
        self._check()
        provider = self.pool.get(agent.get("provider") or "")
        model = agent.get("model") or provider.default_model
        tool_names = [t for t in agent.get("tools", []) if t in T.SCHEMAS]
        if depth >= int(self.orch.get("max_delegation_depth", 2)):
            tool_names = [t for t in tool_names if t != "delegate"]
        if agent_id == "atlas" and "finish" not in tool_names:
            tool_names.append("finish")
        schemas = T.schema_for(tool_names)
        self._mcp_index: dict[str, tuple[dict, str]] = getattr(self, "_mcp_index", {})
        if "mcp" in agent.get("tools", []) and self.store is not None and hasattr(self.store, "connectors"):
            mcp_schemas, idx = M.REGISTRY.schemas_for(self.store.connectors())
            schemas = schemas + mcp_schemas
            self._mcp_index.update(idx)
        max_iter = int(self.orch.get("max_iterations", 24)) if agent_id == "atlas" \
            else int(self.orch.get("specialist_max_iterations", 8))

        prompt = task if not context else f"{task}\n\n---\nContext:\n{context}"
        messages: list[Any] = [provider.user_message(prompt)]
        self.emit("agent_start", agent_id, f"{agent['name']} ← task ({len(prompt)} chars)", depth=depth, model=model)
        final_text = ""
        budget = int(self.orch.get("agent_token_budget", 60000))
        spent_in = 0

        def on_token(text: str, thinking: bool = False):
            self.emit("token", agent_id, text, thinking=thinking, depth=depth)

        for i in range(max_iter):
            self._check()
            try:
                resp = provider.chat(self.system_prompt(agent), messages, schemas, model, on_token=on_token)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.emit("error", agent_id, msg)
                return f"ERROR from {agent['name']}: {msg}"
            with self._usage_lock:
                self.tokens_in += resp.input_tokens
                self.tokens_out += resp.output_tokens
            spent_in += resp.input_tokens
            self.emit("usage", agent_id, "", tokens_in=self.tokens_in, tokens_out=self.tokens_out)
            if resp.refusal:
                self.emit("error", agent_id, resp.refusal)
                return f"{agent['name']} could not complete: {resp.refusal}"
            if not resp.text and not resp.tool_calls:
                # empty reply (free-tier models do this under load): nudge once, then give up cleanly
                if not messages or messages[-1] is not None and not getattr(self, "_nudged", {}).get(agent_id):
                    self._nudged = {**getattr(self, "_nudged", {}), agent_id: True}
                    self.emit("log", agent_id, "(empty reply from model — retrying once)")
                    messages.append(provider.user_message("Your last reply was empty. Continue the task now, using tools where needed."))
                    time.sleep(2)
                    continue
            if resp.text:
                self.emit("log", agent_id, resp.text)
                final_text = resp.text
            if not resp.tool_calls:
                break
            if spent_in > budget and agent_id != "atlas":
                self.emit("log", agent_id, f"(token budget {budget:,} reached after {i + 1} turns — wrapping up with what I have)")
                messages.append(resp.assistant_message)
                messages.extend(provider.tool_results([(c.id, c.name, "SKIPPED: token budget reached. Write your final answer now from what you already have.", True) for c in resp.tool_calls]))
                resp2 = provider.chat(self.system_prompt(agent), messages, [], model, on_token=on_token)
                with self._usage_lock:
                    self.tokens_in += resp2.input_tokens
                    self.tokens_out += resp2.output_tokens
                if resp2.text:
                    self.emit("log", agent_id, resp2.text)
                    final_text = resp2.text
                break
            messages.append(resp.assistant_message)
            results = self._execute_tools(agent, resp.tool_calls, depth)
            finished = next((r for r in results if r[1] == "finish"), None)
            messages.extend(provider.tool_results([(c, n, o, e) for c, n, o, e in results]))
            if finished:
                final_text = finished[2]
                break
        else:
            # ran out of turns mid-tool-use: never return nothing - one final no-tools call to synthesise what we have
            self.emit("log", agent_id, f"(hit max iterations {max_iter} — synthesising from what was gathered)")
            try:
                resp3 = provider.chat(self.system_prompt(agent), messages + [provider.user_message(
                    "You are out of tool turns. Write your best final answer NOW from what you already gathered. "
                    "State clearly what you could not verify.")], [], model, on_token=on_token)
                with self._usage_lock:
                    self.tokens_in += resp3.input_tokens
                    self.tokens_out += resp3.output_tokens
                if resp3.text:
                    self.emit("log", agent_id, resp3.text)
                    final_text = resp3.text
            except Exception as exc:
                self.emit("error", agent_id, f"final synthesis failed: {exc}")
        preview = re.sub(r"\s+", " ", (final_text or "")).strip()[:400]
        self.emit("agent_end", agent_id, f"{agent['name']} done" + (f" — {preview}" if preview else ""), depth=depth)
        return final_text or "(no output)"

    def _execute_tools(self, agent: dict[str, Any], calls: list[ToolCall], depth: int) -> list[tuple[str, str, str, bool]]:
        def one(call: ToolCall) -> tuple[str, str, str, bool]:
            try:
                out = self._tool(agent, call, depth)
                return (call.id, call.name, out, False)
            except Cancelled:
                raise
            except Exception as exc:
                self.emit("error", agent["id"], f"{call.name}: {exc}")
                return (call.id, call.name, f"{type(exc).__name__}: {exc}", True)

        delegations = [c for c in calls if c.name == "delegate"]
        if len(delegations) > 1:
            with ThreadPoolExecutor(max_workers=min(6, len(calls))) as ex:
                return list(ex.map(one, calls))
        return [one(c) for c in calls]

    def _connectors(self) -> list[dict[str, Any]]:
        if not self.store or not hasattr(self.store, "connectors"):
            return []
        try:
            return self.store.connectors()
        except Exception:
            return []

    def _notify(self, text: str) -> None:
        try:
            I.notify(self._connectors(), text)
        except Exception:
            pass

    def _tool(self, agent: dict[str, Any], call: ToolCall, depth: int) -> str:
        args = call.args or {}
        name = call.name
        aid = agent["id"]
        if name == "delegate":
            target = str(args.get("agent_id", "")).strip()
            self.emit("tool", aid, f"delegate → {target}: {str(args.get('task',''))[:160]}")
            return self.run_agent(target, str(args.get("task", "")), str(args.get("context", "") or ""), depth + 1)
        if name == "list_agents":
            return self.roster_text(aid)
        if name == "finish":
            self.emit("tool", aid, "finish")
            return str(args.get("summary", ""))
        if name == "queue_action":
            if not self.store:
                return "no approval queue available in this context"
            kind = str(args.get("kind", "other")); to = str(args.get("to", ""))
            subject = str(args.get("subject", "") or ""); body = str(args.get("body", ""))
            reason = str(args.get("reason", "") or "")
            violations = P.check_outbound(kind, subject, body, self.business, known_text=getattr(self, "_task", ""))
            key = (kind, to)
            self._policy_hits[key] = self._policy_hits.get(key, 0) + (1 if violations else 0)
            flags = ""
            if violations and self._policy_hits[key] <= 2:
                self.emit("policy", aid, f"blocked {kind} → {to}: " + "; ".join(violations), violations=violations)
                return ("POLICY BLOCK - not queued. Fix these and call queue_action again:"
                        + "".join(chr(10) + "- " + v for v in violations))
            if violations:   # third attempt: let it through, but flag it loudly for the owner
                flags = " | ".join(violations)
                self.emit("policy", aid, f"flagged {kind} → {to} after repeated violations: " + flags, violations=violations)
            qid = self.store.add_action(self.run_id, aid, kind, to, subject, body, reason, flags=flags)
            self.emit("approval", aid, f"{kind} → {to}: {subject or body[:60]}", action_id=qid, action_kind=kind, to=to)
            self._notify(f":hourglass_flowing_sand: *Approval needed* — {kind} → {to}: {subject or body[:80]} (queue #{qid})")
            return f"queued for approval (id={qid}). It will only be sent after the owner approves."
        if name == "crm_lookup":
            if not self.store:
                return "no CRM in this context"
            rows = self.store.contacts(str(args.get("query", "")))
            self.emit("tool", aid, f"crm_lookup({args.get('query','')!r}) → {len(rows)} match(es)")
            if not rows:
                return "no matching contacts"
            return "\n".join(f"- {r['name']} | {r['company']} | {r['email']} | stage={r['stage']} | {r['notes'][:120]}" for r in rows[:10])
        if name == "crm_update":
            if not self.store:
                return "no CRM in this context"
            row = self.store.upsert_contact(str(args.get("contact", "")), dict(args.get("fields") or {}))
            self.emit("tool", aid, f"crm_update → {row['name'] or row['email']} stage={row['stage']}")
            synced = I.crm_sync(self._connectors(), row)
            for m in synced:
                self.emit("tool", aid, f"crm sync → {m}")
            return (f"updated contact #{row['id']} ({row['name'] or row['email']}) stage={row['stage']}"
                    + (" · " + "; ".join(synced) if synced else ""))
        if name in ("calendar_free_slots", "calendar_book"):
            conn = next((c for c in self._connectors() if c["kind"] == "gcal"), None)
            if not conn:
                return "no calendar connected — ask the owner to add a Google Calendar connector under Integrations"
            if name == "calendar_free_slots":
                slots = I.gcal_free_slots(conn["config"], str(args.get("from_date", "")), str(args.get("to_date", "")),
                                          int(args.get("duration_minutes") or 30))
                self.emit("tool", aid, f"calendar_free_slots {args.get('from_date')}→{args.get('to_date')}: {len(slots)} free")
                return "\n".join(slots) if slots else "no free slots in that range"
            spec = {"title": str(args.get("title", "")), "start": str(args.get("start", "")), "end": str(args.get("end", "") or ""),
                    "attendee_email": str(args.get("attendee_email", "") or ""), "description": str(args.get("description", "") or "")}
            if conn.get("auto"):
                res = I.gcal_create_event(conn["config"], spec["title"], spec["start"], spec["end"], spec["attendee_email"], spec["description"])
                self.emit("tool", aid, f"calendar_book → {res}")
                return res
            qid = self.store.add_action(self.run_id, aid, "booking", spec["attendee_email"] or conn["name"], spec["title"],
                                        json.dumps(spec, indent=1), str(args.get("reason", "") or "calendar booking"))
            self.emit("approval", aid, f"booking → {spec['attendee_email'] or conn['name']}: {spec['title']} {spec['start']}", action_id=qid, action_kind="booking", to=spec["attendee_email"])
            self._notify(f":calendar: *Booking awaiting approval* — {spec['title']} {spec['start']} (queue #{qid})")
            return f"queued for approval (id={qid}): booking '{spec['title']}' at {spec['start']} — the owner must approve before it lands on the calendar."
        if name.startswith("mcp__"):
            hit = self._mcp_index.get(name)
            if not hit:
                return f"unknown MCP tool {name}"
            conn, tool = hit
            if M.is_write(tool) and not conn.get("auto"):
                payload = json.dumps({"mcp_tool": tool, "arguments": args}, indent=1)
                qid = self.store.add_action(self.run_id, aid, "api_call", conn["name"], f"MCP {tool}", payload,
                                            f"Write-type MCP tool on {conn['name']} — owner approval required")
                self.emit("approval", aid, f"api_call → {conn['name']}: MCP {tool}", action_id=qid, action_kind="api_call", to=conn["name"])
                return f"queued for approval (id={qid}): {tool} on {conn['name']} looks like a write; the owner must approve."
            self.emit("tool", aid, f"mcp {conn['name']}.{tool}({', '.join(f'{k}={str(v)[:40]!r}' for k, v in args.items())})")
            return M.REGISTRY.get(conn).call(tool, args)
        if name == "camera_look":
            if not self.store or not hasattr(self.store, "connectors"):
                return "no cameras in this context"
            from . import vision as V
            cams = [c for c in self.store.connectors() if c["kind"] == "camera"]
            want = str(args.get("camera", "") or "").strip()
            conn = next((c for c in cams if c["name"] == want), None) if want else (cams[0] if cams else None)
            if not conn:
                return "no camera called %r. Cameras: %s" % (want, ", ".join(c["name"] for c in cams) or "(none connected)")
            question = str(args.get("question", "") or "").strip()
            self.emit("tool", aid, f"camera_look {conn['name']}" + (f": {question[:80]}" if question else ""))
            live = (self.configs.get("mode") or "") != "demo"
            prev = self.store.last_vision_event(conn["name"]) if hasattr(self.store, "last_vision_event") else None
            try:
                res = V.analyse(str(conn["config"].get("source", "")), conn["config"], None, live_vlm=live, question=question)
            except Exception as exc:
                return f"camera {conn['name']} unavailable: {str(exc)[:200]}"
            snap = V.save_snapshot(self.configs.get("desk_id", 0), conn["name"], res["annotated"])
            ev = self.store.add_vision_event(conn["name"], res["counts"], motion=res["motion"], backend=res["backend"],
                                             reason="camera_look by " + aid, question=question, answer=res["answer"], snapshot=snap,
                                             source="agent")
            try:
                out = self.run_dir / f"camera-{conn['name']}-{time.strftime('%H%M%S')}.jpg"
                out.write_bytes(res["annotated"])
                if out not in self.deliverables:
                    self.deliverables.append(out)
                self.emit("deliverable", "system", str(out), path=str(out))
            except OSError:
                pass
            lines = [f"Camera {conn['name']} at {time.strftime('%H:%M:%S')}: {V.counts_text(res['counts'])}"
                     + (f" (detector: {res['backend']})" if res["backend"] != "none" else " (no object detector available — rely on the analyst's answer)"),
                     f"Motion vs previous frame: {res['motion']:.2f}" if prev else "First frame from this camera."]
            if res["detections"] and res["backend"] == "yolo":
                lines.append("Objects: " + ", ".join(f"{d['label']} {int(d['conf']*100)}% at {d['box']}" for d in res["detections"][:12]))
            if question:
                lines.append("Analyst: " + (res["answer"] or "(no vision model configured — answer unavailable; detections above are all we have)"))
            lines.append(f"Event #{ev['id']} logged; snapshot saved.")
            return "\n".join(lines)
        if name == "camera_events":
            if not self.store or not hasattr(self.store, "vision_events"):
                return "no camera log in this context"
            from . import vision as V
            try:
                hours = float(args.get("hours") or 24)
            except Exception:
                hours = 24.0
            try:
                limit = max(1, min(int(args.get("limit") or 40), 200))
            except Exception:
                limit = 40
            rows = self.store.vision_events(str(args.get("camera", "") or ""), time.time() - hours * 3600,
                                            str(args.get("query", "") or ""), limit, bool(args.get("alerts_only")))
            self.emit("tool", aid, f"camera_events({args.get('camera','') or 'all'}, {hours:g}h) → {len(rows)}")
            if not rows:
                return f"No camera events in the last {hours:g} hours" + (f" matching {args.get('query')!r}" if args.get("query") else "") + "."
            out = []
            for r in reversed(rows):
                out.append(f"{time.strftime('%a %d %b %H:%M', time.localtime(r['ts']))} | {r['camera']} | {V.counts_text(r['counts'])} | motion {r['motion']:.2f}"
                           + (" | ALERT " + r["reason"] if r.get("triggered") else (" | " + r["reason"] if r.get("reason") else ""))
                           + (f" | analyst: {r['answer'][:160]}" if r.get("answer") else ""))
            return "\n".join(out)
        if name == "remember":
            if not self.store or not hasattr(self.store, "remember"):
                return "no memory in this context"
            row = self.store.remember(str(args.get("key", "")), str(args.get("value", "")), source=self.run_id)
            self.emit("tool", aid, f"remember {row['key']} = {row['value'][:80]}")
            return f"remembered '{row['key']}'"
        if name == "recall":
            if not self.store or not hasattr(self.store, "recall"):
                return "no memory in this context"
            rows = self.store.recall(str(args.get("query", "") or ""))
            self.emit("tool", aid, f"recall({args.get('query','')!r}) → {len(rows)}")
            return "\n".join(f"- {r['key']}: {r['value']}" for r in rows) or "(nothing remembered yet)"
        if name == "list_connectors":
            if not self.store or not hasattr(self.store, "connectors"):
                return "(no connectors in this context)"
            return I.describe(self.store.connectors())
        if name == "http_request":
            if not self.store or not hasattr(self.store, "connector_by_name"):
                return "no connectors in this context"
            conn = self.store.connector_by_name(str(args.get("connector", "")))
            if not conn:
                return f"unknown connector {args.get('connector')!r}. Available:\n" + I.describe(self.store.connectors())
            if conn["kind"] != "http":
                return f"connector {conn['name']} is {conn['kind']}, not an HTTP API"
            method = str(args.get("method", "GET")).upper()
            path = str(args.get("path", ""))
            if method != "GET" and not conn.get("auto"):
                payload = json.dumps({"method": method, "path": path, "params": args.get("params") or {}, "body": args.get("body")}, indent=1)
                qid = self.store.add_action(self.run_id, aid, "api_call", conn["name"], f"{method} {path}", payload,
                                            str(args.get("reason", "") or ""))
                self.emit("approval", aid, f"api_call → {conn['name']}: {method} {path}", action_id=qid, action_kind="api_call", to=conn["name"])
                return f"queued for approval (id={qid}): {method} {path} on {conn['name']} — the owner must approve writes on this connector."
            self.emit("tool", aid, f"http_request {method} {conn['name']}{path}")
            res = I.http_call(conn["config"], method, path, args.get("params") or None, args.get("body"))
            txt = json.dumps(res, ensure_ascii=False)
            return txt[:8000] + ("\n...[truncated]" if len(txt) > 8000 else "")
        if name == "generate_media":
            if not self.store or not hasattr(self.store, "connectors"):
                return "no connectors in this context"
            conn = next((c for c in self.store.connectors() if c["kind"] == "higgsfield"), None)
            if not conn:
                return "no Higgsfield connector on this desk — the owner adds one under Integrations (key id + secret from cloud.higgsfield.ai)"
            media = str(args.get("media") or "image")
            prompt = str(args.get("prompt") or "").strip()
            if not prompt:
                return "generate_media needs a prompt"
            spec = {"media": media, "prompt": prompt, "image_url": args.get("image_url") or "", "duration": args.get("duration"),
                    "aspect_ratio": args.get("aspect_ratio") or "16:9", "purpose": args.get("purpose") or ""}
            if not conn.get("auto"):
                qid = self.store.add_action(self.run_id, aid, "api_call", conn["name"], f"generate {media}: {prompt[:70]}",
                                            json.dumps(spec, indent=1), str(args.get("purpose") or "media generation spends credits"))
                self.emit("approval", aid, f"api_call → {conn['name']}: generate {media}", action_id=qid, action_kind="api_call", to=conn["name"])
                return f"queued for approval (id={qid}): {media} generation on {conn['name']} — spends credits, the owner must approve."
            self.emit("tool", aid, f"generate_media {media} via {conn['name']}")
            res = I.higgsfield_generate(conn["config"], media, prompt, spec["image_url"], spec["duration"], spec["aspect_ratio"])
            return json.dumps({"status": res["status"], "outputs": res["outputs"], "model": res["model"]}, ensure_ascii=False)
        if name == "schedule_task":
            if not self.store or not hasattr(self.store, "add_job"):
                return "no scheduler in this context"
            every = int(args.get("every_minutes") or 0)
            in_min = int(args.get("in_minutes") or 0)
            nxt = time.time() + max(1, in_min if in_min else every) * 60
            job = self.store.add_job("task", str(args.get("name", "scheduled task")), str(args.get("task", "")), every, nxt)
            when = f"every {every} min" if every else f"in {in_min or every} min"
            self.emit("tool", aid, f"schedule_task → #{job['id']} {job['name']} ({when})")
            return f"scheduled job #{job['id']} {when}"
        self.emit("tool", aid, f"{name}({', '.join(f'{k}={str(v)[:60]!r}' for k, v in args.items() if k != 'content')})")
        assert self._ws is not None
        return self._ws.call(name, args)

    # ------------------------------------------------------------------ entry
    def run(self, task: str, mode: str = "auto") -> RunResult:
        self._cancel.clear()
        self._task = task
        self.tokens_in = self.tokens_out = 0
        self.deliverables = []
        self._policy_hits = {}
        self._counts = {"queued": 0, "sent": 0, "crm": 0, "jobs": 0}
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_dir = RUNS_DIR / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "TASK.md").write_text(f"# Task\n\n{task}\n\nMode: {mode}\n", encoding="utf-8")

        def on_deliverable(p: Path):
            if p not in self.deliverables:
                self.deliverables.append(p)
            self.emit("deliverable", "system", str(p), path=str(p))

        self._ws = T.WorkspaceTools(self.run_dir, on_deliverable)
        if self.store:
            self.store.create_run(self.run_id, task, mode, str(self.run_dir))
        self.emit("log", "system", f"run {self.run_id}  mode={mode}  business={self.business.get('name')}")
        status, summary = "done", ""
        try:
            if mode == "auto":
                summary = self.run_agent("atlas", task)
            elif mode in self.workflows:
                summary = self._run_workflow(self.workflows[mode], task)
            else:
                raise ValueError(f"unknown mode '{mode}'")
        except Cancelled:
            status, summary = "cancelled", "Cancelled by user."
            self.emit("error", "system", "cancelled")
        except Exception as exc:
            status, summary = "error", f"{type(exc).__name__}: {exc}"
            self.emit("error", "system", summary + "\n" + traceback.format_exc()[-1500:])
        if status == "done":
            c = self._counts
            summary = (summary or "").rstrip() + (
                f"\n\n---\nVerified by the desk (not model-claimed): {c['queued']} action(s) queued for approval, "
                f"{c['sent']} sent, {c['crm']} CRM update(s), {c['jobs']} task(s) scheduled, "
                f"{len(self.deliverables)} deliverable(s) saved.")
        if summary:
            (self.run_dir / "SUMMARY.md").write_text(summary, encoding="utf-8")
        if self.store:
            self.store.finish_run(self.run_id, status, summary, self.tokens_in, self.tokens_out)
        self.emit("done", "system", summary, status=status, run_dir=str(self.run_dir),
                  deliverables=[str(p) for p in self.deliverables],
                  tokens_in=self.tokens_in, tokens_out=self.tokens_out)
        return RunResult(self.run_id, status, summary, list(self.deliverables),
                         self.tokens_in, self.tokens_out, self.run_dir)

    def _run_workflow(self, wf: dict[str, Any], task: str) -> str:
        outputs: list[tuple[str, str]] = []
        previous = ""
        for i, step in enumerate(wf.get("steps", []), 1):
            self._check()
            all_text = "\n\n".join(f"## {aid}\n{out}" for aid, out in outputs)
            prompt = step["task"].replace("{task}", task).replace("{previous}", previous).replace("{all}", all_text)
            aid = step["agent"]
            self.emit("log", "system", f"step {i}/{len(wf['steps'])}: {aid}")
            out = self.run_agent(aid, prompt, "", depth=1)
            outputs.append((aid, out))
            previous = out
            assert self._ws is not None
            self._ws.save_deliverable(f"{i:02d}_{aid}.md", out)
        if wf.get("synthesize") and "atlas" in self.agents:
            all_text = "\n\n".join(f"## {aid}\n{out}" for aid, out in outputs)
            return self.run_agent(
                "atlas",
                f"The workflow '{wf.get('name')}' has completed for this task:\n{task}\n\n"
                "Below are all step outputs. Produce the final client-ready deliverable(s) — save them with "
                "save_deliverable — applying any reviewer fixes, then call finish with a summary. "
                "Delegate again only if something is clearly missing.",
                all_text, depth=0)
        return outputs[-1][1] if outputs else "(workflow had no steps)"
