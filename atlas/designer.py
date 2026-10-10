"""Design Studio: a conversational solutions designer that turns a business conversation into a desk blueprint.

Flow
  1. The owner chats with the designer. Every designer reply carries a hidden machine block
     <atlas-design>{...}</atlas-design> with three next steps (atlas/nextsteps.py) and the current blueprint draft.
  2. The blueprint (agents, hierarchy, tools, workflows, connectors, policy) grows turn by turn and is drawn
     live on the sketch canvas in the portal.
  3. `blueprint_to_desk` converts the approved blueprint into the desk config the engine runs.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import design_state as DST
from . import integrations as I
from . import nextsteps as NS
from . import templates as T
from . import tools as TL

TAG_OPEN, TAG_CLOSE = "<atlas-design>", "</atlas-design>"
_FENCE = re.compile(r"```(?:[Jj][Ss][Oo][Nn])?\s*(\{.*?\})\s*```", re.S)
# the keys a machine block may carry (the v2 contract, plus "suggestions", the legacy chip list)
BLOCK_KEYS = ("intent", "changes", "blueprint", "plan", "propose", "ask", "answered", "assume", "unassume", "next", "ready",
              "suggestions")

SPECIALIST_TOOLS = ["read_file", "list_files", "web_fetch", "run_python", "save_deliverable", "camera_look", "camera_events", "camera_ask",
                    "record_find", "record_get", "record_save", "crm_lookup", "browse", "calendar_free_slots", "recall"]
ATLAS_TOOLS = ["delegate", "list_agents", "save_deliverable", "read_file", "list_files", "crm_lookup", "crm_update",
                "queue_action", "list_connectors", "http_request", "schedule_task", "mcp", "run_python", "remember", "recall", "generate_media"]
PALETTE = ["#4c90f0", "#32a467", "#ec9a3c", "#9881f3", "#2ec4b6", "#e76a6e", "#68c1ee", "#d1980b", "#c274c2", "#8eb125"]   # muted (Blueprint)
STATUS_MARK = "\x00"                      # on_token prefix for a status line instead of prose
# every connector kind the desk can connect (cameras are the blueprint's own "cameras" list), named as the desk names it
CONNECTOR_KINDS = tuple(k for k in I.KINDS if k != "camera")
T_KIND_LABEL = {k: I.KINDS[k]["label"] for k in CONNECTOR_KINDS}
_KIND_ALIAS = {"email": "smtp", "gmail": "imap", "inbox": "imap", "api": "http", "rest": "http", "form": "webhook", "zapier": "webhook",
               "hermes": "hermes_agent", "sms": "twilio", "calendar": "gcal", "google_calendar": "gcal"}
TRIGGER_KINDS = ("webhook", "inbox", "schedule", "manual")
# a camera journal's cadence as the build sets it (app.py _build_cameras, read by atlas/journal.py): a note on each
# change, at most one every JOURNAL_MIN_GAP_S, at least one every JOURNAL_EVERY_S even when nothing changes, and a
# summary of the notes every JOURNAL_ROLLUP_MIN minutes. The build facts the designer is shown are written from these.
JOURNAL_EVERY_S = 30
JOURNAL_MIN_GAP_S = 8
JOURNAL_ROLLUP_MIN = 15

DESIGNER_SYSTEM = """You are Atlas, the orchestrator of an AI operations desk, talking to a business owner for the first time.
Your job: understand their business, find the highest-value processes to take off their plate, and assemble your own
team - the specialist agents you will brief, run and review - to do that work. Speak in the first person ("I'll add a
researcher who…"); you are the one who will lead this team.

How to run the conversation
- Professional, plain English, no hype, no persona talk. Design turns: 40-110 words, at most one focused question.
- The owner may also just ask you something (advice, a fact, how to price, how to word an email, what a term means).
  Then answer it properly, like a sharp analyst: specific, complete, as long as the answer needs (usually under 200
  words). Do not pivot to selling the desk, do not tack on a question about their business, and do not invent or change
  the team for it: set "intent": "answer" and keep "blueprint" null (the current sketch, if any, stays as it is).
- Make concrete suggestions early. After the first answer you already know enough to sketch a first draft of the desk;
  refine it every turn instead of asking ten questions first.
- Prefer ONE function first (e.g. inbound lead handling, proposal writing, inbox triage, order follow-ups), then extras.
- Every turn, state briefly what changed in the blueprint ("Added a research agent that…").
- If the owner asks what you understood, what you are doing, or why - answer that directly in your own words (what
  they told you, what you inferred, what you are unsure of), then ask your one question. Never answer with a stock line.
- Do not invent a team from a greeting or a question. Sketch a blueprint only once the owner has said something about
  their business or the work; until then "blueprint" is null.
- When the design is solid (usually after 3-5 exchanges), set "ready": true and tell the owner to review the sketch
  and press Approve & build.

Design rules
- Atlas (id "atlas") is always the root orchestrator. Every other agent has "reports_to": "atlas", except members of a
  sub-team, whose "reports_to" is their lead's id. Use a sub-team (one lead + 2-4 members, max depth atlas -> lead ->
  member) only when the work naturally splits into parallel strands with their own coordinator (a research pod over
  several markets, one writer per channel). Flat is the default.
- 2-6 specialist agents, each defined by its FUNCTIONS, not by text. Every agent needs at least one tool that acts;
  a role that would only write text belongs to Atlas. Each agent: short id (a-z, _), name = a plain job title a
  colleague would carry on a badge ("Bookings Manager", "Enquiry Responder", "Floor Watcher" - never a slug, an
  acronym or anything technical), role (3-6 words), goal
  (ONE line, max 100 characters), tools (subset of: record_find / record_get / record_save = the desk's business
  records, crm_lookup, web_fetch, browse = a real browser, calendar_free_slots, camera_ask / camera_events /
  camera_look, run_python, recall, read_file, list_files, save_deliverable),
  "strong": true only if the role needs top-tier judgement or client-facing writing,
  "instructions": 1-3 rules, max 90 characters each, only what the tools cannot say (what to check first, what it
  must never do),
  "engine": "hermes_agent" or "atlas", and "reports_to": "atlas" for a top-level agent or the id of the lead it
  works under. A lead is just an agent whose members name it in "reports_to" - e.g. {"id": "enquiries_lead",
  "reports_to": "atlas"} with {"id": "stock", "reports_to": "enquiries_lead"}. When the owner asks for a pod, a
  sub-team, or "put X under Y", you MUST set reports_to on the members - the structure is drawn from that field. Use "hermes_agent" (the Nous Research Hermes Agent runtime: own browser,
  terminal, file system, skills and per-client long-term memory) for roles that must browse live websites, run code
  or shell commands, work through files over many steps, reconcile data, or remember a client between runs.
  Use "atlas" (the fast built-in loop) for drafting, replying, classifying, summarising and QA.
- Design the team for THIS job. Never reuse a stock line-up: the roles, their instructions and the workflow steps
  must follow from what the owner actually described.
- Workflows: 1-3, each with an id, name, trigger {"kind": webhook|inbox|schedule|manual, "detail": text} and ordered
  "steps": list of agent ids. Atlas reviews and approves outbound messages implicitly; do not list atlas in steps.
- Connectors the desk needs: kinds smtp (send email), imap (watch inbox), http (any REST API), mcp (tool server:
  Slack, Notion, Google Sheets, GitHub...), webhook (web forms, Zapier, Make). Each: {"kind", "name", "purpose", "required": bool}.
- Policy: {"no_money_figures": bool, "max_words": int, "banned_phrases": [..]}. Default no_money_figures true for
  anything customer-facing with quotes/prices.
- Business: {"name","tagline","description","services":[..],"target_clients","tone","sender_name","availability","pricing_notes"}.
  Fill what you know; leave unknown fields out (do not invent a sender name or pricing).

Output format — MANDATORY on every turn
Write your reply to the owner first, in clean light markdown so it scans easily: short paragraphs, **bold** for the
key terms, a numbered or bulleted list when there are steps or options, a "### " subheading only in longer answers, a
table only when comparing options. No headings on short replies. Then, on a new line, append exactly one machine block
and nothing after it:

<atlas-design>{"intent": "design", "next": [{"label": "moves forward", "detail": "what happens if picked", "say": "the owner's exact message"}, {"label": "refines the draft", "detail": "...", "say": "..."}, {"label": "an alternative or added capability", "detail": "...", "say": "..."}], "ready": false, "blueprint": { "business": {...}, "agents": [...], "workflows": [...], "connectors": [...], "policy": {...} }}</atlas-design>

"intent" is "design" when the turn shapes the desk and "answer" when you only answered a question (then "blueprint"
is null).
On a design turn the blueprint must be COMPLETE (full current state, not a diff). On the very first turn, before the owner
has said anything substantive, "blueprint" may be null.

