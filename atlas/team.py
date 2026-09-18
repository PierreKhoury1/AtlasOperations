"""Team architect: a real, validated agent team designed for THIS business and THIS task.

Two paths already existed (the Design Studio chat and the lead's `assemble_team` tool); both were flat (every
specialist reported to Atlas) and neither checked the shape the model returned. This module is the shared core:

  design_team(business, task, provider, ...)   live model -> JSON team -> validate -> one repair turn -> team
  demo_team(business, task)                    deterministic team from task keywords (demo mode, tests)
  validate_team(raw, ...)                      the contract: ids, hierarchy (Atlas -> leads -> members, depth <= 2),
                                               tools from the allow-list, engines that exist, instructions per role
  team_to_agents(team, business, tier)         engine agent configs: leads get `delegate` scoped to their members,
                                               members get `reports_to`, every prompt carries the standing orders
  workflow_for(team)                           an ordered workflow from the team's plan

A sub-team is a lead (reports_to atlas) with 2-4 members (reports_to that lead). The orchestrator lets a lead
delegate only to its own members and shows Atlas the leads with their members nested, so the structure is real at
run time, not just a drawing.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from . import templates as T
from . import tools as TL

MAX_AGENTS = 8
MAX_DEPTH = 2                      # atlas(0) -> lead(1) -> member(2); matches orchestration.max_delegation_depth
LEAD_TOOLS = ["delegate", "list_agents", "save_deliverable", "read_file", "list_files"]

# tools a designed specialist may hold (approvals still gate outbound ones). delegate/list_agents are granted to leads only.
ALLOWED_TOOLS = ["read_file", "list_files", "web_fetch", "run_python", "save_deliverable", "browse", "crm_lookup",
                 "crm_update", "queue_action", "camera_look", "camera_events", "camera_ask", "video_describe",
                 "remember", "recall", "http_request", "calendar_free_slots", "calendar_book", "generate_media", "mcp"]
NEVER_TOOLS = {"finish", "assemble_team"}

PALETTE = ["#7c3aed", "#db2777", "#1f9d63", "#b45309", "#0e7490", "#6d28d9", "#ea580c", "#15803d", "#a21caf", "#0369a1"]

ARCHITECT_SYSTEM = """You are the team architect for Atlas, an AI operations desk. Given a business and a job, you design the
smallest team of AI agents that will do that job well, and the order of work. Atlas (id "atlas") is the root
orchestrator: it briefs the team, reviews the work and holds every outbound action for the owner's approval.

Design rules
- 1 to {max_agents} agents. Prefer fewer. Every agent must earn its place with a distinct responsibility.
- Flat by default: agents report to "atlas". Use a SUB-TEAM only when the work naturally splits into parallel
  strands that need their own coordinator (a research pod covering several markets, one writer per channel,
  a data crew that reconciles several sources). A sub-team = one lead ("reports_to": "atlas") with 2-4 members
  ("reports_to": "<lead id>"). Max depth is atlas -> lead -> member. Leads coordinate, review and merge their
  members' work; they do not do the members' jobs.
- Each agent: "id" (a-z, 0-9, _, max 24), "name", "role" (3-6 words), "goal" (1-2 sentences: what it produces and
  the quality bar), "instructions" (3-6 standing orders written for THIS business and THIS task: what to check
  first, what it must never do, the exact shape it hands back), "tools" (from the list below), "engine",
  "strong" (true only where judgement or client-facing words are the product), "reports_to".
- "engine": "hermes_agent" only if it is available (see below) and the role must browse live sites, run code or
  shell, work through files over many steps, or remember a client between runs. Otherwise "atlas".
- Tools available: {tools}. Outbound tools (queue_action, calendar_book, http_request, browse) are approval-gated.
  Cameras: {cameras}.
- Never invent facts about the business. Where the brief is silent, make the agent ask or verify, not assume.
- Do NOT add Atlas, an "orchestrator", "coordinator" or "project manager" for the whole job: Atlas already briefs,
  reviews, approves and merges. Only sub-team leads coordinate, and only their own members.
- "workflow": the order of work as steps: [{{"agent": "<id>", "task": "what this step must produce"}}]. List leads,
  not their members (a lead runs its own members). Parallel steps: put them in one step with "agents": [ids].
- "deliverables": what the owner receives at the end. "checks": how Atlas verifies the job is done.
- "rationale": 2-3 sentences on why this shape.

