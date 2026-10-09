"""Three ways to proceed: the clickable next steps that end every Atlas turn in the workspace conversation.

A step is plain data, validated here and rendered by the workspace as text (never HTML):
  {"label": "Build the desk now", "detail": "what happens if picked", "action": "build", ...action fields}

  say        `say` (default: the label) is sent as the owner's next chat message - design conversation only
  build      press Approve & build (only while the design blueprint has agents)
  job        prefill the job bar with `job` ("" = just focus it); the owner presses Deploy - runs cost money
  camera     open the attach dialog for `camera` ("" = a new camera)
  live       open the live view of `camera`
  approvals  open the approvals drawer
  ask        ask the camera journal `question` over the last `hours` (1..720)
  paid       switch the design session to paid models, then resend the last message. The click is the owner's consent
             to spend, so the step always reads SWITCH_TO_PAID / PAID_DETAIL, whoever wrote it
  open       go to `href`, a same-origin portal path (/desk...)

Model-written steps (the designer's machine block) come first; the deterministic generators below fill the rest from
the real situation: business, agents, cameras, workflows, pending approvals.
"""
from __future__ import annotations

import itertools
import math
import re
import time
from typing import Any, Iterable

SWITCH_TO_PAID = "Switch to the paid model"
PAID_DETAIL = "About 3p a message; your last message is resent"
APPROVE_BUILD = "Approve & build"                 # the legacy chip text studio.js builds on (/^approve/i, once ready)
ACTIONS = ("say", "build", "job", "camera", "live", "approvals", "ask", "paid", "open")
DESIGN = ("say", "build", "camera", "paid")                   # a design-conversation turn
RUN = ("job", "camera", "live", "approvals", "ask", "open")    # run mode: typed text becomes a job, so never "say"
HREF = re.compile(r"^/desk(?:[/?#][A-Za-z0-9_\-/=&?#.]*)?$")
LIMITS = {"label": 60, "detail": 110, "say": 300, "job": 500, "question": 300, "camera": 60}
_ELL = "\u2026"
_EMOJI = re.compile("[\u231a\u231b\u23e9-\u23fa\u2600-\u27bf\u2b00-\u2bff\u200d\u20e3\ufe0f"
                    "\U0001f000-\U0001faff\U000e0020-\U000e007f]")
_DEPLOY = "runs only when you press Deploy"
_PAID_KEY = SWITCH_TO_PAID.casefold()
# never part of a step: control codes (the whitespace ones become a space instead), bidi marks, embeddings, overrides
# and isolates (U+202E shows a label backwards), zero-width and other invisible format characters, tag characters.
# ZWNJ / ZWJ (U+200C / U+200D) stay: real scripts are shaped with them.
_HIDDEN = re.compile("[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f\xad\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064"
                     "\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]")


# ---------------------------------------------------------------------------- the contract
def _text(v: Any) -> str:
    """Plain one-line text: hidden characters removed, whitespace runs folded to one space, ends stripped."""
    return re.sub(r"\s+", " ", _HIDDEN.sub("", v)).strip() if isinstance(v, str) else ""


def _reads_paid(t: str) -> bool:
    """Text a workspace page acts on as the paid switch: both test /^switch to the paid model/i on the text a click
    sends. casefold() matches at least every string that regex does."""
    return t.casefold().startswith(_PAID_KEY)