"next" - exactly 3 ways for the owner to proceed, one click each:
- Three genuinely different directions: one that moves forward ("action": "build" when the draft is solid, otherwise
  the most useful next question), one concrete refinement of the current draft, one alternative or added capability.
- Specific to this owner's business and this turn (their business, customers, channels, agent names), never generic
  ("Tell me more", "Continue").
- "label": imperative, sentence case, max 7 words, no trailing period, no emoji. "detail": max 14 words, what happens if
  they pick it - no figures, prices or results the owner did not give you. "say": the exact message sent in the owner's
  voice, first person and complete (omit when equal to label).
- "action" only as "build" (proposing to build a draft that has agents) or "camera" (connect a camera feed; add
  "camera": "<camera name>", or "" for a new one). Otherwise leave it out.
- On "answer" turns: three natural follow-ups - go deeper, apply it to their business, back to the desk.

Editing an existing sketch - the canvas is the owner's, not yours
- Start from the "Current blueprint" you are given and change ONLY what this turn calls for. Every agent, workflow,
  connector and business field the owner did not mention is copied over unchanged, word for word.
- An agent's "id" is permanent. Renaming, re-scoping or moving an agent keeps its id; only a genuinely new agent gets a
  new id. Never re-slug ids from names.
- Remove an agent only when the owner asks for that (or agrees to your suggestion to drop it), and say which one went.
- Keep the agents in their existing order; append new ones at the end."""

GREETING = ("Describe the business and the work to take off your plate: enquiries, proposals, invoices, an inbox, cameras. "
            "The team is drafted in the Team panel as you go, and nothing is sent without your approval. "
            "You can also ask a question directly.")

CAMERA_GUIDE = """