Output: ONE JSON object inside <atlas-team>...</atlas-team>, nothing else:
<atlas-team>{{"rationale": "...", "agents": [...], "workflow": [...], "deliverables": [...], "checks": [...]}}</atlas-team>"""


def _slug(s: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(s or "").strip().lower()).strip("_")[:24]


def _loose_json(s: str) -> dict[str, Any] | None:
    m = re.search(r"<atlas-team>\s*(\{.*?\})\s*</atlas-team>", s, re.S)
    txt = m.group(1) if m else None
    if txt is None:
        a, b = s.find("{"), s.rfind("}")
        if a < 0 or b <= a:
            return None
        txt = s[a:b + 1]
    for cand in (txt, re.sub(r",\s*([}\]])", r"\1", txt)):
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- validation (the contract)

def validate_team(raw: Any, *, allowed_tools: list[str] | None = None, max_agents: int = MAX_AGENTS,
                  hermes_available: bool = False, existing_ids: tuple[str, ...] | list[str] = ()) -> tuple[dict[str, Any], list[str]]:
    """Coerce a model/user-produced team into the canonical shape and list every rule it broke.
    Returns (team, errors). The team is always usable (bad parts dropped or defaulted); errors tell the model
    what to fix on the repair turn and are surfaced to the owner as warnings."""
    errors: list[str] = []
    allowed = [t for t in (allowed_tools or ALLOWED_TOOLS) if t in TL.SCHEMAS or t == "mcp"]
    src = raw if isinstance(raw, dict) else {}
    agents_in = src.get("agents") if isinstance(src.get("agents"), list) else (raw if isinstance(raw, list) else [])
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, a in enumerate(agents_in):
        if not isinstance(a, dict):
            errors.append(f"agent #{i + 1} is not an object")
            continue
        aid = _slug(a.get("id") or a.get("name") or f"agent_{i + 1}")
        if not aid or aid in ("atlas", "system", "owner", "orchestrator", "coordinator", "atlas_lead", "team_lead"):
            errors.append(f"agent #{i + 1}: id {a.get('id')!r} is reserved or empty - Atlas already orchestrates the whole job, do not add another orchestrator")
            continue
        if aid in seen or aid in existing_ids:
            errors.append(f"agent id '{aid}' is used twice")
            continue
        seen.add(aid)
        tools_in = [str(t) for t in (a.get("tools") or []) if isinstance(t, str)]
        bad = [t for t in tools_in if t not in allowed and t not in ("delegate", "list_agents")]
        if bad:
            errors.append(f"{aid}: unknown or disallowed tools {bad} (allowed: {', '.join(allowed)})")
        tools = [t for t in tools_in if t in allowed and t not in NEVER_TOOLS]
        if not tools:
            tools = ["read_file", "list_files"]
        instr = a.get("instructions")
        if isinstance(instr, str):
            instr = [x.strip(" -•\t") for x in instr.splitlines()]
        instr = [str(x).strip()[:240] for x in (instr if isinstance(instr, list) else []) if str(x).strip()][:8]
        if len(instr) < 2:
            errors.append(f"{aid}: needs at least 2 standing orders in 'instructions' (has {len(instr)})")
        engine = "hermes_agent" if str(a.get("engine") or "").lower() in ("hermes_agent", "hermes") else "atlas"
        if engine == "hermes_agent" and not hermes_available:
            errors.append(f"{aid}: engine hermes_agent is not connected on this desk - using atlas")
            engine = "atlas"
        agents.append({
            "id": aid, "name": str(a.get("name") or aid.replace("_", " ").title())[:40],
            "role": str(a.get("role") or "Specialist")[:60], "goal": str(a.get("goal") or a.get("description") or "")[:600],
            "instructions": instr, "tools": tools, "engine": engine, "strong": bool(a.get("strong")),
            "reports_to": _slug(a.get("reports_to") or "atlas") or "atlas",
        })
    if not agents:
        errors.append("no agents")
    if len(agents) > max_agents:
        errors.append(f"team too large ({len(agents)}); max is {max_agents}. Merge roles.")
        agents = agents[:max_agents]
    ids = {a["id"] for a in agents}
    # hierarchy: every reports_to must exist, no cycles, depth <= MAX_DEPTH
    for a in agents:
        if a["reports_to"] != "atlas" and a["reports_to"] not in ids:
            errors.append(f"{a['id']}: reports_to '{a['reports_to']}' is not an agent - set to atlas")
            a["reports_to"] = "atlas"
    by_id = {a["id"]: a for a in agents}

    def depth(aid: str, trail: set[str]) -> int:
        a = by_id[aid]
        if a["reports_to"] == "atlas":
            return 1
        if aid in trail:
            return 99
        return 1 + depth(a["reports_to"], trail | {aid})

    for a in agents:
        d = depth(a["id"], set())
        if d >= 99:
            errors.append(f"{a['id']}: reporting cycle - set to atlas")
            a["reports_to"] = "atlas"
        elif d > MAX_DEPTH:
            errors.append(f"{a['id']}: reporting chain deeper than atlas -> lead -> member - set to atlas")
            a["reports_to"] = "atlas"
    for a in agents:
        mem = [b["id"] for b in agents if b["reports_to"] == a["id"]]
        if len(mem) == 1:                              # a pod of one is just a chain: flatten it to atlas
            errors.append(f"{a['id']}: a sub-team needs at least 2 members (had 1: {mem[0]}) - flattened, {mem[0]} now reports to atlas")
            by_id[mem[0]]["reports_to"] = "atlas"
    for a in agents:
        a["members"] = [b["id"] for b in agents if b["reports_to"] == a["id"]]
        a["lead"] = bool(a["members"])
        if a["lead"] and a["engine"] == "hermes_agent":
            errors.append(f"{a['id']}: a lead must run on the atlas engine (it needs delegate) - engine set to atlas")
            a["engine"] = "atlas"
    for i, a in enumerate(agents):
        a["color"] = PALETTE[i % len(PALETTE)]
    # workflow
    wf_in = src.get("workflow")
    if isinstance(wf_in, dict):
        wf_in = wf_in.get("steps")
    steps: list[dict[str, Any]] = []
    for i, st in enumerate(wf_in if isinstance(wf_in, list) else []):
        if isinstance(st, str):
            st = {"agent": st}
        if not isinstance(st, dict):
            continue
        many = st.get("agents") if isinstance(st.get("agents"), list) else [st.get("agent")]
        aids = [_slug(x) for x in many if _slug(x) in ids]
        if not aids:
            errors.append(f"workflow step #{i + 1} names no known agent")
            continue
        steps.append({"agents": aids, "task": str(st.get("task") or "")[:400]})
    if not steps:
        tops = [a["id"] for a in agents if a["reports_to"] == "atlas"]
        steps = [{"agents": [t], "task": ""} for t in tops]
    team = {
        "rationale": str(src.get("rationale") or "")[:600],
        "agents": agents,
        "workflow": steps,
        "deliverables": [str(x)[:200] for x in (src.get("deliverables") or []) if str(x).strip()][:8],
        "checks": [str(x)[:200] for x in (src.get("checks") or []) if str(x).strip()][:8],
        "designed_at": time.time(),
    }
    return team, errors


def tree(team: dict[str, Any]) -> str:
    """Text tree: atlas -> leads -> members."""
    by_id = {a["id"]: a for a in team.get("agents", [])}
    lines = ["atlas: Atlas — orchestrator"]

    def line(a: dict[str, Any], indent: int) -> str:
        eng = " [hermes]" if a.get("engine") == "hermes_agent" else ""
        lead = f" (leads {', '.join(a['members'])})" if a.get("members") else ""
        return "  " * indent + f"- {a['id']}: {a['name']} — {a['role']}{eng}{lead}"

    for a in team.get("agents", []):
        if a.get("reports_to", "atlas") == "atlas":
            lines.append(line(a, 1))
            for m in a.get("members", []):
                if m in by_id:
                    lines.append(line(by_id[m], 2))
    return "\n".join(lines)


# --------------------------------------------------------------------------- team -> engine config

def agent_prompt(a: dict[str, Any], biz: dict[str, Any], by_id: dict[str, dict[str, Any]] | None = None) -> str:
    lines = [f"You are {a['name']}, {a['role']} for {biz.get('name') or 'the business'}.",
             f"Your job: {a.get('goal') or a['role']}."]
    if biz.get("tone"):
        lines.append(f"House tone: {biz['tone']}")
    if biz.get("description"):
        lines.append(f"About the business: {biz['description']}")
    if a.get("instructions"):
        lines.append("Standing orders for this role:")
        lines.extend(f"- {x}" for x in a["instructions"])
    if a.get("members"):
        names = ", ".join(f"{m} ({by_id[m]['role']})" if by_id and m in by_id else m for m in a["members"])
        lines.append(f"You lead a sub-team: {names}. Brief each member with delegate(agent_id, task), run them in parallel "
                     "where the work is independent, review what comes back against the standing orders, and return ONE "
                     "merged result. Do not do their jobs yourself; do not delegate outside your team.")
    elif a.get("reports_to") and a["reports_to"] != "atlas":
        lines.append(f"You report to {a['reports_to']}. Hand back exactly what they asked for, in the shape they asked for.")
    lines.append("Be specific and concise. Never invent facts about the client; say what you assumed. "
                 "Do not include prices, fees or placeholders like [name] in anything customer-facing unless the task supplies them.")
    return "\n".join(lines)


def team_to_agents(team: dict[str, Any], business: dict[str, Any], tier: str = "free", keep_atlas: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Engine agent list: Atlas first (existing config kept when given), then leads and members."""
    by_id = {a["id"]: a for a in team.get("agents", [])}
    roster = "\n".join(
        f"- {a['id']}: {a['name']} — {a['role']}" + (f" (leads: {', '.join(a['members'])})" if a.get("members") else "")
        for a in team.get("agents", []) if a.get("reports_to", "atlas") == "atlas")
    plan = "\n".join(f"{i + 1}. {' + '.join(s['agents'])}: {s['task'] or 'as briefed'}" for i, s in enumerate(team.get("workflow", [])))
    extra = ("Team designed for this job:\n" + roster + (f"\n\nPlan:\n{plan}" if plan else "")
             + (f"\n\nDeliverables: {'; '.join(team['deliverables'])}" if team.get("deliverables") else "")
             + (f"\nDone means: {'; '.join(team['checks'])}" if team.get("checks") else "")
             + "\n\nYou lead this team: delegate to the agents listed above (leads run their own members), run independent "
               "strands in parallel, review what comes back, then merge. Never do a specialist's job yourself. Every "
               "customer-facing message goes through queue_action for owner approval. Keep CRM up to date with crm_update.")
    if keep_atlas:
        atlas = dict(keep_atlas)
        atlas["system_prompt"] = (atlas.get("system_prompt") or T._BASE_ATLAS_PROMPT).rstrip() + "\n\n" + extra
    else:
        atlas = T._atlas(extra)
    agents = [atlas]
    for a in team.get("agents", []):
        tools = list(a["tools"])
        if a.get("members"):
            tools = list(dict.fromkeys(LEAD_TOOLS + tools))
        ent = T._agent(a["id"], a["name"], a["role"], agent_prompt(a, business, by_id), tools=tools, color=a.get("color", ""))
        ent.update({"strong": bool(a.get("strong")), "engine": a.get("engine") or "atlas", "reports_to": a.get("reports_to", "atlas"),
                    "members": list(a.get("members") or []), "goal": a.get("goal", ""), "instructions": list(a.get("instructions") or [])})
        agents.append(ent)
    T.apply_tier(agents, tier)
    if tier == "best":
        for ag in agents:
            if ag.get("strong"):
                ag["model"] = T.STRONG_MODEL
    return agents


