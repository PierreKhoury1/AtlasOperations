"""Design Studio: a conversational solutions designer that turns a business conversation into a desk blueprint.

Flow
  1. The owner chats with the designer. Every designer reply carries a hidden machine block
     <atlas-design>{...}</atlas-design> with suggestion chips and the current blueprint draft.
  2. The blueprint (agents, hierarchy, tools, workflows, connectors, policy) grows turn by turn and is drawn
     live on the sketch canvas in the portal.
  3. `blueprint_to_desk` converts the approved blueprint into the desk config the engine runs.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable

from . import templates as T
from . import tools as TL

_BLOCK = re.compile(r"<atlas-design>\s*(\{.*?\})\s*</atlas-design>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

SPECIALIST_TOOLS = ["read_file", "list_files", "web_fetch", "run_python", "save_deliverable"]
ATLAS_TOOLS = ["delegate", "list_agents", "save_deliverable", "read_file", "list_files", "crm_lookup", "crm_update",
                "queue_action", "list_connectors", "http_request", "schedule_task", "mcp", "run_python", "remember", "recall"]
PALETTE = ["#7c3aed", "#db2777", "#1f9d63", "#b45309", "#0e7490", "#6d28d9", "#ea580c", "#15803d", "#a21caf", "#0369a1"]
STATUS_MARK = "\x00"                      # on_token prefix for a status line instead of prose
CONNECTOR_KINDS = ("smtp", "imap", "http", "mcp", "webhook", "hermes_agent")
TRIGGER_KINDS = ("webhook", "inbox", "schedule", "manual")

DESIGNER_SYSTEM = """You are the Atlas solutions designer at Atlas Ops, an AI-operations consultancy. You are on a scoping call
with a business owner. Your job: understand their business, find the highest-value processes to automate, and design
a "desk" — a team of AI agents run by an orchestrator called Atlas — that will do that work for them.

How to run the conversation
- Professional, plain English, no hype. 40-110 words per turn. One focused question per turn.
- Make concrete suggestions early. After the first answer you already know enough to sketch a first draft of the desk;
  refine it every turn instead of asking ten questions first.
- Prefer ONE function first (e.g. inbound lead handling, proposal writing, inbox triage, order follow-ups), then extras.
- Every turn, state briefly what changed in the blueprint ("Added a research agent that…").
- When the design is solid (usually after 3-5 exchanges), set "ready": true and tell the owner to review the sketch
  and press Approve & build.

Design rules
- Atlas (id "atlas") is always the root orchestrator; every other agent has "reports_to": "atlas".
- 2-6 specialist agents. Each agent: short id (a-z, _), name, role (3-6 words), goal (1-2 sentences: what it produces
  and the quality bar), tools (subset of: read_file, list_files, web_fetch, run_python, save_deliverable), and
  "strong": true only if the role needs top-tier judgement or client-facing writing.
- Workflows: 1-3, each with an id, name, trigger {"kind": webhook|inbox|schedule|manual, "detail": text} and ordered
  "steps": list of agent ids. Atlas reviews and approves outbound messages implicitly; do not list atlas in steps.
- Connectors the desk needs: kinds smtp (send email), imap (watch inbox), http (any REST API), mcp (tool server:
  Slack, Notion, Google Sheets, GitHub...), webhook (web forms, Zapier, Make). Each: {"kind", "name", "purpose", "required": bool}.
- Policy: {"no_money_figures": bool, "max_words": int, "banned_phrases": [..]}. Default no_money_figures true for
  anything customer-facing with quotes/prices.
- Business: {"name","tagline","description","services":[..],"target_clients","tone","sender_name","availability","pricing_notes"}.
  Fill what you know; leave unknown fields out (do not invent a sender name or pricing).

Output format — MANDATORY on every turn
Write your reply to the owner as plain prose first (no markdown headings, no bullet spam). Then, on a new line, append
exactly one machine block and nothing after it:

<atlas-design>{"suggestions": ["3-4 short reply options for the owner, max 8 words each"], "ready": false, "blueprint": { "business": {...}, "agents": [...], "workflows": [...], "connectors": [...], "policy": {...} }}</atlas-design>

