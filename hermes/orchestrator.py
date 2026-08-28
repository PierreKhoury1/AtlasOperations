"""Hermes orchestration core.

Two modes:
  * auto      — the Hermes agent plans, delegates (tool `delegate`), reviews and finishes.
  * <workflow> — deterministic step pipeline from config/workflows.json, optional Hermes synthesis at the end.

Everything reports through `emit(Event)` so any UI (or CLI) can render it.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, agent: str = "system", text: str = "", **data):
        ev = Event(kind, agent, text, data)
        if self.store and self.run_id and kind not in ("usage",):
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
        ]
        if b.get("extra_context"):
            parts.append("\nAdditional context:\n" + b["extra_context"])
        return "\n".join(p for p in parts if p is not None).strip()

    def roster_text(self, exclude: str = "hermes") -> str:
        lines = []
        for a in self.agents.values():
            if a["id"] == exclude:
                continue
            lines.append(f"- {a['id']}: {a['name']} — {a.get('role','')}")
        return "\n".join(lines) or "(no specialists configured)"

    def system_prompt(self, agent: dict[str, Any]) -> str:
        parts = [agent.get("system_prompt", "")]
        if agent["id"] == "hermes" or self.orch.get("include_business_context_in_specialists", True):
            parts.append(self.business_context())
        if "delegate" in agent.get("tools", []):
            parts.append("Specialist agents available via delegate(agent_id, ...):\n" + self.roster_text(agent["id"]))
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------ agent loop
    def run_agent(self, agent_id: str, task: str, context: str = "", depth: int = 0) -> str:
        agent = self.agents.get(agent_id)
        if not agent:
            if agent_id in T.SCHEMAS:
                return (f"ERROR: '{agent_id}' is a TOOL, not an agent. Call the {agent_id} tool directly with its own "
                        f"arguments. Agents you can delegate to: {', '.join(a for a in self.agents if a != 'hermes')}")
            return f"ERROR: unknown agent '{agent_id}'. Agents you can delegate to: {', '.join(a for a in self.agents if a != 'hermes')}"
        self._check()
        provider = self.pool.get(agent.get("provider") or "")
        model = agent.get("model") or provider.default_model
        tool_names = [t for t in agent.get("tools", []) if t in T.SCHEMAS]
        if depth >= int(self.orch.get("max_delegation_depth", 2)):
            tool_names = [t for t in tool_names if t != "delegate"]
        if agent_id == "hermes" and "finish" not in tool_names:
            tool_names.append("finish")
        schemas = T.schema_for(tool_names)
        max_iter = int(self.orch.get("max_iterations", 24)) if agent_id == "hermes" \
            else int(self.orch.get("specialist_max_iterations", 8))

        prompt = task if not context else f"{task}\n\n---\nContext:\n{context}"
        messages: list[Any] = [provider.user_message(prompt)]
        self.emit("agent_start", agent_id, f"{agent['name']} ← task ({len(prompt)} chars)", depth=depth, model=model)
        final_text = ""
        for i in range(max_iter):
            self._check()
            try:
                resp = provider.chat(self.system_prompt(agent), messages, schemas, model)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                self.emit("error", agent_id, msg)
                return f"ERROR from {agent['name']}: {msg}"
            with self._usage_lock:
                self.tokens_in += resp.input_tokens
                self.tokens_out += resp.output_tokens
            self.emit("usage", agent_id, "", tokens_in=self.tokens_in, tokens_out=self.tokens_out)
            if resp.refusal:
                self.emit("error", agent_id, resp.refusal)
                return f"{agent['name']} could not complete: {resp.refusal}"
            if resp.text:
                self.emit("log", agent_id, resp.text)
                final_text = resp.text
            if not resp.tool_calls:
                break
            messages.append(resp.assistant_message)
            results = self._execute_tools(agent, resp.tool_calls, depth)
            finished = next((r for r in results if r[1] == "finish"), None)
            messages.extend(provider.tool_results([(c, n, o, e) for c, n, o, e in results]))
            if finished:
                final_text = finished[2]
                break
        else:
            self.emit("log", agent_id, f"(hit max iterations {max_iter})")
        self.emit("agent_end", agent_id, f"{agent['name']} done", depth=depth)
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
            qid = self.store.add_action(self.run_id, aid, kind, to, subject, body, reason)
            self.emit("approval", aid, f"{kind} → {to}: {subject or body[:60]}", action_id=qid, action_kind=kind, to=to)
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
            return f"updated contact #{row['id']} ({row['name'] or row['email']}) stage={row['stage']}"
        self.emit("tool", aid, f"{name}({', '.join(f'{k}={str(v)[:60]!r}' for k, v in args.items() if k != 'content')})")
        assert self._ws is not None
        return self._ws.call(name, args)

    # ------------------------------------------------------------------ entry
    def run(self, task: str, mode: str = "auto") -> RunResult:
        self._cancel.clear()
        self.tokens_in = self.tokens_out = 0
        self.deliverables = []
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
                summary = self.run_agent("hermes", task)
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
        if wf.get("synthesize") and "hermes" in self.agents:
            all_text = "\n\n".join(f"## {aid}\n{out}" for aid, out in outputs)
            return self.run_agent(
                "hermes",
                f"The workflow '{wf.get('name')}' has completed for this task:\n{task}\n\n"
                "Below are all step outputs. Produce the final client-ready deliverable(s) — save them with "
                "save_deliverable — applying any reviewer fixes, then call finish with a summary. "
                "Delegate again only if something is clearly missing.",
                all_text, depth=0)
        return outputs[-1][1] if outputs else "(workflow had no steps)"
