"""Multi-camera restaurant eval: how well do different vision-language models read real restaurant feeds?

    py -m atlas vision-eval                                  # every default model, both sites
    py -m atlas vision-eval --models anthropic/claude-sonnet-5,google/gemini-3.8-flash --sites crownshy
    py -m atlas vision-eval --smoke                          # 1 frame per camera, 2 models: wiring check

Inputs (data/vision-eval): ground_truth.json (human-reviewed labels) + frames/<camera>_<n>.jpg
  crownshy-*   7 fixed cameras in one fine-dining restaurant during service (1080p, Bon Appetit's 19-camera film)
  kitchen-*    5 synchronised cameras in one real kitchen, one cook, frames at t=60/150/240 s (EPFL Smart Kitchen)

Three tasks per model, all through OpenRouter's OpenAI-compatible endpoint (usage + cost come back per call):
  single     one frame  -> {people, area, activity, issues}        scored: count in range, hazard recall, judge 0-5, hallucinations
  multicam   all cameras of a site at once (the middle frame each) -> site status: distinct people, busiest station, service on?
  temporal   3 frames of one camera over time                       -> what changed (kitchen site only)

Descriptive answers are graded by an LLM judge against the human description (default anthropic/claude-sonnet-5; pick
another with --judge to check judge bias). Counts and hazards are graded deterministically. Output:
workspace/vision-eval/<stamp>/results.json + report.md. Nothing here is mocked: every row is a real model call."""
from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from . import config as cfg
from .config import WORKSPACE_DIR

EVAL_DIR = cfg.DATA_DIR / "vision-eval"
DEFAULT_MODELS = [
    # free
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    # cheap
    "z-ai/glm-5.3-flash",
    "google/gemini-3.1-flash-lite",
    "qwen/qwen3.6-flash",
    # mid
    "google/gemini-3.8-flash",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-5.4-mini",
    # top
    "anthropic/claude-sonnet-5",
    "google/gemini-3.1-pro-preview",
]
DEFAULT_JUDGE = "anthropic/claude-sonnet-5"

SYSTEM_SINGLE = ("You are the vision analyst of a restaurant operations desk. You look at ONE camera frame and report "
                 "exactly what is visible. Never guess; if something is unclear say so. Count people carefully, including "
                 "people only partly visible (a hand, an arm, legs count as one person). Never identify anyone by name. "
                 "Reply with JSON only, no markdown.")
PROMPT_SINGLE = ('Report on this frame as JSON with exactly these keys:\n'
                 '{"people": <integer, all persons visible incl. partial>,\n'
                 ' "area": "<which station or area of the restaurant this camera shows>",\n'
                 ' "activity": "<two sentences: what is happening right now>",\n'
                 ' "issues": ["<any visible safety, hygiene or operations issue: open flame/flare-up, raw meat uncovered, '
                 'bare hands on ready food, knife left out, blocked walkway, spill, etc.>", ...] (empty list if none)}')

SYSTEM_MULTI = ("You are the vision analyst of a restaurant operations desk. You are shown frames from SEVERAL cameras "
                "of ONE restaurant captured at the same moment, each labelled with its camera name. Combine them into one "
                "picture of the whole restaurant. State only what is visible. Never identify anyone by name. "
                "Reply with JSON only, no markdown.")
PROMPT_MULTI = ('Combine all cameras into one status report as JSON with exactly these keys:\n'
                '{"distinct_people": <integer: your best estimate of distinct people across all cameras, remembering cameras overlap>,\n'
                ' "busiest": "<which station/area is busiest right now>",\n'
                ' "service_in_progress": <true|false: is the restaurant actively serving customers>,\n'
                ' "per_camera": {"<camera name>": "<one line>", ...},\n'
                ' "summary": "<three sentences on the state of the restaurant>",\n'
                ' "top_issue": "<the single most important safety/hygiene/operations issue, or \'none\'>"}')

SYSTEM_TEMPORAL = ("You are the vision analyst of a restaurant operations desk. You are shown frames from ONE fixed camera "
                   "at three different times, in order. Describe what changed. State only what is visible. "
                   "Reply with JSON only, no markdown.")
PROMPT_TEMPORAL = ('{"what_changed": "<three sentences: where the people were and what they did across the three frames, and '
                   'what changed on the counters>", "people_per_frame": [<int>, <int>, <int>]}')