The blueprint must be COMPLETE each time (full current state, not a diff). On the very first turn, before the owner
has said anything substantive, "blueprint" may be null."""

GREETING = ("Welcome. I design AI desks for businesses — a small team of agents, run by Atlas, that takes a whole process "
            "off your plate. Tell me what your business does and which task eats the most time each week: answering "
            "enquiries, writing proposals, chasing invoices, watching an inbox, anything repetitive. I will sketch the "
            "team as we talk.")
GREETING_SUGGESTIONS = ["We get enquiries we answer too slowly", "Proposals take us days to write",
                        "Our inbox needs triage every morning", "Customers need order updates"]


# ---------------------------------------------------------------------------- sessions
class DesignSession:
    def __init__(self, mode: str, tier: str = "free"):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.tier = tier
        self.created = time.time()
        self.messages: list[dict[str, Any]] = []         # provider-neutral {"role","content"} history
        self.transcript: list[dict[str, Any]] = [{"role": "assistant", "text": GREETING}]
        self.blueprint: dict[str, Any] | None = None
        self.suggestions: list[str] = list(GREETING_SUGGESTIONS)
        self.ready = False
        self.desk_id: int | None = None
        self.turn = 0
        self.links: list[str] = []
        self.profile: dict[str, Any] | None = None       # company profile from the pre-study of their links
        self.lock = threading.Lock()

    def public(self) -> dict[str, Any]:
        return {"sid": self.id, "mode": self.mode, "tier": self.tier, "transcript": self.transcript,
                "blueprint": self.blueprint, "suggestions": self.suggestions, "ready": self.ready,
                "desk_id": self.desk_id, "turn": self.turn, "links": self.links, "profile": self.profile}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "tier": self.tier, "created": self.created, "messages": self.messages,
                "transcript": self.transcript, "blueprint": self.blueprint, "suggestions": self.suggestions,
                "ready": self.ready, "desk_id": self.desk_id, "turn": self.turn, "links": self.links, "profile": self.profile}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DesignSession":
        s = cls(d.get("mode", "demo"), d.get("tier", "free"))
        for k in ("id", "created", "messages", "transcript", "blueprint", "suggestions", "ready", "desk_id", "turn", "links", "profile"):
            if k in d:
                setattr(s, k, d[k])
        return s

    def apply_profile(self, profile: dict[str, Any]) -> None:
        """Seed the conversation from a study of the owner's links: opening message, chips, first-draft blueprint."""
        self.profile = profile
        opening = profile.get("opening_message") or GREETING
        self.transcript = [{"role": "assistant", "text": opening}]
        self.messages = [{"role": "assistant", "content": opening}]
        self.suggestions = [str(x)[:60] for x in (profile.get("suggestions") or GREETING_SUGGESTIONS)][:4]
        bp = normalise(profile.get("blueprint"), None) if isinstance(profile.get("blueprint"), dict) else None
        if bp and not bp["business"].get("name") and profile.get("name"):
            bp["business"]["name"] = profile["name"]
        if bp and bp.get("agents"):
            self.blueprint = bp


SESSIONS: dict[str, DesignSession] = {}
_SESSIONS_MAX = 200


