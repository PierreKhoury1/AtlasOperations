"""Evaluation + capacity harness.

    py -m hermes eval --template sales_desk --mode demo --n 3 --concurrency 3
    py -m hermes eval --template sales_desk --mode live --n 2 --concurrency 2 --tier free

Runs the template's sample leads through a throw-away desk store, N times each, with a given
concurrency, and reports: completion, latency (p50/p90), tokens, tool errors, policy blocks,
approvals, and a deterministic quality score per queued message. Output: JSON + Markdown in
workspace/evals/<timestamp>/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config as cfg
from . import metrics as MX
from . import policy as P
from . import templates
from .orchestrator import Event, Orchestrator
from .store import Store

EVALS_DIR = cfg.ROOT / "workspace" / "evals"


def lead_task(L: dict[str, str]) -> str:
    return (f"New inbound lead — handle end to end.\nName: {L['name']}\nCompany: {L['company'] or '(individual)'}\n"
            f"Email: {L['email']}\nPhone: {L['phone'] or '-'}\nSource: {L['source']}\n\nEnquiry:\n{L['notes']}")


def build_configs(template: str, mode: str, tier: str, provider_name: str) -> dict[str, Any]:
    t = templates.get(template)
    agents = t["agents"]
    if mode == "demo":
        providers = {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0.05}}}
        for a in agents:
            a["provider"] = "demo"; a["model"] = ""
    else:
        providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
        for name, pc in cfg.DEFAULT_PROVIDERS["providers"].items():
            providers.setdefault("providers", {}).setdefault(name, dict(pc))
        providers["default_provider"] = provider_name
        for a in agents:
            a["provider"] = provider_name
        if provider_name == "openrouter":
            templates.apply_tier(agents, tier)
    return {"providers": providers, "orchestration": cfg.load("orchestration", cfg.DEFAULT_ORCHESTRATION),
            "business": t["business"], "agents": agents, "workflows": t["workflows"], "ui": {}}


def quality(store, business: dict[str, Any]) -> dict[str, Any]:
    """Deterministic checks on everything that reached the approval queue."""
    acts = store.actions()
    checks = []
    for a in acts:
        v = P.check_outbound(a["kind"], a.get("subject") or "", a.get("body") or "", business)
        words = len((a.get("body") or "").split())
        checks.append({"id": a["id"], "kind": a["kind"], "to": a["to"], "violations": v, "words": words,
                       "flagged": bool(a.get("flags")), "has_subject": bool(a.get("subject")) if a["kind"] == "email" else True})
    clean = sum(1 for c in checks if not c["violations"] and not c["flagged"])
    return {"queued": len(checks), "clean": clean, "clean_rate": round(clean / len(checks), 3) if checks else None,
            "avg_words": round(sum(c["words"] for c in checks) / len(checks)) if checks else 0,
            "violations": sorted({x for c in checks for x in c["violations"]})[:10], "items": checks}


def run_eval(template: str, mode: str, n: int, concurrency: int, tier: str, provider_name: str,
             out_dir: Path | None = None, quiet: bool = False) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = out_dir or (EVALS_DIR / f"{stamp}-{template}-{mode}-c{concurrency}")
    out_dir.mkdir(parents=True, exist_ok=True)
    db = Path(tempfile.mkdtemp(prefix="hermes-eval-")) / "eval.db"
    store = Store(db)
    desk = store.add_desk(0, f"eval {template}", template, tier, templates.build_desk(template, {}))
    ds = store.for_desk(desk["id"])
    configs = build_configs(template, mode, tier, provider_name)
    leads = templates.SAMPLE_LEADS.get(template, templates.SAMPLE_LEADS["sales_desk"])
    jobs = [(i, L) for i in range(n) for L in leads]
    lock = threading.Lock()
    results: list[dict[str, Any]] = []
    t0 = time.time()

    def one(item):
        i, L = item
        evs: list[Event] = []
        orch = Orchestrator(configs, ds, lambda ev: evs.append(ev) if ev.kind != "token" else None)
        st = time.time()
        res = orch.run(lead_task(L), "auto")
        row = {"lead": L["name"], "rep": i, "run_id": res.run_id, "status": res.status, "seconds": round(time.time() - st, 1),
               "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
               "errors": [e.text[:160] for e in evs if e.kind == "error"],
               "policy": [e.text[:160] for e in evs if e.kind == "policy"]}
        with lock:
            results.append(row)
            if not quiet:
                print(f"  {res.status:9} {row['seconds']:6.1f}s  in {res.tokens_in:6d} out {res.tokens_out:5d}  {L['name']}  "
                      f"{'ERR ' + row['errors'][0][:60] if row['errors'] else ''}", flush=True)
        return row

    if not quiet:
        print(f"eval: template={template} mode={mode} n={n}x{len(leads)} leads concurrency={concurrency} tier={tier}", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        list(ex.map(one, jobs))
    wall = time.time() - t0
    m = MX.compute(store, desk_id=desk["id"])
    q = quality(ds, configs["business"])
    report = {
        "template": template, "mode": mode, "tier": tier, "provider": provider_name if mode == "live" else "demo",
        "n": n, "leads": len(leads), "concurrency": concurrency, "wall_s": round(wall, 1),
        "throughput_runs_per_min": round(len(jobs) / (wall / 60), 2),
        "metrics": {k: v for k, v in m.items() if k != "per_run"}, "quality": {k: v for k, v in q.items() if k != "items"},
        "runs": results, "queued": q["items"], "when": stamp,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "report.md").write_text(to_markdown(report), encoding="utf-8")
    if not quiet:
        print(to_markdown(report))
        print(f"saved → {out_dir}")
    return report


def to_markdown(r: dict[str, Any]) -> str:
    m, q = r["metrics"], r["quality"]
    d = m.get("duration_s") or {}
    lines = [f"# Eval — {r['template']} · {r['mode']} · tier {r['tier']} · {r['n']}×{r['leads']} leads · concurrency {r['concurrency']}",
             "", "| metric | value |", "|---|---|",
             f"| runs | {m.get('runs')} (done {m.get('done')}, error {m.get('error')}) |",
             f"| completion rate | {m.get('completion_rate')} |",
             f"| wall clock | {r['wall_s']}s → **{r['throughput_runs_per_min']} runs/min** at concurrency {r['concurrency']} |",
             f"| run duration | mean {d.get('mean')}s · p50 {d.get('p50')}s · p90 {d.get('p90')}s · max {d.get('max')}s |",
             f"| tokens / run | in {m.get('tokens', {}).get('per_run_in')} · out {m.get('tokens', {}).get('per_run_out')} (≈ £{m.get('tokens', {}).get('est_cost_gbp_sonnet')} total on Sonnet) |",
             f"| tool calls / errors | {m.get('tool_calls')} / {m.get('tool_errors')} (rate {m.get('tool_error_rate')}) |",
             f"| delegations | {m.get('delegations')} |",
             f"| policy blocks / flags | {m.get('policy_blocks')} / {m.get('policy_flags')} |",
             f"| empty model replies | {m.get('empty_replies')} · provider errors {m.get('provider_errors')} |",
             f"| messages queued | {q.get('queued')} · clean {q.get('clean')} (rate {q.get('clean_rate')}) · avg {q.get('avg_words')} words |",
             ]
    if q.get("violations"):
        lines.append(f"| residual violations | {'; '.join(q['violations'])} |")
    lines += ["", "## Agent latency (s)", "", "| agent | n | mean | p90 |", "|---|---|---|---|"]
    for a, v in (m.get("agent_latency_s") or {}).items():
        lines.append(f"| {a} | {v['n']} | {v['mean']} | {v['p90']} |")
    lines += ["", "## Runs", "", "| lead | rep | status | s | in | out | errors |", "|---|---|---|---|---|---|---|"]
    for x in r["runs"]:
        lines.append(f"| {x['lead']} | {x['rep']} | {x['status']} | {x['seconds']} | {x['tokens_in']} | {x['tokens_out']} | {'; '.join(x['errors'])[:80]} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes eval")
    ap.add_argument("--template", default="sales_desk")
    ap.add_argument("--mode", default="demo", choices=["demo", "live"])
    ap.add_argument("--n", type=int, default=1, help="repeats per sample lead")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--tier", default="free")
    ap.add_argument("--provider", default="openrouter")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    r = run_eval(a.template, a.mode, a.n, a.concurrency, a.tier, a.provider, quiet=a.quiet)
    return 0 if r["metrics"].get("completion_rate", 0) >= 0.99 else 1


if __name__ == "__main__":
    sys.exit(main())