Cameras (the desk's eyes)
- If the owner mentions cameras, CCTV, footage, a shop floor, a lobby, a kitchen, a till or "watching" anything, add a
  "cameras" list to the blueprint: [{"name": "short-slug", "source": "...", "notes": "where it points",
  "focus": "what to document in detail", "journal": true, "alerts": false, "watch_for": "person"}].
- "source" is what the owner gave you: an RTSP address (rtsp://user:pass@ip:554/...), a snapshot URL, a webcam index
  ("0"), or "sample:<clip>" for sample footage from the library below. If they have cameras but gave no address yet,
  use "" and ask for the stream addresses (the owner can also add them later on the Cameras page).
- "journal": true makes the camera keep a detailed, searchable written record of everything it sees (what people do,
  wear, carry, how long they wait, what changes) - the owner can later ask questions about it. Default true.
- "alerts": true only if the owner wants the agents woken when something specific happens; then say what in "focus".
- Give any agent that reads or reports on the cameras the tools camera_ask and camera_events (camera_look for a fresh
  frame). Agents never narrate feeds themselves - the journal does that.
- If the owner wants to try it without their own cameras, offer the sample footage and use "sample:" sources. When they
  want several cameras on one place, use clips that share a site (the campus-* set is five angles of one campus; the kitchen-* set is five angles of one real kitchen - use it for restaurants, cafes and food businesses).
- One watcher per camera: give that agent "camera": "<camera name>" so it only reads its own feed."""


SWITCH_TO_PAID = NS.SWITCH_TO_PAID
# fixed steps for turns the model never answered (quota, error, empty reply)
RETRY_STEP = {"label": "Try that again", "detail": "Asks Atlas to answer your last message again; the sketch so far is kept",
              "say": "Please try my last message again."}
PAID_STEP = {"label": SWITCH_TO_PAID, "detail": NS.PAID_DETAIL, "action": "paid"}   # the only wording a paid step has
BUILD_STEP = {"label": "Build the desk from this draft", "detail": "Uses the sketch as it stands; no model call needed", "action": "build"}
PAID_VLM = os.environ.get("ATLAS_PAID_VLM", "google/gemini-2.5-flash-lite")   # cheap paid eyes for desks off the free tier


def sample_dir() -> Path:
    """ATLAS_SAMPLE_VIDEOS, else ~/AtlasDemo/videos (full local set), else the copy committed in samples/videos."""
    if os.environ.get("ATLAS_SAMPLE_VIDEOS"):
        return Path(os.environ["ATLAS_SAMPLE_VIDEOS"])
    home = Path.home() / "AtlasDemo" / "videos"
    if home.is_dir():
        return home
    return Path(__file__).resolve().parent.parent / "samples" / "videos"


SAMPLE_LABELS = {
    "corner-store_ezymart": "corner shop till, real CCTV (a wallet is taken from a customer's backpack)",
    "retail-store": "retail shop floor and checkout, real CCTV",
    "liquor-store-delivery": "liquor shop counter, real CCTV",
    "hotel-lobby_Browse4": "hotel lobby reception desk, ceiling camera",
    "hotel-lobby_LeftBag": "hotel lobby seating, someone leaves a bag behind",
    "hotel-lobby_Meet_Crowd": "hotel lobby entrance, a group meets and splits",
    "hotel-lobby_Browse_WhileWaiting2": "hotel lobby waiting area, a guest waits",
    "restaurant-sushi-counter": "sushi restaurant, chef at the counter",
    # five synchronised cameras on ONE site (MEVA dataset, CC BY 4.0): use them together for a multi-camera desk
    "campus-lobby": "campus building, lobby and coffee point by the doors, people meeting (same site as the other campus-* clips, same 5 minutes)",
    "campus-entrance": "campus building, main entrance and forecourt from above, people arriving (same site, same 5 minutes)",
    "campus-carpark": "campus car park, cars and people walking through (same site, same 5 minutes)",
    "campus-drive": "campus approach road and paths, vehicles and pedestrians at distance (same site, same 5 minutes)",
    "campus-gym": "campus sports hall, court and bleachers, mostly empty (same site, same 5 minutes)",
    # five synchronised cameras in ONE real kitchen (EPFL-Smart-Kitchen-30, faces blurred, one cook): a restaurant back-of-house desk
    "kitchen-overview": "kitchen, whole room from the doorway corner, cook moving between stations (same kitchen as the other kitchen-* clips, same 5 minutes)",
    "kitchen-wide": "kitchen, long view down the counters from the far end (same kitchen, same 5 minutes)",
    "kitchen-stove": "kitchen, hob and pans from above (same kitchen, same 5 minutes)",
    "kitchen-prep": "kitchen, chopping board and sink, close on the cook's hands (same kitchen, same 5 minutes)",
    "kitchen-sink": "kitchen, sink and back counter, cook washing and plating (same kitchen, same 5 minutes)",
}


SAMPLE_CAMERA_NAMES = {"corner-store_ezymart": "till", "retail-store": "shop-floor", "liquor-store-delivery": "liquor-counter",
                       "hotel-lobby_Browse4": "reception", "hotel-lobby_LeftBag": "lobby-seating",
                       "hotel-lobby_Meet_Crowd": "lobby-entrance", "hotel-lobby_Browse_WhileWaiting2": "lobby-waiting",
                       "restaurant-sushi-counter": "sushi-counter", "campus-lobby": "lobby", "campus-entrance": "entrance",
                       "campus-carpark": "car-park", "campus-drive": "drive", "campus-gym": "gym",
                       "kitchen-overview": "overview", "kitchen-wide": "counters", "kitchen-stove": "stove", "kitchen-prep": "prep",
                       "kitchen-sink": "sink"}


def sample_clips() -> dict[str, str]:
    """Sample footage the owner can try the desk on: {clip name: path}. Any .mp4 in the sample folder counts."""
    d = sample_dir()
    return {p.stem: str(p) for p in sorted(d.glob("*.mp4"))} if d.is_dir() else {}


def resolve_camera_source(src: str) -> str:
    src = (src or "").strip()
    if src.lower().startswith("sample:"):
        return sample_clips().get(src.split(":", 1)[1].strip(), "")
    return src


def sample_name(source: str) -> str:
    """The clip name when a camera's source is sample footage ("sample:<name>", or the file it was resolved to when
    the camera was made), else "". The portal shows such a camera as sample:<name> again."""
    s = (source or "").strip()
    if s.lower().startswith("sample:"):
        return s.split(":", 1)[1].strip()
    if not s.lower().endswith(".mp4"):
        return ""
    try:
        p = Path(s)
        return p.stem if p.parent.resolve() == sample_dir().resolve() and p.is_file() else ""
    except (OSError, ValueError):
        return ""


def camera_guide() -> str:
    clips = sample_clips()
    if not clips:
        return CAMERA_GUIDE + "\n- No sample footage is installed on this desk."
    return CAMERA_GUIDE + "\n- Sample footage library (use as \"sample:<name>\"):\n" + "\n".join(
        f"  {n}: {SAMPLE_LABELS.get(n, n.replace('_', ' ').replace('-', ' '))}" for n in clips)


# ---------------------------------------------------------------------------- sessions
class DesignSession:
    def __init__(self, mode: str, tier: str = "free"):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.tier = tier
        self.created = time.time()
        self.messages: list[dict[str, Any]] = []         # the model's history: {"role","content"}, prose only (DST.history)
        # ts: the time a resumed draft shows; k on the reply to an owner message: "model" | "fixed" | "server"
        self.transcript: list[dict[str, Any]] = [{"role": "assistant", "text": GREETING, "ts": round(self.created, 3)}]
        self.blueprint: dict[str, Any] | None = None
        self.bp_rev = 0                                  # bumped by every write that changes the blueprint
        self.design: dict[str, Any] = DST.empty()        # plan, question, proposal, assumptions, decisions (design_state)
        self.ready = False
        self.desk_id: int | None = None
        self.turn = 0
        self.links: list[str] = []
        self.profile: dict[str, Any] | None = None       # company profile from the pre-study of their links
        self.fixed_turn = False                          # never saved: the last turn's steps are the server's (no model answer)
        self.finish_reason = ""                          # never saved: why the last model call stopped ("length" = cut off)
        self.next = session_next(self, [], fallback=NS.greeting(bool(sample_clips())))
        self.suggestions: list[str] = NS.labels(self.next)   # legacy chips (studio.js, old sessions)
        self.owner_id: int | None = None                 # the account that started it: its draft comes back on any device
        self.lock = threading.Lock()

    def public(self) -> dict[str, Any]:
        since = busy_since(self.id)
        return {"sid": self.id, "mode": self.mode, "tier": self.tier, "transcript": self.transcript,
                "blueprint": self.blueprint, "next": self.next, "suggestions": self.suggestions, "ready": self.ready,
                "desk_id": self.desk_id, "turn": self.turn, "links": self.links, "profile": self.profile,
                "rev": self.bp_rev, "busy": since is not None, "busy_since": since,
                "design": DST.public_view(self.design, self.blueprint)}

    def to_dict(self) -> dict[str, Any]:
        """The saved row. Rollback-safe: the code before the design state restores a fixed key list, so it ignores
        `design` and `bp_rev`, and replays the prose-only `messages` as they are."""
        return {"id": self.id, "mode": self.mode, "tier": self.tier, "created": self.created, "messages": self.messages,
                "transcript": self.transcript, "blueprint": self.blueprint, "next": self.next, "suggestions": self.suggestions,
                "ready": self.ready, "desk_id": self.desk_id, "turn": self.turn, "links": self.links, "profile": self.profile,
                "owner_id": self.owner_id, "design": self.design, "bp_rev": self.bp_rev}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DesignSession":
        s = cls(d.get("mode", "demo"), d.get("tier", "free"))
        for k in ("id", "created", "transcript", "blueprint", "ready", "desk_id", "turn", "links", "profile", "owner_id"):
            if k in d:
                setattr(s, k, d[k])
        # a row saved before the design state (no "design" key, including one an older build saved after a rollback)
        # holds every raw reply, block and all: each is reduced to its prose, so no block is ever replayed
        s.messages = DST.history(d.get("messages"), legacy="design" not in d)
        s.design = DST.load(d.get("design"))
        s.bp_rev = DST.whole(d.get("bp_rev"))
        # sessions saved before next steps carry only the legacy chips: derive the steps from them
        s.refresh_next(d["next"] if isinstance(d.get("next"), list) else d.get("suggestions") or [])
        return s

    def refresh_next(self, items: Any = None) -> list[dict[str, Any]]:
        """Re-check the steps (`items`, default the current ones) against the session as it stands: a restored session,
        a tier switch or a canvas edit can make "paid" or "build" invalid; the sketch's fallback fills any gap."""
        items = self.next if items is None else items
        self.next = session_next(self, items, fallback=None if self.turn or self.blueprint else NS.greeting(bool(sample_clips())))
        self.suggestions = NS.labels(self.next)
        return self.next

    def apply_profile(self, profile: dict[str, Any]) -> None:
        """Seed the conversation from a study of the owner's links: opening message, next steps, first-draft blueprint."""
        self.profile = profile
        opening = profile.get("opening_message")
        opening = opening.strip() if isinstance(opening, str) and opening.strip() else GREETING   # model output: text or nothing
        opening = DST.cut(opening, DST.TRANSCRIPT_CHARS)
        self.transcript = [{"role": "assistant", "text": opening, "ts": round(time.time(), 3)}]
        self.messages = [{"role": "assistant", "content": opening}]
        bp = normalise(profile.get("blueprint"), None) if isinstance(profile.get("blueprint"), dict) else None
        if bp and not bp["business"].get("name") and isinstance(profile.get("name"), str) and profile["name"].strip():
            bp["business"]["name"] = profile["name"]
        if bp and bp.get("agents") and bp != self.blueprint:
            self.blueprint = bp
            self.bp_rev += 1
            if DST.seed(self.design, bp, DST.STUDY_WHY):     # the study's draft is a first draft with no plan
                self.design = DST.clamp(self.design)
        opps_in = profile.get("opportunities")                          # model output: a number or true is not a list
        opps = [o for o in (opps_in if isinstance(opps_in, list) else []) if isinstance(o, dict) and isinstance(o.get("title"), str)]
        # a "why" is shown only when a model wrote it from their pages; the heuristic list's reasons are generic copy
        read = profile.get("source") == "model" and profile.get("opportunities_source") != "heuristic"
        items = ((profile["next"] if isinstance(profile.get("next"), list) else [])     # the opportunities, as the owner's reply
                 + [{"label": o["title"], "detail": (o.get("why") or "") if read else "", "say": o["title"]} for o in opps]
                 + (profile["suggestions"] if isinstance(profile.get("suggestions"), list) else []))
        self.next = session_next(self, items, fallback=None if self.blueprint else NS.greeting(bool(sample_clips())),
                                 paid=False)             # a profile is model (or heuristic) output: never the paid switch
        self.suggestions = NS.labels(self.next)


def session_next(session: DesignSession, items: Any, intent: str = "design", fallback: list | None = None,
                 paid: bool = True) -> list[dict[str, Any]]:
    """The 3 steps under a designer turn: `items` (the model's, or a fixed set) first, then the sketch's fallback.
    Build only once the blueprint has agents; the paid switch only on a live session still on free models, and only
    when `paid`: the server offers it on a turn the model never answered, a model's own steps never add it (the
    prompt never asks for it, so one there was not the owner's idea)."""
    drop = set() if session.blueprint and session.blueprint.get("agents") else {"build"}
    if not paid or session.mode == "demo" or session.tier != "free":
        drop.add("paid")
    fb = fallback if fallback is not None else NS.design_fallback(session.blueprint, session.ready, intent)
    return NS.normalise(items, fb, allow=[a for a in NS.DESIGN if a not in drop])


SESSIONS: dict[str, DesignSession] = {}
_SESSIONS_MAX = 200
# sid -> (token, since): the request that holds a session's turn (spec 7.1). A module registry, not a session field, so
# it outlives SESSIONS eviction and a reloaded copy of the session; never saved.
BUSY: dict[str, tuple[str, float]] = {}
_BUSY_LOCK = threading.Lock()


def busy_since(sid: str) -> float | None:
    """When the turn holding `sid` started, or None when no turn holds it."""
    with _BUSY_LOCK:
        held = BUSY.get(sid)
    return held[1] if held else None


def new_session(mode: str, tier: str = "free") -> DesignSession:
    s = DesignSession(mode, tier)
    if len(SESSIONS) >= _SESSIONS_MAX:
        for k in sorted(SESSIONS, key=lambda k: SESSIONS[k].created)[: _SESSIONS_MAX // 4]:
            SESSIONS.pop(k, None)
    SESSIONS[s.id] = s
    return s


# ---------------------------------------------------------------------------- parsing + normalising
_TAG_ANY = re.compile(r"<(\s*/?\s*)atlas-design", re.I)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think>", re.I)
_DIGITS = re.compile(r"\d+")
_SPECIAL = re.compile(r"</?\|")                                  # a model's special tokens: <|tool_call_begin|>, </|...|>
_LONE_TAG = re.compile(r"^[ \t]*</?[A-Za-z][\w-]*>[ \t]*$", re.M)   # a line that is one wrapper tag: </output>, </response>


def neutralise(text: Any) -> Any:
    """Text bound for a prompt (the owner's messages, the studied profile, the sketch the model is shown) with
    "<atlas-design" written "<atlas_design", opening or closing, in any case: quoted or injected text never reads as a
    machine block, and the model is never handed one to copy. The transcript keeps the owner's words exactly."""
    return _TAG_ANY.sub(lambda m: "<" + m.group(1) + "atlas_design", text) if isinstance(text, str) else text


def _drop_think(raw: str) -> str:
    """Reasoning a model wrote into its reply is never prose: <think>…</think> goes, and an unclosed <think> (a reply
    cut off while thinking) takes everything up to the first machine block, or to the end."""
    raw = _THINK.sub("", raw)
    m = _THINK_OPEN.search(raw)
    if m:
        end = raw.find(TAG_OPEN, m.end())
        raw = raw[:m.start()] + (raw[end:] if end >= 0 else "")
    return raw


def _tail(text: str) -> str:
    """The text after the last block, kept only as far as it is prose. In the 58 stored replies with a block, the
    text after it was never words: a lone wrapper tag (</output>, </response>) or a model's special tokens re-sending
    the block as a tool call (<|tool_calls_section_begin|>… in prod). Both are format, so they go; anything before the
    block is left exactly as written."""
    m = _SPECIAL.search(text)
    return _LONE_TAG.sub("", text[:m.start()] if m else text)


def _parse_body(body: str, loose: bool = True) -> tuple[dict[str, Any] | None, str]:
    """A block's JSON: (dict, "strict") when it reads whole, (dict, "loose") when only a repaired prefix does (tried
    only with `loose`), else (None, "none"). A ```json fence inside the tags is tolerated."""
    s = body.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", s)
    if not s.startswith("{"):
        return None, "none"
    try:
        v = json.loads(s)
        return (v, "strict") if isinstance(v, dict) else (None, "none")
    except (ValueError, RecursionError):
        if not loose:
            return None, "none"
    try:
        v = _loose_json(s)
    except RecursionError:
        v = None
    return (v, "loose") if isinstance(v, dict) else (None, "none")


def parse_reply(raw: str, finish_reason: str = "") -> tuple[str, dict[str, Any] | None, str]:
    """(prose, machine dict | None, how it parsed: strict | loose | fence | misclosed | none).
    - The LAST block wins. Earlier blocks (one quoted from the owner, or injected) are cut from the prose and never
      used; the prose is all the text outside the blocks, including words after the last one (_tail).
    - An opening tag after the last complete block means the model's own block was cut off: no block (the turn
      repairs), so an earlier quoted block can never stand in for it.
    - The mis-closed shape <atlas-design>{…}<atlas-design> counts only when its JSON is strict and the reply was not
      cut off (finish_reason "length").
    - With no tag at all, the last ```json fence holding an object with a block key (BLOCK_KEYS) is the block.
    - <think> reasoning is never prose (_drop_think)."""
    raw = _drop_think(raw or "")
    spans: list[tuple[int, int, str, str]] = []          # (start, end, complete | misclosed | open, body)
    pos = 0
    while True:
        s = raw.find(TAG_OPEN, pos)
        if s < 0:
            break
        b = s + len(TAG_OPEN)
        nxt, close = raw.find(TAG_OPEN, b), raw.find(TAG_CLOSE, b)
        if close >= 0 and (nxt < 0 or close < nxt):
            spans.append((s, close + len(TAG_CLOSE), "complete", raw[b:close]))
            pos = close + len(TAG_CLOSE)
        elif (nxt >= 0 and _parse_body(raw[b:nxt], loose=False)[1] == "strict"
              and not raw[nxt + len(TAG_OPEN):].lstrip().startswith("{")):   # the second tag closes it, opens nothing
            spans.append((s, nxt + len(TAG_OPEN), "misclosed", raw[b:nxt]))
            pos = nxt + len(TAG_OPEN)
        else:                                            # never closed: up to the next opening tag, or to the end
            end = nxt if nxt >= 0 else len(raw)
            spans.append((s, end, "open", raw[b:end]))
            pos = end
    data: dict[str, Any] | None = None
    how = "none"
    if spans:
        _, _, kind, body = spans[-1]
        if kind == "complete":
            data, how = _parse_body(body)
        elif kind == "misclosed" and finish_reason != "length":
            data, how = _parse_body(body, loose=False)[0], "misclosed"
        parts, last = [], 0
        for s, e, _kind, _body in spans:
            parts.append(raw[last:s])
            last = e
        prose = "".join(parts) + _tail(raw[last:])
    else:
        prose = raw
        hits = []
        for f in _FENCE.finditer(raw):
            try:
                cand = json.loads(f.group(1))
            except (ValueError, RecursionError):
                continue
            if isinstance(cand, dict) and any(k in cand for k in BLOCK_KEYS):
                hits.append((f.start(), f.end(), cand))
        if hits:
            data, how = hits[-1][2], "fence"
            parts, last = [], 0
            for s, e, _cand in hits:
                parts.append(raw[last:s])
                last = e
            prose = "".join(parts) + _tail(raw[last:])
    prose = re.sub(r"\n{3,}", "\n\n", prose.replace(TAG_CLOSE, "")).strip()
    return prose, data, how if data is not None else "none"


def split_reply(raw: str, finish_reason: str = "") -> tuple[str, dict[str, Any] | None]:
    """(prose, machine dict | None): parse_reply without how it parsed."""
    prose, data, _how = parse_reply(raw, finish_reason)
    return prose, data


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
    """An id: lower-case letters and digits joined by "_", at most 24 characters, never ending in "_" (a cut that
    landed on a separator used to leave one, so the next normalise gave the item a different id)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return s[:24].rstrip("_") or "agent"


def _suffixed(base: str, taken: Any) -> str:
    """A fresh id for an agent whose slug another agent holds: the base cut to 22 characters plus _2 … _9, so it stays
    distinct within _slug's 24 (a 24-character base plus "_2" is cut straight back to the base). "" when all are taken."""
    stem = base[:22].rstrip("_") or "agent"
    return next((f"{stem}_{n}" for n in range(2, 10) if f"{stem}_{n}" not in taken), "")


REMOVAL_WORDS = re.compile(r"\b(remove|delete|drop|get rid|without|merge|combine|consolidat\w*|fewer|smaller team|simpl\w*|"
                           r"cut|scrap|replace|instead of|swap|lose|kill|no longer|don'?t need|do not need|only keep|just keep)\b", re.I)


def _txt(v: Any, n: int) -> str:
    """One line of plain text from a model or canvas string (anything else is no text): hidden characters removed,
    space separators such as U+202F and whitespace runs folded to one space, clamped to n at a word boundary."""
    t = NS._fit(NS._shown(v, 1), n) if isinstance(v, str) else ""
    return t if NS._visible(t) else ""                 # a cut can leave only joiners: that is no text either


def _name_key(s: Any) -> str:
    """A name as names are compared: plain text, case folded."""
    return NS._text(s).casefold()


def _name_tokens(s: str) -> set[str]:
    """The words that identify a name: 3+ letters, or a number of any length ("Camera 3 Monitor" keeps its 3)."""
    return {t for t in re.split(r"[^a-z0-9]+", str(s or "").lower())
            if (len(t) > 2 or t.isdigit()) and t not in ("the", "and", "for", "agent")}


def _digit_twins(a: str, b: str) -> bool:
    """Names equal once every number is masked, with different numbers ("camera 3 monitor", "camera 1 monitor"): two
    agents, never one renamed."""
    return a != b and _DIGITS.sub("#", a) == _DIGITS.sub("#", b)


def _match_prev(a: dict[str, Any], aid: str, prev_agents: list[dict[str, Any]], taken: set[str]) -> str | None:
    """The id the canvas already knows for this agent, when the model re-slugged or renamed it: an unbound agent of the
    same name or whose name slugs to this id, else shared name words (>= 0.5, +0.5 for the same role). Digit twins never
    match on words."""
    name, role = _name_key(a.get("name")), _name_key(a.get("role"))
    free = [p for p in prev_agents if p["id"] not in taken]
    for p in free:
        if name and _name_key(p.get("name")) == name:
            return p["id"]
    for p in free:
        if aid == _slug(p.get("name")):
            return p["id"]
    toks = _name_tokens(name)
    best, score = None, 0.0
    for p in free:                                                   # "Enquiry writer" -> "Enquiry reply writer": shared words
        pname = _name_key(p.get("name"))
        pt = _name_tokens(pname)
        if not (toks and pt) or _digit_twins(name, pname):
            continue
        s = len(toks & pt) / max(1, min(len(toks), len(pt)))
        if role and _name_key(p.get("role")) == role:
            s += 0.5
        if s > score:
            best, score = p["id"], s
    return best if score >= 0.5 else None


def _bind_agents(agents_in: list[Any], prev_agents: list[dict[str, Any]],
                 remap: bool) -> tuple[list[tuple[dict[str, Any], str]], dict[str, str]]:
    """Each incoming agent with the id it gets, in list order, and {id the model used -> id the canvas knows}.
    Pass 1: an agent whose slug id the canvas already has keeps it (its first occurrence). Pass 2, with remap only: the
    rest bind to an agent nobody bound yet (_match_prev). Two passes, so "Camera 3 Monitor" listed before cam1 and cam2
    can never take their ids. What is left is new: its own slug, or the 22-character suffix when another agent holds
    that slug or the canvas knows it (a new agent never takes over a known id). The same agent listed twice is dropped."""
    cands = [(a, _slug(a.get("id") or a.get("name") or f"agent_{i}")) for i, a in enumerate(agents_in) if isinstance(a, dict)]
    prev_ids = {p["id"] for p in prev_agents}
    final: list[str] = [""] * len(cands)
    holder: dict[str, str] = {}                        # id -> the name (as compared) of the agent that holds it
    named = [_name_key(a.get("name")) or aid.replace("_", " ") for a, aid in cands]
    for k, (a, aid) in enumerate(cands):
        if aid in prev_ids and aid not in holder:
            final[k], holder[aid] = aid, named[k]
    renamed: dict[str, str] = {}
    for k, (a, aid) in enumerate(cands):
        if final[k] or (aid in holder and holder[aid] == named[k]):
            continue
        keep = _match_prev(a, aid, prev_agents, set(holder)) if remap else None
        if keep:
            if aid not in holder:
                renamed[aid] = keep
            final[k] = keep
        elif aid not in holder and aid not in prev_ids:
            final[k] = aid
        else:
            final[k] = _suffixed(aid, set(holder) | prev_ids)
        if final[k]:
            holder[final[k]] = named[k]
    renamed = {k: v for k, v in renamed.items() if k not in holder}   # an id some agent holds always means that agent
    return [(a, f) for (a, _aid), f in zip(cands, final) if f], renamed


def _flag(v: Any) -> bool:
    """A yes from JSON or a form: true, 1, "true", "yes", "1" or "on" ("false" is no longer read as a yes)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v == 1
    return isinstance(v, str) and v.strip().lower() in ("true", "yes", "1", "on")


def _words_cap(v: Any, default: int) -> int:
    """policy.max_words as a whole number of words from 1 to 10,000; anything else ("about 150", 0, true) keeps
    `default` (int("about 150") used to raise and fail the whole turn)."""
    if isinstance(v, str) and re.fullmatch(r"\s*[0-9]{1,6}\s*", v):
        v = int(v)
    elif isinstance(v, float) and v.is_integer():
        v = int(v)
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10_000 else default


def _cam_slug(v: Any) -> str:
    """A camera name as the desk keys it: lower-case letters, digits and hyphens, at most 32 characters."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return ""
    return re.sub(r"[^a-z0-9-]+", "-", str(v).lower()).strip("-")[:32].strip("-")


_BIZ_TEXT = ("name", "tagline", "description", "target_clients", "tone", "sender_name", "availability", "pricing_notes")


def _business(biz: dict[str, Any]) -> dict[str, Any]:
    """The business fields, plain and clamped: the name to 80 characters, the other texts to 600, up to 10 services of
    60 each. Unknown fields go."""
    b: dict[str, Any] = {}
    for k in _BIZ_TEXT:
        v = _txt(biz.get(k), 80 if k == "name" else 600)
        if v:
            b[k] = v
    sv = biz.get("services")
    if isinstance(sv, str):
        sv = sv.split(",")
    if isinstance(sv, list):
        b["services"] = [s for s in (_txt(x, 60) for x in sv) if s][:10]
    return b


def _agent(a: dict[str, Any], aid: str) -> dict[str, Any]:
    """One agent in the canonical shape: plain clamped text, known tools only, at least one tool that acts."""
    from . import team as TM
    tools_in = a.get("tools") if isinstance(a.get("tools"), list) else []
    tools = list(dict.fromkeys(t for t in tools_in if t in SPECIALIST_TOOLS)) or ["read_file", "list_files"]
    if aid != "atlas" and not any(t in TM.FUNCTION_TOOLS for t in tools):
        tools = tools + ["record_find", "record_get"]          # no text-only agents: at least read the business records
    instr = a.get("instructions")
    if isinstance(instr, str):
        instr = [x.strip(" -•\t") for x in instr.splitlines()]
    # the server's own camera line goes BEFORE the clamp (_wire_cameras puts it back): clamped first, it pushed a bound
    # watcher's third rule out on every re-normalise
    rules = [r for r in (NS._shown(x, 1) for x in (instr if isinstance(instr, list) else []) if isinstance(x, str))
             if r and not r.startswith(_CAM_RULE)]
    goal = a.get("goal") if isinstance(a.get("goal"), str) and a["goal"].strip() else a.get("description")
    goal = TM._one_line(NS._HIDDEN.sub("", goal) if isinstance(goal, str) else "", TM.GOAL_CHARS)
    if len(goal) > TM.GOAL_CHARS:                              # one unbroken word: keep it inside the clamp
        goal = goal[:TM.GOAL_CHARS - 1] + "…"
    return {
        "id": aid,
        "name": _txt(a.get("name"), 40) or aid.replace("_", " ").title()[:40],
        "role": _txt(a.get("role"), 60) or "Specialist",
        "goal": goal,
        "tools": tools if aid != "atlas" else list(ATLAS_TOOLS),
        "reports_to": (_slug(a.get("reports_to") or "atlas") or "atlas") if aid != "atlas" else "",
        "camera": _cam_slug(a.get("camera")),
        "strong": _flag(a.get("strong")),
        "engine": "hermes_agent" if str(a.get("engine") or "").lower() in ("hermes_agent", "hermes") else "atlas",
        "instructions": [NS._fit(r, TM.RULE_CHARS) for r in rules][:TM.MAX_RULES],
    }


def _listed(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def normalise(bp: dict[str, Any] | None, prev: dict[str, Any] | None = None, keep_missing: bool = False,
              remap: bool = True) -> dict[str, Any] | None:
    """Coerce a blueprint (the model's, the canvas's, a stored one) into the canonical shape; fall back to `prev` for
    missing sections. Every text is plain and clamped, and normalise(normalise(x)) == normalise(x).
    remap (the model's full blueprints): an agent keeps the id (and colour, and the owner's canvas position) it had in
    `prev` when the model re-slugged or renamed it (_bind_agents). Ops and proposals pass remap=False: their ids are
    authoritative, so a new agent never takes over the id of one that went.
    With keep_missing, agents the model silently left out are carried over - the owner did not ask for a removal, so a
    truncated or lazy reply must not shrink the team."""
    if not isinstance(bp, dict):
        return prev
    prev = prev if isinstance(prev, dict) else {}
    out: dict[str, Any] = {}
    biz = bp.get("business") if isinstance(bp.get("business"), dict) else prev.get("business")
    out["business"] = _business(biz if isinstance(biz, dict) else {})

    agents_in = bp["agents"] if isinstance(bp.get("agents"), list) else _listed(prev.get("agents"))
    prev_agents = [a for a in _listed(prev.get("agents")) if isinstance(a, dict) and a.get("id")]
    bound, renamed = _bind_agents(agents_in, prev_agents, remap)
    agents = [_agent(a, aid) for a, aid in bound]
    seen = {a["id"] for a in agents}
    agents = [a for a in agents if a["id"] != "atlas"]
    if keep_missing and prev_agents:                                 # the owner asked for no removal: nobody disappears
        # re-shaped like any agent (a no-op on a normalised one), so an older stored sketch missing a field still wires
        kept = [_agent(p, p["id"]) for p in prev_agents if p["id"] not in seen and p["id"] != "atlas"]
        agents.extend(kept)
        out["carried_over"] = [p["id"] for p in kept]
    agents = agents[:8]
    # colours stick to the agent, not to its position in the list; new agents take the first unused one
    prev_color = {p["id"]: p.get("color") for p in prev_agents if p.get("color")}
    used = {prev_color[a["id"]] for a in agents if a["id"] in prev_color}
    spare = [c for c in PALETTE if c not in used] or list(PALETTE)
    for a in agents:
        c = prev_color.get(a["id"])
        if not c:
            if not spare:
                spare = list(PALETTE)
            c = spare.pop(0)
        a["color"] = c
        a["reports_to"] = renamed.get(a["reports_to"], a["reports_to"])
    # the owner's canvas positions (POST /api/design/<sid>/layout) ride along with the blueprint
    lay_in = bp.get("layout") if isinstance(bp.get("layout"), dict) else prev.get("layout")
    layout = {}
    for k, v in (lay_in.items() if isinstance(lay_in, dict) else ()):
        if isinstance(v, dict) and all(isinstance(v.get(c), (int, float)) and not isinstance(v.get(c), bool)
                                       and math.isfinite(v[c]) for c in ("x", "y")):
            kid = renamed.get(str(k), str(k))
            layout[kid] = {"x": int(v["x"]), "y": int(v["y"])}
    out["layout"] = layout
    # hierarchy contract shared with atlas/team.py: unknown leads, cycles and chains deeper than 2 collapse to atlas
    from . import team as TM
    shape, _errs = TM.validate_team({"agents": [{**a, "instructions": a.get("instructions") or ["as briefed", "as briefed"]} for a in agents]},
                                    allowed_tools=SPECIALIST_TOOLS)
    struct = {s["id"]: s for s in shape["agents"]}
    for a in agents:
        s = struct.get(a["id"], {})
        a["reports_to"] = s.get("reports_to", "atlas")
        a["members"] = list(s.get("members") or [])
    out["agents"] = agents
    ids = {a["id"] for a in agents}

    wfs_in = bp["workflows"] if isinstance(bp.get("workflows"), list) else _listed(prev.get("workflows"))
    wfs: list[dict[str, Any]] = []
    for i, w in enumerate(wfs_in):
        if not isinstance(w, dict):
            continue
        trig = w.get("trigger") if isinstance(w.get("trigger"), dict) else {"kind": str(w.get("trigger") or "manual")}
        kind = str(trig.get("kind") or "manual").strip().lower()
        kind = {"email": "inbox", "form": "webhook", "cron": "schedule", "timer": "schedule", "api": "webhook"}.get(kind, kind)
        if kind not in TRIGGER_KINDS:
            kind = "manual"
        steps = [_slug(s if isinstance(s, str) else s.get("agent") if isinstance(s, dict) else "") for s in _listed(w.get("steps"))]
        steps = [renamed.get(s, s) for s in steps]
        steps = [s for s in steps if s in ids]
        wfs.append({"id": _slug(w.get("id") or w.get("name") or f"workflow_{i}"),
                    "name": _txt(w.get("name"), 60) or f"Workflow {i + 1}",
                    "trigger": {"kind": kind, "detail": _txt(trig.get("detail"), 200)},
                    "steps": steps})
    out["workflows"] = wfs[:4]

    cons_in = bp["connectors"] if isinstance(bp.get("connectors"), list) else _listed(prev.get("connectors"))
    cons: list[dict[str, Any]] = []
    for c in cons_in:
        if isinstance(c, str):
            c = {"kind": c}
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "").strip().lower()
        kind = _KIND_ALIAS.get(kind, kind)
        if kind not in CONNECTOR_KINDS:
            continue
        cons.append({"kind": kind, "name": _txt(c.get("name"), 40) or _txt(T_KIND_LABEL[kind], 40),
                     "purpose": _txt(c.get("purpose"), 160), "required": bool(c.get("required", True))})
    out["connectors"] = cons[:8]

    cams_in = bp["cameras"] if isinstance(bp.get("cameras"), list) else _listed(prev.get("cameras"))
    cams: list[dict[str, Any]] = []
    seen_c: set[str] = set()
    for i, c in enumerate(cams_in):
        if isinstance(c, str):
            c = {"name": c}
        if not isinstance(c, dict):
            continue
        name = _cam_slug(c.get("name")) or f"camera-{i + 1}"
        if name in seen_c:
            continue
        seen_c.add(name)
        src = c.get("source")
        src = str(src).strip()[:400].rstrip() if isinstance(src, (str, int)) and not isinstance(src, bool) else ""   # 0 = webcam 0
        cams.append({"name": name, "source": src,
                     "notes": _txt(c.get("notes"), 200), "focus": _txt(c.get("focus"), 300),
                     "journal": c.get("journal", True) not in (False, "0", "false", "off"),
                     "alerts": c.get("alerts", False) in (True, "1", "true", "on"),
                     "watch_for": _txt(c.get("watch_for"), 60) or "person"})
    cams = cams[:12]
    _wire_cameras(agents, cams)
    out["cameras"] = cams

    pol_in = bp.get("policy") if isinstance(bp.get("policy"), dict) else prev.get("policy")
    pol_in = pol_in if isinstance(pol_in, dict) else {}
    prev_pol = prev.get("policy") if isinstance(prev.get("policy"), dict) else {}
    banned = pol_in.get("banned_phrases")
    if isinstance(banned, str):
        banned = banned.split(",")
    out["policy"] = {"no_money_figures": pol_in.get("no_money_figures", True) is not False,   # only a real false lifts it
                     "max_words": _words_cap(pol_in.get("max_words"), _words_cap(prev_pol.get("max_words"), 220)),
                     "banned_phrases": [p for p in (_txt(x, 120) for x in _listed(banned)) if p][:20]}
    return out


_CAM_TOOLS = ("camera_ask", "camera_events", "camera_look")
# "log" as a word (log, logs, logged, logger, logging, logbook), never inside catalog, blog, dialog or login
_CAM_READER = re.compile(r"summar|digest|report|document|\blog(?:s|ged|ger|ging|book)?\b|record|overview|incident|safety|security"
                         r"|cross.?camera", re.I)
# a camera agent that reads across the journals rather than watching one feed
_CAM_READS = re.compile(_CAM_READER.pattern + r"|journal|\bread|all cameras|every camera", re.I)
_CAM_RULE = "Your camera is "


def _cam_in_name(name: str, cams: list[str]) -> str:
    """The camera an agent's name names ("Till Watcher" -> till, "Shop Floor Watcher" -> shop-floor); the longest wins."""
    words = " " + " ".join(re.findall(r"[a-z0-9]+", name.lower())) + " "
    hits = [c for c in cams if " " + " ".join(re.findall(r"[a-z0-9]+", c)) + " " in words]
    return max(hits, key=len) if hits else ""


def _wire_cameras(agents: list[dict[str, Any]], cams: list[dict[str, Any]]) -> None:
    """Make a camera team hang together (in place). Journals do the watching; agents read them.
    - Bound: a camera agent (it has camera tools) whose camera is one of the desk's, whose name names a camera ("Till
      Watcher" -> till) or carries a number ("Camera 3 Monitor") watches one camera, and is told which one.
    - Reader: any other camera agent whose name, role or goal reads across ("Reads every camera journal", a summariser,
      a reporter) gets camera "" and the read tools.
    - The rest: bound in order when bound + rest are at least two, else readers (one camera agent reads every journal).
    - Placeholders camera-N (source "", the owner fills them in) only for bound agents left without a camera.
    - A non-camera agent that summarises / documents / reports, in a team with cameras, gets the read tools first, so
      it is classed like any camera agent now and on every re-normalise."""
    cam_ids = {a["id"] for a in agents if any(t in a["tools"] for t in _CAM_TOOLS)}
    if not cam_ids and not cams:
        return
    for a in agents:
        if a["id"] not in cam_ids and _CAM_READER.search(f"{a['name']} {a['role']} {a['goal']}"):
            a["tools"] = ["camera_events", "camera_ask"] + a["tools"]
            cam_ids.add(a["id"])
    names = [c["name"] for c in cams]
    want: dict[str, str] = {}                          # agent id -> its camera
    bound: list[str] = []
    readers: list[str] = []
    rest: list[str] = []
    for a in agents:
        if a["id"] not in cam_ids:
            continue
        cam = a["camera"] if a.get("camera") in names else _cam_in_name(a["name"], names)
        if cam:
            want[a["id"]] = cam
            bound.append(a["id"])
        elif _DIGITS.search(a["name"]):
            bound.append(a["id"])
        elif _CAM_READS.search(f"{a['name']} {a['role']} {a['goal']}"):
            readers.append(a["id"])
        else:
            rest.append(a["id"])
    (bound if len(bound) + len(rest) >= 2 else readers).extend(rest)
    bound_set = set(bound)
    need = [a["id"] for a in agents if a["id"] in bound_set and a["id"] not in want]   # in list order
    free = [n for n in names if n not in want.values()]
    k = 0
    while len(free) < len(need) and len(cams) < 12:
        k += 1
        ph = f"camera-{k}"
        if ph not in names:
            names.append(ph)
            free.append(ph)
            cams.append({"name": ph, "source": "", "notes": "", "focus": "", "journal": True, "alerts": False, "watch_for": "person"})
    for aid in need:
        want[aid] = free.pop(0) if free else ""
    for a in agents:
        a["instructions"] = [x for x in a["instructions"] if not x.startswith(_CAM_RULE)]
        cam = want.get(a["id"], "") if a["id"] in bound_set else ""
        a["camera"] = cam
        if cam:
            a["instructions"] = ([f"{_CAM_RULE}'{cam}': pass camera=\"{cam}\" to camera_ask, camera_events and camera_look."]
                                 + a["instructions"])[:8]
        elif a["id"] in readers:
            a["tools"] = ["camera_events", "camera_ask"] + [t for t in a["tools"] if t not in ("camera_events", "camera_ask")]
    if len(bound) > 1:
        agents.sort(key=lambda a: a["id"] not in bound_set)   # stable: watchers first, whoever reads across them after


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
        # before the turn (Phase A): a session with a team and no plan gets its plan seeded, kept even if the turn fails
        if DST.seed(session.design, session.blueprint):
            session.design = DST.clamp(session.design)
        session.turn += 1
        session.transcript.append({"role": "user", "text": DST.cut(user_text, DST.TRANSCRIPT_CHARS), "ts": round(time.time(), 3)})
        session.messages = DST.history(session.messages + [{"role": "user", "content": user_text}])
        session.fixed_turn = False                       # _live_turn sets it when the model never answered
        session.finish_reason = ""                       # _live_turn sets it from the model's answer
        if session.mode == "demo":
            raw = _demo_turn(session, user_text, on_token)
        else:
            raw = _live_turn(session, providers_cfg, designer_model, on_token)
        prose, data = split_reply(raw, session.finish_reason)
        prose = prose.strip()                                # light markdown: the workspace renders it
        if not prose:
            prose = "Noted. I have updated the sketch — tell me more, or build the desk when it looks right."
        steps: Any = []
        intent = "design"
        if data:
            steps = data["next"] if isinstance(data.get("next"), list) and data["next"] else data.get("suggestions")
            intent = "answer" if str(data.get("intent") or "").lower() == "answer" else "design"
            # a follow-up that never mentioned dropping anyone must not shrink the team (truncated / lazy replies did)
            protect = bool(session.blueprint and session.blueprint.get("agents")) and not REMOVAL_WORDS.search(user_text)
            bp = normalise(data.get("blueprint"), session.blueprint, keep_missing=protect)
            if bp and (bp.get("agents") or bp.get("business")):
                carried = bp.pop("carried_over", [])
                if carried:
                    names = [a["name"] for a in bp["agents"] if a["id"] in carried]
                    prose += "\n\n_Kept from the previous draft: " + ", ".join(names) + ". Say \"remove …\" to drop one._"
                if bp != session.blueprint:              # an answer turn hands back the sketch as it was: no new revision
                    session.blueprint = bp
                    session.bp_rev += 1
                if DST.seed(session.design, bp, DST.FIRST_DRAFT_WHY):   # a first draft whose block carried no plan
                    session.design = DST.clamp(session.design)
            if isinstance(data.get("ready"), bool):
                session.ready = data["ready"] and bool(session.blueprint and session.blueprint.get("agents"))
        # after the blueprint settles, so "build" is real; the paid switch only from the server's own steps
        session.next = session_next(session, steps, intent, paid=session.fixed_turn)
        session.suggestions = NS.labels(session.next)
        prose = DST.cut(prose, DST.TRANSCRIPT_CHARS)
        session.transcript.append({"role": "assistant", "text": prose, "ts": round(time.time(), 3),
                                   "k": "fixed" if session.fixed_turn else "model"})
        # the model's history holds prose only (the sketch reaches the model as state, not as old blocks), and a
        # fixed reply (quota, error, no answer) never enters it: the owner's message stays for the next try
        if not session.fixed_turn:
            session.messages.append({"role": "assistant", "content": prose})
        session.messages = DST.history(session.messages)
        return {"text": prose, "next": session.next, "suggestions": session.suggestions, "blueprint": session.blueprint,
                "ready": session.ready, "turn": session.turn}


def _fixed_block(session: DesignSession, *steps: dict[str, Any]) -> str:
    """Machine block for a turn the model never answered, and the mark that its steps are the server's (only these
    may offer the paid switch). No "ready": the design state stays as it was."""
    session.fixed_turn = True
    return chr(10) + "<atlas-design>" + json.dumps({"next": list(steps), "blueprint": None}) + "</atlas-design>"


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

    # every prompt input that is not the model's own reply has the block tag neutralised: the owner's messages, the
    # links and the profile studied from their (third-party) pages, and the sketch with its owner- and model-written names
    system = DESIGNER_SYSTEM + camera_guide()
    if session.profile:
        prof = {k: session.profile.get(k) for k in ("name", "summary", "sector", "services", "locations", "customers", "team_hint",
                                                   "channels", "tech", "tone", "opportunities") if session.profile.get(k)}
        system += ("\n\nYou already studied the owner's public links (" + neutralise(", ".join(map(str, session.links[:3])))
                   + "). Company profile - treat as known facts, do not ask for them again, reference them naturally:\n"
                   + neutralise(json.dumps(prof, ensure_ascii=False))[:3500])
    if session.blueprint:
        system += ("\n\nCurrent blueprint (update it, keep what still holds):\n"
                   + neutralise(json.dumps(session.blueprint, ensure_ascii=False)))
    msgs = [prov.user_message(neutralise(m["content"])) if m["role"] == "user"
            else {"role": "assistant", "content": neutralise(m["content"])} for m in session.messages]
    raw = ""
    err = ""
    for attempt in range(2):
        try:
            r = prov.chat(system, msgs, [], model or "", on_token=tok)
            raw = r.text or ""
            session.finish_reason = str(getattr(r, "stop_reason", "") or "")    # test stubs answer with .text only
            break
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(2.0)
    if not raw and err:
        if "free-models-per-day" in err or ("429" in err and ":free" in (model or "")):
            return ("Today's free model allowance on this account is used up (it resets at midnight UTC). You can switch "
                    "this conversation to the paid model, about 3p a message, or come back after the reset. The sketch so far is kept."
                    + _fixed_block(session, PAID_STEP, BUILD_STEP, {**RETRY_STEP, "label": "Try the free model again",
                                                                    "detail": "Works again after the midnight UTC reset"}))
        return (f"The model did not answer ({err}). Say that again in a moment — the sketch so far is kept."
                + _fixed_block(session, RETRY_STEP, PAID_STEP, BUILD_STEP))
    if not raw.strip():                                    # some free models return content="" - try the fallback model once
        fallback = (getattr(prov, "cfg", {}) or {}).get("fallback_model") or ""
        if fallback and fallback != model:
            try:
                r = prov.chat(system, msgs, [], fallback, on_token=tok)
                raw = r.text or ""
                session.finish_reason = str(getattr(r, "stop_reason", "") or "")
            except Exception as exc:
                err = f"{type(exc).__name__}: {str(exc)[:160]}"
    if not raw.strip():
        return ("I did not get a reply from the model that time - nothing was lost. Say that again, or switch to the paid model."
                + _fixed_block(session, RETRY_STEP, PAID_STEP, BUILD_STEP))
    # repair pass: the prose came back without a usable machine block -> ask for the block alone (not streamed).
    # Only once the owner has actually said something about the business (a greeting or "what did you understand"
    # must not conjure a team).
    _, data = split_reply(raw, session.finish_reason)
    said = " ".join(m["content"] for m in session.messages if m["role"] == "user" and isinstance(m["content"], str))
    substantive = bool(session.blueprint) or bool(session.profile) or len(said.split()) >= 12
    answered = bool(data) and str(data.get("intent") or "").lower() == "answer"     # a plain question: no team for it
    if substantive and not answered and not (data and isinstance(data.get("blueprint"), dict)):
        if session.blueprint:
            ask = ("Output ONLY the <atlas-design>{...}</atlas-design> block, nothing else. Start from the Current blueprint "
                   "in your instructions and apply ONLY the changes your last reply described - same ids, same order, "
                   "everything else copied unchanged. Keep the same next steps.")
        else:
            ask = ("Output ONLY the <atlas-design>{...}</atlas-design> block, nothing else. Include your best FIRST-DRAFT "
                   "blueprint for what has been said so far (make reasonable assumptions; the owner can adjust it on the "
                   "canvas). Keep the same next steps. If the owner has only asked general questions and not described "
                   "work to take on, set \"intent\": \"answer\" and \"blueprint\": null instead.")
        try:
            fix = prov.chat(system, msgs + [{"role": "assistant", "content": raw}, prov.user_message(ask)], [], model or "")
            _, data2 = split_reply(fix.text or "", str(getattr(fix, "stop_reason", "") or ""))
            if data2:                                  # "<" escaped: a tag inside a JSON string can never cut the block
                raw = (split_reply(raw, session.finish_reason)[0] + chr(10) + TAG_OPEN
                       + json.dumps(data2, ensure_ascii=False).replace("<", "\\u003c") + TAG_CLOSE)
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
    bp = _demo_blueprint(kind, name, turn)
    label = {"sales": "inbound enquiry desk", "proposal": "proposal desk", "inbox": "inbox triage desk", "orders": "order-care desk"}[kind]
    whose = (name + ("'" if name.endswith("s") else "'s")) if name else "the"
    scripts = {
        1: (f"Understood — that points to an {label}. I have sketched the core: Atlas orchestrating, a researcher and a writer. "
            f"Every outbound message will wait for your approval, and the policy layer blocks prices and placeholders. "
            f"Who are your customers, and how do enquiries usually arrive — web form, email, phone?",
            [{"label": "Wire up the website form", "detail": "Form enquiries become the desk's first trigger", "say": "Mostly through our website form."},
             {"label": "Watch one shared inbox", "detail": "Email enquiries land in one inbox the desk triages", "say": "By email to one inbox."},
             {"label": "Cover WhatsApp and SMS too", "detail": "Those enquiries land in one queue; phone calls you note by hand",
              "say": "Phone and WhatsApp mostly."}]),
        2: ("Good. I have added a CRM keeper so every contact gets a stage and a dated next action, and wired the intake as the "
            "first trigger. What tone should the replies have, and is there anything the agents must never say or promise?",
            [{"label": "Keep replies warm and local", "detail": "Friendly and never pushy, checked before it reaches you",
              "say": "Warm and local, never pushy."},
             {"label": "Never quote prices in writing", "detail": "The policy layer blocks money figures in every draft",
              "say": "Never quote prices in writing."},
             {"label": "Make replies formal and precise", "detail": "Short, exact wording for professional clients", "say": "Formal and precise."}]),
        3: (_demo_complete(bp),
            [{"label": "Build the desk now", "detail": f"Creates {whose} desk; nothing is sent without your approval",
              "action": "build"},
             _demo_addition(kind, bp),
             {"label": "Explain the approval flow", "detail": "What waits for you and what runs on its own",
              "say": "Explain the approval flow: what waits for me and what doesn't?"}]),
    }
    prose, nxt = scripts.get(turn, ("[Scripted preview] This deployment has no model key, so I can't actually respond to what you typed. "
                                    "The sketch shows a sample desk. For the real designer, run the desk with a live model "
                                    "(locally: AtlasDesk launcher; hosted: set OPENROUTER_API_KEY).",
                                    [{"label": "Build the sample desk", "detail": "Builds the desk sketched on the canvas", "action": "build"},
                                     {"label": "Show me how to go live", "detail": "What a live model key changes", "say": "How do I go live?"}]))
    cams = getattr(session, "_demo_cams", None)
    if cams is None and re.search(r"camera|cctv|footage|lobby|hotel|till|restaurant|kitchen", low):
        cams = _demo_cameras(low)
        session._demo_cams = cams  # type: ignore[attr-defined]
    if cams:
        bp["cameras"] = cams
        bp["agents"].append({"id": "watch_reporter", "name": "Camera reporter", "role": "Reads the camera journal",
                             "goal": "Answer questions and write the daily report from the camera journal, citing times.",
                             "tools": ["camera_ask", "camera_events", "save_deliverable"]})
        prose_note = f" I have added {len(cams)} camera{'s' if len(cams) != 1 else ''} with a detailed journal, and a reporter who reads it."
        if turn == 3:
            c0 = cams[0]
            nxt = nxt[:2] + [{"label": f"Use your own feed for {c0['name']}", "action": "camera", "camera": c0["name"],
                              "detail": ("Swap the sample footage for your RTSP stream or a recording" if c0.get("source")
                                         else "Add its stream address or a recording")}]
    else:
        prose_note = ""
    if turn >= 3 and "never" in low:
        bp["policy"]["banned_phrases"] = list(dict.fromkeys(bp["policy"]["banned_phrases"] + ["guarantee", "promise"]))
    prose += prose_note
    if on_token:
        for w in prose.split(" "):
            on_token(w + " ")
            time.sleep(0.018)
    return prose + "\n<atlas-design>" + json.dumps({"next": nxt, "ready": turn >= 3, "blueprint": bp}) + "</atlas-design>"


def _scheduled(bp: dict[str, Any]) -> dict[str, Any] | None:
    return next((w for w in bp.get("workflows") or [] if (w.get("trigger") or {}).get("kind") == "schedule"), None)


def _demo_complete(bp: dict[str, Any]) -> str:
    """Turn 3's prose: the reviewer and the scheduled workflow only when the sketch has them."""
    qa = next((a for a in bp.get("agents") or [] if a.get("id") == "qa"), None)
    wf = _scheduled(bp)
    gate = (f"the {qa['name'].lower()} checks every draft before it reaches your approval queue" if qa
            else "every draft waits in your approval queue")
    sched = f", and \"{wf['name']}\" runs on a schedule ({wf['trigger'].get('detail') or 'timed'})" if wf else ""
    return (f"Noted, added to policy. The team is complete: {gate}{sched}. Review the sketch — click any agent to adjust its "
            "role or tools — then build the desk. Next we connect your data live.")


def _demo_addition(kind: str, bp: dict[str, Any]) -> dict[str, Any]:
    """Turn 3's second step: something the sketch lacks. A follow-up chaser only where no workflow already runs on a
    schedule (the sales sketch has its 3-day chaser, the orders sketch its stuck-order sweep); else a weekly summary."""
    if _scheduled(bp):
        return {"label": "Add a weekly summary for me", "detail": "What the team did, what is waiting and what is stuck",
                "say": "Add a weekly summary for me: what the team did, what's waiting and what's stuck."}
    what = {"proposal": "proposals", "inbox": "emails"}.get(kind, "quotes")
    return {"label": f"Add a chaser for quiet {what}", "detail": f"A scheduled workflow that follows up {what} nobody answered",
            "say": f"Add a workflow that chases {what} nobody answered after 3 days."}


def _demo_cameras(low: str) -> list[dict[str, Any]]:
    clips = sample_clips()
    if "hotel" in low or "lobby" in low:
        want = [n for n in clips if n.startswith("hotel-lobby")]
    elif "restaurant" in low or "kitchen" in low or "sushi" in low:
        want = [n for n in clips if n.startswith("restaurant")]
    else:
        want = [n for n in clips if n.startswith(("corner-store", "retail-store", "liquor"))]
    want = want or list(clips)[:2]
    out = []
    for n in want[:4]:
        out.append({"name": SAMPLE_CAMERA_NAMES.get(n, n[:32]),
                    "source": f"sample:{n}", "notes": SAMPLE_LABELS.get(n, n), "focus": "", "journal": True, "alerts": False,
                    "watch_for": "person"})
    return out if clips else [{"name": "front-door", "source": "", "notes": "", "focus": "", "journal": True,
                               "alerts": False, "watch_for": "person"}]


# ---------------------------------------------------------------------------- blueprint -> desk config
def _agent_prompt(a: dict[str, Any], biz: dict[str, Any]) -> str:
    from . import team as TM                            # one prompt builder for every designed agent (short, function-first)
    return TM.agent_prompt(a, biz)


def blueprint_to_desk(bp: dict[str, Any], tier: str = "free", name: str = "") -> dict[str, Any]:
    """Turn an approved blueprint into {business, agents, workflows} for store.add_desk. `name` (the owner's company)
    stands in when the blueprint names no business, so no prompt ever carries the template's placeholder name."""
    bp = normalise(bp) or {}
    biz_in = bp.get("business") or {}
    base = json.loads(json.dumps(T.CONSULTANCY["business"]))
    b = {**base, **biz_in}
    if not str(biz_in.get("name") or "").strip():
        b["name"] = name.strip() or "this business"
    b["model"] = "custom"
    b["currency"] = b.get("currency") or "GBP"
    pol = bp.get("policy") or {}
    b["policy"] = {"no_money_figures": bool(pol.get("no_money_figures", True)), "max_words": int(pol.get("max_words") or 220),
                   "banned_phrases": list(pol.get("banned_phrases") or [])}
    roster = "\n".join(f"- {a['id']}: {a['name']} — {a['role']}" + (f" (leads: {', '.join(a['members'])})" if a.get("members") else "")
                       for a in bp.get("agents") or [] if (a.get("reports_to") or "atlas") == "atlas")
    atlas_extra = ("Specialists on this desk:\n" + roster + "\n\nYou lead this team: brief the specialists with delegate (leads run "
                   "their own members), run independent strands in parallel, review what comes back, then merge. Never do a "
                   "specialist's job yourself. Every customer-facing message goes through queue_action for owner "
                    "approval. Keep CRM up to date with crm_update.") if roster else ""
    agents = [T._atlas(atlas_extra)]
    from . import team as TM
    by_id = {a["id"]: a for a in bp.get("agents") or []}
    for a in bp.get("agents") or []:
        tools = list(a["tools"])
        if a.get("members"):                                   # a sub-team lead: needs delegate for its members
            tools = list(dict.fromkeys(TM.LEAD_TOOLS + tools))
        agents.append(T._agent(a["id"], a["name"], a["role"], TM.agent_prompt(a, b, by_id), tools=tools, color=a["color"]))
        agents[-1]["strong"] = bool(a.get("strong"))
        agents[-1]["engine"] = "atlas" if a.get("members") else (a.get("engine") or "atlas")
        agents[-1]["reports_to"] = a.get("reports_to") or "atlas"
        agents[-1]["members"] = list(a.get("members") or [])
        agents[-1]["instructions"] = list(a.get("instructions") or [])
        agents[-1]["goal"] = a.get("goal", "")
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
