"""The designer's server ops (round 2 designer spec section 6): what a design turn's machine block may change, and how.

  - Validation (6.2, 5.3): at most 30 ops, known ops only, the fields each update may set, text clamped as normalise
    clamps it. An op names its item by id, else by its exact name; never by a fuzzy match.
  - A full blueprint on an edit turn is diffed against the view the model was shown (5.5), so it takes the same path
    as ops; a first draft's blueprint becomes add ops (5.4).
  - Apply (6.5): onto the blueprint as it is now (bp1), ids authoritative (normalise with remap=False), the owner's
    recorded canvas edits winning over the model's values, camera addresses never written by the model.
  - Consent (6.3): a removal is applied only when the owner's message names the item, asks for a removal and the block
    parsed strictly. Every other removal, and the model's own propose, becomes the one pending proposal: declined
    entries, expiry, re-validation and the server's apply on accept.
  - The cap (6.4): at most MAX_NEW_AGENTS new agents a turn (leads first, then agents a workflow names), a first
    draft's look-alike agents collapsed into one, the rest queued in the plan with their definitions; build_item and
    Continue add them later.
  - The plan (6.7): upserts, done, prune, full. Questions (6.8): validation, the default's forms, answers, the wait and
    the default. Assumptions (6.9).
  - The ledger of what was applied, the decision it makes and the sentence-1 check (6.9); build facts, the cost
    estimate and the clone detector (6.10); the ready gate (6.12).

Nothing here calls a model or touches a session: apply_block works on copies and returns the turn's result, for the turn
(atlas/designer.py) to commit in one block. Model-written text in the notes passes style.plain_md."""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import design_state as DST
from . import designer as DS
from . import integrations as I
from . import nextsteps as NS
from .style import plain_md


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(os.environ.get(name) or default).strip())
    except ValueError:
        v = default
    return max(lo, min(hi, v))


MAX_OPS = 30                    # ops a turn: a capped first draft needs 29 (3 agents, 4 workflows, 8 connectors, 12 cameras, 2 sets)
MAX_NEW_AGENTS = _env_int("DESIGN_MAX_NEW_AGENTS", 3, 1, 8)     # new agents a turn: one function's team
TEAM_MAX = 8                    # agents in a sketch (normalise keeps 8)
WORKFLOWS_MAX, CONNECTORS_MAX, CAMERAS_MAX = 4, 8, 12            # what normalise keeps
KEEP_MAX = 2                    # keep ops a turn
WHY_CHARS = 90                  # an op's why
LEDGER_NAME_CHARS = 160
EXPIRE_AFTER = 2                # design turns a proposal waits for the owner's OK before it expires
HOLD_AFTER_EXPIRY = 3           # design turns after an expiry in which the model may not propose the same change
ASK_CHARS = (10, 160)           # a question's length
ASK_OVERLAP = 0.6               # a new question this close to a resolved one is a re-ask
PROSE_OVERLAP = 0.5             # a trailing "?" sentence this close to the asked or a resolved question is removed
DEFAULT_AFTER = 2               # design turns an open question waits before its default is used
FACTS_MAX, FACT_CHARS = 8, 140  # per-blueprint build facts
READER_ROLE = "Reads every camera journal"
# the build's cadence as atlas/desk/app.py builds a desk (api_design_build): a schedule trigger is a task job every 1,440
# minutes whose first run is 86,400 s after the build; an inbox trigger is an inbox_watch job every 2 minutes. Each
# journal note is two vision requests (the note and its verify pass, atlas/journal.py write_note), plus one summary
# every JOURNAL_ROLLUP_MIN minutes (maybe_rollup).
SCHEDULE_EVERY_MIN, SCHEDULE_FIRST_S, INBOX_EVERY_MIN = 1440, 86400, 2
NOTE_REQUESTS = 2

AGENT_FIELDS = ("name", "role", "goal", "tools", "instructions", "reports_to", "strong", "engine", "camera")
WORKFLOW_FIELDS = ("name", "trigger", "steps")
CAMERA_FIELDS = ("notes", "focus", "journal", "alerts", "watch_for", "source")
# designer._BIZ_TEXT + services, written out: nothing here reads designer at import time, so designer can import this
# module at its top (designer -> design_state -> designer is already a cycle)
BUSINESS_FIELDS = ("name", "tagline", "description", "target_clients", "tone", "sender_name", "availability", "pricing_notes",
                   "services")
POLICY_FIELDS = ("no_money_figures", "max_words", "banned_phrases")
OPS = ("add_agent", "update_agent", "keep", "remove_agent", "build_item", "add_workflow", "update_workflow",
       "remove_workflow", "add_camera", "update_camera", "remove_camera", "add_connector", "remove_connector",
       "set_business", "set_policy")
REMOVE_OPS = {"remove_agent": "agent", "remove_workflow": "workflow", "remove_camera": "camera",
              "remove_connector": "connector"}
LEDGER_KINDS = ("add", "update", "remove", "merge", "park", "keep", "queue", "build", "propose", "accept", "decline",
                "default", "answer", "undo")
NOT_NAMED = "not named in your message"
WAITING_HEAD = "_Waiting for your OK: "    # the note under a reply that made a proposal (6.13)
WAITING_TAIL = " (waiting for your OK)"     # how the ledger's sentence states the same proposal (6.9)
_MONEY = re.compile(r"money|pric|figure", re.I)      # the owner's words that name the no-money-figures rule


# ---------------------------------------------------------------------------- small helpers
def _norm(v: Any) -> str:
    """A name as names are compared (6.2): plain text, case folded, "-" and "_" read as spaces, spaces collapsed.
    Exact, never fuzzy: "Door Watcher", "door_watcher" and "door  watcher" are the same name."""
    return " ".join(re.sub(r"[-_]+", " ", DS._name_key(v)).split())


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _and(names: list[str], most: int = 4) -> str:
    """"A", "A and B", "A, B and C"; past `most` names the rest are "N more"."""
    names = [n for n in names if n]
    if len(names) > most:
        names = names[:most - 1] + [f"{len(names) - most + 1} more"]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _fit_names(prefix: str, names: list[str], n: int) -> str:
    """prefix + as many of the names as fit in n characters, then "and N more"."""
    for k in range(len(names), 0, -1):
        rest = len(names) - k
        s = prefix + (", ".join(names[:k]) + f" and {rest} more" if rest else _and(names, len(names)))
        if len(s) <= n:
            return s
    return NS._fit(prefix + (names[0] if names else ""), n, "…")


_STOP = frozenset("the and for you your our with what which how should would does are this that them they one per want "
                  "need any all each from into when who have has get its".split())


def _tokens(text: Any) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", NS._text(text).casefold()) if (len(t) > 2 or t.isdigit()) and t not in _STOP}


def overlap(a: Any, b: Any) -> float:
    """How much one question repeats another: shared words over the shorter one's words (0..1)."""
    x, y = _tokens(a), _tokens(b)
    return len(x & y) / min(len(x), len(y)) if x and y else 0.0


def entry(kind: str, name: Any, why: Any = "", fields: Any = (), what: str = "agent") -> dict[str, Any]:
    """One ledger line (6.9): {kind, what, name, fields, why}, built from what was applied, never from the prose."""
    return {"kind": kind, "what": what, "name": DS._txt(name, LEDGER_NAME_CHARS) or what,
            "fields": [f for f in _list(list(fields)) if isinstance(f, str)], "why": DS._txt(why, WHY_CHARS)}


def new_changes() -> dict[str, Any]:
    """The turn's `changes` (done event): applied = the ledger; skipped = values and ops that were not used (wrong type,
    unknown op or id, past a limit); skipped_q = questions not asked; queued = agent names held in the plan; kept_edits
    = fields the owner changed during the turn; converted = removals turned into the proposal; dropped = what was not
    applied and why; proposed = the proposal this turn made."""
    return {"applied": [], "skipped": 0, "skipped_q": 0, "queued": [], "kept_edits": [], "converted": [], "dropped": [],
            "proposed": ""}


# ---------------------------------------------------------------------------- owner edits (6.5)
def edited(owner_edits: Any, kind: str, ident: str, fld: str, rev0: int) -> bool:
    """The owner changed this field (or the whole item, field "*") on the canvas after the turn's snapshot `rev0`.
    owner_edits = {(kind, id, field): bp_rev}, kept in memory by /blueprint, /camera and /layout; kind is agent,
    workflow, camera, connector, business or policy (id "" for the last two)."""
    if not isinstance(owner_edits, dict):
        return False
    for k in ((kind, ident, fld), (kind, ident, "*")):
        rev = owner_edits.get(k)
        if isinstance(rev, int) and not isinstance(rev, bool) and rev > rev0:
            return True
    return False


def _rules(a: dict[str, Any]) -> list[str]:
    return [r for r in _list(a.get("instructions")) if isinstance(r, str) and not r.startswith(DS._CAM_RULE)]


def edits_between(before: Any, after: Any) -> set[tuple[str, str, str]]:
    """The (kind, id, field) pairs an owner's write changed, compared on the fields an op may set: what /blueprint and
    /camera record in owner_edits. Both sides are normalised blueprints, so a normalise artefact is never an edit; the
    server's own camera line is not a rule the owner wrote."""
    b = before if isinstance(before, dict) else {}
    a = after if isinstance(after, dict) else {}
    out: set[tuple[str, str, str]] = set()

    def by(bp: dict[str, Any], sec: str, key: str) -> dict[str, dict[str, Any]]:
        return {x[key]: x for x in _list(bp.get(sec)) if isinstance(x, dict) and isinstance(x.get(key), str)}

    old = by(b, "agents", "id")
    for i, x in by(a, "agents", "id").items():
        p = old.get(i)
        for f in AGENT_FIELDS if p else ():
            pv, xv = (_rules(p), _rules(x)) if f == "instructions" else (p.get(f), x.get(f))
            if pv != xv:
                out.add(("agent", i, f))
    for sec, kind, key, fields in (("workflows", "workflow", "id", WORKFLOW_FIELDS), ("cameras", "camera", "name", CAMERA_FIELDS)):
        old = by(b, sec, key)
        for i, x in by(a, sec, key).items():
            p = old.get(i)
            out |= {(kind, i, f) for f in fields if p and p.get(f) != x.get(f)}
    for sec, fields in (("business", BUSINESS_FIELDS), ("policy", POLICY_FIELDS)):
        p = b.get(sec) if isinstance(b.get(sec), dict) else {}
        x = a.get(sec) if isinstance(a.get(sec), dict) else {}
        out |= {(sec, "", f) for f in fields if p.get(f) != x.get(f)}
    return out


# ---------------------------------------------------------------------------- the camera source rule (6.5)
def _clip(src: str) -> str:
    """The clip a "sample:<clip>" source names, when the desk has that clip; else ""."""
    s = src.strip()
    if not s.lower().startswith("sample:"):
        return ""
    clip = s.split(":", 1)[1].strip()
    return clip if clip in DS.sample_clips() else ""


def _is_sample(src: Any) -> bool:
    s = str(src).strip() if isinstance(src, (str, int)) and not isinstance(src, bool) else ""
    return bool(DS.sample_name(s)) or s.lower().startswith("sample:")


def model_source(new: Any, stored: Any = None) -> str | None:
    """The source the model may write: "" or "sample:<a clip this desk has>", and over a stored source only when that
    one is empty or a sample (`stored` None = a new camera). None = the model's value is not used: an echo of the view's
    redacted form ("rtsp:// (set)"), an address (only the owner sets those, on the canvas or the Cameras page), an
    unknown clip, or any value over an address the owner set."""
    if isinstance(new, bool) or not isinstance(new, (str, int)):
        return None
    s = str(new).strip()
    if DST.is_redacted(s):
        return None
    if s:
        clip = _clip(s)
        if not clip:
            return None
        s = "sample:" + clip
    if stored is not None:
        st = str(stored).strip() if isinstance(stored, (str, int)) and not isinstance(stored, bool) else ""
        if st and not _is_sample(st):
            return None
    return s


# ---------------------------------------------------------------------------- clones (6.10, 6.4)
def _mask(text: Any, cams: list[str]) -> str:
    """Text with every camera name and every number replaced by "#", as clones are compared."""
    t = " " + _norm(text) + " "
    for c in sorted({_norm(x) for x in cams if _norm(x)}, key=len, reverse=True):
        t = re.sub(r"(?<![a-z0-9])" + re.escape(c) + r"(?![a-z0-9])", "#", t)
    return " ".join(DS._DIGITS.sub("#", t).split())


def _clone_sig(a: dict[str, Any], cams: list[str]) -> tuple[Any, ...] | None:
    """What two clones share: role, goal, tools and rules once numbers and camera names are masked. An agent with no
    goal has no definition to compare and is nobody's clone."""
    if not NS._text(a.get("goal")):
        return None
    tools = sorted(t for t in _list(a.get("tools")) if isinstance(t, str))
    return (_mask(a.get("role"), cams), _mask(a.get("goal"), cams), tuple(tools), tuple(_mask(r, cams) for r in _rules(a)))


def clone_groups(agents: list[Any], cams: list[str]) -> list[list[int]]:
    """Indices of agents that are identical apart from a number or a camera ("Camera 1 Monitor" ... "Camera 5
    Monitor" with the same role, goal, tools and rules), each group in list order, groups by their first member."""
    groups: dict[tuple[Any, ...], list[int]] = {}
    for i, a in enumerate(agents):
        s = _clone_sig(a, cams) if isinstance(a, dict) else None
        if s is not None:
            groups.setdefault(s, []).append(i)
    return [g for g in groups.values() if len(g) >= 2]


