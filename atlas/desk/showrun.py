"""Showrunner: keeps a REAL orchestration run going for the site's hero while visitors are watching.

The hero (site/orch.js) polls GET /api/orch/live. While at least one visitor polled in the last VIEWER_TTL
seconds and the previous run ended more than IDLE_GAP seconds ago, a new run starts on a dedicated hidden
"showroom" desk (never the public demo desk) with every agent pinned to a free model, so it costs nothing
and is never blocked by the spend cap. Every orchestrator event - agent spawn, each token, hand-back,
approval, done - is normalised to {i, t, k, a, inst, x, d} with t = seconds since the run started, and the
client animates them at their real timing. No key / demo mode => live=false and the client falls back to a
recorded real run, labelled as a replay. Nothing here is scripted.

Usage (usage spec §1.3 L12, §4.6 S53). The runs count in the website's own share of the free pool, not in any
account's: a public meter (lane auto, the per-run cap) goes to the Orchestrator, Hermes inner requests included. No
run starts with fewer than 2 requests left in that share or in the shared pool (Atlas plus one specialist); the feed
then says so and shows the recorded run until the reset. A run that reaches the share mid-way ends with status limit.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from .. import usage as U
from ..orchestrator import Event, Orchestrator

FREE_MODEL = os.environ.get("SHOW_MODEL", os.environ.get("ATLAS_FREE_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"))
IDLE_GAP = float(os.environ.get("SHOW_GAP_S", "75"))            # pause between runs while people watch
MAX_RUN_S = float(os.environ.get("SHOW_MAX_RUN_S", "180"))      # hard stop per run
VIEWER_TTL = 40.0                                                # a poll counts as "watching" for this long
MAX_PER_HOUR = int(os.environ.get("SHOW_MAX_PER_HOUR", "18"))   # free-tier request budget guard
KEEP_KINDS = {"agent_start", "agent_end", "token", "tool", "approval", "policy", "error", "done", "sent"}
MIN_REQUESTS = 2                                                 # Atlas plus one specialist (usage spec §1.3 L12)
RECHECK_S = 60.0                                                 # while the share is used up, look again at most this often

TASKS = [
    "New lead via the website form: Priya Nair, priya.n@example.com, 07700 900123. \"Looking to sell my 2-bed flat in "
    "Pimlico SW1V and buy a 3-bed in Battersea. Want a valuation this week and to understand fees.\" Research the area "
    "and comparable prices, draft a personal reply that books a valuation slot, update the CRM, and have QA check tone and policy.",
    "Inbound WhatsApp: \"Hi, we viewed 14 Elm Road on Saturday. Is the landlord open to a 2-year tenancy and pets? "
    "We can move in on the 1st.\" - Tom & Aisha. Check what we know, draft a reply that keeps them warm without "
    "promising terms we can't give, log the enquiry, and schedule a follow-up for Monday.",
    "Emergency call-out request from a lead in SW1V: no hot water overnight, two small kids at home. Confirm service "
    "coverage and SLA, draft an under-90-word plain-text reply with the gas-safety check included and no prices, "
    "update the CRM stage and queue the reply for the owner.",
    "A past client, Marcus Dell, emailed: \"Thinking about letting my Clapham flat rather than selling - what would "
    "it fetch and what do you charge for full management?\" Research rental comparables, draft a clear reply with "
    "next steps (no fee figures in writing), update CRM to 'lettings enquiry', QA the draft.",
]


class ShowRunner:
    def __init__(self, ctx: dict[str, Any]):
        self.ctx = ctx
        self._lock = threading.Lock()
        self.run: dict[str, Any] | None = None
        self.last_end = 0.0
        self.last_view = 0.0
        self.task_i = 0
        self.desk_id: int | None = None
        self.starts: list[float] = []
        # the website's share (L12) or the shared pool cannot carry a run: {"until", "reason", "limit", "checked"}
        self.limited: dict[str, Any] | None = None

    # ------------------------------------------------------------------ desk
    def _desk(self) -> dict[str, Any]:
        store = self.ctx["store"]
        if self.desk_id:
            d = store.desk(self.desk_id)
            if d:
                return d
        for d in store.desks_for(0):
            if d.get("name") == "Atlas showroom":
                self.desk_id = d["id"]
                return d
        T = self.ctx["templates"]
        tpl = self.ctx.get("template") or "sales_desk"
        d = store.add_desk(0, "Atlas showroom", tpl, "free", T.build_desk(tpl, {}))
        self.desk_id = d["id"]
        return d

    # ------------------------------------------------------------------ lifecycle
    def _no_key(self) -> str:
        if self.ctx["mode"]() == "demo":
            return "no model key on the server - showing a recorded real run"
        blocked = self.ctx.get("blocked")
        if blocked and blocked():
            return "no model key on the server - showing a recorded real run"
        return ""

    def offline_reason(self) -> str:
        reason = self._no_key()
        if reason:
            return reason
        lim = self.limited
        if lim and time.time() < lim["until"]:
            return lim["reason"]
        return ""

    def touch(self) -> None:
        self.last_view = time.time()
        self._maybe_start()

    def _maybe_start(self) -> None:
        with self._lock:
            now = time.time()
            if self.run and not self.run["done"]:
                return
            if now - self.last_end < IDLE_GAP or self._no_key():
                return
            lim = self.limited
            if lim and now < lim["until"] and now - lim["checked"] < RECHECK_S:
                return
            self.starts = [s for s in self.starts if now - s < 3600]
            if len(self.starts) >= MAX_PER_HOUR:
                return
            if self._start_locked():
                self.starts.append(now)

    def meter(self, desk: dict[str, Any]) -> U.Meter:
        """The website's meter for one live run: the public share (L12), lane auto (it never takes the per-minute
        slots kept for owners), the per-run cap (L4)."""
        return U.Meter(owner_id=0, desk_id=int(desk["id"]), scope="public", lane="auto", surface="show.run",
                       run_cap=U.run_cap(0))

    def _start_locked(self) -> bool:
        """Start the next run, unless the website's share cannot carry one (S53). True when a run started."""
        desk = self._desk()
        meter = self.meter(desk)
        try:
            U.run_begin(meter, house=False, free=True, need=MIN_REQUESTS, surface="show.run")
        except U.UsageLimit as exc:
            until = exc.retry_at or exc.resets_at or (time.time() + 3600)
            self.limited = {"until": float(until), "checked": time.time(), "limit": exc.body(),
                            "reason": f"The live run has used today's share; next one after {U.until_text(exc) or 'the reset'}."}
            return False
        self.limited = None
        d = dict(desk)
        d["tier"] = "free"
        configs = self.ctx["desk_configs"](d)
        names = {}
        for a in configs.get("agents", []):
            a["model"] = FREE_MODEL                         # pinned: $0, never spend-capped
            names[a["id"]] = a.get("name") or a["id"]
        task = TASKS[self.task_i % len(TASKS)] + " Run the independent parts in parallel and queue anything outbound for the owner."
        self.task_i += 1
        run: dict[str, Any] = {"id": None, "task": task, "t0": time.time(), "events": [], "done": False, "status": "",
                               "model": FREE_MODEL, "desk": desk["name"], "names": names}
        self.run = run
        holder: dict[str, Any] = {}

        def emit(ev: Event) -> None:
            if ev.kind not in KEEP_KINDS or (ev.kind == "token" and ev.data.get("thinking")):
                return
            t = round(ev.ts - run["t0"], 3)
            inst = str(ev.data.get("inst") or ev.agent)
            evs = run["events"]
            if ev.kind == "token":
                # coalesce bursts from the same instance (keeps the feed small; timing stays sub-100 ms)
                if evs and evs[-1]["k"] == "token" and evs[-1]["inst"] == inst and t - evs[-1]["t"] < 0.08:
                    evs[-1]["x"] += ev.text
                    return
                evs.append({"i": len(evs), "t": t, "k": "token", "a": ev.agent, "inst": inst, "x": ev.text})
                return
            d = {k: ev.data[k] for k in ("parent", "assignment", "model", "depth", "status", "action_kind", "to") if k in ev.data}
            d["name"] = names.get(ev.agent, ev.agent)
            evs.append({"i": len(evs), "t": t, "k": ev.kind, "a": ev.agent, "inst": inst, "x": (ev.text or "")[:600], "d": d})
            if ev.kind == "done":
                run["done"] = True
                run["status"] = str(ev.data.get("status") or "done")
                self.last_end = time.time()

        store = self.ctx["store"].for_desk(desk["id"])
        orch = Orchestrator(configs, store, emit, meter=meter)
        holder["orch"] = orch

        def work() -> None:
            try:
                orch.run(task, "auto")
            except BaseException as exc:                      # the orchestrator already emits done/error; last line of defence
                run["events"].append({"i": len(run["events"]), "t": round(time.time() - run["t0"], 3), "k": "done",
                                      "a": "system", "inst": "system", "x": f"{type(exc).__name__}: {str(exc)[:200]}", "d": {"status": "error"}})
            finally:
                if not run["done"]:
                    run["done"] = True
                    run["status"] = run["status"] or "error"
                    self.last_end = time.time()
                run["id"] = run["id"] or orch.run_id

        def watchdog() -> None:
            time.sleep(MAX_RUN_S)
            if not run["done"]:
                orch.cancel()

        threading.Thread(target=work, daemon=True, name="showrun").start()
        threading.Thread(target=watchdog, daemon=True, name="showrun-watchdog").start()
        for _ in range(50):
            if orch.run_id:
                break
            time.sleep(0.02)
        run["id"] = orch.run_id or f"show-{int(run['t0'])}"
        return True

    # ------------------------------------------------------------------ snapshot for the client
    def snapshot(self, since: int = 0, run_id: str = "") -> dict[str, Any]:
        self.touch()
        reason = self.offline_reason()
        out: dict[str, Any] = {"live": not reason, "reason": reason, "model": FREE_MODEL, "run": None, "events": [], "idx": 0, "next_in": 0}
        if reason:
            return out
        run = self.run
        now = time.time()
        if run is None or (run["done"] and run_id and run_id != run["id"]):
            out["next_in"] = max(0.0, IDLE_GAP - (now - self.last_end)) if run is None or run["done"] else 0
            return out
        if run_id and run_id != run["id"]:
            since = 0                                          # client was on an older run: send the new one from the start
        evs = run["events"]
        since = max(0, min(int(since or 0), len(evs)))
        out["run"] = {"id": run["id"], "task": run["task"], "started": run["t0"], "elapsed": round(now - run["t0"], 3),
                      "done": run["done"], "status": run["status"], "model": run["model"], "desk": run["desk"], "n": len(evs)}
        out["events"] = evs[since:]
        out["idx"] = len(evs)
        if run["done"]:
            out["next_in"] = max(0.0, IDLE_GAP - (now - self.last_end))
        return out