JUDGE_SYSTEM = ("You grade a vision model's answer about a camera frame against a careful human description of the same "
                "frame. The human description is NOT exhaustive: it lists the main things, not every object. A model claim is a "
                "hallucination only if it CONTRADICTS the human description or is implausible for that scene (wrong number of "
                "people, wrong activity, an object that would not be there). Ordinary details the human simply did not mention "
                "(a fridge, a bottle, a door) are NOT hallucinations. Be lenient about wording. Keep every list to at most 5 "
                "short items (under 12 words each). Reply with JSON only, starting with '{'.")


VERIFY_SUFFIX = "+verify"
SYSTEM_VERIFY = ("You audit a vision model's JSON report about ONE camera frame, claim by claim, against that same frame. "
                 "Keep a claim only if the frame clearly shows it; soften what is plausible but not clearly visible "
                 "(\"appears to\", \"unclear\"); delete what the frame does not support: named small items, brands, text on "
                 "screens or labels, a person's role, an activity you cannot actually see. Fix the people count only if the "
                 "frame clearly contradicts it (partial people count). Never add anything. Reply with JSON only, no markdown.")
PROMPT_VERIFY = ('Return the corrected report with exactly the same keys ("people", "area", "activity", "issues") plus '
                 '"removed": ["<each claim you deleted>", ...] and "softened": ["<each claim you softened>", ...].\n\n'
                 'REPORT TO AUDIT:\n')


def verify_single(caller: Caller, model: str, parsed: dict[str, Any], frame: Path, context: str) -> dict[str, Any]:
    """Second call: the report + the frame -> the corrected report. Unparseable audit = report kept as it was."""
    content = [{"type": "text", "text": PROMPT_VERIFY + json.dumps(parsed)[:2500] + "\n\nContext: " + context}, _img(frame)]
    res = caller.chat(model, SYSTEM_VERIFY, content, max_tokens=1500)
    j = _extract_json(res.get("text", "")) if "text" in res else None
    out = {"verify_latency": res.get("latency"), "verify_cost": res.get("cost"), "verify_tokens": res.get("prompt_tokens")}
    if not j or not isinstance(j.get("activity"), str):
        return {**out, "verify_error": res.get("error") or "audit not parseable", "removed": [], "softened": []}
    fixed = {k: j.get(k, parsed.get(k)) for k in ("people", "area", "activity", "issues")}
    return {**out, "parsed": fixed, "removed": j.get("removed") or [], "softened": j.get("softened") or []}


