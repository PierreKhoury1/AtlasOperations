"""Performance + capacity metrics computed from the store (runs + events).

Everything the engine does is an event, so latency per agent, tool error rate, policy hits, empty
replies and throughput can be derived after the fact — no extra instrumentation in the hot path.
"""
from __future__ import annotations

import statistics
import time
from collections import defaultdict
from typing import Any


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1)))))
    return xs[k]


def compute(store, desk_id: int | None = None, since: float | None = None, run_ids: list[str] | None = None) -> dict[str, Any]:
    c = store._conn
    where, args = [], []
    if desk_id is not None:
        where.append("r.desk_id=?"); args.append(desk_id)
    if since is not None:
        where.append("r.created>=?"); args.append(since)
    if run_ids:
        where.append("r.id IN (%s)" % ",".join("?" * len(run_ids))); args.extend(run_ids)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    runs = [dict(zip(("id", "created", "status", "tokens_in", "tokens_out", "task"), r)) for r in
            c.execute(f"SELECT r.id,r.created,r.status,r.tokens_in,r.tokens_out,r.task FROM runs r{w}", args).fetchall()]
    if not runs:
        return {"runs": 0}
    ids = [r["id"] for r in runs]
    q = "SELECT run_id,ts,kind,agent,text FROM events WHERE run_id IN (%s) ORDER BY ts" % ",".join("?" * len(ids))
    ev_by_run: dict[str, list[tuple]] = defaultdict(list)
    for row in c.execute(q, ids).fetchall():
        ev_by_run[row[0]].append(row)

    durations, agent_lat, tool_calls, tool_errors, policy_blocks, policy_flags = [], defaultdict(list), 0, 0, 0, 0
    empty_replies, approvals, delegations, provider_errors, done, errored, cancelled = 0, 0, 0, 0, 0, 0, 0
    tool_kinds: dict[str, int] = defaultdict(int)
    per_run: list[dict[str, Any]] = []
    for r in runs:
        evs = ev_by_run.get(r["id"], [])
        start = r["created"]
        end = next((e[1] for e in evs if e[2] == "done"), None)
        dur = (end - start) if end else None
        if dur is not None:
            durations.append(dur)
        starts: dict[str, list[float]] = defaultdict(list)
        n_tools = n_err = n_pol = n_appr = 0
        for _, ts, kind, agent, text in evs:
            if kind == "agent_start":
                starts[agent].append(ts)
            elif kind == "agent_end" and starts.get(agent):
                agent_lat[agent].append(ts - starts[agent].pop())
            elif kind == "tool":
                n_tools += 1
                tool_kinds[(text.split("(")[0].split(" ")[0] or "?")[:24]] += 1
                if text.startswith("delegate"):
                    delegations += 1
            elif kind == "error":
                n_err += 1
                if "HTTP " in text or "Error:" in text or "Timeout" in text:
                    provider_errors += 1
            elif kind == "policy":
                n_pol += 1
                if text.startswith("flagged"):
                    policy_flags += 1
                else:
                    policy_blocks += 1
            elif kind == "approval":
                n_appr += 1
            elif kind == "log" and "empty reply" in text:
                empty_replies += 1
        tool_calls += n_tools; tool_errors += n_err; approvals += n_appr
        if r["status"] == "done":
            done += 1
        elif r["status"] == "error":
            errored += 1
        elif r["status"] == "cancelled":
            cancelled += 1
        per_run.append({"id": r["id"], "status": r["status"], "duration_s": round(dur, 1) if dur else None,
                        "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"], "tools": n_tools, "errors": n_err,
                        "policy": n_pol, "approvals": n_appr, "task": (r["task"] or "")[:60]})
    span = (max(r["created"] for r in runs) - min(r["created"] for r in runs)) or 1.0
    tin = sum(r["tokens_in"] or 0 for r in runs); tout = sum(r["tokens_out"] or 0 for r in runs)
    return {
        "runs": len(runs), "done": done, "error": errored, "cancelled": cancelled,
        "completion_rate": round(done / len(runs), 3),
        "duration_s": {"mean": round(statistics.mean(durations), 1) if durations else None,
                       "p50": round(_pct(durations, 50), 1), "p90": round(_pct(durations, 90), 1), "max": round(max(durations), 1) if durations else None},
        "agent_latency_s": {a: {"n": len(v), "mean": round(statistics.mean(v), 1), "p90": round(_pct(v, 90), 1)} for a, v in agent_lat.items()},
        "tool_calls": tool_calls, "tool_errors": tool_errors,
        "tool_error_rate": round(tool_errors / tool_calls, 3) if tool_calls else 0.0,
        "tools_by_kind": dict(sorted(tool_kinds.items(), key=lambda x: -x[1])[:12]),
        "delegations": delegations, "approvals_queued": approvals,
        "policy_blocks": policy_blocks, "policy_flags": policy_flags, "empty_replies": empty_replies,
        "provider_errors": provider_errors,
        "tokens": {"in": tin, "out": tout, "per_run_in": round(tin / len(runs)), "per_run_out": round(tout / len(runs)),
                   "est_cost_gbp_sonnet": round((tin * 3 + tout * 15) / 1_000_000 * 0.78, 3)},
        "throughput_runs_per_min": round(len(runs) / (span / 60), 2) if len(runs) > 1 else None,
        "window_s": round(span, 1),
        "per_run": per_run,
    }


def capacity(store, active_runs: int, max_workers: int = 6) -> dict[str, Any]:
    """Rough capacity map for this deployment from recent runs."""
    m = compute(store, since=time.time() - 24 * 3600)
    mean = (m.get("duration_s") or {}).get("mean") or 0
    threads = __import__("threading").active_count()
    return {
        "active_runs": active_runs, "threads": threads, "cpu_count": __import__("os").cpu_count(),
        "mean_run_s": mean,
        "est_runs_per_hour_per_slot": round(3600 / mean, 1) if mean else None,
        "note": "Each run = 1 thread + up to %d parallel specialist threads. Model rate limits, not CPU, are the ceiling on the free tier." % max_workers,
        "last_24h": {k: m.get(k) for k in ("runs", "completion_rate", "tool_error_rate", "empty_replies", "provider_errors", "throughput_runs_per_min")},
    }