def _cam_names(bp: Any) -> list[str]:
    return [c["name"] for c in _list(bp.get("cameras") if isinstance(bp, dict) else None)
            if isinstance(c, dict) and isinstance(c.get("name"), str)]


def clones(bp: Any) -> list[list[str]]:
    """The sketch's clone groups by name: shown to the model, written as a build fact, and the eval's count."""
    agents = DST._agents(bp)
    return [[DS._txt(agents[i].get("name"), 40) or agents[i]["id"] for i in g] for g in clone_groups(agents, _cam_names(bp))]


def _collapsed_name(name: str, cams: list[str], camera_agent: bool) -> str:
    """The name of a first draft's collapsed clones: the first clone's name without its number ("Camera 1 Monitor" ->
    "Camera Monitor"); one without a number loses the camera it names ("Door Watcher" -> "Camera Watcher")."""
    flat = " ".join(name.split())
    base = " ".join(DS._DIGITS.sub(" ", flat).split()).strip(" -–—#:,.")
    if base == flat:
        words = base
        for c in sorted({_norm(x) for x in cams if _norm(x)}, key=len, reverse=True):
            words = re.sub(r"(?i)(?<![a-z0-9])" + r"[\s_-]+".join(map(re.escape, c.split())) + r"(?![a-z0-9])", " ", words)
        words = " ".join(words.split()).strip(" -–—#:,.")
        if NS._visible(words) >= 2:
            base = words
    if camera_agent and len(base.split()) < 2 and "camera" not in base.casefold():
        base = ("Camera " + base).strip()
    return DS._txt(base, 40) or DS._txt(name, 40) or "Camera Reader"


# ---------------------------------------------------------------------------- build facts and cost (6.10)
def static_facts() -> list[str]:
    """The build facts every design prompt carries, written from the build code's own numbers."""
    return [
        f"A schedule runs once a day; the first run is {SCHEDULE_FIRST_S // 3600} h after the build; no set time of day.",
        f"An inbox is checked every {INBOX_EVERY_MIN} minutes once it is connected.",
        f"A camera journal writes a note on each change (at most one every {DS.JOURNAL_MIN_GAP_S} s) and at least one "
        f"every {DS.JOURNAL_EVERY_S} s even when nothing changes; each note is {NOTE_REQUESTS} AI requests (note + check), "
        f"plus a summary every {DS.JOURNAL_ROLLUP_MIN} min.",
        "WhatsApp and Instagram reply only within 24 h of the customer's last message; a first WhatsApp message to a new "
        "contact needs an approved template.",
        "Connectors other than the web form are connected after the build.",
    ]


def _trigger(w: dict[str, Any]) -> str:
    t = w.get("trigger")
    return str(t.get("kind") or "") if isinstance(t, dict) else ""


def _steps(w: dict[str, Any]) -> list[str]:
    return [s for s in _list(w.get("steps")) if isinstance(s, str)]


def blueprint_facts(bp: Any) -> list[str]:
    """What the build will and will not do for this sketch, at most FACTS_MAX lines of FACT_CHARS: look-alike agents,
    each scheduled or inbox workflow's cadence, Hermes Agent runtimes to connect, cameras with no source yet."""
    bp = bp if isinstance(bp, dict) else {}
    out = [f"{_and(g, 5)}: identical apart from a number or a camera" for g in clones(bp)]
    for w in _list(bp.get("workflows")):
        if not isinstance(w, dict):
            continue
        name = DS._txt(w.get("name"), 60) or "A workflow"
        if _trigger(w) == "schedule":
            out.append(f"{name}: runs once a day; first run {SCHEDULE_FIRST_S // 3600} h after you build")
        elif _trigger(w) == "inbox":
            out.append(f"{name}: checks the inbox every {INBOX_EVERY_MIN} minutes once an inbox is connected")
    out += [f"{DS._txt(a.get('name'), 40) or a['id']}: runs on Hermes Agent; that runtime must be connected"
            for a in DST._agents(bp) if a.get("engine") == "hermes_agent"]
    out += [f"{c['name']}: needs a stream address or a recording before it can watch" for c in _list(bp.get("cameras"))
            if isinstance(c, dict) and isinstance(c.get("name"), str) and not str(c.get("source") or "").strip()]
    return [DST.cut(x, FACT_CHARS) for x in out][:FACTS_MAX]


def journal_requests_day() -> int:
    """One journaling camera at its floor cadence: a note (NOTE_REQUESTS requests) every JOURNAL_EVERY_S, plus a
    summary every JOURNAL_ROLLUP_MIN; 2 x 86,400 / 30 + 1,440 / 15 = 5,856 requests a day."""
    return NOTE_REQUESTS * 86400 // DS.JOURNAL_EVERY_S + 1440 // DS.JOURNAL_ROLLUP_MIN


def _pool() -> int:
    return _env_int("ATLAS_FREE_POOL_DAY", 1000, 1, 10**7)


def cost(bp: Any, pool: int | None = None, l2: int | None = None) -> dict[str, Any]:
    """The sketch's AI requests, an estimate from the formula (labelled so): journaling cameras at full rate, scheduled
    workflows once a day (at least 1 + steps a run), inbox workflows per new message (at least 1 + steps). Set against
    P, the shared free allowance (`pool`: the usage module's pool size, else ATLAS_FREE_POOL_DAY, default 1,000), or
    the owner's own-work allowance `l2` when the usage module gives one; with it, a journal is paced to about one note
    per camera every 1,440 / floor(L2 / 2 / n) minutes. `over` is true when the daily estimate passes that allowance."""
    bp = bp if isinstance(bp, dict) else {}
    wfs = [w for w in _list(bp.get("workflows")) if isinstance(w, dict)]
    n = sum(1 for c in _list(bp.get("cameras")) if isinstance(c, dict) and c.get("journal", True) is not False)
    journals = n * journal_requests_day()
    sched = [1 + len(_steps(w)) for w in wfs if _trigger(w) == "schedule"]
    inbox = [1 + len(_steps(w)) for w in wfs if _trigger(w) == "inbox"]
    p = pool if isinstance(pool, int) and not isinstance(pool, bool) and pool > 0 else _pool()
    l2 = l2 if isinstance(l2, int) and not isinstance(l2, bool) and l2 > 0 else None
    per_day = journals + sum(sched)
    paced = None
    if l2 and n:
        k = l2 // NOTE_REQUESTS // n
        paced = round(1440 / k) if k else None
    limit = l2 or p
    return {"estimate": True, "per_day": per_day, "cameras": n, "journals_per_day": journals,
            "per_camera_day": journal_requests_day(), "schedules_per_day": sum(sched), "inbox_per_message": min(inbox) if inbox else 0,
            "allowance": p, "own_work_allowance": l2, "paced_every_min": paced, "over": per_day > limit}


def cost_lines(c: dict[str, Any]) -> list[str]:
    """The estimate as the Plan strip shows it, every figure from cost()."""
    out = []
    if c.get("cameras"):
        n = c["cameras"]
        line = f"{n} camera{'s' if n != 1 else ''} journaling: ~{c['journals_per_day']:,} AI requests a day at full rate (est.)"
        if c.get("paced_every_min"):
            line += f"; paced to one note per camera every ~{c['paced_every_min']} min"
        out.append(line)
    if c.get("schedules_per_day"):
        out.append(f"Scheduled workflows: at least {c['schedules_per_day']} AI requests a day (est.)")
    if c.get("inbox_per_message"):
        out.append(f"Inbox workflows: at least {c['inbox_per_message']} AI requests per new message (est.)")
    if c.get("over") and c.get("own_work_allowance"):
        out.append(f"Over the {c['own_work_allowance']:,} AI requests a day Atlas may make on its own (est.)")
    elif c.get("over"):
        out.append(f"Over the shared allowance of {c['allowance']:,} AI requests a day (est.)")
    return [DST.cut(x, FACT_CHARS) for x in out]


def facts(bp: Any, pool: int | None = None, l2: int | None = None) -> dict[str, Any]:
    """The `facts` argument of design_state.view (what the model is shown): build facts, cost and clones."""
    return {"build_facts": blueprint_facts(bp), "cost": cost(bp, pool, l2), "clones": clones(bp)}


def public_facts(bp: Any, pool: int | None = None, l2: int | None = None) -> list[str]:
    """The `facts` argument of design_state.public_view (the Plan strip): build facts, then the cost lines."""
    return blueprint_facts(bp) + cost_lines(cost(bp, pool, l2))


# ---------------------------------------------------------------------------- consent: does the owner name it (6.3)
def _forms(kind: str, item: dict[str, Any]) -> set[str]:
    """How the owner can name an item: an agent by its full name, its id with "_" read as a space, or (for "Camera N
    Monitor") its name without the number; a camera by its name; a workflow by name or id; a connector by its name,
    its kind or the kind's label."""
    if kind == "agent":
        name = _norm(item.get("name"))
        forms = {name, _norm(item.get("id"))}
        bare = " ".join(DS._DIGITS.sub(" ", name).split())
        if bare != name:
            forms.add(bare)
    elif kind == "camera":
        forms = {_norm(item.get("name"))}
    elif kind == "workflow":
        forms = {_norm(item.get("name")), _norm(item.get("id"))}
    else:
        k = str(item.get("kind") or "")
        forms = {_norm(item.get("name")), _norm(k), _norm(I.KINDS.get(k, {}).get("label"))}
    return {f for f in forms if NS._visible(f) >= 2}