def new_session(mode: str, tier: str = "free") -> DesignSession:
    s = DesignSession(mode, tier)
    if len(SESSIONS) >= _SESSIONS_MAX:
        for k in sorted(SESSIONS, key=lambda k: SESSIONS[k].created)[: _SESSIONS_MAX // 4]:
            SESSIONS.pop(k, None)
    SESSIONS[s.id] = s
    return s


# ---------------------------------------------------------------------------- parsing + normalising
def split_reply(raw: str) -> tuple[str, dict[str, Any] | None]:
    """Return (prose, machine dict|None). Tolerates missing tags / fenced JSON / trailing junk."""
    raw = raw or ""
    m = _BLOCK.search(raw)
    data: dict[str, Any] | None = None
    prose = raw
    if m:
        prose = raw[: m.start()].strip()
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = _loose_json(m.group(1))
    else:
        f = list(_FENCE.finditer(raw))
        if f:
            last = f[-1]
            try:
                cand = json.loads(last.group(1))
                if isinstance(cand, dict) and ("blueprint" in cand or "suggestions" in cand):
                    data = cand
                    prose = (raw[: last.start()] + raw[last.end():]).strip()
            except json.JSONDecodeError:
                pass
        if data is None and "<atlas-design>" in raw:          # opened but never closed (cut off)
            prose = raw.split("<atlas-design>", 1)[0].strip()
    prose = re.sub(r"</?atlas-design>", "", prose).strip()
    return prose, data if isinstance(data, dict) else None


def _loose_json(s: str) -> dict[str, Any] | None:
    s = s.strip()
    for cut in range(len(s), max(0, len(s) - 400), -1):        # walk back to the last parseable prefix + closers
        chunk = s[:cut]
        for tail in ("", "}", "}}", "]}", "]}}", "\"}", "\"]}", "\"]}}"):
            try:
                v = json.loads(chunk + tail)
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                continue
    return None


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return s[:24] or "agent"


def normalise(bp: dict[str, Any] | None, prev: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Coerce a model-produced blueprint into the canonical shape; fall back to `prev` for missing sections."""
    if not isinstance(bp, dict):
        return prev
    prev = prev or {}
    out: dict[str, Any] = {}
    biz = bp.get("business") if isinstance(bp.get("business"), dict) else prev.get("business") or {}
    b: dict[str, Any] = {}
    for k in ("name", "tagline", "description", "target_clients", "tone", "sender_name", "availability", "pricing_notes"):
        v = biz.get(k)
        if isinstance(v, str) and v.strip():
            b[k] = v.strip()
    sv = biz.get("services")
    if isinstance(sv, str):
        sv = [x.strip() for x in sv.split(",")]
    if isinstance(sv, list):
        b["services"] = [str(x).strip() for x in sv if str(x).strip()][:10]
    out["business"] = b

    agents_in = bp.get("agents") if isinstance(bp.get("agents"), list) else prev.get("agents") or []
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, a in enumerate(agents_in):
        if not isinstance(a, dict):
            continue
        aid = _slug(a.get("id") or a.get("name") or f"agent_{i}")
        if aid in seen:
            continue
        seen.add(aid)
        tools = a.get("tools") if isinstance(a.get("tools"), list) else []
        tools = [t for t in tools if t in SPECIALIST_TOOLS] or ["read_file", "list_files"]
        agents.append({
            "id": aid,
            "name": str(a.get("name") or aid.replace("_", " ").title())[:40],
            "role": str(a.get("role") or "Specialist")[:60],
            "goal": str(a.get("goal") or a.get("description") or "")[:600],
            "tools": tools if aid != "atlas" else list(ATLAS_TOOLS),
            "reports_to": "atlas" if aid != "atlas" else "",
            "strong": bool(a.get("strong")),
            "engine": "hermes_agent" if str(a.get("engine") or "").lower() in ("hermes_agent", "hermes") else "atlas",
        })
    agents = [a for a in agents if a["id"] != "atlas"][:8]
    for i, a in enumerate(agents):
        a["color"] = PALETTE[i % len(PALETTE)]
    out["agents"] = agents
    ids = {a["id"] for a in agents}

    wfs_in = bp.get("workflows") if isinstance(bp.get("workflows"), list) else prev.get("workflows") or []
    wfs: list[dict[str, Any]] = []
    for i, w in enumerate(wfs_in):
        if not isinstance(w, dict):
            continue
        trig = w.get("trigger") if isinstance(w.get("trigger"), dict) else {"kind": str(w.get("trigger") or "manual")}
        kind = str(trig.get("kind") or "manual").lower()
        kind = {"email": "inbox", "form": "webhook", "cron": "schedule", "timer": "schedule", "api": "webhook"}.get(kind, kind)
        if kind not in TRIGGER_KINDS:
            kind = "manual"
        steps = w.get("steps") if isinstance(w.get("steps"), list) else []
        steps = [_slug(s if isinstance(s, str) else (s or {}).get("agent")) for s in steps]
        steps = [s for s in steps if s in ids]
        wfs.append({"id": _slug(w.get("id") or w.get("name") or f"workflow_{i}"),
                    "name": str(w.get("name") or f"Workflow {i + 1}")[:60],
                    "trigger": {"kind": kind, "detail": str(trig.get("detail") or "")[:200]},
                    "steps": steps})
    out["workflows"] = wfs[:4]

    cons_in = bp.get("connectors") if isinstance(bp.get("connectors"), list) else prev.get("connectors") or []
    cons: list[dict[str, Any]] = []
    for c in cons_in:
        if isinstance(c, str):
            c = {"kind": c}
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "").lower()
        kind = {"email": "smtp", "gmail": "imap", "inbox": "imap", "api": "http", "rest": "http", "form": "webhook", "zapier": "webhook"}.get(kind, kind)
        if kind not in CONNECTOR_KINDS:
            continue
        cons.append({"kind": kind, "name": str(c.get("name") or T_KIND_LABEL.get(kind, kind))[:40],
                     "purpose": str(c.get("purpose") or "")[:160], "required": bool(c.get("required", True))})
    out["connectors"] = cons[:8]

    pol_in = bp.get("policy") if isinstance(bp.get("policy"), dict) else prev.get("policy") or {}
    banned = pol_in.get("banned_phrases")
    if isinstance(banned, str):
        banned = [x.strip() for x in banned.split(",")]
    out["policy"] = {"no_money_figures": bool(pol_in.get("no_money_figures", True)),
                     "max_words": int(pol_in.get("max_words") or 220),
                     "banned_phrases": [str(x).strip() for x in (banned or []) if str(x).strip()][:20]}
    return out


T_KIND_LABEL = {"smtp": "Email sending", "imap": "Inbox", "http": "API", "mcp": "Tool server", "webhook": "Web form", "hermes_agent": "Hermes Agent"}


# ---------------------------------------------------------------------------- the turn
def _provider_for(mode: str, providers_cfg: dict[str, Any] | None):
    from .providers import ProviderPool
    if mode == "demo" or not providers_cfg:
        return None, ""
    pool = ProviderPool(providers_cfg)
    prov_name = providers_cfg.get("default_provider") or pool.default_name
    prov = pool.get(prov_name)
    model = T.STRONG_MODEL if prov_name == "openrouter" and False else ""      # tier-gated later; default model for now
    return prov, model


def reply(session: DesignSession, user_text: str, on_token: Callable[[str], None] | None = None,
          providers_cfg: dict[str, Any] | None = None, designer_model: str = "") -> dict[str, Any]:
    """Run one designer turn. Streams prose tokens through on_token (machine block is withheld)."""
    user_text = (user_text or "").strip()
    with session.lock:
        session.turn += 1
        session.transcript.append({"role": "user", "text": user_text})
        session.messages.append({"role": "user", "content": user_text})
        if session.mode == "demo":
            raw = _demo_turn(session, user_text, on_token)
        else:
            raw = _live_turn(session, providers_cfg, designer_model, on_token)
        prose, data = split_reply(raw)
        prose = re.sub(r"\*\*|__|^#+\s*", "", prose, flags=re.M).strip()      # no markdown in the chat column
        if not prose:
            prose = "Noted. I have updated the sketch — tell me more, or press Approve & build when it looks right."
        suggestions = []
        if data:
            sg = data.get("suggestions")
            if isinstance(sg, list):
                suggestions = [str(x).strip()[:60] for x in sg if str(x).strip()][:4]
            bp = normalise(data.get("blueprint"), session.blueprint)
            if bp and (bp.get("agents") or bp.get("business")):
                session.blueprint = bp
            if isinstance(data.get("ready"), bool):
                session.ready = data["ready"] and bool(session.blueprint and session.blueprint.get("agents"))
        session.suggestions = suggestions or session.suggestions
        session.transcript.append({"role": "assistant", "text": prose})
        session.messages.append({"role": "assistant", "content": raw})
        if len(session.messages) > 24:                       # keep the context bounded; blueprint state is in the last reply
            session.messages = session.messages[-16:]
        return {"text": prose, "suggestions": session.suggestions, "blueprint": session.blueprint, "ready": session.ready,
                "turn": session.turn}


def _live_turn(session: DesignSession, providers_cfg: dict[str, Any] | None, model: str,
               on_token: Callable[[str], None] | None) -> str:
    from .providers import ProviderPool
    pool = ProviderPool(providers_cfg or {})
    prov = pool.get()
    buf: list[str] = []
    gate = {"open": True}

    def tok(text: str, thinking: bool = False):
        if thinking or not on_token:
            return
        buf.append(text)
        if not gate["open"]:
            return
        joined = "".join(buf)
        cut = joined.find("<atlas")
        if cut >= 0:                                   # machine block starts: stop streaming prose
            gate["open"] = False
            on_token(STATUS_MARK + "Sketching the blueprint")
            return
        if "<" in text:                                # hold back a tag fragment so "<her" + "mes-design>" never leaks
            head = text.split("<", 1)[0]
            if head:
                on_token(head)
            gate["held"] = text[len(head):]
            return
        held = gate.pop("held", "")
        on_token(held + text)

    system = DESIGNER_SYSTEM
    if session.profile:
        prof = {k: session.profile.get(k) for k in ("name", "summary", "sector", "services", "locations", "customers", "team_hint",
                                                   "channels", "tech", "tone", "opportunities") if session.profile.get(k)}
        system += ("\n\nYou already studied the owner's public links (" + ", ".join(session.links[:3]) + "). Company profile - treat as known facts, "
                   "do not ask for them again, reference them naturally:\n" + json.dumps(prof, ensure_ascii=False)[:3500])
    if session.blueprint:
        system += "\n\nCurrent blueprint (update it, keep what still holds):\n" + json.dumps(session.blueprint, ensure_ascii=False)
    msgs = [prov.user_message(m["content"]) if m["role"] == "user" else {"role": "assistant", "content": m["content"]}
            for m in session.messages]
    raw = ""
    err = ""
    for attempt in range(2):
        try:
            r = prov.chat(system, msgs, [], model or "", on_token=tok)
            raw = r.text or ""
            break
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(2.0)
    if not raw and err:
        return (f"The model did not answer ({err}). Say that again in a moment — the sketch so far is kept."
                + chr(10) + '<atlas-design>{"suggestions": ["Continue"], "ready": false, "blueprint": null}</atlas-design>')
    if not raw.strip():
        raw = ("Understood. Could you tell me a little more about who your customers are and how they reach you?"
               + chr(10) + '<atlas-design>{"suggestions": ["Mostly by email", "Through our website form", "Phone and WhatsApp"], "ready": false, "blueprint": null}</atlas-design>')
    # repair pass: the prose came back without a usable machine block -> ask for the block alone (not streamed)
    _, data = split_reply(raw)
    if session.turn >= 1 and not (data and isinstance(data.get("blueprint"), dict)):
        try:
            fix = prov.chat(system, msgs + [{"role": "assistant", "content": raw},
                                           prov.user_message("Output ONLY the <atlas-design>{...}</atlas-design> block, nothing else. "
                                                             "Include your best FIRST-DRAFT blueprint for what has been said so far (make reasonable "
                                                             "assumptions; the owner can adjust it on the canvas). Keep the same suggestions.")],
                            [], model or "")
            _, data2 = split_reply(fix.text or "")
            if data2:
                raw = split_reply(raw)[0] + chr(10) + "<atlas-design>" + json.dumps(data2, ensure_ascii=False) + "</atlas-design>"
        except Exception:
            pass
    return raw


# ---------------------------------------------------------------------------- demo designer (no model)
_DEMO_KINDS = [
    ("sales", ("enquir", "lead", "quote", "book", "customer", "sales", "slow", "respond", "reply"), "sales_desk"),
    ("proposal", ("proposal", "pitch", "tender", "consult", "advis", "scope", "brief"), "consultancy"),
    ("inbox", ("inbox", "email", "triage", "mail", "support", "ticket"), "sales_desk"),
    ("orders", ("order", "shop", "store", "ship", "deliver", "ecom", "product"), "ecommerce"),
]


def _demo_blueprint(kind: str, biz_hint: str, turn: int) -> dict[str, Any]:
    base = {
        "sales": {
            "agents": [
                {"id": "research", "name": "Researcher", "role": "Lead & company research", "goal": "Find who the enquirer is, their company, size and public signals. Cite sources. At most 2 pages fetched.", "tools": ["web_fetch", "read_file", "list_files"]},
                {"id": "writer", "name": "Reply writer", "role": "First-response drafting", "goal": "Draft a short, specific first reply in the business tone with one clear next step. No prices, no placeholders.", "tools": ["read_file", "list_files"], "strong": True},
                {"id": "crm", "name": "CRM keeper", "role": "Pipeline hygiene", "goal": "Set the stage, log a one-line summary and a dated next action for every lead.", "tools": ["read_file", "list_files"]},
                {"id": "qa", "name": "Quality reviewer", "role": "Outbound quality gate", "goal": "Check every outbound draft against policy and tone; return fixes, not opinions.", "tools": ["read_file"], "strong": True},
            ],
            "workflows": [{"id": "inbound_lead", "name": "Inbound lead → researched reply", "trigger": {"kind": "webhook", "detail": "Website form / Zapier POST"}, "steps": ["research", "writer", "qa", "crm"]},
                          {"id": "followups", "name": "3-day follow-up chaser", "trigger": {"kind": "schedule", "detail": "Daily 08:30"}, "steps": ["writer", "qa"]}],
            "connectors": [{"kind": "webhook", "name": "Website form", "purpose": "New enquiries in", "required": True},
                           {"kind": "smtp", "name": "Email sending", "purpose": "Approved replies out", "required": True},
                           {"kind": "imap", "name": "Inbox", "purpose": "Replies and new email enquiries", "required": False}],
            "policy": {"no_money_figures": True, "max_words": 180, "banned_phrases": ["guaranteed", "best price"]},
        },
        "proposal": {
            "agents": [
                {"id": "research", "name": "Researcher", "role": "Client & market brief", "goal": "Build a one-page brief on the client, sector and the problem stated.", "tools": ["web_fetch", "read_file", "list_files"]},
                {"id": "strategy", "name": "Strategist", "role": "Approach & roadmap", "goal": "Recommend an approach with phases, deliverables and risks.", "tools": ["read_file"], "strong": True},
                {"id": "finance", "name": "Pricing analyst", "role": "Effort & pricing", "goal": "Estimate effort and produce fixed-fee and day-rate options.", "tools": ["run_python", "read_file"]},
                {"id": "proposal", "name": "Proposal writer", "role": "Client-facing proposal", "goal": "Write the complete proposal in the house tone, ready to send.", "tools": ["save_deliverable", "read_file"], "strong": True},
                {"id": "qa", "name": "Reviewer", "role": "Proposal review", "goal": "Review for clarity, promises and pricing consistency; return concrete fixes.", "tools": ["read_file"], "strong": True},
            ],
            "workflows": [{"id": "new_proposal", "name": "Brief → proposal", "trigger": {"kind": "manual", "detail": "Owner pastes the brief"}, "steps": ["research", "strategy", "finance", "proposal", "qa"]}],
            "connectors": [{"kind": "smtp", "name": "Email sending", "purpose": "Send approved proposals", "required": True},
                           {"kind": "mcp", "name": "Google Drive", "purpose": "Past proposals as reference", "required": False}],
            "policy": {"no_money_figures": False, "max_words": 900, "banned_phrases": ["guarantee"]},
        },
        "inbox": {
            "agents": [
                {"id": "triage", "name": "Triage agent", "role": "Classify & prioritise email", "goal": "Label each email (lead, support, invoice, spam), set urgency, extract the ask.", "tools": ["read_file"]},
                {"id": "writer", "name": "Reply drafter", "role": "Draft replies", "goal": "Draft replies for anything routine in the house tone; escalate the rest.", "tools": ["read_file"], "strong": True},
                {"id": "crm", "name": "CRM keeper", "role": "Log & next action", "goal": "Log every thread against the contact with a next action.", "tools": ["read_file"]},
            ],
            "workflows": [{"id": "inbox_triage", "name": "Inbox triage", "trigger": {"kind": "inbox", "detail": "Every 2 minutes, unread mail"}, "steps": ["triage", "writer", "crm"]}],
            "connectors": [{"kind": "imap", "name": "Inbox", "purpose": "Read unread mail", "required": True},
                           {"kind": "smtp", "name": "Email sending", "purpose": "Send approved replies", "required": True}],
            "policy": {"no_money_figures": True, "max_words": 160, "banned_phrases": []},
        },
        "orders": {
            "agents": [
                {"id": "orders", "name": "Order agent", "role": "Order status lookups", "goal": "Look up orders and shipments via the store API and summarise status.", "tools": ["read_file", "run_python"]},
                {"id": "writer", "name": "Customer writer", "role": "Customer updates", "goal": "Write short, warm status updates and delay apologies with a clear next step.", "tools": ["read_file"], "strong": True},
                {"id": "ops", "name": "Ops checker", "role": "Exceptions & escalation", "goal": "Flag stuck orders, refunds and anything needing a human.", "tools": ["read_file", "run_python"]},
            ],
            "workflows": [{"id": "order_update", "name": "Customer asks about an order", "trigger": {"kind": "inbox", "detail": "support@ inbox"}, "steps": ["orders", "writer"]},
                          {"id": "stuck_orders", "name": "Daily stuck-order sweep", "trigger": {"kind": "schedule", "detail": "Daily 07:00"}, "steps": ["orders", "ops", "writer"]}],
            "connectors": [{"kind": "http", "name": "Store API (Shopify)", "purpose": "Orders, shipments, refunds", "required": True},
                           {"kind": "imap", "name": "Support inbox", "purpose": "Customer questions in", "required": True},
                           {"kind": "smtp", "name": "Email sending", "purpose": "Updates out", "required": True}],
            "policy": {"no_money_figures": False, "max_words": 150, "banned_phrases": ["guaranteed"]},
        },
    }[kind]
    bp = json.loads(json.dumps(base))
    # grow with the conversation so the canvas animates in stages
    if turn == 1:
        bp["agents"] = bp["agents"][:2]
        bp["workflows"] = bp["workflows"][:1]
        bp["connectors"] = bp["connectors"][:1]
    elif turn == 2:
        bp["agents"] = bp["agents"][:3]
        bp["connectors"] = bp["connectors"][:2]
    bp["business"] = {"name": biz_hint} if biz_hint else {}
    return bp


def _demo_turn(session: DesignSession, text: str, on_token: Callable[[str], None] | None) -> str:
    low = text.lower()
    kind = getattr(session, "_demo_kind", None)
    if not kind:
        for k, words, _ in _DEMO_KINDS:
            if any(w in low for w in words):
                kind = k
                break
        kind = kind or "sales"
        session._demo_kind = kind  # type: ignore[attr-defined]
    name = getattr(session, "_demo_name", "")
    m = re.search(r"\b(?i:we are|we're|i run|i own|called|at)\s+([A-Z][\w&'\-]*(?: [A-Z][\w&'\-]*){0,3})", text)
    if m and not name:
        name = m.group(1).strip()
        session._demo_name = name  # type: ignore[attr-defined]
    turn = session.turn
    label = {"sales": "inbound enquiry desk", "proposal": "proposal desk", "inbox": "inbox triage desk", "orders": "order-care desk"}[kind]
    scripts = {
        1: (f"Understood — that points to an {label}. I have sketched the core: Atlas orchestrating, a researcher and a writer. "
            f"Every outbound message will wait for your approval, and the policy layer blocks prices and placeholders. "
            f"Who are your customers, and how do enquiries usually arrive — web form, email, phone?",
            ["Mostly through our website form", "By email to one inbox", "Phone and WhatsApp mostly", "A mix of all of these"]),
        2: ("Good. I have added a CRM keeper so every contact gets a stage and a dated next action, and wired the intake as the "
            "first trigger. What tone should the replies have, and is there anything the agents must never say or promise?",
            ["Warm and local, never pushy", "Formal and precise", "Never quote prices in writing", "No guarantees or timelines"]),
        3: ("Noted, added to policy. The team is complete: a quality reviewer now checks every draft before it reaches your "
            "approval queue, and a follow-up chaser runs on a schedule. Review the sketch — click any agent to adjust its role or "
            "tools — then press Approve & build. Next we connect your data live.",
            ["Approve & build", "Add a second workflow", "Rename an agent", "Explain the approval flow"]),
    }
    prose, sugg = scripts.get(turn, ("Adjusted. The sketch reflects it — approve when ready, or tell me what else to change.",
                                     ["Approve & build", "Add an agent", "Change the trigger"]))
    bp = _demo_blueprint(kind, name, turn)
    if turn >= 3 and "never" in low:
        bp["policy"]["banned_phrases"] = list(dict.fromkeys(bp["policy"]["banned_phrases"] + ["guarantee", "promise"]))
    if on_token:
        for w in prose.split(" "):
            on_token(w + " ")
            time.sleep(0.018)
    return prose + "\n<atlas-design>" + json.dumps({"suggestions": sugg, "ready": turn >= 3, "blueprint": bp}) + "</atlas-design>"


# ---------------------------------------------------------------------------- blueprint -> desk config
def _agent_prompt(a: dict[str, Any], biz: dict[str, Any]) -> str:
    lines = [f"You are {a['name']}, {a['role']} for {biz.get('name') or 'the business'}.",
             f"Your job: {a.get('goal') or a['role']}."]
    if biz.get("tone"):
        lines.append(f"House tone: {biz['tone']}")
    if biz.get("description"):
        lines.append(f"About the business: {biz['description']}")
    lines.append("Be specific and concise. Never invent facts about the client; say what you assumed. "
                 "Do not include prices, fees or placeholders like [name] in anything customer-facing unless the task supplies them.")
    return "\n".join(lines)


def blueprint_to_desk(bp: dict[str, Any], tier: str = "free") -> dict[str, Any]:
    """Turn an approved blueprint into {business, agents, workflows} for store.add_desk."""
    bp = normalise(bp) or {}
    biz_in = bp.get("business") or {}
    base = json.loads(json.dumps(T.CONSULTANCY["business"]))
    b = {**base, **biz_in}
    b["model"] = "custom"
    b["currency"] = b.get("currency") or "GBP"
    pol = bp.get("policy") or {}
    b["policy"] = {"no_money_figures": bool(pol.get("no_money_figures", True)), "max_words": int(pol.get("max_words") or 220),
                   "banned_phrases": list(pol.get("banned_phrases") or [])}
    roster = "\n".join(f"- {a['id']}: {a['name']} — {a['role']}" for a in bp.get("agents") or [])
    atlas_extra = ("Specialists on this desk:\n" + roster + "\n\nEvery customer-facing message goes through queue_action for owner "
                    "approval. Keep CRM up to date with crm_update.") if roster else ""
    agents = [T._atlas(atlas_extra)]
    for a in bp.get("agents") or []:
        agents.append(T._agent(a["id"], a["name"], a["role"], _agent_prompt(a, b), tools=a["tools"], color=a["color"]))
        agents[-1]["strong"] = bool(a.get("strong"))
        agents[-1]["engine"] = a.get("engine") or "atlas"
    strong_ids = {a["id"] for a in bp.get("agents") or [] if a.get("strong")}
    T.apply_tier(agents, tier)
    if tier == "best":
        for ag in agents:
            if ag["id"] in strong_ids:
                ag["model"] = T.STRONG_MODEL
    workflows = []
    for w in bp.get("workflows") or []:
        steps = []
        for i, sid in enumerate(w["steps"]):
            tmpl = "{task}" if i == 0 else "Task: {task}\n\nWork so far:\n{all}"
            steps.append({"agent": sid, "task": tmpl})
        if steps:
            workflows.append({"id": w["id"], "name": w["name"], "description": " -> ".join(w["steps"]),
                              "synthesize": True, "steps": steps, "trigger": w["trigger"]})
    return {"business": b, "agents": agents, "workflows": workflows, "blueprint": bp}