def _fit(s: str, n: int, mark: str = "") -> str:
    """Clamp to n characters at a word boundary (one overlong word is cut hard); `mark` flags the cut."""
    if len(s) <= n:
        return s
    room = n - len(mark)
    head = s[:room + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head else head[:room]
    return cut[:room].rstrip(" ,;:-\u2013\u2014") + mark


def _key(s: str) -> str:
    """Comparison key: trailing whitespace and . ! ? \u2026 dropped, case folded. A scan from the end, linear in len(s): an
    end-anchored regex backtracks quadratically on long runs such as ". . . . x"."""
    end = len(s)
    while end and (s[end - 1] in ".!?\u2026" or s[end - 1].isspace()):
        end -= 1
    return s[:end].casefold()


def _hours(v: Any) -> int | float:
    if isinstance(v, bool):
        return 24
    try:
        h = float(v)
    except (TypeError, ValueError):
        return 24
    if not math.isfinite(h):
        return 24
    h = max(1.0, min(720.0, h))
    return int(h) if h.is_integer() else round(h, 2)


def clean(item: Any) -> dict[str, Any] | None:
    """One step checked against the contract, or None. A plain string is a "say" step. Unknown keys go; text is plain
    data (never HTML-escaped): hidden characters removed, whitespace normalised, clamped.
    The paid switch spends the owner's money on one click, so its words are never the writer's: a "paid" step always
    reads SWITCH_TO_PAID / PAID_DETAIL. The pages switch on any sent text that starts like SWITCH_TO_PAID, so a say step
    whose label or message reads that way IS the paid step, and a step of any other action that reads that way says one
    thing and does another: dropped."""
    if isinstance(item, str):
        item = {"label": item}
    if not isinstance(item, dict):
        return None
    action = _text(item.get("action")).lower() or "say"
    if action not in ACTIONS:
        return None
    raw, say = _text(item.get("label")), _text(item.get("say"))
    if _reads_paid(raw) or (action == "say" and _reads_paid(say)):
        if action not in ("say", "paid"):
            return None
        action = "paid"
    if action == "paid":
        return {"label": SWITCH_TO_PAID, "detail": PAID_DETAIL, "action": "paid"}
    label = _fit(raw.rstrip(" ." + _ELL), LIMITS["label"]).rstrip(" ." + _ELL)
    if len(label) < 2 or _EMOJI.search(label):
        return None
    head = label[0].upper()                      # sentence case (a head that uppercases to two letters stays as is)
    out: dict[str, Any] = {"label": (head if len(head) == 1 else label[0]) + label[1:]}
    detail = _fit(_text(item.get("detail")), LIMITS["detail"], _ELL)
    if detail:
        out["detail"] = detail
    out["action"] = action
    if action == "say":
        out["say"] = _fit(say or raw, LIMITS["say"])
    elif action == "job":
        out["job"] = _fit(_text(item.get("job")), LIMITS["job"], _ELL)
    elif action in ("camera", "live"):
        cam = _text(item.get("camera"))
        if len(cam) > LIMITS["camera"] or (action == "live" and not cam):
            return None
        out["camera"] = cam
    elif action == "ask":
        out["question"] = _fit(_text(item.get("question")) or raw, LIMITS["question"])
        out["hours"] = _hours(item.get("hours", 24))
    elif action == "open":
        href = _text(item.get("href"))
        if not HREF.fullmatch(href) or ".." in href:
            return None
        out["href"] = href
    return out


def _payload(st: dict[str, Any]) -> str:
    """What a step does, for de-duplication: the text it sends, the camera or page it opens, or the action itself."""
    a = st["action"]
    if a in ("say", "job", "ask"):
        t = st.get("say") or st.get("job") or st.get("question") or ""
        return "t:" + _key(t) if t else ""
    if a in ("camera", "live"):
        return f"{a}:{st['camera'].casefold()}"
    return "open:" + st["href"] if a == "open" else a


def normalise(items: Any, fallback: Iterable[Any] = (), allow: Iterable[str] = ACTIONS, n: int = 3) -> list[dict[str, Any]]:
    """Exactly n distinct steps: `items` (the model's) first, invalid or disallowed ones dropped, duplicates (same label
    or same say/job/question text, case-insensitive) skipped, then `fallback`, then generic spares."""
    allow = set(allow)
    out: list[dict[str, Any]] = []
    seen_l: set[str] = set()
    seen_p: set[str] = set()
    for raw in itertools.chain(items if isinstance(items, (list, tuple)) else (), fallback or (), _SPARE):
        st = clean(raw)
        if not st or st["action"] not in allow:
            continue
        lk, pk = _key(st["label"]), _payload(st)
        if lk in seen_l or (pk and pk in seen_p):
            continue
        seen_l.add(lk)
        if pk:
            seen_p.add(pk)
        out.append(st)
        if len(out) >= n:
            break
    return out


def labels(steps: list[dict[str, Any]]) -> list[str]:
    """Legacy chip strings (studio.js, the current workspace page, old sessions): the text a chip shows and sends, at
    most 60 characters as the chips always were. The pages act on two texts: APPROVE_BUILD builds in studio.js once the
    sketch is ready, SWITCH_TO_PAID switches the tier. A say step's message when it fits, else its label."""
    out = []
    for s in steps:
        say = s.get("say") or ""
        out.append(SWITCH_TO_PAID if s["action"] == "paid" else APPROVE_BUILD if s["action"] == "build"
                   else say if say and len(say) <= LIMITS["label"] else s["label"])
    return out


# ---------------------------------------------------------------------------- generators (each returns >= 3 candidates)
def _st(label: str, detail: str = "", action: str = "say", **f: Any) -> dict[str, Any]:
    return {"label": label, "detail": detail, "action": action, **f}


def _poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def _hm(ts: Any) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _names(xs: Any) -> list[str]:
    return [x for x in (_text(v) for v in xs or []) if x]


def _cam(name: str) -> str:
    return name if re.search(r"cam", name, re.I) else f"the {name} camera"


def _cams_txt(cams: list[str]) -> str:
    return _cam(cams[0]) if len(cams) == 1 else f"the {cams[0]} and {cams[1]} cameras" if len(cams) == 2 else "the cameras"


def _uniq(xs: Iterable[dict[str, Any] | None]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    return [x for x in xs if x and not (x["label"] in seen or seen.add(x["label"]))]


def _approvals(n: int) -> dict[str, Any]:
    return _st(f"Review {n} pending approval{'s' if n != 1 else ''}", "Drafts the agents queued; nothing is sent until you approve",
               "approvals")


_NEW_CAMERA = _st("Connect a camera", "RTSP, a recording or sample footage; it keeps a journal you can question", "camera", camera="")
_SPARE = [                                      # last resort: only reached when a caller's fallback runs dry
    _st("Explain what the desk can do", "Atlas walks through its team, tools and approvals",
        say="Explain what this desk can do for me, with one example."),
    _st("Ask what is still missing", "Atlas names the open questions", say="What do you still need to know from me?"),
    _st("Show me an example job", "One worked example of a job this desk would take on", say="Show me one example job, start to finish."),
    _st("Open the full dashboard", "Runs, approvals, cameras and automations", "open", href="/desk#dash"),
    _st("Look back at past runs", "Every job the team has done, step by step", "open", href="/desk#runs"),
    _st("Review the team", "Roles, tools and standing orders", "open", href="/desk#team"),
    _st("Open approvals", "Anything the agents want to send waits here", "approvals"),
    _NEW_CAMERA,
]


def greeting(has_samples: bool = False) -> list[dict[str, Any]]:
    """Under the opening message: three different first processes (sample footage when the desk has some)."""
    sample = _st("Try it on sample hotel footage", "Real lobby CCTV; the desk keeps a journal you can question",
                 say="Try it on sample hotel footage")
    cams = _st("Document the shop cameras", "Each camera keeps a written journal you can question",
               say="I want my shop cameras documented")
    inbox = _st("Triage the inbox every morning", "Sort the mail, draft routine replies, escalate the rest",
                say="Our inbox needs triage every morning")
    return [_st("Answer enquiries faster", "A team that researches each lead and drafts the reply for your approval",
                say="We get enquiries we answer too slowly"),
            _st("Speed up writing proposals", "Research, pricing and a client-ready draft for your review",
                say="Proposals take us days to write"),
            *([sample, cams, inbox] if has_samples else [cams, inbox])]


def design_fallback(blueprint: dict[str, Any] | None = None, ready: bool = False, intent: str = "design") -> list[dict[str, Any]]:
    """A designer turn from the current sketch: forward (build when ready, else close the gaps), a refinement of a real
    agent, an added capability. An answer turn gets follow-ups: deeper, applied to the business, back to the desk."""
    bp = blueprint if isinstance(blueprint, dict) else {}
    biz = _text(bp["business"].get("name") if isinstance(bp.get("business"), dict) else "")[:40]
    agents = [a for a in bp.get("agents") or [] if isinstance(a, dict) and _text(a.get("name"))]
    cams = [c for c in bp.get("cameras") or [] if isinstance(c, dict) and _text(c.get("name"))]
    triggers = [w["trigger"] for w in bp.get("workflows") or [] if isinstance(w, dict) and isinstance(w.get("trigger"), dict)]
    if intent == "answer":
        back = (f"Back to the desk: what is still missing from the {len(agents)}-agent draft?" if agents else
                f"Back to the desk: which job should {_poss(biz)} team take on first?" if biz else
                "Back to the desk: which job should the team take on first?")
        return [_st("Go deeper on that", "A fuller answer with a worked example", say="Go deeper on that, with a worked example."),
                _st(f"Apply it to {biz}" if biz else "Apply it to my business", "What it changes in day-to-day work",
                    say=f"How would that apply to {biz or 'my business'} in practice?"),
                _st("Back to designing the desk", "Pick the design up where it left off", say=back),
                _st("Turn it into a checklist", "The answer as steps to follow", say="Turn that into a short checklist I can follow.")]
    if not agents:
        at = f" at {biz}" if biz else ""
        return [_st("Start with incoming enquiries", "A team that researches each enquiry and drafts the reply",
                    say=f"Start with incoming enquiries{at}: we answer them too slowly."),
                _st("Start with proposals and quotes", "Research, pricing and a client-ready draft for your review",
                    say=f"Start with proposals and quotes{at}: they take days to write."),
                _st("Connect a camera feed", "Each camera keeps a written journal you can question", "camera", camera=""),
                _st("Start with the inbox", "Sort the mail, draft routine replies, escalate the rest",
                    say=f"Start with the inbox{at}: it needs triage every morning.")]
    first = _text(agents[0]["name"])[:40]
    roles = " ".join(f"{a.get('name', '')} {a.get('role', '')}" for a in agents).lower()
    build = _st("Build the desk now", f"Creates {_poss(biz) if biz else 'the'} {len(agents)}-agent desk; nothing is sent without your approval",
                "build")
    gaps = _st("Ask what is still missing", "Atlas names the open questions before you build",
               say="What do you still need to know before we build this?")
    strict = _st(f"Tighten {_poss(first)} rules", f"What {first} must check first and must never do",
                 say=f"Make {_poss(first)} standing orders stricter: what it must check first and what it must never do.")
    review = None if re.search(r"review|quality|\bqa\b|check|approv", roles) else _st(
        "Add a quality reviewer", "Checks every draft before it reaches your approvals",
        say="Add a reviewer who checks every draft before it reaches my approval queue.")
    planned = next((_text(c["name"]) for c in cams if not _text(c.get("source"))), "")
    cam = (_st(f"Connect the {planned} camera", "Add its stream address, a recording or sample footage", "camera", camera=planned)
           if planned else None if cams else _st("Add a camera to the desk", "It keeps a written journal you can question", "camera", camera=""))
    chase = None if any(t.get("kind") == "schedule" for t in triggers) else _st(
        "Add a daily follow-up sweep", "Chases anyone who has not replied in 3 days",
        say="Add a daily sweep that chases anyone who hasn't replied in 3 days.")
    report = _st("Add a weekly summary for me", "What the team did, what is waiting and what is stuck",
                 say="Add a weekly summary for me: what the team did, what's waiting and what's stuck.")
    fwd, later = (build, gaps) if ready else (gaps, build)
    return _uniq((fwd, review or strict, cam or chase or report, strict, chase, report, later))


EXAMPLE_JOBS = {                                # first workflow of the built-in templates -> a job worth trying first
    "new_lead": "New enquiry from a first-time customer asking about availability next week: research them, draft a first "
                "reply and queue it for my approval.",
    "new_client_proposal": "Draft a proposal for a new client who wants month-end reporting cut from ten days to five: "
                           "approach, timeline and fee options.",
    "discovery_brief": "Prepare a discovery brief and five sharp questions for a first call with a new client.",
    "client_email": "Draft a short email to a client confirming next week's workshop and the one thing we need from them.",
    "campaign_pitch": "Pitch three campaign concepts for a spring promotion, each with a channel plan and one line of copy.",
    "gtm_plan": "Draft positioning, pricing and a 90-day go-to-market plan for our next feature launch.",
    "product_launch": "Plan the launch of a new product: listing copy, a launch email and the first week's promotion.",
    "camera_alert": "Assess the latest camera event and draft a message for staff only if it needs action.",
    "daily_digest": "Write today's site digest from the camera journal: busiest times, deliveries and anything unusual, with times.",
    "alert_triage": "Triage today's highest-severity detection and say whether containment is justified.",
    "incident_report": "Write an incident report for the last 24 hours of security events: timeline, then what the evidence does not show.",
}


def _example_job(wf: Any, agents: list[dict[str, Any]], cams: list[str], biz: str) -> dict[str, Any]:
    wf = wf if isinstance(wf, dict) else {}
    name = _text(wf.get("name"))[:60]
    if name:
        names = {a.get("id"): _text(a["name"]) for a in agents}
        steps = [s.get("agent") if isinstance(s, dict) else s for s in wf.get("steps") or []]
        chain = " → ".join(dict.fromkeys(names[s] for s in steps if isinstance(s, str) and s in names))
        kind = str((wf["trigger"] if isinstance(wf.get("trigger"), dict) else {}).get("kind") or "manual")
        job = EXAMPLE_JOBS.get(str(wf.get("id") or "")) or (
            f"Test '{name}': take one realistic incoming enquiry end to end and queue the reply for my approval."
            if kind in ("webhook", "inbox") else f"Run '{name}' now instead of waiting for its schedule."
            if kind == "schedule" else f"Run '{name}' on a realistic example and show me each agent's output.")
        return _st(f"Run a test job: {name}", f"Prefilled, {_DEPLOY}" + (f": {chain}" if chain else ""), "job", job=job)
    if cams:
        return _st("Run a camera digest now", f"Written from the journal; prefilled, {_DEPLOY}", "job",
                   job=f"Write a digest of the last hour on {_cams_txt(cams)}: who came and went, anything left behind or unusual, with times.")
    if agents:
        one = _text(agents[0]["name"])[:40] if len(agents) == 1 else ""      # never "each of 1 agents"
        return _st("Run a sample job across the team", f"One short output from {one or f'each of {len(agents)} agents'}; {_DEPLOY}",
                   "job", job=f"Show what this team can do for {biz or 'the business'}: one short example output from {one or 'each agent'}.")
    return _st("Ask Atlas what to take on first", f"Three jobs and the specialists each needs; {_DEPLOY}", "job",
               job=f"List three jobs this desk could take off {_poss(biz) if biz else 'the business'} plate this week, and the "
                   "specialists each would need.")


def desk_open(business: str = "", agents: list[dict[str, Any]] | None = None, cameras: list[str] | None = None, pending: int = 0,
              workflow: dict[str, Any] | None = None, planned: list[str] | None = None) -> list[dict[str, Any]]:
    """Opening an existing desk (run mode, never "say"): approvals waiting, an example job from its first workflow, its
    cameras (connect a planned one / ask / live), a test task for a real agent, then the team and past runs."""
    biz = _text(business)[:40]
    agents = [a for a in agents or [] if isinstance(a, dict) and _text(a.get("name"))]
    cams = _names(cameras)
    planned = [p for p in _names(planned) if p not in cams]
    out = [_approvals(pending)] if pending > 0 else []
    out.append(_example_job(workflow, agents, cams, biz))
    if planned:
        out.append(_st(f"Connect the {planned[0]} camera", "Atlas planned it; add the stream address or a recording", "camera",
                       camera=planned[0]))
    if cams:
        x = _cams_txt(cams)
        out += [_st(f"Ask what {x} saw today", "Answered from the camera journal, with times and frames", "ask",
                    question=f"What happened on {x} today? Give times.", hours=24),
                _st(f"Watch {_cam(cams[0])} live", "Live picture with detections and the latest journal note", "live", camera=cams[0])]
    elif not planned:
        out.append(_NEW_CAMERA)
    if agents:
        name, role = _text(agents[0]["name"])[:40], _text(agents[0].get("role"))[:80]
        out.append(_st(f"Give {name} a test task", f"{_fit(role, 60, _ELL)}; {_DEPLOY}" if role else f"Prefilled, {_DEPLOY}", "job",
                       job=f"Give {name} one realistic case for its role" + (f" ({role})" if role else "") + " and show me its full output."))
    out += [_st("Review the team's standing orders", f"{len(agents)} agent{'s' if len(agents) != 1 else ''}: roles, tools and rules",
                "open", href="/desk#team"),
            _st("Look back at past runs", "Every job the team has done, step by step", "open", href="/desk#runs")]
    return out


def camera_answer(question: str = "", evidence: list[dict[str, Any]] | None = None, hours: Any = 24,
                  camera_names: list[str] | None = None) -> list[dict[str, Any]]:
    """After a camera question (run mode, never "say"). `evidence`: the journal rows used, best match first."""
    q, h = _text(question), _hours(hours)
    ev = [e for e in evidence or [] if isinstance(e, dict)]
    names = _names(camera_names)
    best = ev[0] if ev else {}
    cam = _text(best.get("camera"))[:60] or (names[0] if names else "")
    wider = 168 if h < 168 else 720 if h < 720 else 0
    span = "7 days" if wider == 168 else "30 days"
    out = []
    if best:
        at = _hm(best.get("ts"))
        if at and cam:
            out.append(_st(f"Ask what happened around {at}", f"The minutes either side, on {_cam(cam)}", "ask",
                           question=f"What happened on {_cam(cam)} in the minutes around {at}?", hours=h))
        if cam in names:
            out.append(_st(f"Watch {_cam(cam)} live", "Live picture with detections and the latest journal note", "live", camera=cam))
        if q:
            out.append(_st("Write it up as a report", f"Times, cameras and journal entries cited; {_DEPLOY}", "job",
                           job=f"Write a short report from the camera journal on this question: {q} Cite times, camera names "
                               "and the journal entries used."))
        if wider and q:
            out.append(_st(f"Check the last {span} too", "The same question over a longer window", "ask", question=q, hours=wider))
    else:
        if wider and q:
            out.append(_st(f"Search the last {span} instead", "Nothing matched in this window; widen it", "ask", question=q, hours=wider))
        if cam:
            out.append(_st(f"Check the feed of {_cam(cam)}", "See its latest frame and switch the source if it is wrong", "camera", camera=cam))
    other = next((c for c in names if c != cam), "")
    if other and q:
        out.append(_st(f"Ask {_cam(other)} the same", f"The same question, about {_cam(other)} only", "ask", question=f"On {_cam(other)}: {q}", hours=h))
    tail = [_st("Open the camera page", "Every camera's journal, events and settings", "open", href="/desk#cameras"),
            _st("Ask what happened in the last hour", "A quick recap from every camera's journal", "ask",
                question="What happened in the last hour?", hours=1)]
    another = _st("Connect another camera", _NEW_CAMERA["detail"], "camera", camera="")
    return _uniq(out + (tail + [another] if best else [another] + tail))


_ENDED = {"error": "with an error", "failed": "with an error", "cancelled": "cancelled", "interrupted": "interrupted",
          "lost": "without finishing"}


def run_done(status: str = "done", task: str = "", pending: int = 0, camera_names: list[str] | None = None) -> list[dict[str, Any]]:
    """After a job run (run mode, never "say"): the approvals it left, a follow-up job, or a retry when it failed."""
    st = _text(status).lower() or "done"
    t = _fit(_text(task), LIMITS["job"], _ELL)
    cams = _names(camera_names)
    appr = _approvals(pending) if pending > 0 else None
    look = _st("Ask what the cameras saw meanwhile", "A recap of the last hour from the camera journal", "ask",
               question=f"What happened on {_cams_txt(cams)} in the last hour?", hours=1) if cams else None
    if st == "done":
        again = (_st("Run it again on another case", f"Prefills this job to edit; {_DEPLOY}", "job", job=t) if t else
                 _st("Give the team its next job", f"Focuses the job bar; {_DEPLOY}", "job", job=""))
        out = [appr, again,
               _st("Schedule it as an automation", "Run this job on a timer from the Automations page", "open", href="/desk#automations"),
               look, _st("Read the full run log", "Every step, tool call and result of the run", "open", href="/desk#runs")]
    else:
        out = [_st("Run the job again", f"The last run ended {_ENDED.get(st, repr(st))}; edit it, then press Deploy", "job", job=t),
               _st("See how far it got" if st == "cancelled" else "See what went wrong", "Every step, tool call and error on the Runs page",
                   "open", href="/desk#runs"),
               appr, _st("Check the team's tools and rules", "Roles, tools and standing orders on the Team page", "open", href="/desk#team"),
               look]
    return [x for x in out if x]