def _named_in(text: str, forms: set[str]) -> bool:
    """One of the forms, singular or plural, as whole words of the owner's message (already _norm'ed)."""
    for f in forms:
        variants = {f, f + "s", f + "es"} | ({f[:-1]} if f.endswith("s") and len(f) > 3 else set())
        if any(re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", text) for v in variants):
            return True
    return False


def named(text: Any, kind: str, item: dict[str, Any]) -> bool:
    return _named_in(_norm(text), _forms(kind, item))


# ---------------------------------------------------------------------------- proposals (6.3)
def _targets(x: dict[str, Any]) -> set[str]:
    return ({"agent:" + i for i in x["ids"]} | {"camera:" + c for c in x["cameras"]} | {"workflow:" + w for w in x["workflows"]}
            | {"connector:" + _norm(n) for n in x["connectors"]})


def proposal_sig(x: dict[str, Any]) -> str:
    """A proposal's signature in `declined`: kind and sorted agent ids ("merge:cam1,cam2"). Park and remove take the same
    agents away, so they share one ("remove:..."): an owner who said no to parking them said no to removing them, and
    the owner's wording, which picks park or remove, cannot bring a declined change back. One with no agents is signed
    by what it does take (its cameras, workflows, connectors), so two of those never share "remove:"."""
    kind = "remove" if x["kind"] in ("remove", "park") else x["kind"]
    return DST.sig(kind, x["ids"] or sorted(_targets(x)))


def covers(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """A new proposal may replace the pending one only if it takes everything that one takes."""
    return _targets(new) >= _targets(old)


def _valid(x: dict[str, Any]) -> bool:
    if x["kind"] == "merge":
        return len(x["ids"]) >= 2 and bool(x.get("into"))
    if x["kind"] == "replace":
        return len(x["ids"]) >= 1 and bool(x.get("into"))
    return bool(x["ids"] or x["cameras"] or x["workflows"] or x["connectors"])


def target_names(x: dict[str, Any], bp: Any) -> list[str]:
    """What a proposal takes, by name: agents, workflows, connectors, then its cameras in one entry."""
    names = {a["id"]: DS._txt(a.get("name"), 40) or a["id"] for a in DST._agents(bp)}
    wfs = {w.get("id"): DS._txt(w.get("name"), 60) for w in _list(bp.get("workflows") if isinstance(bp, dict) else None)
           if isinstance(w, dict)}
    out = [names.get(i) or i.replace("_", " ") for i in x["ids"]]
    out += [f"the {wfs.get(w) or w.replace('_', ' ')} workflow" for w in x["workflows"]]
    out += [f"the {c} connector" for c in x["connectors"]]
    if x["cameras"]:
        out.append(("camera " if len(x["cameras"]) == 1 else "cameras ") + ", ".join(x["cameras"]))
    return out


_PVERBS = {"remove": ("remove", "removing", "removed"), "park": ("park", "parking", "parked"),
           "merge": ("merge", "merging", "merged"), "replace": ("replace", "replacing", "replaced")}


def proposal_phrase(x: dict[str, Any], bp: Any, form: int = 0) -> str:
    """"park Door Watcher, Till Watcher and cameras door, till", "merging A and B into C" (form 0 verb, 1 -ing, 2
    past). Model-written names: embed with plain_md."""
    into = DS._txt((x.get("into") or {}).get("name"), 40) if x.get("into") else ""
    tail = f" into {into}" if x["kind"] == "merge" else f" with {into}" if x["kind"] == "replace" else ""
    return f"{_PVERBS[x['kind']][form]} {_and(target_names(x, bp), 6)}{tail}"


def bound_only(ids: list[str], bp: Any) -> list[str]:
    """Cameras every agent bound to is among `ids` (and at least one is): they go with those agents."""
    out = []
    for c in _cam_names(bp):
        who = {a["id"] for a in DST._agents(bp) if a.get("camera") == c}
        if who and who <= set(ids):
            out.append(c)
    return out


def revalidate(x: dict[str, Any], bp: Any) -> dict[str, Any] | None:
    """The proposal against the team as it is now (6.3 accept, step 1): its targets that still exist; None when a merge
    keeps fewer than 2 agents, a replace none, or a removal nothing at all."""
    bp = bp if isinstance(bp, dict) else {}
    ids = {a["id"] for a in DST._agents(bp)}
    cams = set(_cam_names(bp))
    wfs = {w.get("id") for w in _list(bp.get("workflows")) if isinstance(w, dict)}
    cons = {_norm(c.get("name")) for c in _list(bp.get("connectors")) if isinstance(c, dict)}
    y = {**x, "ids": [i for i in x["ids"] if i in ids], "cameras": [c for c in x["cameras"] if c in cams],
         "workflows": [w for w in x["workflows"] if w in wfs], "connectors": [n for n in x["connectors"] if _norm(n) in cons]}
    return y if _valid(y) else None


# ---------------------------------------------------------------------------- the plan (6.7)
def plan_room(design: dict[str, Any]) -> bool:
    """Room for one more plan item: below PLAN_MAX, or after pruning the oldest done item, else a dropped one, else
    the oldest later one. False when none can go (the plan is full: a new item is refused)."""
    plan = design["plan"]
    if len(plan) < DST.PLAN_MAX:
        return True
    for status in ("done", "dropped", "later"):
        cands = [it for it in plan if it["status"] == status]
        if cands:
            plan.remove(min(cands, key=lambda it: it["added_dturn"]))
            return True
    return False


def relink(design: dict[str, Any], removed: set[str], merged: dict[str, str] | None = None) -> None:
    """Plan links after a change to the team: merged ids point at the agent they went into, removed ids go."""
    merged = merged or {}
    for it in design["plan"]:
        out: list[str] = []
        for a in it["agents"]:
            a = merged.get(a, a)
            if a not in out and (a not in removed or a in merged.values()):
                out.append(a)
        it["agents"] = out[:DST.ITEM_AGENTS]


def settle(design: dict[str, Any], bp: Any, cur: int) -> None:
    """A queued agent the sketch now has (the same id and name, added by a turn's own ops or a full blueprint) no
    longer waits: it leaves pending_agents for the item's agents, so Continue never adds it twice."""
    have = {a["id"]: a for a in DST._agents(bp)}
    for it in design["plan"]:
        keep = []
        for d in it["pending_agents"]:
            a = have.get(d["id"])
            if a is not None and _norm(a.get("name")) == _norm(d.get("name")):
                it["agents"] = (it["agents"] + ([d["id"]] if d["id"] not in it["agents"] else []))[:DST.ITEM_AGENTS]
                it["touched_dturn"] = cur
            else:
                keep.append(d)
        it["pending_agents"] = keep


def mark_done(design: dict[str, Any], ids: set[str], cur: int) -> None:
    """The server's "done" (only it sets it): an item whose agents all exist and that waits on no queued agent."""
    for it in design["plan"]:
        if it["status"] in ("done", "dropped") or it["pending_agents"]:
            continue
        if it["agents"] and all(a in ids for a in it["agents"]):
            it["status"] = "done"
            it["touched_dturn"] = cur


def full_note() -> str:
    return f"_The plan is full ({DST.PLAN_MAX} items): drop or merge one._"


def kept_note(kept: list[dict[str, Any]], bp: Any) -> str:
    """The note for fields the owner changed on the canvas while the turn ran, which the model's change did not
    overwrite: "_Kept your edits: Writer (goal), the business details (name)._"."""
    names = {a["id"]: DS._txt(a.get("name"), 40) for a in DST._agents(bp)}
    wfs = {w.get("id"): DS._txt(w.get("name"), 60) for w in _list(bp.get("workflows") if isinstance(bp, dict) else None)
           if isinstance(w, dict)}
    by: dict[tuple[str, str], list[str]] = {}
    for k in kept:
        by.setdefault((k["kind"], k["id"]), []).append(k["field"])
    parts = []
    for (kind, ident), fields in by.items():
        what = {"agent": names.get(ident) or ident, "workflow": f"the {wfs.get(ident) or ident} workflow",
                "camera": f"camera {ident}", "business": "the business details", "policy": "the policy"}.get(kind, ident)
        parts.append(f"{plain_md(what)} ({', '.join(dict.fromkeys(fields))})")
    return f"_Kept your edits: {_and(parts, 4)}._"


# ---------------------------------------------------------------------------- questions (6.8)
def resolve_default(options: list[Any], default: Any) -> tuple[int, str]:
    """(the default option's index, how it was given): the one option marked "default": true; else an integer, where
    0 is the first option and the option count is the last (counted from 1), any other integer being ambiguous; else
    the first option. Ambiguous and unmarked both fall back to the first; the question line names it either way."""
    marks = [i for i, o in enumerate(options) if isinstance(o, dict) and o.get("default") is True]
    if len(marks) == 1:
        return marks[0], "marked"
    if isinstance(default, int) and not isinstance(default, bool):
        if default == 0:
            return 0, "zero"
        if default == len(options):
            return len(options) - 1, "last"
        return 0, "ambiguous"
    return 0, "first"


def validate_ask(ask: Any, design: dict[str, Any], resolved_now: bool = False,
                 pending: bool = False) -> tuple[dict[str, Any] | None, str]:
    """(the open question to store, "") or (None, why it is not asked). Asked only when no question is open (or the open
    one was resolved this turn) and no change waits for the owner's OK; the text is 10-160 characters ending "?"; it
    settles a decision (a slug) no resolved question settled; it has 2-3 options, each a plain "say" step under the
    next-steps contract, no two alike; and it repeats no resolved question (word overlap below 0.6)."""
    if not isinstance(ask, dict):
        return None, "type"
    if DST.open_question(design) and not resolved_now:
        return None, "open"
    if pending:
        return None, "pending"
    text = NS._text(ask.get("text"))
    if not (ASK_CHARS[0] <= len(text) <= ASK_CHARS[1]) or not text.endswith("?"):
        return None, "text"
    decides = DST._decides(ask.get("decides"))
    if not decides:
        return None, "decides"
    resolved = [q for q in design["questions"] if q["status"] != "open"]
    if any(q["decides"] == decides for q in resolved):
        return None, "resolved"
    raw = _list(ask.get("options"))
    if not DST.OPTIONS_MIN <= len(raw) <= DST.OPTIONS_MAX:
        return None, "options"
    opts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for o in raw:
        st = NS.clean(o)
        if not st or st["action"] != "say":
            return None, "options"
        ks = {"l:" + NS._key(st["label"]), "s:" + NS._key(st["say"])}
        if ks & seen:
            return None, "options"
        seen |= ks
        opts.append(st)
    if any(overlap(text, q["text"]) >= ASK_OVERLAP for q in resolved):
        return None, "repeat"
    idx, _form = resolve_default(raw, ask.get("default"))
    return {"id": None, "text": text, "decides": decides, "status": "open", "asked_dturn": 0, "options": opts,
            "default": idx, "waited": 0}, ""


def _decides_words(q: dict[str, Any]) -> str:
    return q["decides"].replace("_", " ")


def answer(design: dict[str, Any], q: dict[str, Any], label: Any, cur: int, source: str = "owner") -> dict[str, Any] | None:
    """Resolve the open question `q` with `label` (an option's label, the model's short summary, or the default): its
    options go, its answer and turn stay (so it is never asked again), and a decision is written. Returns the ledger
    line, or None when the label is no text."""
    text = DS._txt(label, DST.ANSWER_CHARS)
    if not text or q.get("status") != "open":
        return None
    q.update(status="defaulted" if source == "default" else "answered", answer=text, resolved_dturn=cur)
    for k in ("options", "default", "waited"):
        q.pop(k, None)
    design["decisions"].append({"turn": cur, "text": DST.cut(f"{_decides_words(q)}: {text}", DST.DECISION_CHARS),
                                "why": DST.cut(q["text"], DST.DECISION_CHARS), "source": source})
    return entry("default" if source == "default" else "answer", text, q["text"], what="question")


def default_label(q: dict[str, Any]) -> str:
    opts = q.get("options") or []
    return opts[q.get("default", 0)]["label"] if opts and 0 <= q.get("default", 0) < len(opts) else ""


def wait(design: dict[str, Any], cur: int) -> tuple[list[str], list[dict[str, Any]]]:
    """One design turn passes for the open question (asked on an earlier turn) while no change waits for the owner's
    OK: at 1 the "still open" note, at DEFAULT_AFTER its default is used, with an assumption, a decision and a note.
    Returns (notes, ledger lines)."""
    q = DST.open_question(design)
    if not q or DST.pending_proposal(design) or q["asked_dturn"] >= cur:
        return [], []
    q["waited"] += 1
    label = default_label(q)
    qt, lt = plain_md(q["text"]), plain_md(label)
    if q["waited"] < DEFAULT_AFTER:
        return [f"_Still open: {qt} I use {lt} after your next message._"], []
    line = answer(design, q, label, cur, source="default")
    design["assumptions"].append({"id": DST.mint(design, "a"), "text": DST.cut(f"Assumed: {label} (question not answered)",
                                                                                 DST.ASSUME_CHARS), "turn": cur})
    return [f'_No answer to "{qt}": I used {lt}. Say otherwise any time._'], [line] if line else []


def question_line(design: dict[str, Any]) -> str:
    """The line the server adds under a reply while a question is open: its text and what is used if it is skipped."""
    q = DST.open_question(design)
    return f"Question: {plain_md(q['text'])} (if you skip: {plain_md(default_label(q))})" if q else ""


_SENT_END = re.compile(r"[.!?…](?=\s|$)|\n")


def drop_reask(prose: str, design: dict[str, Any], ask_text: Any = "") -> str:
    """Prose without a trailing "?" sentence that repeats the question the block asked, the open one or a resolved one
    (word overlap >= 0.5): the server shows the question itself, and a resolved one is never asked again."""
    p = (prose or "").rstrip().rstrip("*_").rstrip()
    if not p.endswith("?"):
        return prose
    bounds = list(_SENT_END.finditer(p[:-1]))
    start = bounds[-1].end() if bounds else 0
    last = p[start:].strip()
    texts = [t for t in [ask_text] + [q["text"] for q in design["questions"]] if isinstance(t, str) and t]
    if any(overlap(last, t) >= PROSE_OVERLAP for t in texts):
        return p[:start].rstrip()
    return prose


# ---------------------------------------------------------------------------- the ledger, decision and sentence 1 (6.9)
def _shown(e: dict[str, Any]) -> str:
    n = plain_md(e["name"])
    return {"workflow": f"the {n} workflow", "camera": f"camera {n}", "connector": f"the {n} connector",
            "business": "the business details", "policy": "the policy"}.get(e.get("what"), n)


def _group(entries: list[dict[str, Any]]) -> str:
    """Ledger lines by name, agents first; a few cameras by name ("cameras door, till"), more workflows, cameras or
    connectors than that as a count ("5 cameras")."""
    parts = [_shown(e) for e in entries if e.get("what") not in ("workflow", "camera", "connector")]
    for what in ("workflow", "camera", "connector"):
        es = [e for e in entries if e.get("what") == what]
        if what == "camera" and 1 <= len(es) <= 3:
            parts.append(("camera " if len(es) == 1 else "cameras ") + ", ".join(plain_md(e["name"]) for e in es))
        elif len(es) <= 2:
            parts += [_shown(e) for e in es]
        else:
            parts.append(f"{len(es)} {what}s")
    return _and(parts, 6)


def ledger_sentence(applied: list[dict[str, Any]]) -> str:
    """One sentence of what the turn did, from the ledger alone: "Added Floor Watcher; proposed removing Door Watcher
    (waiting for your OK)." "No change to the sketch." when nothing was."""
    by: dict[str, list[dict[str, Any]]] = {}
    for e in applied:
        by.setdefault(e["kind"], []).append(e)
    clauses = []
    adds = by.get("add", []) + by.get("build", [])
    if adds:
        clauses.append("added " + _group(adds))
    if by.get("update"):
        clauses.append("changed " + _and([_shown(e) + (f" ({', '.join(e['fields'][:3])})" if e["fields"] and e["what"] in
                                                     ("agent", "workflow", "camera") else "") for e in by["update"]], 3))
    for k, verb in (("merge", "merged"), ("remove", "removed"), ("park", "parked"), ("keep", "kept")):
        if by.get(k):
            clauses.append(verb + " " + _group(by[k]))
    if by.get("queue"):
        clauses.append("queued " + _group(by["queue"]) + " for the next step")
    for k in ("accept", "decline", "undo"):
        clauses += [plain_md(e["name"]) for e in by.get(k, [])]
    clauses += ["noted your answer: " + plain_md(e["name"]) for e in by.get("answer", [])]
    clauses += ["used the default: " + plain_md(e["name"]) for e in by.get("default", [])]
    if by.get("propose"):
        clauses.append("proposed " + plain_md(by["propose"][-1]["name"]) + WAITING_TAIL)
    if not clauses:
        return "No change to the sketch."
    s = "; ".join(clauses)
    return s[0].upper() + s[1:] + ("" if s.endswith((".", "?", "!")) else ".")


def decision_for(applied: list[dict[str, Any]], cur: int) -> dict[str, Any] | None:
    """The turn's decision record: the ledger's sentence (<= 120) and its first why; never the prose."""
    if not applied:
        return None
    why = next((e["why"] for e in applied if e["why"]), "")
    return {"turn": cur, "text": DST.cut(ledger_sentence(applied).rstrip("."), DST.DECISION_CHARS),
            "why": DST.cut(why, DST.DECISION_CHARS), "source": "designer"}


# sentence 1's verb and the ledger kinds that make it true; "proposed" needs a pending proposal
_VERBS: dict[str, tuple[str, ...]] = {"drafted": ("add", "build"), "added": ("add", "build"), "changed": ("update",),
                                      "merged": ("merge", "park", "remove"), "parked": ("merge", "park", "remove"),
                                      "removed": ("merge", "park", "remove"), "proposed": ()}


def split_first(prose: str) -> tuple[str, str]:
    """(sentence 1, the rest): up to the first . ! ? or … followed by a space, or the first line break."""
    text = (prose or "").lstrip()
    m = _SENT_END.search(text)
    end = m.end() if m else len(text)
    return text[:end].strip(), text[end:]


def first_verb(sentence: str) -> str:
    m = re.match(r"[\s*_#>`~-]*([A-Za-z]+)", sentence or "")
    return m.group(1).casefold() if m else ""


@dataclass
class Turn:
    """A design turn's result, computed on copies; the turn commits all of it at once or none of it (6.14)."""
    blueprint: dict[str, Any] | None
    design: dict[str, Any]
    changes: dict[str, Any] = field(default_factory=new_changes)
    notes: list[str] = field(default_factory=list)
    decision: dict[str, Any] | None = None
    intent: str = "design"
    design_turn: bool = True          # an applied design turn: dturn advanced
    ready: bool | None = None         # the model's "ready", when it sent one
    asked: dict[str, Any] | None = None       # the question this turn asked (its options are the turn's steps)
    proposed: dict[str, Any] | None = None    # the proposal this turn made
    changed: bool = False             # the blueprint changed: a new revision


def check_sentence1(prose: str, t: Turn) -> str:
    """The reply with sentence 1 rebuilt from the ledger when it claims what did not happen: its verb has no ledger
    line (Drafted / Added need an add or a build, Changed an update, Merged / Parked / Removed a merge, park or remove,
    Proposed a pending proposal), or a removal the model made became a proposal or was not applied, or agents it
    added were queued or dropped. Otherwise the prose is returned as it was."""
    head, rest = split_first(prose)
    if not head:
        return prose
    verb = first_verb(head)
    kinds = {e["kind"] for e in t.changes["applied"]}
    bad = False
    if verb in _VERBS:
        need = _VERBS[verb]
        bad = not DST.pending_proposal(t.design) if not need else not (kinds & set(need))
    ch = t.changes
    claims_proposal = verb == "proposed" and DST.pending_proposal(t.design) is not None    # already says what happened
    if (ch["converted"] or any(d.get("what") in ("remove", "proposal") for d in ch["dropped"])) and not claims_proposal:
        bad = True
    if verb in ("drafted", "added") and (ch["queued"] or any(d.get("what") == "add" for d in ch["dropped"])):
        bad = True
    if not bad:
        return prose
    return ledger_sentence(ch["applied"]) + rest


def compute_ready(model_ready: Any, bp: Any, design: dict[str, Any]) -> bool:
    """`ready` (6.12): the model's last ready, a sketch with agents, no open question and no change waiting."""
    return (model_ready is True and bool(DST._agents(bp)) and DST.open_question(design) is None
            and DST.pending_proposal(design) is None)


# ---------------------------------------------------------------------------- validation (6.2)
def _ref(o: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = o.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
            return str(v).strip()
    return ""


def _sets(o: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    st = o.get("set")
    return {k: v for k, v in st.items() if k in fields} if isinstance(st, dict) else {}


def _why(o: dict[str, Any]) -> str:
    return DS._txt(o.get("why") if isinstance(o.get("why"), str) and o["why"].strip() else o.get("reason"), WHY_CHARS)


_SERVER_OWNED = ("layout", "color", "members", "carried_over", "workflows")


def validate(changes: Any) -> tuple[list[dict[str, Any]], int]:
    """(the ops in a canonical shape, how many were skipped): at most MAX_OPS, known ops only, each with what it needs
    (an agent, an id or name, a non-empty "set" of the fields that op may set). `why` (or its alias `reason`) is kept
    to WHY_CHARS. Ids are resolved when the op is applied, against the blueprint as it is then."""
    lst = _list(changes)
    skipped = max(0, len(lst) - MAX_OPS)
    ops: list[dict[str, Any]] = []
    for i, o in enumerate(lst[:MAX_OPS]):
        name = o.get("op").strip().lower() if isinstance(o, dict) and isinstance(o.get("op"), str) else ""
        if name not in OPS:
            skipped += 1
            continue
        op: dict[str, Any] = {"op": name, "why": _why(o), "i": i, "omitted": o.get("_omitted") is True}
        ok = True
        if name == "add_agent":
            a = o.get("agent")
            ok = isinstance(a, dict) and bool(NS._text(a.get("name")) or NS._text(a.get("id")))
            if ok:
                op["agent"] = {k: v for k, v in a.items() if k not in _SERVER_OWNED}
        elif name in ("update_agent", "keep", "remove_agent", "update_workflow", "remove_workflow", "update_camera",
                      "remove_camera", "remove_connector"):
            op["ref"] = _ref(o, "name", "kind", "id") if name == "remove_connector" else \
                _ref(o, "name", "id") if name.endswith("_camera") else _ref(o, "id", "name")
            ok = bool(op["ref"])
            if ok and name.startswith("update_"):
                op["set"] = _sets(o, {"update_agent": AGENT_FIELDS, "update_workflow": WORKFLOW_FIELDS,
                                      "update_camera": CAMERA_FIELDS}[name])
                ok = bool(op["set"])
        elif name == "build_item":
            op["ref"] = _ref(o, "plan", "id", "item")
            ok = bool(op["ref"])
        elif name == "add_workflow":
            w = o.get("workflow")
            ok = isinstance(w, dict) and bool(NS._text(w.get("name")) or NS._text(w.get("id")))
            op["workflow"] = w
        elif name == "add_camera":
            c = o.get("camera")
            c = {"name": c} if isinstance(c, str) else c
            ok = isinstance(c, dict) and bool(DS._cam_slug(c.get("name")))
            op["camera"] = c
        elif name == "add_connector":
            c = o.get("connector")
            c = {"kind": c} if isinstance(c, str) else c
            ok = isinstance(c, dict)
            op["connector"] = c
        elif name in ("set_business", "set_policy"):
            op["set"] = _sets(o, BUSINESS_FIELDS if name == "set_business" else POLICY_FIELDS)
            ok = bool(op["set"])
        if ok:
            ops.append(op)
        else:
            skipped += 1
    return ops, skipped


# ---------------------------------------------------------------------------- a full blueprint as ops (5.4, 5.5)
def _wf_shape(w: dict[str, Any], renamed: dict[str, str]) -> dict[str, Any]:
    """A workflow as normalise shapes it, its steps as ids (the model's ids read through `renamed`)."""
    shaped = DS.normalise({"workflows": [w], "agents": []})["workflows"][0]
    steps = [DS._slug(s if isinstance(s, str) else s.get("agent") if isinstance(s, dict) else "") for s in _list(w.get("steps"))]
    shaped["steps"] = list(dict.fromkeys(renamed.get(s, s) for s in steps if s and s != "agent"))
    return shaped


def _cam_shape(c: dict[str, Any]) -> dict[str, Any]:
    return {"notes": DS._txt(c.get("notes"), 200), "focus": DS._txt(c.get("focus"), 300),
            "journal": c.get("journal", True) not in (False, "0", "false", "off"),
            "alerts": c.get("alerts", False) in (True, "1", "true", "on"), "watch_for": DS._txt(c.get("watch_for"), 60) or "person"}


def _con_kind(c: Any) -> str:
    k = str((c.get("kind") if isinstance(c, dict) else c) or "").strip().lower()
    return DS._KIND_ALIAS.get(k, k)


def blueprint_ops(model_bp: Any, view_team: Any) -> list[dict[str, Any]]:
    """A full blueprint in the old schema as v2 ops against the team the model was shown (5.5; with an empty view, a
    first draft's adds, 5.4). Layout, colours and members are the server's and go first. Agents bind to the view's ids
    with the two-pass rule (6.6: the only path that binds by name); workflows by id, else exact name; cameras by name;
    connectors by name, else kind. A source equal to a redacted form is no change. Items of a section the blueprint
    lists that it leaves out become removals marked _omitted (5.5 decides: a proposal on a strict parse, kept
    silently otherwise)."""
    bp = model_bp if isinstance(model_bp, dict) else {}
    view = view_team if isinstance(view_team, dict) else {}
    view = view.get("team") if isinstance(view.get("team"), dict) else view
    ops: list[dict[str, Any]] = []
    renamed: dict[str, str] = {}
    if isinstance(bp.get("agents"), list):
        prev = [a for a in _list(view.get("agents")) if isinstance(a, dict) and isinstance(a.get("id"), str)]
        clean = [{k: v for k, v in a.items() if k not in _SERVER_OWNED} if isinstance(a, dict) else a for a in bp["agents"]]
        bound, renamed = DS._bind_agents(clean, prev, remap=True)
        by = {p["id"]: p for p in prev}
        for a, fid in bound:
            if fid == "atlas":
                continue
            if fid in by:
                old, new = DS._agent(by[fid], fid), DS._agent(a, fid)
                new["reports_to"] = renamed.get(new["reports_to"], new["reports_to"])
                # only what the model wrote: a field it left out (or a blank camera) is no change, so a lazy copy
                # never clears rules, tools or a camera binding
                sent = {f for f in AGENT_FIELDS if f in a} | ({"goal"} if "description" in a else set())
                if not new["camera"]:
                    sent.discard("camera")
                st = {f: new[f] for f in AGENT_FIELDS if f in sent and new[f] != old[f]}
                if st:
                    ops.append({"op": "update_agent", "id": fid, "set": st, "why": a.get("why") or ""})
            else:
                ops.append({"op": "add_agent", "agent": {**a, "id": fid}, "why": a.get("why") or ""})
        seen = {fid for _a, fid in bound}
        ops += [{"op": "remove_agent", "id": p["id"], "_omitted": True} for p in prev if p["id"] not in seen]
    if isinstance(bp.get("workflows"), list):
        prev_w = [w for w in _list(view.get("workflows")) if isinstance(w, dict) and isinstance(w.get("id"), str)]
        used: set[str] = set()
        for w in bp["workflows"]:
            if not isinstance(w, dict):
                continue
            wid = DS._slug(w.get("id") or w.get("name"))
            match = next((p for p in prev_w if p["id"] == wid and p["id"] not in used), None) or next(
                (p for p in prev_w if p["id"] not in used and _norm(p.get("name")) == _norm(w.get("name"))), None)
            new = _wf_shape(w, renamed)
            if match is None:
                ops.append({"op": "add_workflow", "workflow": {**w, "steps": new["steps"]}})
                continue
            used.add(match["id"])
            old = _wf_shape(match, {})
            st = {f: new[f] for f in WORKFLOW_FIELDS if f in w and new[f] != old[f]}
            if st:
                ops.append({"op": "update_workflow", "id": match["id"], "set": st})
        ops += [{"op": "remove_workflow", "id": p["id"], "_omitted": True} for p in prev_w if p["id"] not in used]
    if isinstance(bp.get("cameras"), list):
        prev_c = {c["name"]: c for c in _list(view.get("cameras")) if isinstance(c, dict) and isinstance(c.get("name"), str)}
        seen_c: set[str] = set()
        for c in bp["cameras"]:
            c = {"name": c} if isinstance(c, str) else c
            name = DS._cam_slug(c.get("name")) if isinstance(c, dict) else ""
            if not name or name in seen_c:
                continue
            seen_c.add(name)
            if name not in prev_c:
                ops.append({"op": "add_camera", "camera": c})
                continue
            old, new = _cam_shape(prev_c[name]), _cam_shape(c)
            st = {f: new[f] for f in new if f in c and new[f] != old[f]}
            src = c.get("source")
            if src is not None and not DST.is_redacted(src) and str(src).strip() != str(prev_c[name].get("source") or ""):
                st["source"] = src
            if st:
                ops.append({"op": "update_camera", "name": name, "set": st})
        ops += [{"op": "remove_camera", "name": n, "_omitted": True} for n in prev_c if n not in seen_c]
    if isinstance(bp.get("connectors"), list):
        prev_k = [c for c in _list(view.get("connectors")) if isinstance(c, dict)]
        used_k: list[int] = []
        for c in bp["connectors"]:
            kind = _con_kind(c)
            if kind not in DS.CONNECTOR_KINDS:
                continue
            name = _norm(c.get("name")) if isinstance(c, dict) else ""
            hit = next((i for i, p in enumerate(prev_k) if i not in used_k and name and _norm(p.get("name")) == name), None)
            if hit is None:                                # the only one of its kind, renamed or unnamed: the same one
                same = [i for i, p in enumerate(prev_k) if i not in used_k and p.get("kind") == kind]
                hit = same[0] if len(same) == 1 else None
            if hit is None:
                ops.append({"op": "add_connector", "connector": c})
            else:
                used_k.append(hit)
        ops += [{"op": "remove_connector", "name": p.get("name"), "_omitted": True} for i, p in enumerate(prev_k)
                if i not in used_k and p.get("name")]
    if isinstance(bp.get("business"), dict):
        old = view.get("business") if isinstance(view.get("business"), dict) else {}
        new = DS._business(bp["business"])
        st = {k: v for k, v in new.items() if v != DS._business(old).get(k)}
        if st:
            ops.append({"op": "set_business", "set": st})
    if isinstance(bp.get("policy"), dict):
        old = view.get("policy") if isinstance(view.get("policy"), dict) else {}
        o = DS.normalise({"policy": old})["policy"]
        n = DS.normalise({"policy": bp["policy"]}, {"policy": old})["policy"]
        st = {k: n[k] for k in POLICY_FIELDS if k in bp["policy"] and n[k] != o[k]}
        if st:
            ops.append({"op": "set_policy", "set": st})
    return ops


# ---------------------------------------------------------------------------- applying (6.3 - 6.7)
@dataclass(eq=False)
class _Cand:
    """One new agent this turn: an add_agent op's definition, or a queued definition a build_item draws."""
    agent: dict[str, Any]
    mid: str                                  # the id it asks for: the model's id, else its name as a slug
    name: str
    why: str = ""
    op: dict[str, Any] | None = None
    item: dict[str, Any] | None = None        # build_item: the plan item it waits in
    pdef: dict[str, Any] | None = None        # build_item: its queued definition
    workflows: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    drawn: bool = False
    aid: str = ""

    def __post_init__(self) -> None:
        self.keys |= {self.mid, DS._slug(self.name), _norm(self.name)}


class _Work:
    """The blueprint and design state of one turn, on copies, with what was applied."""

    def __init__(self, bp1: Any, design: dict[str, Any], *, text: Any = "", strict: bool = True, owner_edits: Any = None,
                 rev0: int = 0, cur: int = 0):
        self.bp1 = bp1 if isinstance(bp1, dict) else None
        empty = DS.normalise({})
        self.bp = DS.normalise(copy.deepcopy(self.bp1), self.bp1, remap=False) if self.bp1 else empty
        for k, v in empty.items():
            self.bp.setdefault(k, copy.deepcopy(v))
        self.design = design
        self.text = text if isinstance(text, str) else ""
        self.norm_text = _norm(self.text)
        self.removal_words = bool(DS.REMOVAL_WORDS.search(self.text))
        self.strict = strict
        self.owner_edits = owner_edits
        self.rev0 = rev0
        self.cur = cur
        self.changes = new_changes()
        self.notes: list[str] = []
        self.idmap: dict[str, str] = {}       # an id the model used -> the id the agent got (collapsed clones)
        self.namemap: dict[str, str] = {}     # a collapsed clone's name -> the agent it went into
        self.removed: set[str] = set()
        self.merged: dict[str, str] = {}
        self.reports: dict[str, Any] = {}     # agent id -> the lead its op named (resolved once every agent is in)
        self.wf_steps: dict[str, list[Any]] = {}   # workflow id -> the steps its op named (resolved likewise)
        self.overflow: list[_Cand] = []
        self.unconsented: list[tuple[str, str, bool, str]] = []   # (kind, key, owner asked, why)
        self.keeps = 0
        self.touched = False

    # ------------------------------------------------------------ lookups
    def log(self, kind: str, name: Any, why: Any = "", fields: Any = (), what: str = "agent") -> None:
        self.changes["applied"].append(entry(kind, name, why, fields, what))

    def skip(self, n: int = 1) -> None:
        self.changes["skipped"] += n

    def drop(self, what: str, name: Any, reason: str) -> None:
        self.changes["dropped"].append({"what": what, "name": DS._txt(name, LEDGER_NAME_CHARS), "reason": reason})

    def agent_ids(self) -> set[str]:
        return {a["id"] for a in self.bp["agents"]}

    def agent_by_id(self, aid: str) -> dict[str, Any] | None:
        return next((a for a in self.bp["agents"] if a["id"] == aid), None)

    def agent_ref(self, ref: Any) -> str:
        """The id of the agent `ref` names: its id, an id the model used this turn, or its exact name; else ""."""
        if not isinstance(ref, str) or not ref.strip():
            return ""
        ids = self.agent_ids()
        r = ref.strip()
        if r in ids:
            return r
        m = self.idmap.get(r) or self.idmap.get(DS._slug(r))
        if m in ids:
            return m
        k = _norm(r)
        for a in self.bp["agents"]:
            if _norm(a.get("name")) == k or _norm(a["id"]) == k:
                return a["id"]
        m = self.namemap.get(k)
        return m if m in ids else ""

    def find_agent(self, ref: Any) -> dict[str, Any] | None:
        aid = self.agent_ref(ref)
        return self.agent_by_id(aid) if aid else None

    def find_workflow(self, ref: Any) -> dict[str, Any] | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        wfs = self.bp["workflows"]
        return next((w for w in wfs if w["id"] == ref.strip()), None) or next(
            (w for w in wfs if _norm(w["id"]) == _norm(ref) or _norm(w.get("name")) == _norm(ref)), None)

    def find_camera(self, ref: Any) -> dict[str, Any] | None:
        slug = DS._cam_slug(ref)
        return next((c for c in self.bp["cameras"] if slug and c["name"] == slug), None) or next(
            (c for c in self.bp["cameras"] if _norm(c["name"]) == _norm(ref) and _norm(ref)), None)

    def find_connector(self, ref: Any) -> dict[str, Any] | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        cons = self.bp["connectors"]
        hit = next((c for c in cons if _norm(c.get("name")) == _norm(ref)), None)
        if hit:
            return hit
        kind = _con_kind(ref)
        same = [c for c in cons if c.get("kind") == kind or _norm(I.KINDS.get(c.get("kind"), {}).get("label")) == _norm(ref)]
        return same[0] if len(same) == 1 else None

    def find(self, kind: str, ref: Any) -> dict[str, Any] | None:
        return {"agent": self.find_agent, "workflow": self.find_workflow, "camera": self.find_camera,
                "connector": self.find_connector}[kind](ref)

    @staticmethod
    def key_of(kind: str, item: dict[str, Any]) -> str:
        return item["id"] if kind in ("agent", "workflow") else item["name"]

    @staticmethod
    def name_of(kind: str, item: dict[str, Any]) -> str:
        return DS._txt(item.get("name"), 60) or str(item.get("id") or item.get("kind") or kind)

    def plan_item(self, ref: str) -> dict[str, Any] | None:
        return next((it for it in self.design["plan"] if it["id"] == ref), None)

    def unedited(self, kind: str, ident: str, sets: dict[str, Any]) -> dict[str, Any]:
        """`sets` without the fields the owner changed during the turn: those stay the owner's (kept_edits)."""
        out = {}
        for f, v in sets.items():
            if edited(self.owner_edits, kind, ident, f, self.rev0):
                self.changes["kept_edits"].append({"kind": kind, "id": ident, "field": f})
            else:
                out[f] = v
        return out

    # ------------------------------------------------------------ removals and consent (6.3)
    def remove_item(self, kind: str, item: dict[str, Any] | None, why: str = "", log: bool = True, strip: bool = True) -> None:
        if item is None:
            return
        if kind == "agent":
            aid = item["id"]
            self.bp["agents"] = [a for a in self.bp["agents"] if a["id"] != aid]
            self.removed.add(aid)
            self.bp["layout"].pop(aid, None)
            for a in self.bp["agents"]:
                if a.get("reports_to") == aid:
                    a["reports_to"] = self.merged.get(aid, "atlas")
            for w in list(self.bp["workflows"]) if strip else ():
                if aid in w["steps"]:
                    w["steps"] = [s for s in w["steps"] if s != aid]
                    if not w["steps"] and w["id"] not in self.wf_steps:      # nothing left to run: it goes too
                        self.bp["workflows"].remove(w)
                        self.log("remove", w.get("name"), "no steps left", what="workflow")
        else:
            sec = {"workflow": "workflows", "camera": "cameras", "connector": "connectors"}[kind]
            self.bp[sec] = [x for x in self.bp[sec] if x is not item]
            if kind == "workflow":
                self.wf_steps.pop(item["id"], None)
        if log:
            self.log("remove", self.name_of(kind, item), why, what=kind)
        self.touched = True

    def consent(self, ops: list[dict[str, Any]]) -> None:
        """Every removal first (6.3): applied when the owner's message names the item and asks for a removal and the
        block parsed strictly; an omission on a parse that is not strict is kept silently (5.5) unless the owner asked
        for it; every other removal waits as part of the turn's proposal."""
        for op in ops:
            kind = REMOVE_OPS.get(op["op"])
            if not kind:
                continue
            op["done"] = True
            item = self.find(kind, op["ref"])
            if item is None:
                if not op["omitted"]:
                    self.skip()
                continue
            asked = self.removal_words and _named_in(self.norm_text, _forms(kind, item))
            if asked and self.strict:
                self.remove_item(kind, item, op["why"])
            elif op["omitted"] and not self.strict and not asked:
                continue
            else:
                self.unconsented.append((kind, self.key_of(kind, item), asked, op["why"]))

    # ------------------------------------------------------------ new agents: collapse, cap, queue (6.4)
    def candidates(self, ops: list[dict[str, Any]]) -> list[_Cand]:
        present = {a["id"]: a for a in self.bp["agents"]}
        cands: list[_Cand] = []
        seen: dict[str, _Cand] = {}
        items: set[str] = set()
        for op in ops:
            if op.get("done"):
                continue
            if op["op"] == "add_agent":
                d = op["agent"]
                mid = DS._slug(d.get("id") or d.get("name"))
                name = DS._txt(d.get("name"), 40) or mid.replace("_", " ").title()
                if mid == "atlas":
                    op["done"] = True
                    self.skip()
                    continue
                if mid in present and _norm(present[mid].get("name")) == _norm(name):
                    op["as_update"] = mid                  # the same agent again: its fields update it
                    continue
                if mid in seen and _norm(seen[mid].name) == _norm(name):
                    op["dup_of"] = seen[mid]
                    continue
                c = _Cand(agent=d, mid=mid, name=name, why=op["why"], op=op)
                op["cand"] = c
                cands.append(c)
                seen.setdefault(mid, c)
            elif op["op"] == "build_item":
                item = self.plan_item(op["ref"])
                if item is None or not item["pending_agents"] or item["id"] in items:
                    op["done"] = True
                    self.skip()
                    continue
                items.add(item["id"])
                why = op["why"] or f"from the plan: {item['title']}"
                op["cands"] = [_Cand(agent=d, mid=d["id"], name=d["name"], why=why, op=op, item=item,
                                     pdef=d, workflows=list(d.get("workflows") or [])) for d in item["pending_agents"]
                               if not (d["id"] in present and _norm(present[d["id"]].get("name")) == _norm(d["name"]))]   # settle() takes those
                cands += op["cands"]
        return cands

    def collapse(self, cands: list[_Cand], ops: list[dict[str, Any]]) -> list[_Cand]:
        """A first draft's look-alike agents become one (6.4): the first clone's definition, its name without the
        number, camera "" (it reads every journal; a per-camera role becomes the reader's), every clone's cameras kept.
        References to any clone (workflow steps, leads, plan links) go to it."""
        adds = [c for c in cands if c.item is None]
        cams = _cam_names(self.bp) + [DS._cam_slug(op["camera"].get("name")) for op in ops if op["op"] == "add_camera"]
        shaped = [DS._agent(c.agent, c.mid) for c in adds]
        for g in clone_groups(shaped, cams):
            members = [adds[i] for i in g]
            first, sh = members[0], shaped[g[0]]
            camera_agent = any(t in sh["tools"] for t in DS._CAM_TOOLS)
            name = _collapsed_name(first.name, cams, camera_agent)
            agent = {**first.agent, "name": name, "camera": ""}
            if camera_agent and not DS._CAM_READS.search(f"{name} {sh['role']} {sh['goal']}"):
                agent["role"] = READER_ROLE
            taken = self.agent_ids() | {c.mid for c in cands if c not in members} | {"atlas"}
            mid = DS._slug(name)
            mid = mid if mid not in taken else DS._suffixed(mid, taken) or first.mid
            new = _Cand(agent={**agent, "id": mid}, mid=mid, name=name, why=first.why, op=first.op,
                        keys=set().union(*(m.keys for m in members)))
            first.op["cand"] = new
            for m in members:
                self.idmap[m.mid] = mid
                self.namemap[_norm(m.name)] = mid
                if m is not first:
                    m.op["done"] = True
            cands = [new if c is first else c for c in cands if c is first or c not in members]
            kind = "camera agents" if camera_agent else "agents"
            split = "one per camera" if camera_agent else "keep them separate"
            self.notes.append(f"_Merged {len(members)} look-alike {kind} into one {plain_md(name)}; say \"{split}\" to split them._")
        return cands

    def draw(self, cands: list[_Cand], ops: list[dict[str, Any]]) -> None:
        """Which new agents this turn adds (6.4): at most MAX_NEW_AGENTS, within the 8-agent sketch; leads (agents
        another new agent reports to) first, then agents a workflow names, then the rest, each in op order."""
        leads: set[str] = set()
        for c in cands:
            rt = c.agent.get("reports_to")
            if isinstance(rt, str) and rt.strip():
                leads |= {DS._slug(rt), _norm(rt)}
        leads -= {"atlas"}
        named_wf: set[str] = set()
        for op in ops:
            steps = _list(op["workflow"].get("steps")) if op["op"] == "add_workflow" else \
                _list(op["set"].get("steps")) if op["op"] == "update_workflow" else []
            for s in steps:
                if isinstance(s, str):
                    named_wf |= {DS._slug(s), _norm(s)}

        def rank(c: _Cand) -> int:
            return 0 if c.keys & leads else 1 if (c.keys & named_wf or c.workflows) else 2

        room = max(0, min(MAX_NEW_AGENTS, TEAM_MAX - len(self.bp["agents"])))
        for i in sorted(range(len(cands)), key=lambda i: (rank(cands[i]), i))[:room]:
            cands[i].drawn = True

    def add(self, c: _Cand) -> bool:
        if len(self.bp["agents"]) >= TEAM_MAX:
            return False
        taken = self.agent_ids() | {"atlas"}
        aid = c.mid if c.mid not in taken else DS._suffixed(c.mid, taken)
        if not aid:
            return False
        agent = DS._agent(c.agent, aid)
        self.bp["agents"].append(agent)
        c.aid = aid
        for k, v in list(self.idmap.items()):
            if v == c.mid:
                self.idmap[k] = aid
        if "reports_to" in c.agent:
            self.reports[aid] = c.agent.get("reports_to")
        self.log("add", agent["name"], c.why)
        self.touched = True
        return True

    def draw_queued(self, c: _Cand) -> bool:
        """Add one queued definition (build_item, Continue): its own id unless one is there now, back into the
        workflows that listed it when they still exist; the item is done once nothing waits in it."""
        item, d = c.item, c.pdef
        if item is None or d is None or not any(x is d for x in item["pending_agents"]):
            return False
        there = self.agent_by_id(d["id"])
        if there is not None and _norm(there.get("name")) == _norm(d.get("name")):    # already in the sketch: settled
            item["pending_agents"] = [x for x in item["pending_agents"] if x is not d]
            item["agents"] = (item["agents"] + ([d["id"]] if d["id"] not in item["agents"] else []))[:DST.ITEM_AGENTS]
            item["status"] = "done" if not item["pending_agents"] else item["status"]
            return False
        if len(self.bp["agents"]) >= TEAM_MAX:
            return False
        taken = self.agent_ids() | {"atlas"}
        aid = d["id"] if d["id"] not in taken else DS._suffixed(d["id"], taken)
        if not aid:
            return False
        agent = DS._agent({k: v for k, v in d.items() if k != "workflows"}, aid)
        self.bp["agents"].append(agent)
        c.aid = aid
        for wid in d.get("workflows") or []:
            w = next((w for w in self.bp["workflows"] if w["id"] == wid), None)
            if w is not None:
                steps = self.wf_steps[wid] if wid in self.wf_steps else w["steps"]
                if aid not in steps:
                    steps.append(aid)
        item["pending_agents"] = [x for x in item["pending_agents"] if x is not d]
        if d["id"] != aid:
            relink(self.design, set(), {d["id"]: aid})
        if aid not in item["agents"]:
            item["agents"] = (item["agents"] + [aid])[:DST.ITEM_AGENTS]
        if not item["pending_agents"]:
            item["status"] = "done"
        item["touched_dturn"] = self.cur
        self.log("build", agent["name"], c.why or f"from the plan: {item['title']}")
        self.touched = True
        return True

    def queue(self) -> None:
        """New agents past the cap wait in one plan item (6.4): "Add <names>", next, their clamped definitions with the
        workflows that listed them. At most PENDING_MAX wait across the plan; a full plan refuses the item."""
        if not self.overflow:
            return
        room = DST.PENDING_MAX - sum(len(it["pending_agents"]) for it in self.design["plan"])
        take, lost = self.overflow[:max(0, room)], self.overflow[max(0, room):]
        for c in lost:
            self.drop("add", c.name, f"{DST.PENDING_MAX} agents already wait in the plan")
        if take and not plan_room(self.design):
            if full_note() not in self.notes:
                self.notes.append(full_note())
            for c in take:
                self.drop("add", c.name, "the plan is full")
            return
        defs = [(c, DST.agent_def({**c.agent, "id": c.mid, "workflows": c.workflows,     # a lead named by name, as its id
                                   **({"reports_to": self.agent_ref(c.agent["reports_to"]) or c.agent["reports_to"]}
                                      if isinstance(c.agent.get("reports_to"), str) else {})}, workflows=True)) for c in take]
        defs = [(c, d) for c, d in defs if d]
        if not defs:
            return
        names = [d["name"] for _c, d in defs]
        self.design["plan"].append({
            "id": DST.mint(self.design, "p"), "title": _fit_names("Add ", names, DST.TITLE_CHARS), "status": "next",
            "why": DST.cut(f"{defs[0][1]['role']}; queued ({MAX_NEW_AGENTS} new agents a turn)", DST.WHY_CHARS),
            "agents": [], "pending_agents": [d for _c, d in defs], "added_dturn": self.cur, "touched_dturn": self.cur})
        self.changes["queued"] = names
        for c, d in defs:
            self.log("queue", d["name"], c.why)
        self.notes.append(f"_Queued in the plan: {plain_md(_and(names))} (at most {MAX_NEW_AGENTS} new agents a turn)._")

    def queued_match(self, ref: Any) -> _Cand | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        keys = {ref.strip(), DS._slug(ref), _norm(ref)}
        return next((c for c in self.overflow if c.keys & keys), None)

    # ------------------------------------------------------------ the other ops (6.2, 6.5)
    def update_agent(self, a: dict[str, Any], sets: dict[str, Any], why: str) -> None:
        sets = self.unedited("agent", a["id"], {k: v for k, v in sets.items() if k in AGENT_FIELDS})
        if not sets:
            return
        if "reports_to" in sets:
            self.reports[a["id"]] = sets["reports_to"]
            lead = self.agent_ref(sets["reports_to"])
            sets["reports_to"] = lead or sets["reports_to"]
        before = DS._agent(a, a["id"])
        new = DS._agent({**a, **sets}, a["id"])
        changed = [f for f in AGENT_FIELDS if new[f] != before[f]]
        if not changed:
            return
        i = next(i for i, x in enumerate(self.bp["agents"]) if x is a)
        self.bp["agents"][i] = {**new, **{k: a[k] for k in ("color", "members") if k in a}}
        self.log("update", new["name"], why, changed)
        self.touched = True

    def workflow(self, op: dict[str, Any]) -> None:
        if op["op"] == "add_workflow":
            w = op["workflow"]
            wid0 = DS._slug(w.get("id") or w.get("name"))
            name = DS._txt(w.get("name"), 60)
            ex = next((x for x in self.bp["workflows"] if x["id"] == wid0), None)
            if ex is not None and (not name or _norm(name) == _norm(ex.get("name"))):
                self.update_workflow(ex, {k: w[k] for k in WORKFLOW_FIELDS if k in w}, op["why"])
                return
            if len(self.bp["workflows"]) >= WORKFLOWS_MAX:
                self.skip()
                self.drop("add", name or wid0, f"a sketch holds {WORKFLOWS_MAX} workflows")
                return
            taken = {x["id"] for x in self.bp["workflows"]}
            wid = wid0 if wid0 not in taken else DS._suffixed(wid0, taken)
            if not wid:
                self.skip()
                return
            trig = w.get("trigger") if isinstance(w.get("trigger"), dict) else {"kind": str(w.get("trigger") or "manual")}
            self.bp["workflows"].append({"id": wid, "name": name or f"Workflow {len(taken) + 1}", "trigger": trig, "steps": []})
            self.wf_steps[wid] = list(_list(w.get("steps")))
            self.log("add", name or wid, op["why"], what="workflow")
            self.touched = True
            return
        w = self.find_workflow(op["ref"])
        if w is None:
            self.skip()
            return
        self.update_workflow(w, op["set"], op["why"])

    def update_workflow(self, w: dict[str, Any], sets: dict[str, Any], why: str) -> None:
        sets = self.unedited("workflow", w["id"], sets)
        changed = []
        if "name" in sets and DS._txt(sets["name"], 60) and DS._txt(sets["name"], 60) != w.get("name"):
            w["name"] = DS._txt(sets["name"], 60)
            changed.append("name")
        if "trigger" in sets:
            t = sets["trigger"] if isinstance(sets["trigger"], dict) else {"kind": str(sets["trigger"] or "manual")}
            shaped = DS.normalise({"workflows": [{"name": "x", "trigger": t}]})["workflows"][0]["trigger"]
            if shaped != w.get("trigger"):
                w["trigger"] = shaped
                changed.append("trigger")
        if "steps" in sets and isinstance(sets["steps"], list):
            now = [self.agent_ref(s if isinstance(s, str) else s.get("agent") if isinstance(s, dict) else "") for s in sets["steps"]]
            if [s for s in now if s] != w["steps"]:
                changed.append("steps")
            self.wf_steps[w["id"]] = list(sets["steps"])
        if changed:
            self.log("update", w.get("name"), why, changed, what="workflow")
            self.touched = True

    def camera(self, op: dict[str, Any]) -> None:
        if op["op"] == "add_camera":
            c = op["camera"]
            name = DS._cam_slug(c.get("name"))
            ex = next((x for x in self.bp["cameras"] if x["name"] == name), None)
            if ex is not None:
                self.update_camera(ex, {k: c[k] for k in CAMERA_FIELDS if k in c}, op["why"])
                return
            if len(self.bp["cameras"]) >= CAMERAS_MAX:
                self.skip()
                self.drop("add", name, f"a sketch holds {CAMERAS_MAX} cameras")
                return
            src = model_source(c.get("source"))
            self.bp["cameras"].append({"name": name, "source": src or "", **_cam_shape(c)})
            self.log("add", name, op["why"], what="camera")
            self.touched = True
            return
        cam = self.find_camera(op["ref"])
        if cam is None:
            self.skip()
            return
        self.update_camera(cam, op["set"], op["why"])

    def update_camera(self, cam: dict[str, Any], sets: dict[str, Any], why: str) -> None:
        sets = self.unedited("camera", cam["name"], sets)
        new = {**cam}
        if "source" in sets:
            src = model_source(sets["source"], cam.get("source") or "")
            if src is not None:
                new["source"] = src
            elif not DST.is_redacted(sets["source"]):
                self.skip()                                # an address, an unknown clip, or over the owner's address
        shaped = _cam_shape({**cam, **{k: v for k, v in sets.items() if k != "source"}})
        new.update(shaped)
        changed = [f for f in CAMERA_FIELDS if new.get(f) != cam.get(f)]
        if changed:
            cam.update(new)
            self.log("update", cam["name"], why, changed, what="camera")
            self.touched = True

    def connector(self, op: dict[str, Any]) -> None:
        c = op["connector"]
        kind = _con_kind(c)
        if kind not in DS.CONNECTOR_KINDS:
            self.skip()
            return
        name = DS._txt(c.get("name"), 40) or DS._txt(DS.T_KIND_LABEL[kind], 40)
        if any(x.get("kind") == kind and _norm(x.get("name")) == _norm(name) for x in self.bp["connectors"]):
            return
        if len(self.bp["connectors"]) >= CONNECTORS_MAX:
            self.skip()
            self.drop("add", name, f"a sketch holds {CONNECTORS_MAX} connectors")
            return
        self.bp["connectors"].append({"kind": kind, "name": name, "purpose": DS._txt(c.get("purpose"), 160),
                                      "required": bool(c.get("required", True))})
        self.log("add", name, op["why"], what="connector")
        self.touched = True

    def business(self, sets: dict[str, Any], why: str) -> None:
        sets = self.unedited("business", "", sets)
        old = self.bp["business"]
        new = DS._business({**old, **sets})
        changed = [f for f in BUSINESS_FIELDS if new.get(f) != old.get(f)]
        if changed:
            self.bp["business"] = new
            self.log("update", "Business", why, changed, what="business")
            self.touched = True

    def policy(self, sets: dict[str, Any], why: str) -> None:
        """Policy changes; loosening one (no money figures lifted, a banned phrase taken out) only when the owner's
        message names it ("money", "price", "figure", or the phrase): else the rule stays, with a note."""
        sets = self.unedited("policy", "", sets)
        old = self.bp["policy"]
        new = dict(old)
        if "no_money_figures" in sets:
            v = sets["no_money_figures"] is not False
            if old.get("no_money_figures", True) and not v and not _MONEY.search(self.text):
                self.notes.append("_Kept the rule: no money figures; you did not ask to change it._")
            else:
                new["no_money_figures"] = v
        if "max_words" in sets:
            new["max_words"] = DS._words_cap(sets["max_words"], DS._words_cap(old.get("max_words"), 220))
        if isinstance(sets.get("banned_phrases"), (list, str)):         # null or a number: no change
            b = sets["banned_phrases"]
            b = b.split(",") if isinstance(b, str) else b
            given = [p for p in (DS._txt(x, 120) for x in _list(b)) if p]
            gone = [p for p in old.get("banned_phrases") or [] if _norm(p) not in {_norm(x) for x in given}]
            kept = [p for p in gone if _norm(p) not in self.norm_text]
            for p in kept:
                self.notes.append(f'_Kept the rule: never write "{plain_md(p)}"; you did not ask to change it._')
            new["banned_phrases"] = list(dict.fromkeys(given + kept))[:20]
        changed = [f for f in POLICY_FIELDS if new.get(f) != old.get(f)]
        if changed:
            self.bp["policy"] = new
            self.log("update", "Policy", why, changed, what="policy")
            self.touched = True

    # ------------------------------------------------------------ the turn's ops, end to end
    def apply(self, ops: list[dict[str, Any]], first_draft: bool) -> None:
        self.consent(ops)
        cands = self.candidates(ops)
        if first_draft:
            cands = self.collapse(cands, ops)
        self.draw(cands, ops)
        for op in ops:
            if op.get("done"):
                continue
            k = op["op"]
            if k == "add_agent":
                if "as_update" in op or "dup_of" in op:
                    a = self.agent_by_id(op["as_update"]) if "as_update" in op else self.agent_by_id(op["dup_of"].aid)
                    if a is not None:                      # the same name (as names compare): no rename
                        self.update_agent(a, {f: op["agent"][f] for f in AGENT_FIELDS if f in op["agent"] and f != "name"}, op["why"])
                    else:
                        self.skip()
                    continue
                c = op["cand"]
                if not (c.drawn and self.add(c)):
                    self.overflow.append(c)
            elif k == "build_item":
                for c in op["cands"]:
                    if c.drawn:
                        self.draw_queued(c)
            elif k == "update_agent":
                a = self.find_agent(op["ref"])
                if a is None:
                    self.skip()
                else:
                    self.update_agent(a, op["set"], op["why"])
            elif k == "keep":
                a = self.find_agent(op["ref"])
                if a is None or self.keeps >= KEEP_MAX:
                    self.skip()
                else:
                    self.keeps += 1
                    self.log("keep", a["name"], op["why"])
            elif k in ("add_workflow", "update_workflow"):
                self.workflow(op)
            elif k in ("add_camera", "update_camera"):
                self.camera(op)
            elif k == "add_connector":
                self.connector(op)
            elif k == "set_business":
                self.business(op["set"], op["why"])
            elif k == "set_policy":
                self.policy(op["set"], op["why"])

    def finish(self) -> dict[str, Any] | None:
        """Resolve what the ops named by name (workflow steps, leads: an agent added later in the list counts), then
        normalise with ids authoritative (remap=False) against bp1's agents still present (their colours stay).
        The blueprint as bp1 left it when no op changed anything."""
        for wid, raw in self.wf_steps.items():
            w = next((x for x in self.bp["workflows"] if x["id"] == wid), None)
            if w is None:
                continue
            steps: list[str] = []
            for r in raw:
                r = r.get("agent") or r.get("id") if isinstance(r, dict) else r
                aid = self.agent_ref(r)
                if aid and aid not in steps:
                    steps.append(aid)
                elif not aid:
                    q = self.queued_match(r)
                    if q is not None and wid not in q.workflows:
                        q.workflows.append(wid)
            w["steps"] = steps
        for aid, raw in self.reports.items():
            a = self.agent_by_id(aid)
            if a is not None:
                lead = self.agent_ref(raw)
                a["reports_to"] = lead if lead and lead != aid else "atlas"
        if not self.touched:
            return self.bp1
        ids = self.agent_ids()
        prev = {**self.bp1, "agents": [a for a in _list(self.bp1.get("agents")) if isinstance(a, dict) and a.get("id") in ids]} \
            if self.bp1 else None
        out = DS.normalise(self.bp, prev, remap=False)
        out.pop("carried_over", None)
        self.bp = out
        return out

    # ------------------------------------------------------------ the plan, proposals, questions, assumptions
    def plan_agents(self, refs: list[Any]) -> list[str]:
        """A plan item's agents: ids, names of agents in the sketch (this turn's included), queued agents by their
        definition's id, or the slug of an agent still to come (a planned agent; done once one with that id exists)."""
        out: list[str] = []
        for r in refs[:12]:
            if not isinstance(r, str) or not r.strip():
                continue
            aid = self.agent_ref(r)
            if not aid:
                q = self.queued_match(r)
                aid = DS._slug(q.mid) if q else DS._slug(r)
            if aid and aid not in ("agent", "atlas") and aid not in out and aid not in self.removed:
                out.append(aid)
        return out[:DST.ITEM_AGENTS]

    def plan(self, entries: list[dict[str, Any]]) -> None:
        """The block's plan upserts (5.3, 6.7): a known id merges into that item; an item without one merges into a
        live item with the same title or the same agents, else it is new (at the end, room permitting). Only the
        server sets "done"."""
        plan = self.design["plan"]
        refused = False
        for e in entries:
            eid = e.get("id") if isinstance(e.get("id"), str) else ""
            item = next((it for it in plan if it["id"] == eid), None) if eid else None
            title = DS._txt(e.get("title"), DST.TITLE_CHARS)
            title = title if NS._visible(title) >= 2 else ""
            agents = self.plan_agents(e["agents"]) if isinstance(e.get("agents"), list) else None
            status = e.get("status").strip().lower() if isinstance(e.get("status"), str) else ""
            if status and (status == "done" or status not in DST.PLAN_STATUS):
                self.skip()
                status = ""
            why = DS._txt(e.get("why") if isinstance(e.get("why"), str) else e.get("reason"), DST.WHY_CHARS)
            if item is None:
                k = _norm(title)
                item = next((it for it in plan if it["status"] != "dropped" and (
                    (k and _norm(it["title"]) == k) or (agents and set(agents) == set(it["agents"])))), None)
            if item is None:
                if not title:
                    self.skip()
                    continue
                if not plan_room(self.design):
                    refused = True
                    continue
                plan.append({"id": DST.mint(self.design, "p"), "title": title, "status": status or "later", "why": why,
                             "agents": agents or [], "pending_agents": [], "added_dturn": self.cur, "touched_dturn": self.cur})
                continue
            if title:
                item["title"] = title
            if why:
                item["why"] = why
            if status and status != item["status"]:
                item["status"] = status
                item["touched_dturn"] = self.cur
            if agents is not None and agents != item["agents"]:
                item["agents"] = agents
                item["touched_dturn"] = self.cur
        if refused:
            self.changes["skipped"] += 1
            if full_note() not in self.notes:
                self.notes.append(full_note())

    def refs(self, v: Any, find: Callable[[Any], str]) -> list[str]:
        out: list[str] = []
        for r in _list(v)[:DST.PROPOSAL_LIST * 2]:
            x = find(r)
            if x and x not in out:
                out.append(x)
        return out[:DST.PROPOSAL_LIST]

    def asked_all(self, x: dict[str, Any]) -> bool:
        """The owner's message asks for this removal (6.3 rules 1-2): a removal word and every target named."""
        if not self.removal_words:
            return False
        items = ([("agent", self.agent_by_id(i)) for i in x["ids"]] + [("camera", self.find_camera(c)) for c in x["cameras"]]
                 + [("workflow", self.find_workflow(w)) for w in x["workflows"]]
                 + [("connector", self.find_connector(n)) for n in x["connectors"]])
        return bool(items) and all(it is not None and _named_in(self.norm_text, _forms(k, it)) for k, it in items)

    def model_proposal(self, p: dict[str, Any]) -> dict[str, Any] | None:
        kind = p.get("kind").strip().lower() if isinstance(p.get("kind"), str) else ""
        if kind not in DST.PROPOSAL_KINDS:
            self.skip()
            return None
        ids = self.refs(p.get("ids"), self.agent_ref)
        x = {"id": None, "dturn": self.cur, "kind": kind, "ids": ids,
             "cameras": self.refs(p.get("cameras"), lambda r: (self.find_camera(r) or {}).get("name", "")),
             "workflows": self.refs(p.get("workflows"), lambda r: (self.find_workflow(r) or {}).get("id", "")),
             "connectors": self.refs(p.get("connectors"), lambda r: (self.find_connector(r) or {}).get("name", "")),
             "into": DST.agent_def(p.get("into")) if kind in ("merge", "replace") else None,
             "reason": DS._txt(p.get("reason") if isinstance(p.get("reason"), str) else p.get("why"), DST.REASON_CHARS),
             "status": "pending"}
        if kind in ("remove", "park"):
            x["cameras"] = (x["cameras"] + [c for c in bound_only(ids, self.bp) if c not in x["cameras"]])[:DST.PROPOSAL_LIST]
        if not _valid(x):
            self.skip()
            return None
        return x

    def removal_proposal(self) -> dict[str, Any] | None:
        """The turn's removals that need the owner's OK, as one proposal: "remove" when the message asked for a
        removal, "park" when it did not (a redirect: "forget the cameras, focus on WhatsApp"), with the cameras bound
        only to those agents."""
        x: dict[str, Any] = {"id": None, "dturn": self.cur, "kind": "remove" if self.removal_words else "park", "ids": [],
                             "cameras": [], "workflows": [], "connectors": [], "into": None, "reason": "", "status": "pending"}
        sec = {"agent": "ids", "camera": "cameras", "workflow": "workflows", "connector": "connectors"}
        for kind, key, _asked, why in self.unconsented:
            if self.find(kind, key) is not None and key not in x[sec[kind]]:
                x[sec[kind]].append(key)
                x["reason"] = x["reason"] or why
        x["cameras"] += [c for c in bound_only(x["ids"], self.bp) if c not in x["cameras"]]
        for k in sec.values():
            x[k] = x[k][:DST.PROPOSAL_LIST]
        x["reason"] = x["reason"] or NOT_NAMED
        return x if _valid(x) else None

    def admit(self, x: dict[str, Any], asked: bool) -> str:
        """"" when x becomes the pending proposal, else why not: a change the owner declined, or one that expired
        within HOLD_AFTER_EXPIRY design turns, is not proposed again unless the owner asks for it this turn; and a
        pending proposal is replaced only by one that takes everything it takes."""
        sig = proposal_sig(x)
        if not asked:
            for d in self.design["declined"]:
                if d["sig"] == sig and d["by"] == "owner":
                    return "you declined that change"
                if d["sig"] == sig and self.cur - d["dturn"] <= HOLD_AFTER_EXPIRY:
                    return "that change waited for your OK and expired"
        old = DST.pending_proposal(self.design)
        if old is not None and proposal_sig(x) == proposal_sig(old) and _targets(x) == _targets(old):
            return "same"                                  # already waiting: its expiry clock keeps running
        if old is not None and not covers(x, old):
            return "another change already waits for your OK"
        x["id"], x["dturn"] = DST.mint(self.design, "x"), self.cur
        self.design["proposal"] = x
        return ""

    def proposals(self, block_propose: Any) -> dict[str, Any] | None:
        cands: list[tuple[dict[str, Any], bool, str]] = []
        if isinstance(block_propose, dict):
            x = self.model_proposal(block_propose)
            if x is not None:
                cands.append((x, self.asked_all(x), "model"))
        if self.unconsented:
            x = self.removal_proposal()
            if x is not None:
                cands.append((x, all(a for _k, _key, a, _w in self.unconsented), "removal"))
        made = fresh = None                                # made: the one waiting now; fresh: made by this turn
        for x, asked, src in cands:
            names = target_names(x, self.bp)
            if made is not None and src == "removal" and covers(made, {**x, "cameras": []}):
                self.changes["converted"] = names          # the model's own proposal already takes them
                continue
            why = "only one change can wait for your OK" if made is not None else self.admit(x, asked)
            if why == "same":                              # proposed again: the one waiting stands, nothing new
                made = DST.pending_proposal(self.design)
                if src == "removal":
                    self.changes["converted"] = names
                self.notes.append(f"_Still waiting for your OK: {plain_md(proposal_phrase(made, self.bp))}._")
                continue
            if why:
                self.drop("remove" if src == "removal" else "proposal", proposal_phrase(x, self.bp), why)
                self.notes.append(f"_Kept {plain_md(_and(names, 6))}: {why}._")
                continue
            made = fresh = x
            if src == "removal":
                self.changes["converted"] = names
            self.changes["proposed"] = proposal_phrase(x, self.bp)
            self.log("propose", proposal_phrase(x, self.bp, 1), x["reason"], what="proposal")
            self.notes.append(f"{WAITING_HEAD}{plain_md(proposal_phrase(x, self.bp))}._")
        return fresh

    def expire(self) -> None:
        """A proposal the owner left for EXPIRE_AFTER design turns expires (6.3): "Kept X: not confirmed" is decided
        by default, and the model may not propose it again for HOLD_AFTER_EXPIRY design turns."""
        x = DST.pending_proposal(self.design)
        if x is None or self.cur - x["dturn"] < EXPIRE_AFTER:
            return
        x["status"] = "expired"
        self.design["declined"].append({"sig": proposal_sig(x), "by": "expiry", "dturn": self.cur})
        self.design["decisions"].append({"turn": self.cur, "text": DST.cut(f"Kept {_and(target_names(x, self.bp), 6)}: not confirmed",
                                                                            DST.DECISION_CHARS),
                                         "why": x["reason"], "source": "default"})

    def questions(self, block: dict[str, Any]) -> dict[str, Any] | None:
        """The block's answer to the open question, the open question's wait (and default), then a new question."""
        resolved_now = False
        q = DST.open_question(self.design)
        for a in _list(block.get("answered")):
            ref = a.get("q") if isinstance(a.get("q"), str) else a.get("id")
            line = answer(self.design, q, a.get("answer"), self.cur) if q is not None and ref == q["id"] else None
            if line is None:
                self.skip()
                continue
            self.changes["applied"].append(line)
            resolved_now, q = True, None
        if q is not None:
            notes, lines = wait(self.design, self.cur)
            self.notes += notes
            self.changes["applied"] += lines
            resolved_now = resolved_now or bool(lines)
        ask = block.get("ask")
        if ask is None:
            return None
        nq, _why = validate_ask(ask, self.design, resolved_now, DST.pending_proposal(self.design) is not None)
        if nq is None:
            self.changes["skipped_q"] += 1
            return None
        nq["id"], nq["asked_dturn"] = DST.mint(self.design, "q"), self.cur
        self.design["questions"].append(nq)
        return nq

    def assumptions(self, block: dict[str, Any]) -> None:
        drop = {x for x in _list(block.get("unassume")) if isinstance(x, str)}
        a = [x for x in self.design["assumptions"] if x["id"] not in drop]
        have = {_norm(x["text"]) for x in a}
        for t in _list(block.get("assume")):
            text = DS._txt(t, DST.ASSUME_CHARS)
            if text and _norm(text) not in have:
                have.add(_norm(text))
                a.append({"id": DST.mint(self.design, "a"), "text": text, "turn": self.cur})
        self.design["assumptions"] = a[-DST.ASSUME_MAX:]


# ---------------------------------------------------------------------------- a design turn's block, applied (Phase C)
_DESIGN_KEYS = ("changes", "blueprint", "propose", "plan")


def apply_block(bp1: Any, design: dict[str, Any], block: Any, *, owner_text: Any = "", how: str = "strict",
                finish_reason: str = "", bp_view: Any = None, owner_edits: Any = None, rev0: int = 0) -> Turn:
    """Phase C of a design turn, on copies (nothing passed in is changed):
    types -> validate -> consent -> first-draft clone collapse -> cap -> apply onto bp1 (ids authoritative) -> plan ->
    proposal -> question -> assumptions -> decision. An answer turn ("intent": "answer" with nothing that designs)
    changes nothing and advances no design-turn clock. `how` / `finish_reason`: the parse (only "strict" with a reply
    that was not cut off can consent to a removal). `bp_view`: the view the model was shown (its "team"), which a full
    blueprint is diffed against; default the view of bp1. `owner_edits` / `rev0`: the owner's edits recorded during the
    turn and the revision the turn started from."""
    typed, skipped = DST.typed(block)
    design = copy.deepcopy(DST.clamp(design))
    design_content = any(typed.get(k) for k in _DESIGN_KEYS if k != "blueprint") or isinstance(typed.get("blueprint"), dict)
    ready = typed.get("ready") if isinstance(typed.get("ready"), bool) else None
    if str(typed.get("intent") or "").strip().lower() == "answer" and not design_content:
        t = Turn(blueprint=bp1 if isinstance(bp1, dict) else None, design=design, intent="answer", design_turn=False, ready=ready)
        t.changes["skipped"] = skipped
        return t
    cur = design["dturn"] + 1
    design["dturn"] = cur
    strict = how == "strict" and finish_reason != "length"
    w = _Work(bp1, design, text=owner_text, strict=strict, owner_edits=owner_edits, rev0=rev0, cur=cur)
    w.skip(skipped)
    first_draft = not w.bp["agents"]
    w.expire()
    # "_omitted" marks the diff's own removals (5.5); the model never sets it
    raw = [{k: v for k, v in o.items() if k != "_omitted"} for o in typed.get("changes") or []]
    if isinstance(typed.get("blueprint"), dict):
        if raw:
            w.skip()                                       # ops win; a blueprint beside them is not applied twice
        else:
            view = bp_view if isinstance(bp_view, dict) else DST.view(w.bp1, design)
            raw = blueprint_ops(typed["blueprint"], view)
    ops, n = validate(raw)
    w.skip(n)
    w.apply(ops, first_draft)
    final = w.finish()
    if w.changes["kept_edits"]:
        w.notes.append(kept_note(w.changes["kept_edits"], final))
    ids = {a["id"] for a in DST._agents(final)}
    relink(design, w.removed, w.merged)
    entries = typed.get("plan") or []
    w.plan(entries)
    if first_draft and not entries:
        DST.seed(design, final, DST.FIRST_DRAFT_WHY)
    w.queue()
    settle(design, final, cur)
    mark_done(design, ids, cur)
    proposed = w.proposals(typed.get("propose"))
    asked = w.questions(typed)
    w.assumptions(typed)
    dec = decision_for(w.changes["applied"], cur)
    if dec:
        design["decisions"].append(dec)
    return Turn(blueprint=final, design=DST.clamp(design), changes=w.changes, notes=w.notes, decision=dec, intent="design",
                design_turn=True, ready=ready, asked=asked, proposed=proposed,
                changed=final is not bp1 and final != bp1)


# ---------------------------------------------------------------------------- server-only changes (Phase A)
def accept(bp: Any, design: dict[str, Any]) -> dict[str, Any] | None:
    """Apply the pending proposal on the owner's OK (6.3), deterministically and with no model:
    re-validate it against the team as it is (a merge left with fewer than 2 agents, or a removal left with nothing, is
    withdrawn); remove / park: the agents, the listed cameras, workflows and connectors go, and their ids leave
    workflow steps (a workflow left with no steps goes), plan links and the layout; park also adds a "later" item
    "<names> (parked)"; merge / replace: the agents go and `into` comes in with a fresh id (suffixed when an agent
    still present holds it), taking their place in workflow steps, leads and plan links; cameras stay unless listed.
    Ids are authoritative (normalise remap=False). Nothing advances: the design-turn clock is not a turn's.
    Returns {blueprint, design, ledger, notes, changed, withdrawn, undo} or None when nothing is pending."""
    x0 = DST.pending_proposal(DST.clamp(design))
    if x0 is None:
        return None
    before = {"blueprint": copy.deepcopy(bp), "design": DST.clamp(design)}
    design = copy.deepcopy(DST.clamp(design))
    w = _Work(bp, design, cur=design["dturn"])
    x = revalidate(design["proposal"], w.bp)
    if x is None:
        design["proposal"] = None
        return {"blueprint": bp, "design": DST.clamp(design), "ledger": [], "notes": [], "changed": False, "withdrawn": True,
                "undo": None}
    past = proposal_phrase(x, w.bp, 2)
    names = target_names(x, w.bp)
    if x["kind"] in ("merge", "replace"):
        into = dict(x["into"] or {})
        present = {a["id"] for a in w.bp["agents"] if a["id"] not in x["ids"]} | {"atlas"}
        iid = into.get("id") if into.get("id") not in present else DS._suffixed(into.get("id") or "agent", present)
        at = min((i for i, a in enumerate(w.bp["agents"]) if a["id"] in x["ids"]), default=len(w.bp["agents"]))
        w.merged = {i: iid for i in x["ids"]}
        for wf in w.bp["workflows"]:
            wf["steps"] = list(dict.fromkeys(iid if s in x["ids"] else s for s in wf["steps"]))
        for aid in x["ids"]:
            w.remove_item("agent", w.agent_by_id(aid), log=False, strip=False)
        w.bp["agents"].insert(min(at, len(w.bp["agents"])), DS._agent({**into, "camera": ""}, iid))
    else:
        for aid in x["ids"]:
            w.remove_item("agent", w.agent_by_id(aid), log=False)
    for c in x["cameras"]:
        w.remove_item("camera", w.find_camera(c), log=False)
    for wid in x["workflows"]:
        w.remove_item("workflow", w.find_workflow(wid), log=False)
    for n in x["connectors"]:
        w.remove_item("connector", w.find_connector(n), log=False)
    w.touched = True
    final = w.finish()
    design["proposal"] = {**x, "status": "accepted"}           # as re-validated: what was applied
    relink(design, w.removed, w.merged)
    notes = []
    if x["kind"] == "park":
        if plan_room(design):
            title = _fit_names("", [n for n in names], DST.TITLE_CHARS - len(" (parked)")) + " (parked)"
            design["plan"].append({"id": DST.mint(design, "p"), "title": title, "status": "later", "why": DST.cut(x["reason"], DST.WHY_CHARS),
                                   "agents": [], "pending_agents": [], "added_dturn": design["dturn"], "touched_dturn": design["dturn"]})
        else:
            notes.append(full_note())
    design["decisions"].append({"turn": design["dturn"], "text": DST.cut("Accepted: " + past, DST.DECISION_CHARS),
                                "why": DST.cut(x["reason"], DST.DECISION_CHARS), "source": "owner"})
    ledger = [entry("accept", past, x["reason"], what="proposal")]
    return {"blueprint": final, "design": DST.clamp(design), "ledger": ledger, "notes": notes,
            "changed": final != bp, "withdrawn": False,
            "undo": {**before, "proposal": x["id"], "names": names}}


def decline(bp: Any, design: dict[str, Any]) -> dict[str, Any] | None:
    """The owner says no to the pending proposal: it is declined, and a `declined` entry by the owner keeps the model
    from proposing the same change again (the owner can still ask for it). Returns {design, ledger} or None."""
    design = copy.deepcopy(DST.clamp(design))
    x = DST.pending_proposal(design)
    if x is None:
        return None
    x["status"] = "declined"
    design["declined"].append({"sig": proposal_sig(x), "by": "owner", "dturn": design["dturn"]})
    kept = _and(target_names(x, bp), 6)
    design["decisions"].append({"turn": design["dturn"], "text": DST.cut(f"Kept {kept}", DST.DECISION_CHARS),
                                "why": DST.cut("You declined: " + proposal_phrase(x, bp), DST.DECISION_CHARS), "source": "owner"})
    return {"design": DST.clamp(design), "ledger": [entry("decline", f"kept {kept}", x["reason"], what="proposal")]}


def apply_queued(bp: Any, design: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    """Add the agents waiting in plan item `item_id` (Continue, with no model; build_item does the same inside a
    turn): up to MAX_NEW_AGENTS and the 8-agent sketch, back into the workflows that listed them; the item is done once
    nothing waits in it. Returns {blueprint, design, ledger, changed} or None when nothing waits there."""
    design = copy.deepcopy(DST.clamp(design))
    item = next((it for it in design["plan"] if it["id"] == item_id and it["pending_agents"]), None)
    if item is None:
        return None
    w = _Work(bp, design, cur=design["dturn"])
    added = 0
    for d in list(item["pending_agents"]):
        if added >= MAX_NEW_AGENTS:
            break
        added += w.draw_queued(_Cand(agent=d, mid=d["id"], name=d["name"], item=item, pdef=d))   # one already there: settled
    final = w.finish()
    mark_done(design, {a["id"] for a in DST._agents(final)}, design["dturn"])
    return {"blueprint": final, "design": DST.clamp(design), "ledger": w.changes["applied"], "changed": final != bp}


def skip(design: dict[str, Any]) -> dict[str, Any] | None:
    """"Skip this question" (server-only, 6.8): the open question's default is used, as the auto-default uses it, with
    the assumption it rests on. Returns {design, ledger} or None when no question is open."""
    design = copy.deepcopy(DST.clamp(design))
    q = DST.open_question(design)
    if q is None:
        return None
    label = default_label(q)
    line = answer(design, q, label, design["dturn"], source="default")
    if line is None:
        return None
    design["assumptions"].append({"id": DST.mint(design, "a"), "turn": design["dturn"],
                                  "text": DST.cut(f"Assumed: {label} (you skipped the question)", DST.ASSUME_CHARS)})
    return {"design": DST.clamp(design), "ledger": [line]}


def undo(saved: Any) -> dict[str, Any] | None:
    """Put back what an accepted proposal took (the Undo step, server-only, 6.3), from what accept() kept: the
    blueprint and the design state from before it. The proposal becomes "undone" and the owner's declined entry keeps
    the model from proposing it again. Returns {blueprint, design, ledger} or None when that proposal is not there."""
    if not isinstance(saved, dict) or not isinstance(saved.get("design"), dict):
        return None
    design = copy.deepcopy(DST.clamp(saved["design"]))
    x = DST.pending_proposal(design)
    if x is None or x["id"] != saved.get("proposal"):
        return None
    bp = copy.deepcopy(saved.get("blueprint")) if isinstance(saved.get("blueprint"), dict) else None
    names = target_names(x, bp)
    x["status"] = "undone"
    design["declined"].append({"sig": proposal_sig(x), "by": "owner", "dturn": design["dturn"]})
    design["decisions"].append({"turn": design["dturn"], "text": DST.cut("Undone: " + proposal_phrase(x, bp, 2), DST.DECISION_CHARS),
                                "why": DST.cut("You asked to put back " + _and(names, 6), DST.DECISION_CHARS), "source": "owner"})
    return {"blueprint": bp, "design": DST.clamp(design),
            "ledger": [entry("undo", "put back " + _and(names, 6), x["reason"], what="proposal")]}


# ---------------------------------------------------------------------------- the server's own steps (6.11)
# Every one is a plain "say" step under the next-steps contract; a click sends its say, and the Phase A matcher
# (designer.decide) knows it by the design state's `offered` entry.
DECLINE_STEP = {"label": "Keep the team as it is", "detail": "Nothing is removed", "action": "say",
                "say": "No, keep them as they are."}


def accept_step(x: dict[str, Any], bp: Any) -> dict[str, Any] | None:
    """"Merge 5 agents into Floor Watcher" / "Remove Door Watcher" / "Park Door Watcher and 1 more": the reason as its
    detail, and the say spells out the whole change ("Yes: merge Camera 1 Monitor, … into Floor Watcher.")."""
    names = target_names(x, bp)
    into = DS._txt((x.get("into") or {}).get("name"), 40) if x.get("into") else ""
    if x["kind"] == "merge":
        label = f"Merge {len(x['ids'])} agents into {into}"
    elif x["kind"] == "replace":
        label = f"Replace {_and(names, 2)} with {into}"          # NS.clean fits it to 60 at a word boundary
    else:
        label = _fit_names("Park " if x["kind"] == "park" else "Remove ", names, DST.TITLE_CHARS)
    return NS.clean({"label": label, "detail": x["reason"], "action": "say",
                     "say": DST.cut(f"Yes: {proposal_phrase(x, bp)}.", NS.LIMITS["say"])})


def skip_step(q: dict[str, Any]) -> dict[str, Any] | None:
    return NS.clean({"label": "Skip this question", "detail": f"Atlas assumes: {default_label(q)}", "action": "say",
                     "say": "Skip that question; use your default."})


def continue_step(item: dict[str, Any]) -> dict[str, Any] | None:
    """Continue on a plan item: server-only when agents wait in it, else the say goes to the model."""
    return NS.clean({"label": f"Continue: {item['title']}", "detail": item.get("why") or "", "action": "say",
                     "say": f"Go ahead with the next part of the plan: {item['title']}."})


def undo_step(names: list[str]) -> dict[str, Any] | None:
    return NS.clean({"label": _fit_names("Undo: put back ", names, DST.TITLE_CHARS),
                     "detail": "Restores the team as it was before", "action": "say", "say": "Undo that change."})