def workflow_for(team: dict[str, Any], name: str = "Designed plan") -> dict[str, Any] | None:
    steps = []
    for i, st in enumerate(team.get("workflow", [])):
        for aid in st["agents"]:
            head = (st["task"] + "\n\n" if st["task"] else "")
            tmpl = head + ("{task}" if i == 0 else "Task: {task}\n\nWork so far:\n{all}")
            steps.append({"agent": aid, "task": tmpl})
    if not steps:
        return None
    return {"id": "designed_plan", "name": name, "description": " -> ".join(" + ".join(s["agents"]) for s in team["workflow"]),
            "synthesize": True, "steps": steps, "trigger": {"kind": "manual", "detail": "designed for a job"}}


# --------------------------------------------------------------------------- live design

def _user_brief(business: dict[str, Any], task: str, existing: list[dict[str, Any]] | None) -> str:
    b = {k: business.get(k) for k in ("name", "tagline", "description", "services", "target_clients", "tone", "extra_context") if business.get(k)}
    parts = ["Business:\n" + json.dumps(b, ensure_ascii=False)[:2500], "Job:\n" + task.strip()[:3000]]
    if existing:
        parts.append("Specialists already on the desk (reuse an id only if the role truly matches; otherwise design fresh):\n"
                     + "\n".join(f"- {a['id']}: {a.get('role', '')}" for a in existing if a.get("id") != "atlas"))
    return "\n\n".join(parts)