def _b64(p: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()


def _img(p: Path) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": _b64(p)}}


def _key() -> tuple[str, str]:
    providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    pc = (providers.get("providers") or {}).get("openrouter") or {}
    import os
    key = (pc.get("api_key") or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise SystemExit("no OpenRouter key (config/providers.json openrouter.api_key or OPENROUTER_API_KEY)")
    return (pc.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/"), key


def _judge_parse(text: str) -> dict[str, Any]:
    """Parse the judge's JSON; if it was truncated, salvage the scalar grades at least."""
    v = _extract_json(text)
    if v is not None:
        return v
    out: dict[str, Any] = {}
    m = re.search(r'"accuracy"\s*:\s*(\d)', text or "")
    if m:
        out["accuracy"] = int(m.group(1))
    for k in ("area_ok", "busiest_ok"):
        m = re.search(rf'"{k}"\s*:\s*(true|false)', text or "")
        if m:
            out[k] = m.group(1) == "true"
    return out


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    for cand in (t, t[t.find("{"):t.rfind("}") + 1] if "{" in t else ""):
        if not cand:
            continue
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except Exception:
            continue
    return None


class Caller:
    def __init__(self, base: str, key: str, timeout: float = 180):
        self.base, self.key, self.timeout = base, key, timeout
        self._client = httpx.Client(timeout=timeout)
        self._lock = threading.Lock()

    def chat(self, model: str, system: str, content: list[dict[str, Any]], max_tokens: int = 4000) -> dict[str, Any]:
        from .providers import model_extras
        # thinking models (Gemini 3.x, Qwen 3.6, GLM 5) burn the budget on reasoning before the JSON: give them room,
        # ask for low effort (OpenRouter drops the field for models that have no such knob)
        payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.1, "usage": {"include": True},
                   "reasoning": {"effort": "low"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                   **model_extras(model)}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                   "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas vision eval"}
        last = ""
        for attempt in range(6):
            t0 = time.time()
            try:
                r = self._client.post(self.base + "/chat/completions", headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last = f"transport: {exc}"
                time.sleep(2 * (attempt + 1))
                continue
            dt = time.time() - t0
            if r.status_code in (429, 500, 502, 503, 504, 408):
                last = f"HTTP {r.status_code}: {r.text[:160]}"
                time.sleep((12 if r.status_code == 429 else 3) * (attempt + 1))
                continue
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}", "latency": dt}
            j = r.json()
            if "error" in j and not j.get("choices"):
                last = f"api: {json.dumps(j['error'])[:200]}"
                time.sleep(3 * (attempt + 1))
                continue
            try:
                msg = j["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                return {"error": f"no choices: {json.dumps(j)[:200]}", "latency": dt}
            text = msg.get("content") or ""
            if isinstance(text, list):
                text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
            if not text.strip() and msg.get("reasoning"):
                text = msg["reasoning"]
            u = j.get("usage") or {}
            return {"text": text, "latency": dt, "prompt_tokens": u.get("prompt_tokens"),
                    "completion_tokens": u.get("completion_tokens"), "cost": u.get("cost"),
                    "finish": (j["choices"][0].get("finish_reason") or ""), "raw_len": len(text)}
        return {"error": last or "gave up", "latency": 0.0}


# ---------------------------------------------------------------------------- jobs
def build_jobs(gt: dict[str, Any], sites: list[str], frames_per_cam: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for site in sites:
        s = gt["sites"][site]
        cams = s["cameras"]
        for cam, spec in cams.items():
            for i, f in enumerate(spec["frames"][:frames_per_cam], start=1):
                p = EVAL_DIR / "frames" / f"{cam}_{i}.jpg"
                if p.is_file():
                    jobs.append({"task": "single", "site": site, "camera": cam, "frame": i, "path": str(p), "gt": f,
                                 "area": spec["area"], "context": s["context"]})
        mid = min(2, frames_per_cam)
        paths = {cam: EVAL_DIR / "frames" / f"{cam}_{mid}.jpg" for cam in cams}
        if all(p.is_file() for p in paths.values()):
            jobs.append({"task": "multicam", "site": site, "paths": {c: str(p) for c, p in paths.items()},
                         "gt": s["multicam"], "context": s["context"]})
        if "temporal" in s and frames_per_cam >= 3:
            for cam, desc in s["temporal"].items():
                ps = [EVAL_DIR / "frames" / f"{cam}_{i}.jpg" for i in (1, 2, 3)]
                if all(p.is_file() for p in ps):
                    jobs.append({"task": "temporal", "site": site, "camera": cam, "paths": [str(p) for p in ps],
                                 "gt": {"desc": desc, "people": [f["people"] for f in cams[cam]["frames"]]},
                                 "context": s["context"]})
    return jobs


def run_job(caller: Caller, model: str, job: dict[str, Any]) -> dict[str, Any]:
    ctx = f"Context: {job['context']}"
    verify = model.endswith(VERIFY_SUFFIX)
    model_id = model[:-len(VERIFY_SUFFIX)] if verify else model
    if job["task"] == "single":
        content = [{"type": "text", "text": PROMPT_SINGLE + "\n\n" + ctx}, _img(Path(job["path"]))]
        res = caller.chat(model_id, SYSTEM_SINGLE, content)
    elif job["task"] == "multicam":
        content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT_MULTI + "\n\n" + ctx +
                                          f"\nCameras: {', '.join(job['paths'])}."}]
        for cam, p in job["paths"].items():
            content.append({"type": "text", "text": f"[camera: {cam}]"})
            content.append(_img(Path(p)))
        res = caller.chat(model_id, SYSTEM_MULTI, content, max_tokens=6000)
    else:
        content = [{"type": "text", "text": PROMPT_TEMPORAL + "\n\n" + ctx}]
        for i, p in enumerate(job["paths"], start=1):
            content.append({"type": "text", "text": f"[frame {i} of 3]"})
            content.append(_img(Path(p)))
        res = caller.chat(model_id, SYSTEM_TEMPORAL, content)
    out = {k: v for k, v in job.items() if k not in ("gt", "context")}
    out["model"] = model
    out.update(res)
    out["parsed"] = _extract_json(res.get("text", "")) if "text" in res else None
    if verify and job["task"] == "single" and isinstance(out["parsed"], dict):
        v = verify_single(caller, model_id, out["parsed"], Path(job["path"]), job["context"])
        out["draft"] = out["parsed"]
        out.update(v)
        out["cost"] = (out.get("cost") or 0) + (v.get("verify_cost") or 0)
        out["latency"] = (out.get("latency") or 0) + (v.get("verify_latency") or 0)
    return out


# ---------------------------------------------------------------------------- scoring
def _hazard_hits(groups: list[list[str]], text: str) -> tuple[int, int]:
    low = text.lower()
    hit = sum(1 for g in groups if any(k.lower() in low for k in g))
    return hit, len(groups)


def judge_single(caller: Caller, judge: str, gt: dict[str, Any], area: str, parsed: dict[str, Any]) -> dict[str, Any]:
    q = (f"HUMAN description of the frame:\nArea: {area}\n{gt['desc']}\n\nMODEL answer:\n{json.dumps(parsed)[:1500]}\n\n"
         'Grade as JSON: {"accuracy": <0-5: 5 = everything right, 3 = mostly right with a wrong detail, 0 = wrong scene>, '
         '"area_ok": <true|false: did it identify the station/area correctly>, '
         '"hallucinations": ["<each concrete thing the model claims that the human did NOT see>", ...], '
         '"missed": ["<important things the human saw that the model missed>", ...]}')
    res = caller.chat(judge, JUDGE_SYSTEM, [{"type": "text", "text": q}], max_tokens=900)
    return {"judge_raw": res.get("text", res.get("error")), "judge_cost": res.get("cost"), **_judge_parse(res.get("text", ""))}


def judge_multicam(caller: Caller, judge: str, gt: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    q = (f"HUMAN description of the whole site from all cameras:\n{gt['desc']}\nBusiest area (any of): {gt['busiest']}\n"
         f"Service in progress: {gt['service_in_progress']}\n\nMODEL answer:\n{json.dumps(parsed)[:2500]}\n\n"
         'Grade as JSON: {"accuracy": <0-5 for the summary + per-camera lines>, "busiest_ok": <true|false>, '
         '"hallucinations": ["..."], "missed": ["..."]}')
    res = caller.chat(judge, JUDGE_SYSTEM, [{"type": "text", "text": q}], max_tokens=900)
    return {"judge_raw": res.get("text", res.get("error")), "judge_cost": res.get("cost"), **_judge_parse(res.get("text", ""))}


def judge_temporal(caller: Caller, judge: str, gt: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    q = (f"HUMAN description of what changed across the three frames:\n{gt['desc']}\n\nMODEL answer:\n{json.dumps(parsed)[:1500]}\n\n"
         'Grade as JSON: {"accuracy": <0-5>, "hallucinations": ["..."], "missed": ["..."]}')
    res = caller.chat(judge, JUDGE_SYSTEM, [{"type": "text", "text": q}], max_tokens=900)
    return {"judge_raw": res.get("text", res.get("error")), "judge_cost": res.get("cost"), **_judge_parse(res.get("text", ""))}


def score(caller: Caller, judge: str, job: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    sc: dict[str, Any] = {}
    p = out.get("parsed")
    if "error" in out or p is None:
        sc["ok"] = False
        return sc
    sc["ok"] = True
    if job["task"] == "single":
        gt = job["gt"]
        lo, hi = gt["people"]
        try:
            n = int(p.get("people"))
        except (TypeError, ValueError):
            n = None
        sc["people"] = n
        sc["people_ok"] = n is not None and lo <= n <= hi
        sc["people_near"] = n is not None and lo - 1 <= n <= hi + 1
        issues = p.get("issues") or []
        text = " ".join(map(str, issues)) if isinstance(issues, list) else str(issues)
        hit, tot = _hazard_hits(gt.get("hazards", []), text + " " + str(p.get("activity", "")))
        sc["hazard_hit"], sc["hazard_total"] = hit, tot
        sc["issues_reported"] = len(issues) if isinstance(issues, list) else 1
        sc.update(judge_single(caller, judge, gt, job["area"], p))
    elif job["task"] == "multicam":
        gt = job["gt"]
        sc["service_ok"] = bool(p.get("service_in_progress")) == bool(gt["service_in_progress"])
        try:
            sc["distinct_people"] = int(p.get("distinct_people"))
        except (TypeError, ValueError):
            sc["distinct_people"] = None
        if "distinct_people" in gt and sc["distinct_people"] is not None:
            sc["distinct_ok"] = sc["distinct_people"] == gt["distinct_people"]
        sc.update(judge_multicam(caller, judge, gt, p))
    else:
        gt = job["gt"]
        ppf = p.get("people_per_frame") or []
        oks = []
        for (lo, hi), v in zip(gt["people"], ppf if isinstance(ppf, list) else []):
            try:
                oks.append(lo <= int(v) <= hi)
            except (TypeError, ValueError):
                oks.append(False)
        sc["people_frames_ok"] = sum(oks)
        sc["people_frames_total"] = len(gt["people"])
        sc.update(judge_temporal(caller, judge, gt, p))
    return sc


# ---------------------------------------------------------------------------- report
def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.mean(xs), 2) if xs else None


def summarise(rows: list[dict[str, Any]], models: list[str]) -> list[dict[str, Any]]:
    table = []
    for m in models:
        R = [r for r in rows if r["model"] == m]
        S = [r for r in R if r["task"] == "single"]
        M = [r for r in R if r["task"] == "multicam"]
        T = [r for r in R if r["task"] == "temporal"]
        ok = [r for r in S if r["score"].get("ok")]
        hz_hit = sum(r["score"].get("hazard_hit", 0) for r in ok)
        hz_tot = sum(r["score"].get("hazard_total", 0) for r in ok)
        halls = [len(r["score"].get("hallucinations") or []) for r in ok if isinstance(r["score"].get("hallucinations"), list)]
        lat = [r.get("latency") for r in R if r.get("latency")]
        cost = sum(r.get("cost") or 0 for r in R)
        row = {
            "model": m,
            "calls": len(R),
            "failed": sum(1 for r in R if not r["score"].get("ok")),
            "count_exact": round(100 * sum(1 for r in ok if r["score"].get("people_ok")) / len(ok)) if ok else None,
            "count_near": round(100 * sum(1 for r in ok if r["score"].get("people_near")) / len(ok)) if ok else None,
            "hazard_recall": round(100 * hz_hit / hz_tot) if hz_tot else None,
            "area_ok": round(100 * sum(1 for r in ok if r["score"].get("area_ok")) / len(ok)) if ok else None,
            "judge_single": _mean([r["score"].get("accuracy") for r in ok]),
            "halluc_per_answer": _mean(halls),
            "issues_per_frame": _mean([r["score"].get("issues_reported") for r in ok]),
            "multicam_judge": _mean([r["score"].get("accuracy") for r in M if r["score"].get("ok")]),
            "multicam_service_ok": sum(1 for r in M if r["score"].get("service_ok")),
            "multicam_busiest_ok": sum(1 for r in M if r["score"].get("busiest_ok")),
            "temporal_judge": _mean([r["score"].get("accuracy") for r in T if r["score"].get("ok")]),
            "temporal_counts": (sum(r["score"].get("people_frames_ok", 0) for r in T), sum(r["score"].get("people_frames_total", 0) for r in T)),
            "latency_p50": round(statistics.median(lat), 1) if lat else None,
            "latency_max": round(max(lat), 1) if lat else None,
            "cost_usd": round(cost, 4),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in R),
        }
        table.append(row)
    return table


def write_report(outdir: Path, table: list[dict[str, Any]], rows: list[dict[str, Any]], args, judge_cost: float) -> None:
    lines = ["# Multi-camera restaurant eval: vision models on real feeds", "",
             f"Run: {outdir.name}. Sites: {', '.join(args.sites)}. Judge: `{args.judge}`. "
             f"Frames per camera: {args.frames}. Single-frame calls per model: {sum(1 for r in rows if r['model']==table[0]['model'] and r['task']=='single') if table else 0}.", "",
             "Scores: count_exact = % of frames where the people count fell inside the human range; count_near = within one of it; "
             "hazard_recall = % of human-flagged issues the model mentioned; area_ok = judge says the station was identified; "
             "judge = 0-5 accuracy vs the human description; halluc = invented details per answer (judge-counted); "
             "issues/frame = how many issues the model raised (over-flagging shows here). multicam = one call with every camera of a site. "
             "temporal = three frames of one camera over time (kitchen site).", "",
             "| model | fail | count exact | count near | hazard recall | area ok | judge | halluc | issues/frame | multicam judge | service ok | busiest ok | temporal judge | temporal counts | p50 s | max s | cost $ |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in table:
        tc = f"{r['temporal_counts'][0]}/{r['temporal_counts'][1]}" if r["temporal_counts"][1] else "-"
        n_sites = len(args.sites)
        lines.append(f"| {r['model']} | {r['failed']}/{r['calls']} | {r['count_exact']}% | {r['count_near']}% | {r['hazard_recall']}% | "
                     f"{r['area_ok']}% | {r['judge_single']} | {r['halluc_per_answer']} | {r['issues_per_frame']} | {r['multicam_judge']} | "
                     f"{r['multicam_service_ok']}/{n_sites} | {r['multicam_busiest_ok']}/{n_sites} | {r['temporal_judge']} | {tc} | "
                     f"{r['latency_p50']} | {r['latency_max']} | {r['cost_usd']} |")
    lines += ["", f"Judge cost: ${judge_cost:.4f}. Total model cost: ${sum(r['cost_usd'] for r in table):.4f}.", ""]

    # per-camera judge accuracy heatmap
    cams = sorted({r["camera"] for r in rows if r["task"] == "single"})
    lines += ["## Judge accuracy per camera (mean 0-5)", "", "| model | " + " | ".join(cams) + " |", "|---|" + "---|" * len(cams)]
    for t in table:
        cells = []
        for c in cams:
            xs = [r["score"].get("accuracy") for r in rows if r["model"] == t["model"] and r["task"] == "single" and r["camera"] == c and r["score"].get("ok")]
            cells.append(str(_mean(xs)) if xs else "-")
        lines.append(f"| {t['model']} | " + " | ".join(cells) + " |")

    # multicam answers verbatim (short)
    lines += ["", "## Multi-camera summaries (verbatim)", ""]
    for r in rows:
        if r["task"] != "multicam":
            continue
        p = r.get("parsed") or {}
        lines.append(f"**{r['model']}** on {r['site']} (judge {r['score'].get('accuracy')}, busiest: {p.get('busiest')!r}, "
                     f"service: {p.get('service_in_progress')!r}, people: {p.get('distinct_people')!r}): {p.get('summary') or r.get('error') or r.get('text','')[:300]}")
        lines.append("")

    # worst hallucinations
    lines += ["## Hallucinations the judge flagged (sample)", ""]
    for t in table:
        hs = []
        for r in rows:
            if r["model"] == t["model"] and isinstance(r["score"].get("hallucinations"), list):
                hs += [f"{r.get('camera', r['site'])}: {h}" for h in r["score"]["hallucinations"]]
        if hs:
            lines.append(f"- **{t['model']}** ({len(hs)}): " + "; ".join(hs[:6]))
    lines += ["", "## Failures", ""]
    for r in rows:
        if not r["score"].get("ok"):
            lines.append(f"- {r['model']} {r['task']} {r.get('camera', r['site'])}#{r.get('frame','')}: {r.get('error') or 'unparseable answer: ' + (r.get('text') or '')[:120]!r}")
    (outdir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas vision-eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated OpenRouter model ids")
    ap.add_argument("--judge", default=DEFAULT_JUDGE)
    ap.add_argument("--sites", default="crownshy,kitchen")
    ap.add_argument("--frames", type=int, default=3, help="frames per camera (1-3)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--smoke", action="store_true", help="1 frame per camera, first two models, no temporal")
    ap.add_argument("--verify", action="store_true", help="run each model as '<model>+verify': the single-frame report is audited "
                    "by a second call against the same frame before judging (compare with a plain run via --merge)")
    ap.add_argument("--out", default="")
    ap.add_argument("--resume", default="", help="results.json of an earlier run: keep its successful rows, rerun only the failed ones")
    ap.add_argument("--merge", default="", help="comma-separated results.json files: no model calls, merge (later files win per row) and write the report to --out")
    args = ap.parse_args(argv)
    args.sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.smoke:
        args.frames, models = 1, models[:2]
    if args.verify:
        models = [m if m.endswith(VERIFY_SUFFIX) else m + VERIFY_SUFFIX for m in models]

    if args.merge:
        merged: dict[tuple, dict[str, Any]] = {}
        for f in args.merge.split(","):
            for r in json.loads(Path(f.strip()).read_text(encoding="utf-8")):
                k = (r["model"], r["task"], r.get("camera") or r["site"], r.get("frame"))
                if k not in merged or r["score"].get("ok") or not merged[k]["score"].get("ok"):
                    merged[k] = r
        rows = list(merged.values())
        models = [m for m in models if any(r["model"] == m for r in rows)]
        outdir = Path(args.out or (WORKSPACE_DIR / "vision-eval" / ("merged-" + time.strftime("%Y%m%d-%H%M%S"))))
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "results.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
        table = summarise(rows, models)
        judge_cost = sum((r["score"].get("judge_cost") or 0) for r in rows)
        (outdir / "summary.json").write_text(json.dumps({"models": table, "judge": args.judge, "sites": args.sites,
                                                          "frames": args.frames, "judge_cost": judge_cost}, indent=1), encoding="utf-8")
        write_report(outdir, table, rows, args, judge_cost)
        print(f"merged {len(rows)} rows -> {outdir / 'report.md'}")
        for t in table:
            print(f"{t['model']:45s} fail {t['failed']}/{t['calls']}  count {t['count_exact']}%/{t['count_near']}%  hazard {t['hazard_recall']}%  "
                  f"judge {t['judge_single']}  halluc {t['halluc_per_answer']}  multicam {t['multicam_judge']}  temporal {t['temporal_judge']}  "
                  f"p50 {t['latency_p50']}s  ${t['cost_usd']}")
        return 0

    gt = json.loads((EVAL_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    jobs = build_jobs(gt, args.sites, args.frames)
    base, key = _key()
    caller = Caller(base, key)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.out) if args.out else WORKSPACE_DIR / "vision-eval" / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} jobs x {len(models)} models -> {outdir}", flush=True)

    rows: list[dict[str, Any]] = []
    lock = threading.Lock()
    done = 0
    keep: set[tuple[str, str, str, Any]] = set()
    if args.resume:
        for r in json.loads(Path(args.resume).read_text(encoding="utf-8")):
            if r["model"] in models and r["score"].get("ok"):
                rows.append(r)
                keep.add((r["model"], r["task"], r.get("camera") or r["site"], r.get("frame")))
        print(f"resume: keeping {len(rows)} successful rows", flush=True)

    def work(model: str, job: dict[str, Any]) -> dict[str, Any]:
        out = run_job(caller, model, job)
        out["score"] = score(caller, args.judge, job, out)
        return out

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, m, j) for m in models for j in jobs
                if (m, j["task"], j.get("camera") or j["site"], j.get("frame")) not in keep]
        for f in as_completed(futs):
            r = f.result()
            with lock:
                rows.append(r)
                done += 1
                tag = r.get("camera") or r["site"]
                st = "ERR " + str(r.get("error"))[:60] if "error" in r else ("judge=" + str(r["score"].get("accuracy")))
                print(f"[{done}/{len(futs)}] {r['model']:45s} {r['task']:8s} {tag:22s} {r.get('latency', 0):5.1f}s ${(r.get('cost') or 0):.4f} {st}", flush=True)
            if done % 20 == 0:
                (outdir / "results.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")

    for r in rows:  # keep the report readable: no base64, no giant raw text
        r.pop("parsed_raw", None)
    (outdir / "results.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    table = summarise(rows, models)
    judge_cost = sum((r["score"].get("judge_cost") or 0) for r in rows)
    (outdir / "summary.json").write_text(json.dumps({"models": table, "judge": args.judge, "sites": args.sites,
                                                      "frames": args.frames, "judge_cost": judge_cost}, indent=1), encoding="utf-8")
    write_report(outdir, table, rows, args, judge_cost)
    print(f"\nreport: {outdir / 'report.md'}")
    for t in table:
        print(f"{t['model']:45s} fail {t['failed']}/{t['calls']}  count {t['count_exact']}%/{t['count_near']}%  hazard {t['hazard_recall']}%  "
              f"judge {t['judge_single']}  halluc {t['halluc_per_answer']}  multicam {t['multicam_judge']}  temporal {t['temporal_judge']}  "
              f"p50 {t['latency_p50']}s  ${t['cost_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