def design_team(business: dict[str, Any], task: str, provider, model: str = "", *, allowed_tools: list[str] | None = None,
                hermes_available: bool = False, cameras: list[str] | None = None, existing: list[dict[str, Any]] | None = None,
                max_agents: int = MAX_AGENTS, on_token: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Ask the model for a team, validate, feed the errors back once. Returns {team, errors, warnings, turns}."""
    allowed = [t for t in (allowed_tools or ALLOWED_TOOLS) if t in TL.SCHEMAS or t == "mcp"]
    system = ARCHITECT_SYSTEM.format(max_agents=max_agents, tools=", ".join(allowed),
                                     cameras=", ".join(cameras) if cameras else
                                     "none connected yet (the owner adds them on the Cameras page; any agent whose job is "
                                     "to watch feeds still needs camera_look, camera_events and camera_ask)")
    system += ("\n\nHermes Agent runtime: " + ("AVAILABLE (engine hermes_agent allowed)." if hermes_available
                                              else "NOT connected - every agent must use engine \"atlas\"."))
    msgs = [provider.user_message(_user_brief(business, task, existing))]
    turns = 0
    team: dict[str, Any] = {}
    errors: list[str] = ["no answer"]
    warnings: list[str] = []
    raw = ""
    for attempt in range(2):
        turns += 1
        r = provider.chat(system, msgs, [], model or "", on_token=on_token)
        raw = r.text or ""
        data = _loose_json(raw)
        if data is None:
            errors = ["the reply contained no <atlas-team> JSON block"]
        else:
            team, errors = validate_team(data, allowed_tools=allowed, max_agents=max_agents, hermes_available=hermes_available)
        if not errors:
            break
        if attempt == 0:
            warnings = list(errors)
            msgs.append({"role": "assistant", "content": raw})
            msgs.append(provider.user_message("Your team broke these rules:\n- " + "\n- ".join(errors)
                                              + "\n\nFix them and output the COMPLETE corrected <atlas-team>{...}</atlas-team> block only."))
    if not team:
        team, _ = validate_team({"agents": []})
    team["source"] = {"model": model or getattr(provider, "default_model", ""), "turns": turns, "task": task.strip()[:500]}
    return {"team": team, "errors": errors, "warnings": warnings, "turns": turns, "raw": raw[-2000:]}


# --------------------------------------------------------------------------- demo design (no model)

_KINDS = [
    ("research", ("research", "compare", "competitor", "market", "find", "investigate", "analyse", "analyze", "review", "audit")),
    ("write", ("write", "draft", "email", "proposal", "reply", "message", "post", "newsletter", "pitch", "chase", "follow")),
    ("data", ("data", "spreadsheet", "reconcile", "invoice", "numbers", "report", "csv", "report", "forecast", "count")),
    ("cameras", ("camera", "cctv", "footage", "shop floor", "watch", "after hours", "video")),
    ("outreach", ("outreach", "prospect", "leads", "lead", "campaign", "cold")),
]


def demo_team(business: dict[str, Any], task: str, cameras: list[str] | None = None) -> dict[str, Any]:
    """Deterministic team from task keywords - shows the structure without a model (demo mode, tests)."""
    t = " " + re.sub(r"\s+", " ", task.lower()) + " "
    kinds = [k for k, words in _KINDS if any(w in t for w in words)] or ["research", "write"]
    name = business.get("name") or "the business"
    agents: list[dict[str, Any]] = []
    many = any(w in t for w in (" competitors", " markets", " regions", " channels", " several ", " all our ", " each ", " every "))
    m = re.search(r"\b(\d+)\s+(competitors|markets|regions|channels|suppliers|products)", t)
    if "research" in kinds and (many or m):
        n = min(4, max(2, int(m.group(1)) if m else 3))
        agents.append({"id": "research_lead", "name": "Research Lead", "role": "Runs the research pod", "reports_to": "atlas",
                       "goal": "Splits the research into strands, briefs the analysts, merges their findings into one brief with sources.",
                       "instructions": ["Split the job into independent strands and brief one analyst per strand",
                                        "Reject any finding without a source or a stated assumption",
                                        "Merge into one brief: facts, gaps, next questions"], "tools": ["read_file", "list_files", "save_deliverable"]})
        for i in range(n):
            agents.append({"id": f"analyst_{i + 1}", "name": f"Analyst {i + 1}", "role": "Researches one strand", "reports_to": "research_lead",
                           "goal": "Facts and figures for one strand, with sources.",
                           "instructions": ["Fetch primary sources first", "Separate verified facts from inference", "Return a bullet list with links"],
                           "tools": ["web_fetch", "read_file", "list_files"]})
    elif "research" in kinds:
        agents.append({"id": "researcher", "name": "Researcher", "role": "Finds and verifies facts", "reports_to": "atlas",
                       "goal": "A short, sourced brief answering the research part of the job.",
                       "instructions": ["Fetch sources before quoting figures", "Separate verified facts from inference", "Return bullets with links"],
                       "tools": ["web_fetch", "read_file", "list_files"]})
    if "data" in kinds:
        agents.append({"id": "analyst_data", "name": "Data Analyst", "role": "Reconciles and computes", "reports_to": "atlas",
                       "goal": "Correct numbers, computed with code, with the working shown.",
                       "instructions": ["Load the files from workspace/inputs", "Compute with run_python, never by hand", "Report totals with the method"],
                       "tools": ["run_python", "read_file", "list_files", "save_deliverable"]})
    if "cameras" in kinds:
        agents.append({"id": "watcher", "name": "Site Watcher", "role": "Reads the cameras", "reports_to": "atlas",
                       "goal": "What the cameras saw, with event ids cited.",
                       "instructions": ["Use camera_ask for questions about the past", "Use camera_look for right now", "Cite event ids"],
                       "tools": ["camera_ask", "camera_events", "camera_look"]})
    if "write" in kinds or "outreach" in kinds:
        agents.append({"id": "writer", "name": "Writer", "role": f"Writes in {name}'s voice", "reports_to": "atlas",
                       "goal": "Customer-ready drafts in the house tone, queued for approval.",
                       "instructions": ["Plain English, one next step per message", "No prices unless the task supplies them", "Queue anything outbound with queue_action"],
                       "tools": ["read_file", "list_files", "queue_action"]})
    agents.append({"id": "qa", "name": "QA Reviewer", "role": "Checks before Atlas signs off", "reports_to": "atlas",
                   "goal": "A pass/fail review with concrete fixes.",
                   "instructions": ["Check every claim against the sources", "Flag anything customer-facing that promises dates or money", "Return fixes, not opinions"],
                   "tools": ["read_file", "list_files"]})
    tops = [a["id"] for a in agents if a["reports_to"] == "atlas" and a["id"] != "qa"]
    wf = [{"agent": aid, "task": ""} for aid in tops] + [{"agent": "qa", "task": "Review everything produced so far."}]
    team, _ = validate_team({"rationale": f"Demo design from the job text ({', '.join(kinds)}). A live model designs for the real brief.",
                             "agents": agents, "workflow": wf, "deliverables": ["A saved deliverable answering the job"],
                             "checks": ["QA pass", "every outbound item queued for approval"]}, hermes_available=False)
    team["source"] = {"model": "demo", "turns": 0, "task": task.strip()[:500]}
    return team


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from . import config as cfg
    p = argparse.ArgumentParser(prog="atlas team", description="Design a team for a job (live model or --demo)")
    p.add_argument("task")
    p.add_argument("--business", default="", help="business name (default from config/business.json)")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--hermes", action="store_true", help="tell the architect a Hermes Agent runtime is connected")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    business = cfg.load_all().get("business", {})
    if args.business:
        business = {**business, "name": args.business}
    if args.demo:
        res = {"team": demo_team(business, args.task), "errors": [], "warnings": [], "turns": 0}
    else:
        from .providers import ProviderPool
        pool = ProviderPool(cfg.load("providers", cfg.DEFAULT_PROVIDERS))
        prov = pool.get()
        t0 = time.time()
        res = design_team(business, args.task, prov, "", hermes_available=args.hermes)
        res["seconds"] = round(time.time() - t0, 1)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        return 0
    team = res["team"]
    print(tree(team))
    print("\nplan:")
    for i, s in enumerate(team["workflow"]):
        print(f"  {i + 1}. {' + '.join(s['agents'])}: {s['task'] or 'as briefed'}")
    if team.get("rationale"):
        print("\nwhy: " + team["rationale"])
    for a in team["agents"]:
        print(f"\n{a['id']} tools={','.join(a['tools'])} engine={a['engine']}")
        for x in a["instructions"]:
            print(f"   - {x}")
    if res.get("warnings"):
        print("\nrepaired after first draft: " + "; ".join(res["warnings"]))
    if res.get("errors"):
        print("\nstill wrong: " + "; ".join(res["errors"]))
    if res.get("seconds"):
        print(f"\n{res['turns']} turn(s), {res['seconds']}s")
    return 0 if not res.get("errors") else 1
