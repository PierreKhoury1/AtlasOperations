"""Three ways to proceed (atlas/nextsteps.py): the step contract, normalising, the generators, the designer wiring and
the endpoints that carry "next"."""
import json
import re
import time

import pytest

from atlas import designer as DS
from atlas import nextsteps as NS


def J(r):
    return json.loads(r.data)


# ------------------------------------------------------------------ the contract
PAID = {"label": DS.SWITCH_TO_PAID, "detail": NS.PAID_DETAIL, "action": "paid"}


def test_clean_strings_and_the_paid_switch():
    assert NS.clean("We get enquiries we answer too slowly") == {
        "label": "We get enquiries we answer too slowly", "action": "say", "say": "We get enquiries we answer too slowly"}
    assert NS.clean(DS.SWITCH_TO_PAID) == PAID == DS.PAID_STEP
    assert NS.clean(" switch to the PAID model. ") == PAID
    assert NS.clean({"label": "Go paid", "say": DS.SWITCH_TO_PAID}) == PAID          # the paid step's words are ours
    # an explicit other action is never turned into the switch; one that reads as the switch says one thing and does
    # another (the legacy chip would carry its label, which the workspace page acts on): dropped
    assert NS.clean({"label": DS.SWITCH_TO_PAID, "action": "job"}) is None
    assert NS.clean({"label": "Run the digest", "action": "job", "say": DS.SWITCH_TO_PAID})["action"] == "job"   # say unused
    for bad in (None, 3, ["Do it"], {"label": 5}, {"detail": "no label"}, {"label": "x"}, {"label": " . "}):
        assert NS.clean(bad) is None


def test_clean_keeps_html_looking_text_as_plain_text():
    st = NS.clean({"label": "  <b>Send</b>   &amp; log\n it.  ", "detail": "<img src=x onerror=alert(1)>",
                   "say": "<script>alert('x')</script> & \"quoted\"", "html": "<b>x</b>", "onclick": "evil()"})
    assert st == {"label": "<b>Send</b> &amp; log it", "detail": "<img src=x onerror=alert(1)>", "action": "say",
                  "say": "<script>alert('x')</script> & \"quoted\""}


def test_clean_label_detail_and_say_limits():
    assert NS.clean({"label": "build the desk now."})["label"] == "Build the desk now"       # sentence case, no trailing period
    assert NS.clean({"label": "Route enquiries → the researcher"})["label"] == "Route enquiries → the researcher"   # arrows are text
    assert NS.clean({"label": "Launch it \U0001F680"}) is None and NS.clean({"label": "✅ Approve it"}) is None   # no emoji
    text = "Add a reviewer who checks every single outbound draft before it reaches the approval queue"
    st = NS.clean({"label": text})
    assert st["label"] == "Add a reviewer who checks every single outbound draft before" and st["say"] == text
    assert NS.clean({"label": "A" * 80})["label"] == "A" * 60
    detail = NS.clean({"label": "Go on", "detail": "word " * 40})["detail"]
    assert len(detail) <= 110 and detail.endswith("…") and "  " not in detail
    assert len(NS.clean({"label": "Go on", "say": "very long message " * 40})["say"]) <= 300
    assert NS.clean({"label": "Go on", "detail": "   "}) == {"label": "Go on", "action": "say", "say": "Go on"}


def test_clean_action_fields_and_unknown_keys():
    assert NS.clean({"label": "Do it", "action": "delete_everything"}) is None
    assert NS.clean({"label": "Build the desk", "action": " BUILD ", "say": "x", "job": "y", "href": "/desk"}) == {
        "label": "Build the desk", "action": "build"}
    assert NS.clean({"label": "Run the digest", "action": "job", "job": "Write\n the   digest", "say": "ignored"}) == {
        "label": "Run the digest", "action": "job", "job": "Write the digest"}
    assert NS.clean({"label": "Type a job", "action": "job"})["job"] == ""                    # "" = just focus the job bar
    assert len(NS.clean({"label": "Long job", "action": "job", "job": "x " * 400})["job"]) <= 500
    assert NS.clean({"label": "New camera", "action": "camera"}) == {"label": "New camera", "action": "camera", "camera": ""}
    assert NS.clean({"label": "Watch it", "action": "live"}) is None                         # live needs a camera
    assert NS.clean({"label": "Watch it", "action": "camera", "camera": "c" * 61}) is None
    assert NS.clean({"label": "Watch till live", "action": "live", "camera": " till "})["camera"] == "till"
    assert NS.clean({"label": "Approvals", "action": "approvals"}) == {"label": "Approvals", "action": "approvals"}


@pytest.mark.parametrize("hours,want", [(None, 24), (0, 1), (-5, 1), (5000, 720), ("12", 12), (True, 24), ("soon", 24),
                                        (float("nan"), 24), (float("inf"), 24), (1.5, 1.5), (48.0, 48)])
def test_ask_hours_are_clamped(hours, want):
    st = NS.clean({"label": "Who waited longest?", "action": "ask", "hours": hours})
    assert st["hours"] == want and st["question"] == "Who waited longest?"
    assert NS.clean({"label": "Ask it", "action": "ask"})["hours"] == 24


@pytest.mark.parametrize("href", ["/desk", "/desk/", "/desk#runs", "/desk/ops", "/desk/workspace?desk=3", "/desk?x=1#cameras"])
def test_open_accepts_portal_paths(href):
    assert NS.clean({"label": "Open it", "action": "open", "href": href})["href"] == href


@pytest.mark.parametrize("href", [None, "", "javascript:alert(1)", "JavaScript:alert(1)", "//evil.com", "//evil.com/desk",
                                  "https://evil.com/desk", "http://desk", "/deskevil", "/desk/../api/actions", "/desk#a b",
                                  "/desk\\evil", "/desk#<script>", "/desk#x\"onmouseover=1", "/api/actions", "desk", "/desk%2F..",
                                  " /desk\n#runs"])
def test_open_rejects_everything_else(href):
    assert NS.clean({"label": "Open it", "action": "open", "href": href}) is None


# ------------------------------------------------------------------ normalise + legacy strings
def test_normalise_order_dedupe_and_fill():
    items = [{"label": "Build the desk now", "action": "build"}, "x", {"label": "build THE desk now.", "say": "Other text"},
             {"label": "Ship it", "action": "build"}, {"label": "Ask about tone", "say": "What tone?"},
             {"label": "Tone question", "say": "what tone"}, {"label": "Run digest", "action": "job", "job": "Digest"}]
    out = NS.normalise(items, [{"label": "Add a camera", "action": "camera"}, {"label": "Unused", "say": "Unused"}], allow=NS.DESIGN)
    assert [s["label"] for s in out] == ["Build the desk now", "Ask about tone", "Add a camera"]
    assert NS.normalise("not a list", [{"label": "Fine then"}])[0]["label"] == "Fine then"
    assert len(NS.normalise(["One option", "Two options"], [], n=5)) == 5


@pytest.mark.parametrize("allow", [NS.DESIGN, NS.RUN, NS.ACTIONS, ("say",), ("open",), ("say", "camera")])
def test_normalise_always_returns_exactly_three(allow):
    out = NS.normalise([], [], allow=allow)
    assert len(out) == 3 and all(s["action"] in allow for s in out)
    assert len({s["label"] for s in out}) == 3


def test_labels_are_the_strings_a_legacy_chip_sends():
    steps = NS.normalise([{"label": "Wire the form", "say": "Mostly our website form."}, {"label": "Go paid", "action": "paid"},
                          {"label": "Build it", "action": "build"}], [])
    assert NS.labels(steps) == ["Mostly our website form.", DS.SWITCH_TO_PAID, "Approve & build"]
    # chips stay at most 60 characters, as they always were: a longer message is represented by its label
    long = NS.normalise([{"label": "Tighten the writer's rules", "say": "Make the writer's standing orders stricter: what it must check first."},
                         {"label": "New camera", "action": "camera", "camera": ""}, {"label": "Go deeper", "say": "Go deeper on that."}], [])
    assert NS.labels(long) == ["Tighten the writer's rules", "New camera", "Go deeper on that."]


# ------------------------------------------------------------------ generators
BP = {"business": {"name": "Bright Smile Dental"},
      "agents": [{"id": "research", "name": "Lead Researcher", "role": "Finds out who enquired"},
                 {"id": "writer", "name": "Reply Writer", "role": "Drafts replies"}],
      "workflows": [{"id": "inbound", "name": "Inbound enquiry", "trigger": {"kind": "webhook"}, "steps": ["research", "writer"]}],
      "cameras": [{"name": "front-desk", "source": ""}]}
AGENTS = [{"id": "research", "name": "Lead Researcher", "role": "Researches the lead"}, {"id": "writer", "name": "Outreach Writer", "role": "Drafts"}]
WF = {"id": "new_lead", "name": "New lead → outreach", "steps": [{"agent": "research", "task": "{task}"}, {"agent": "writer", "task": "{task}"}]}


def _ok(cands, allow):
    """>= 3 valid candidates with distinct labels; normalising them gives exactly 3 allowed steps, all from the generator."""
    cleaned = [NS.clean(c) for c in cands]
    assert len(cands) >= 3 and all(cleaned), cands
    assert len({c["label"].casefold() for c in cleaned}) == len(cleaned)
    out = NS.normalise([], cands, allow=allow)
    assert len(out) == 3 and all(s["action"] in allow and s in cleaned for s in out)
    return out


def test_greeting_and_design_fallback_are_specific():
    g = _ok(NS.greeting(True), NS.DESIGN)
    assert NS.labels(g)[2] == "Try it on sample hotel footage" and all(s["action"] == "say" for s in g)
    assert "Try it on sample hotel footage" not in NS.labels(_ok(NS.greeting(False), NS.DESIGN))
    ready = _ok(NS.design_fallback(BP, True), NS.DESIGN)
    assert ready[0]["action"] == "build" and "Bright Smile Dental" in ready[0]["detail"] and "2-agent" in ready[0]["detail"]
    drafting = _ok(NS.design_fallback(BP, False), NS.DESIGN)
    assert drafting[0]["action"] == "say" and drafting[1]["label"] == "Add a quality reviewer"
    assert drafting[2] == {"label": "Connect the front-desk camera", "detail": "Add its stream address, a recording or sample footage",
                           "action": "camera", "camera": "front-desk"}
    reviewed = json.loads(json.dumps(BP))
    reviewed["agents"].append({"id": "qa", "name": "QA", "role": "Quality review"})
    assert "Tighten Lead Researcher's rules" in [s["label"] for s in _ok(NS.design_fallback(reviewed, False), NS.DESIGN)]
    answer = _ok(NS.design_fallback(BP, False, "answer"), NS.DESIGN)
    assert [s["label"] for s in answer] == ["Go deeper on that", "Apply it to Bright Smile Dental", "Back to designing the desk"]
    assert "2-agent draft" in answer[2]["say"]
    empty = NS.design_fallback(None)
    _ok(empty, NS.DESIGN)
    assert not any(c["action"] == "build" for c in empty)                   # nothing to build yet
    assert "at Acme" in NS.design_fallback({"business": {"name": "Acme"}})[0]["say"]


def test_desk_open_uses_the_desk():
    d = _ok(NS.desk_open("Acme Estates", AGENTS, ["till", "yard"], 2, WF), NS.RUN)
    assert d[0] == {"label": "Review 2 pending approvals", "detail": "Drafts the agents queued; nothing is sent until you approve",
                    "action": "approvals"}
    assert d[1]["action"] == "job" and d[1]["label"] == "Run a test job: New lead → outreach"
    assert d[1]["job"] == NS.EXAMPLE_JOBS["new_lead"] and "Lead Researcher → Outreach Writer" in d[1]["detail"]
    assert d[2] == {"label": "Ask what the till and yard cameras saw today", "detail": "Answered from the camera journal, with times and frames",
                    "action": "ask", "question": "What happened on the till and yard cameras today? Give times.", "hours": 24}
    assert {"label": "Watch the till camera live", "detail": "Live picture with detections and the latest journal note", "action": "live",
            "camera": "till"} in NS.desk_open("Acme Estates", AGENTS, ["till", "yard"], 2, WF)
    bare = _ok(NS.desk_open("Acme Estates", AGENTS, [], 0, {"id": "custom", "name": "Daily sweep", "trigger": {"kind": "schedule"}}), NS.RUN)
    assert bare[0]["job"] == "Run 'Daily sweep' now instead of waiting for its schedule."
    assert bare[1]["action"] == "camera" and bare[1]["camera"] == "" and bare[2]["label"] == "Give Lead Researcher a test task"
    planned = _ok(NS.desk_open("", [], [], 1, None, ["back-door"]), NS.RUN)
    assert [s["action"] for s in planned] == ["approvals", "job", "camera"] and planned[2]["camera"] == "back-door"
    # no workflow: a sample job across the team, worded for the team it really has
    assert NS.desk_open("Acme", AGENTS, [], 0, None)[0]["detail"] == "One short output from each of 2 agents; runs only when you press Deploy"
    solo = _ok(NS.desk_open("Kite Yoga", [{"id": "rep", "name": "Reporter", "role": "Reads the journal"}], [], 0, None), NS.RUN)
    assert solo[0]["detail"] == "One short output from Reporter; runs only when you press Deploy"
    assert solo[0]["job"] == "Show what this team can do for Kite Yoga: one short example output from Reporter."


def test_camera_answer_follows_the_evidence():
    ts = time.time() - 600
    ev = [{"id": 7, "camera": "till", "ts": ts}, {"id": 3, "camera": "yard", "ts": ts - 3000}]
    c = _ok(NS.camera_answer("Was anything left behind?", ev, 24, ["till", "yard"]), NS.RUN)
    at = time.strftime("%H:%M", time.localtime(ts))
    assert c[0] == {"label": f"Ask what happened around {at}", "detail": "The minutes either side, on the till camera", "action": "ask",
                    "question": f"What happened on the till camera in the minutes around {at}?", "hours": 24, "camera": "till"}
    assert c[1]["action"] == "live" and c[1]["camera"] == "till"
    assert c[2]["action"] == "job" and "Was anything left behind?" in c[2]["job"]
    none = _ok(NS.camera_answer("Was anything left behind?", [], 24, ["till"]), NS.RUN)
    assert none[0]["hours"] == 168 and none[0]["question"] == "Was anything left behind?"
    assert none[1] == {"label": "Check the feed of the till camera", "detail": "See its latest frame and switch the source if it is wrong",
                       "action": "camera", "camera": "till"}
    assert NS.camera_answer("q?", [], 24, ["cam-2"])[1]["label"] == "Check the feed of cam-2"     # no "the cam-2 camera"
    assert _ok(NS.camera_answer("x", [], 720, []), NS.RUN)[0]["label"] == "Connect another camera"   # no wider window left
    # a camera seen only in the evidence (an external node, no connector) gets no live view
    assert not any(s["action"] == "live" for s in NS.camera_answer("q?", [{"id": 1, "camera": "esp32", "ts": ts}], 24, ["till"]))


def test_run_done_after_success_and_failure():
    done = _ok(NS.run_done("done", "Draft a reply to Jane", 2, []), NS.RUN)
    assert [s["action"] for s in done] == ["approvals", "job", "open"] and done[1]["job"] == "Draft a reply to Jane"
    assert done[2]["href"] == "/desk#automations"
    failed = _ok(NS.run_done("error", "Draft a reply to Jane", 0, ["till"]), NS.RUN)
    assert failed[0]["label"] == "Run the job again" and failed[0]["job"] == "Draft a reply to Jane" and "with an error" in failed[0]["detail"]
    assert failed[1] == {"label": "See what went wrong", "detail": "Every step, tool call and error on the Runs page", "action": "open",
                         "href": "/desk#runs"}
    assert _ok(NS.run_done("cancelled", "", 0, []), NS.RUN)[1]["label"] == "See how far it got"
    long = NS.normalise([], NS.run_done("done", "word " * 300, 0, []), allow=NS.RUN)
    assert len(long[0]["job"]) <= 500


def test_run_mode_generators_never_say():
    ev = [{"id": 1, "camera": "till", "ts": time.time()}]
    calls = []
    for cams in ([], ["till"], ["till", "yard", "door"]):
        for pending in (0, 1, 3):
            calls += [NS.desk_open("Acme", AGENTS, cams, pending, WF), NS.desk_open("", [], cams, pending, None, ["back-door"]),
                      NS.camera_answer("Was anything left?", ev, 24, cams), NS.camera_answer("", [], 720, cams)]
            calls += [NS.run_done(st, task, pending, cams) for st in ("done", "error", "cancelled", "interrupted", "")
                      for task in ("Draft a reply", "")]
    for cands in calls:
        _ok(cands, NS.RUN)
        assert all(NS.clean(c)["action"] in NS.RUN for c in cands)


# ------------------------------------------------------------------ designer wiring
class _Fake:
    """A provider whose every chat returns `text` (or raises it)."""
    def __init__(self, text):
        self.text = text

    def chat(self, *a, **k):
        if isinstance(self.text, Exception):
            raise self.text
        return type("R", (), {"text": self.text})()

    def user_message(self, t):
        return {"role": "user", "content": t}


def _live_reply(monkeypatch, raw, session=None, text="hello there"):
    import types
    import atlas.providers as P
    monkeypatch.setattr(P.ProviderPool, "get", lambda self, name="": _Fake(raw))
    monkeypatch.setattr(DS, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))   # no retry back-off
    s = session or DS.new_session("live")
    return s, DS.reply(s, text, providers_cfg={"providers": {}}, designer_model="x/y:free")


def test_designer_turns_carry_three_steps(monkeypatch):
    s = DS.new_session("demo")
    assert len(s.next) == 3 and s.suggestions == NS.labels(s.next) and s.public()["next"] == s.next == s.to_dict()["next"]
    r1 = DS.reply(s, "We are Acme Estates and our enquiries get answered too slowly")
    assert len(r1["next"]) == 3 and r1["suggestions"] == NS.labels(r1["next"]) and r1["next"][0]["label"] == "Wire up the website form"
    DS.reply(s, "Mostly through our website form")
    r3 = DS.reply(s, "Warm tone, never quote prices")
    assert r3["next"][0] == {"label": "Build the desk now", "detail": "Creates Acme Estates' desk; nothing is sent without your approval",
                             "action": "build"}
    # a model that offers to build with no team yet, on a plain answer: build is dropped, follow-ups fill the gap
    block = {"intent": "answer", "next": [{"label": "Build the desk now", "action": "build"},
                                          {"label": "Tell me about pricing", "say": "How should I price this?"}], "blueprint": None}
    _, r = _live_reply(monkeypatch, "Price on value.\n<atlas-design>" + json.dumps(block) + "</atlas-design>")
    assert [x["label"] for x in r["next"]] == ["Tell me about pricing", "Go deeper on that", "Apply it to my business"]
    # a model still writing the old "suggestions" list is understood
    _, r = _live_reply(monkeypatch, 'Sure.\n<atlas-design>{"suggestions": ["Mostly by email", "Phone"], "blueprint": null}</atlas-design>')
    assert NS.labels(r["next"])[:2] == ["Mostly by email", "Phone"] and len(r["next"]) == 3


def test_quota_turn_offers_paid_only_on_free_live_sessions(monkeypatch):
    quota = RuntimeError("HTTP 429: Rate limit exceeded: free-models-per-day")
    s, r = _live_reply(monkeypatch, quota)
    assert [x["action"] for x in r["next"]] == ["paid", "say", "say"] and r["next"][1]["label"] == "Try the free model again"
    assert DS.SWITCH_TO_PAID in r["suggestions"]
    s.blueprint = DS.normalise({"agents": [{"id": "w", "name": "Writer", "role": "Drafts", "tools": ["crm_lookup"]}]})
    s.ready = True
    _, r = _live_reply(monkeypatch, quota, s)
    assert [x["action"] for x in r["next"]] == ["paid", "build", "say"] and r["ready"] is True      # an error keeps the design state
    s.tier = "balanced"
    _, r = _live_reply(monkeypatch, RuntimeError("boom"), s)
    assert [x["label"] for x in r["next"]][:2] == ["Try that again", "Build the desk from this draft"]
    assert not any(x["action"] == "paid" for x in r["next"])


def test_saved_sessions_and_profiles_get_steps():
    old = DS.DesignSession.from_dict({"id": "abc", "mode": "live", "tier": "free", "turn": 2, "blueprint": None,
                                      "suggestions": ["Mostly via the website form", DS.SWITCH_TO_PAID]})
    assert [x["action"] for x in old.next] == ["say", "paid", "say"] and old.next[0]["say"] == "Mostly via the website form"
    assert old.suggestions == NS.labels(old.next)
    again = DS.DesignSession.from_dict(old.to_dict())
    assert again.next == old.next
    s = DS.new_session("demo")
    prof = {"name": "Kite Yoga", "opening_message": "I read your site.", "suggestions": ["Something else"], "source": "model",
            "opportunities": [{"title": "Same-hour replies to new enquiries", "why": "Every hour of delay loses bookings."},
                              {"title": "Follow-up chaser for quotes that went quiet", "why": "Most lost jobs are never chased."}]}
    s.apply_profile(prof)
    assert [x["label"] for x in s.next] == ["Same-hour replies to new enquiries", "Follow-up chaser for quotes that went quiet", "Something else"]
    assert s.next[0]["detail"] == "Every hour of delay loses bookings." and s.suggestions == NS.labels(s.next)
    # the heuristic profile (no model read the site) has the same openings, without its boilerplate reasons
    h = DS.new_session("demo")
    h.apply_profile({**prof, "source": "heuristic"})
    assert [x["label"] for x in h.next] == [x["label"] for x in s.next] and not any("detail" in x for x in h.next[:2])


def test_a_model_profile_without_opportunities_shows_no_generic_reasons(monkeypatch):
    """The model wrote the profile but named no opportunities: the generic list stands in, and its stock reasons are
    not shown as if they had been read on the owner's pages."""
    import atlas.study as ST
    page = {"url": "https://kite.example/", "title": "Kite Yoga", "text": "Yoga classes for beginners. Book a class online.", "home": True}
    monkeypatch.setattr(ST, "crawl", lambda links, say=None: {"pages": [page], "signals": {}, "platform": "", "nav": [], "errors": []})
    prof = ST.study(["https://kite.example/"], provider=_Fake('{"name": "Kite Yoga", "opening_message": "I read your site."}'))
    assert prof["source"] == "model" and prof["opportunities_source"] == "heuristic" and prof["opportunities"][0]["why"]
    s = DS.new_session("demo")
    s.apply_profile(prof)
    assert [x["label"] for x in s.next] == [o["title"] for o in prof["opportunities"][:3]]
    assert not any("detail" in x for x in s.next)
    named = ST.study(["https://kite.example/"], provider=_Fake(json.dumps({
        "name": "Kite Yoga", "opening_message": "I read your site.",
        "opportunities": [{"title": "Fill last-minute class gaps", "why": "Your timetable lists late cancellations."}]})))
    assert named["source"] == "model" and "opportunities_source" not in named
    s.apply_profile(named)
    assert s.next[0] == {"label": "Fill last-minute class gaps", "detail": "Your timetable lists late cancellations.", "action": "say",
                         "say": "Fill last-minute class gaps"}


# ------------------------------------------------------------------ review 9 Oct: regressions
HOSTILE = [{"label": "Build the desk now", "detail": "Free, nothing is charged", "action": "paid"},   # a spend behind other words
           {"label": "Looks good, continue", "say": "Switch to the paid model"},       # the message the pages switch on
           {"label": "Carry on", "say": "switch to the paid model, then carry on"},     # ... by prefix, as they test it
           {"label": "\u202dSwitch to the paid model now", "action": "camera", "camera": ""},   # reads as it, opens a camera
           {"label": "Add a reviewer", "say": "Add a reviewer who checks every draft."}]


def _switches(text: str) -> bool:
    """What workspace.js (and the pending UI) test on a sent text before switching to paid models."""
    return re.match(r"switch to the paid model", text, re.I) is not None


def test_a_model_cannot_dress_up_the_paid_switch(monkeypatch):
    """Major: on a live free-tier session a model block put the paid switch behind "Build the desk now / Free, nothing
    is charged", and a say step whose message is "Switch to the paid model" behind "Looks good, continue". A paid step's
    words are always the server's, and only the server's own steps (a turn the model never answered) offer it."""
    assert [NS.clean(x) for x in HOSTILE[:4]] == [PAID, PAID, PAID, None]
    s = DS.new_session("live")                                                   # free tier: the switch would be allowed
    s.blueprint = DS.normalise({"business": {"name": "Acme"}, "agents": [{"id": "writer", "name": "Writer", "role": "Writes"}]})
    _, r = _live_reply(monkeypatch, "Here is the draft.\n<atlas-design>" + json.dumps({"next": HOSTILE, "blueprint": None})
                       + "</atlas-design>", s)
    assert r["next"][0] == {"label": "Add a reviewer", "action": "say", "say": "Add a reviewer who checks every draft."}
    assert len(r["next"]) == 3 and not any(x["action"] == "paid" for x in r["next"]) and s.to_dict()["next"] == r["next"]
    assert not any(_switches(t) for t in [x.get("say") or x["label"] for x in r["next"]] + r["suggestions"])   # every text a click sends
    # the legacy "suggestions" list and a study profile are model output too
    _, r = _live_reply(monkeypatch, 'Sure.\n<atlas-design>{"suggestions": ["Switch to the paid model", "Mostly by email"], '
                                    '"blueprint": null}</atlas-design>', s)
    assert not any(x["action"] == "paid" for x in r["next"]) and r["suggestions"][0] == "Mostly by email"
    p = DS.new_session("live")
    p.apply_profile({"name": "Kite Yoga", "opening_message": "I read your site.", "next": HOSTILE[:2], "suggestions": [DS.SWITCH_TO_PAID]})
    assert len(p.next) == 3 and not any(x["action"] == "paid" for x in p.next)
    # the server's own offer, on a turn the model never answered, is the one paid step, in its own words
    _, r = _live_reply(monkeypatch, RuntimeError("HTTP 429: Rate limit exceeded: free-models-per-day"), s)
    assert r["next"][0] == PAID and [x["action"] for x in r["next"]] == ["paid", "build", "say"]
    assert [_switches(t) for t in r["suggestions"]] == [True, False, False]
    # the next answered turn drops it again, even when the model copies it from the history it was shown
    _, r = _live_reply(monkeypatch, "Fine.\n<atlas-design>" + json.dumps({"next": [DS.PAID_STEP], "blueprint": None}) + "</atlas-design>", s)
    assert not any(x["action"] == "paid" for x in r["next"])
    # a session saved before this fix with a dressed-up paid step comes back with the real words
    old = DS.DesignSession.from_dict({"id": "dressed", "mode": "live", "tier": "free", "turn": 3, "next": HOSTILE[:1]})
    assert old.next[0] == PAID and old.suggestions[0] == DS.SWITCH_TO_PAID


def test_text_drops_control_bidi_and_invisible_characters():
    """Minor: NUL, ESC/ANSI and U+202E (shows a label backwards) passed through into labels, details and messages."""
    st = NS.clean({"label": "Build\u202e won ti", "detail": "x\x00y\x1b[31mred\u2066!\u2069",
                   "say": "a\u200bb\u200fc\ufeffd\x7f\x9f\U000e0041e"})
    assert st == {"label": "Build won ti", "detail": "xy[31mred!", "action": "say", "say": "abcde"}
    assert NS.clean({"label": "Wire\tthe\r\nform\x1fnow\x85ok"})["label"] == "Wire the form now ok"   # whitespace controls: a space
    arabic, devanagari = "\u0645\u200c\u0631 enquiries", "\u0915\u094d\u200d\u0937"                  # joiners shape real scripts
    assert NS.clean({"label": arabic, "detail": devanagari}) == {"label": arabic, "detail": devanagari, "action": "say", "say": arabic}
    assert NS.clean({"label": "\u202e\u202d\x00\u200b"}) is None
    assert NS.clean({"label": "Watch it", "action": "live", "camera": "\u2066\u2069"}) is None
    # the paid check runs on the text a page would send, after the hidden characters are gone
    assert NS.clean({"label": "Go on", "say": "\u202dSwitch to\u200b the paid model"}) == PAID


def test_step_keys_stay_linear_on_long_punctuation_runs():
    """Minor: the trailing-punctuation regex in _key backtracked quadratically (0.7 s per step at 16k characters of
    ". . ."), and ran on the model's unclamped label and message while the session lock was held."""
    assert NS._key("Done. . . !? … ") == "done" and NS._key("") == "" and NS._key(" ... ") == ""
    run = ". " * 16000 + "x"                                     # 32k characters: seconds per call before the fix
    t0 = time.perf_counter()
    assert NS._key(run) == run
    NS.normalise([{"label": run}, {"label": "Fine", "say": run}, {"label": "Ask it", "action": "ask", "question": run}], [], allow=NS.ACTIONS)
    assert time.perf_counter() - t0 < 0.5


def test_legacy_studio_chip_still_builds_and_chips_stay_short():
    """Minor: on the ready turn the build chip read "Build the desk now", which the /desk studio sent as a chat message
    (studio.js builds only on /^approve/i once ready); chips had also grown past their old 60 characters."""
    s = DS.new_session("demo")
    for text in ("We are Acme Estates and our enquiries get answered too slowly", "Mostly through our website form"):
        DS.reply(s, text)
    r3 = DS.reply(s, "Warm tone, never quote prices")
    assert r3["ready"] is True and r3["next"][0]["label"] == "Build the desk now" and r3["next"][0]["action"] == "build"
    assert r3["suggestions"] == ["Approve & build", "Add a weekly summary for me", "Explain the approval flow"]   # the sketch has its chaser
    assert re.match(r"approve", r3["suggestions"][0], re.I)
    for steps in (NS.greeting(True), NS.design_fallback(BP, True), NS.design_fallback(BP, False), NS.design_fallback(BP, False, "answer")):
        assert all(len(c) <= 60 for c in NS.labels(NS.normalise([], steps, allow=NS.DESIGN)))


def test_server_steps_claim_only_what_the_desk_does():
    """Minor (nothing fake): a demo step said calls are logged, but the desk has WhatsApp and SMS hooks and no call
    handling (a call reaches it only as a note the owner adds); the greeting promised proposals "in hours, not days"."""
    import atlas.desk.app as A
    hooks = {r.rule for r in A.app.url_map.iter_rules() if r.rule.startswith("/hook/")}
    assert {"/hook/<token>/whatsapp", "/hook/<token>/sms"} <= hooks and not any(re.search(r"call|voice|phone", h) for h in hooks)
    r1 = DS.reply(DS.new_session("demo"), "We are Acme Estates and our enquiries get answered too slowly")
    assert r1["next"][2] == {"label": "Cover WhatsApp and SMS too", "detail": "Those enquiries land in one queue; phone calls you note by hand",
                             "action": "say", "say": "Phone and WhatsApp mostly."}
    words = " ".join(f"{x['label']} {x.get('detail', '')}" for x in r1["next"] + NS.greeting(True)).lower()
    assert "calls are logged" not in words and "hours, not days" not in words
    assert NS.greeting(False)[1]["label"] == "Speed up writing proposals"


# ------------------------------------------------------------------ endpoints
@pytest.fixture()
def client(app_client, monkeypatch):
    import atlas.desk.app as A
    monkeypatch.setattr(A, "OPEN", False)        # accounts on, whatever an earlier test switched
    return app_client


def _signup(c, email: str, company: str) -> None:
    import atlas.desk.app as A
    A._RL_AUTH._hits.clear()                     # the whole suite signs up from one IP; stay under the auth rate limit
    assert J(c.post("/signup", json={"name": company, "company": company, "email": email, "password": "password1"}))["ok"]


def _done_event(body: str) -> dict:
    evs = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    return evs[-1]


def test_design_endpoints_carry_next(client):
    c = client
    _signup(c, "next-design@example.com", "Next Co")
    S = J(c.post("/api/design/start", json={"tier": "free"}))
    assert len(S["next"]) == 3 and S["suggestions"] == NS.labels(S["next"])
    done = _done_event(c.post(f"/api/design/{S['sid']}/say", json={"text": "We are Next Co and our enquiries get answered too slowly"}).data.decode())
    assert done["t"] == "done" and len(done["next"]) == 3 and done["suggestions"] == NS.labels(done["next"])
    assert all(NS.clean(x) == x for x in done["next"])                         # already clean on the wire
    assert J(c.get(f"/api/design/{S['sid']}"))["next"] == done["next"]
    DS.SESSIONS.pop(S["sid"], None)                                            # survives a restart
    assert J(c.get(f"/api/design/{S['sid']}"))["next"] == done["next"]


def _wait_run(c, rid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = J(c.get(f"/api/runs/{rid}"))
        if not r.get("active") and r.get("status") != "running":
            return r
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_api_next_desk_open_and_run_done(client):
    c = client
    _signup(c, "next-run@example.com", "Acme Next")
    assert c.post("/api/next", json={"context": "desk_open"}).status_code == 409          # no desk yet
    d = J(c.post("/api/desks", json={"name": "Acme Next", "template": "sales_desk", "tier": "free"}))
    assert c.post("/api/next", json=["desk_open"]).status_code == 400                       # a body that is not an object
    task = "Draft a two-line hello to a new customer"
    rid = J(c.post("/api/runs", json={"task": task, "mode": "auto"}))["run_id"]
    run = _wait_run(c, rid)
    r = J(c.post("/api/next", json={"context": "run_done", "run_id": rid, "status": "error", "task": "ignored"}))
    pending = len(J(c.get("/api/actions?status=pending")))
    assert r["context"] == "run_done" and r["pending"] == pending and len(r["next"]) == 3
    assert not any(x["action"] == "say" for x in r["next"])
    assert run["status"] == "done" and next(x for x in r["next"] if x["action"] == "job")["job"] == task   # the DB's status and task
    assert (r["next"][0]["action"] == "approvals") == bool(pending)
    f = J(c.post("/api/next", json={"context": "run_done", "status": "error", "task": "Chase the Jones invoice"}))
    assert f["next"][0]["job"] == "Chase the Jones invoice" and f["next"][1]["href"] == "/desk#runs"
    assert c.post("/api/next", json={"context": "run_done", "run_id": "nope"}).status_code == 404
    assert c.post("/api/next", json={"context": "whatever"}).status_code == 400

    import atlas.desk.app as A
    A.store.add_connector(d["id"], "camera", "till", {"source": "sample:none"}, False)
    A.store.add_action("r-next", "writer", "email", "jane@example.com", "Hello", "Hi Jane", "first reply", d["id"])
    o = J(c.post("/api/next", json={"context": "desk_open"}))
    n = len(J(c.get("/api/actions?status=pending")))
    assert o["pending"] == n and len(o["next"]) == 3 and not any(x["action"] == "say" for x in o["next"])
    assert o["next"][0] == {"label": f"Review {n} pending approval{'s' if n != 1 else ''}",
                            "detail": "Drafts the agents queued; nothing is sent until you approve", "action": "approvals"}
    assert o["next"][1]["label"] == "Run a test job: New lead → outreach" and o["next"][1]["job"] == NS.EXAMPLE_JOBS["new_lead"]
    assert o["next"][2]["action"] == "ask" and "till" in o["next"][2]["question"]
    # runs are scoped to the desk: from another desk the same run id is not found
    J(c.post("/api/desks", json={"name": "Other Next", "template": "blank"}))
    assert c.post("/api/next", json={"context": "run_done", "run_id": rid}).status_code == 404
    blank = J(c.post("/api/next", json={"context": "desk_open"}))
    assert blank["pending"] == 0 and blank["next"][0]["label"] == "Ask Atlas what to take on first"
    c.post("/logout")
    assert c.post("/api/next", json={"context": "desk_open"}).status_code == 401


def test_vision_ask_carries_camera_next(client):
    c = client
    _signup(c, "next-cams@example.com", "Yard Co")
    d = J(c.post("/api/desks", json={"name": "Yard Co", "template": "site_watch", "tier": "free"}))
    import atlas.desk.app as A
    A.store.add_connector(d["id"], "camera", "yard", {"source": "sample:none"}, False)
    A.store.add_vision_event(d["id"], "yard", {"truck": 1}, reason="truck seen", answer="A lorry reversing into the yard", triggered=True)
    r = J(c.post("/api/vision/ask", json={"question": "when did the lorry come?", "hours": 24}))
    assert r["mode"] == "demo" and len(r["next"]) == 3 and not any(x["action"] == "say" for x in r["next"])
    assert r["next"][0]["action"] == "ask" and "yard" in r["next"][0]["question"] and r["next"][0]["hours"] == 24
    assert r["next"][1] == {"label": "Watch the yard camera live", "detail": "Live picture with detections and the latest journal note",
                            "action": "live", "camera": "yard"}
    assert "when did the lorry come?" in r["next"][2]["job"]
    empty = J(c.post("/api/vision/ask", json={"question": "was the gate open?", "hours": 1, "camera": "nope"}))
    assert empty["evidence"] == [] and empty["next"][0]["hours"] == 168 and empty["next"][1]["camera"] == "yard"
    assert "camera" not in empty["next"][0]                                   # a camera this desk does not have is no filter
    # a question that names its own time: the search keeps to it whatever the hours, so the wider window is asked in words
    pinned = J(c.post("/api/vision/ask", json={"question": "Was the gate open yesterday?", "hours": 24, "camera": "yard"}))
    assert pinned["retrieval"]["window"] == "yesterday" and pinned["evidence"] == []
    wide = pinned["next"][0]
    assert wide == {"label": "Ask about the last 7 days", "detail": "A recap over the longer window, with times", "action": "ask",
                    "question": "What happened on the yard camera in the last 7 days? Give times.", "hours": 168, "camera": "yard"}
    again = J(c.post("/api/vision/ask", json={"question": wide["question"], "hours": wide["hours"], "camera": wide["camera"]}))
    assert again["retrieval"]["window"] == "last 7 days" and again["evidence"]                # the step really widens the search


def test_design_state_keeps_next_through_layout_canvas_tier_study_and_build(client, monkeypatch):
    """Every way the client gets the session back carries the same steps; layout leaves them alone; canvas edits and a
    tier switch re-check build / paid; a planned camera of the built desk is offered on desk_open."""
    import atlas.desk.app as A
    import atlas.study as ST
    c = client
    A._RL_MODEL._hits.clear()
    _signup(c, "next-state@example.com", "State Co")
    sid = J(c.post("/api/design/start", json={}))["sid"]
    for text in ("We are State Co and our enquiries get answered too slowly", "Mostly through our website form",
                 "Warm tone, never quote prices"):
        done = _done_event(c.post(f"/api/design/{sid}/say", json={"text": text}).data.decode())
    assert done["next"][0]["action"] == "build" and len(done["next"]) == 3
    ids = [a["id"] for a in done["blueprint"]["agents"]]
    assert J(c.post(f"/api/design/{sid}/layout", json={"positions": {ids[0]: {"x": 5, "y": 6}}}))["layout"] == {ids[0]: {"x": 5, "y": 6}}
    DS.SESSIONS.pop(sid, None)                                                 # restored from the DB
    back = J(c.get(f"/api/design/{sid}"))
    assert back["next"] == done["next"] and back["blueprint"]["layout"] == {ids[0]: {"x": 5, "y": 6}}
    # a canvas with no agents left cannot be built: the build step goes, in the reply and in the saved session
    r = J(c.post(f"/api/design/{sid}/blueprint", json={"blueprint": {"agents": []}}))
    assert r["ready"] is False and len(r["next"]) == 3 and not any(x["action"] == "build" for x in r["next"])
    assert r["next"][:2] == [x for x in done["next"] if x["action"] != "build"]
    DS.SESSIONS.pop(sid, None)
    assert J(c.get(f"/api/design/{sid}"))["next"] == r["next"]
    # on paid models the paid switch is gone at once, not only after the next turn
    live = DS.new_session("live")
    live.refresh_next([DS.PAID_STEP, DS.RETRY_STEP])
    assert [x["action"] for x in live.next][:2] == ["paid", "say"]
    t = J(c.post(f"/api/design/{live.id}/tier", json={"tier": "balanced"}))
    assert t["tier"] == "balanced" and t["next"][0]["label"] == "Try that again" and not any(x["action"] == "paid" for x in t["next"])
    DS.SESSIONS.pop(live.id, None)
    assert J(c.get(f"/api/design/{live.id}"))["next"] == t["next"]
    # the study's done event carries the steps it seeded
    monkeypatch.setattr(ST, "study", lambda links, **k: {
        "name": "Kite Yoga", "opening_message": "I read your site.", "suggestions": ["Something else"],
        "opportunities": [{"title": "Same-hour replies to new enquiries", "why": "Every hour of delay loses bookings."}]})
    sid2 = J(c.post("/api/design/start", json={}))["sid"]
    ev = _done_event(c.post(f"/api/design/{sid2}/study", json={"links": ["https://example.com"]}).data.decode())
    assert ev["t"] == "done" and [x["label"] for x in ev["next"]][:2] == ["Same-hour replies to new enquiries", "Something else"]
    assert ev["suggestions"] == NS.labels(ev["next"]) and J(c.get(f"/api/design/{sid2}"))["next"] == ev["next"]
    # a built desk with a camera Atlas planned but nobody connected: desk_open offers to connect it
    bp = {"business": {"name": "State Co"}, "agents": [{"id": "rep", "name": "Reporter", "role": "Reads the journal", "tools": ["camera_ask"]}],
          "cameras": [{"name": "back-door", "source": ""}]}
    assert J(c.post(f"/api/design/{sid2}/build", json={"blueprint": bp}))["cameras_missing"] == ["back-door"]
    o = J(c.post("/api/next", json={"context": "desk_open"}))
    assert o["next"][0]["label"] == "Run a sample job across the team"        # no workflow designed: none borrowed from a template
    assert o["next"][1] == {"label": "Connect the back-door camera", "detail": "Atlas planned it; add the stream address or a recording",
                            "action": "camera", "camera": "back-door"}
# ------------------------------------------------------------------ review 10 Oct (UI port): backend minors
@pytest.mark.parametrize("opps", [3, True, "Fill the gaps", {"title": "Fill the gaps"}, None, 0.5])
def test_a_profile_whose_opportunities_are_not_a_list_still_seeds_the_draft(opps):
    """Minor: apply_profile iterated "opportunities" unchecked, so a model profile with "opportunities": 3 or true raised
    TypeError and the study ended in an error event. Any non-list is no opportunities; non-text openings and names go."""
    s = DS.new_session("demo")
    s.apply_profile({"name": "Kite Yoga", "opening_message": "I read your site.", "source": "model", "opportunities": opps})
    assert len(s.next) == 3 and s.transcript[0]["text"] == "I read your site." and s.suggestions == NS.labels(s.next)
    for bad in (["not", "text"], 7, "   ", None):
        s.apply_profile({"name": 5, "opening_message": bad, "opportunities": opps,
                         "blueprint": {"agents": [{"id": "w", "name": "Writer", "role": "Writes replies"}]}})
        assert s.transcript[0]["text"] == DS.GREETING and s.messages[0]["content"] == DS.GREETING
        assert s.blueprint and s.blueprint["business"].get("name", "") == "" and len(s.next) == 3


def test_blank_looking_labels_go_and_joined_scripts_stay():
    """Minors: a label made only of Hangul fillers, ZWNJ, the braille blank and other default-ignorable characters
    passed as a blank-looking step; one starting with such a character escaped the paid-wording rule; and _EMOJI
    rejected any label holding a ZWJ, which Devanagari and Malayalam are shaped with."""
    for blank in ("\u3164\u3164", "\u200c\u200c", "\u115f\u1160", "\u034f\u034f", "\u180e\u180e", "\u2800\u2800", "\uffa0\uffa0",
                  "\u17b4\u17b5", "\u200d\u200c\u200d", "\u200c.\u200c", "\U0001d173\U0001d174", "\ufff0\ufff1"):
        assert NS.clean({"label": blank}) is None, repr(blank)
        assert NS.clean({"label": "Go on", "detail": blank}) == {"label": "Go on", "action": "say", "say": "Go on"}
    devanagari = "\u0915\u094d\u200d\u0937 \u0915\u0947 \u0932\u093f\u090f \u091c\u0935\u093e\u092c"
    malayalam = "\u0d28\u0d4d\u200d \u0d15\u0d47\u0d33 \u0d2e\u0d31\u0d41\u0d2a\u0d1f\u0d3f"
    for text in (devanagari, malayalam):
        assert NS.clean({"label": text}) == {"label": text, "action": "say", "say": text}
    assert NS.clean({"label": "Hi \U0001F469\u200d\U0001F4BB"}) is None                     # an emoji ZWJ sequence is still emoji
    assert NS.clean({"label": "Mark it \u2714\ufe0f"}) is None and NS.clean({"label": "Route it \u2192 the writer"})["label"].endswith("writer")
    # the paid switch's words, behind a joiner or a blank: read as the switch (dropped for other actions, made the real step for say)
    assert NS.clean({"label": "\u200cSwitch to the paid model", "action": "camera", "camera": ""}) is None
    assert NS.clean({"label": "\u3164Switch to the paid model", "action": "job", "job": "x"}) is None
    for say in ("\u180eSwitch to the paid model", "\u200dSwitch\u200c to the paid model, then carry on", "\u2800switch to the PAID model"):
        assert NS.clean({"label": "Go on", "say": say}) == PAID
    assert NS.clean({"label": "\u200c\u200dSwitch to the paid model"}) == PAID
    # a blank-looking message, job, question or camera is no text: the label is sent, the bar is only focused
    assert NS.clean({"label": "Go on", "say": "\u200c\u200d"}) == {"label": "Go on", "action": "say", "say": "Go on"}
    assert NS.clean({"label": "Draft it", "action": "job", "job": "\u3164\u200c"})["job"] == ""
    assert NS.clean({"label": "Who came in", "action": "ask", "question": "\u2800\u200d"})["question"] == "Who came in"
    assert NS.clean({"label": "Watch the gate", "action": "live", "camera": "\u200c\u200d"}) is None
    assert NS.clean({"label": "Watch camera 1", "action": "live", "camera": "1"})["camera"] == "1"        # a one-character name is a name


def test_legacy_chips_never_repeat():
    """Minor: normalise de-duplicated label against label and message against message, but one step's chip (its
    message) could equal another step's chip (its label): two identical chips."""
    steps = NS.normalise([{"label": "Yy", "say": "Hello there"}, {"label": "Hello there", "say": "Z" * 70}], [])
    chips = NS.labels(steps)
    assert chips[0] == "Hello there" and len({c.casefold().rstrip(".") for c in chips}) == 3 and len(steps) == 3
    for items in ([{"label": "Build it", "action": "build"}, {"label": "Approve & build", "say": "x"}],
                  [{"label": "Go deeper", "say": "Tell me more."}, {"label": "Tell me more", "say": "Something else entirely"}]):
        chips = NS.labels(NS.normalise(items, [], allow=NS.DESIGN))
        assert len({c.casefold().rstrip(".") for c in chips}) == 3, chips
    for steps in (NS.greeting(True), NS.design_fallback(BP, True), NS.design_fallback(BP, False, "answer"),
                  NS.desk_open("Acme", AGENTS, ["till"], 2, WF), NS.run_done("done", "Chase the invoice", 1, ["till"])):
        chips = NS.labels(NS.normalise([], steps, allow=NS.ACTIONS))
        assert len({c.casefold() for c in chips}) == 3


@pytest.mark.parametrize("brief", ["We are Acme Estates and our enquiries get answered too slowly", "Proposals take us days to write",
                                   "Our inbox needs triage every morning", "Our shop ships orders late"])
def test_demo_turn_three_claims_only_what_its_sketch_has(brief):
    """Minor (nothing fake): turn 3 of the scripted designer offered "a second workflow" chaser where the sketch already
    ran one, and its prose claimed a reviewer and a scheduled chaser on sketches that have neither."""
    s = DS.new_session("demo")
    for text in (brief, "Mostly by email", "Warm tone, never pushy"):
        r = DS.reply(s, text)
    bp, text = r["blueprint"], r["text"]
    sched = [w for w in bp["workflows"] if (w.get("trigger") or {}).get("kind") == "schedule"]
    qa = [a for a in bp["agents"] if a["id"] == "qa"]
    assert ("runs on a schedule" in text) == bool(sched) and ("checks every draft" in text) == bool(qa)
    assert all(f'"{w["name"]}"' in text for w in sched) and "follow-up chaser runs" not in text
    assert "second workflow" not in json.dumps(r["next"]).lower() and r["next"][0]["action"] == "build"
    step = r["next"][1]
    if sched:
        assert step == {"label": "Add a weekly summary for me", "detail": "What the team did, what is waiting and what is stuck",
                        "action": "say", "say": "Add a weekly summary for me: what the team did, what's waiting and what's stuck."}
    else:
        assert step["label"].startswith("Add a chaser for quiet") and step["detail"].startswith("A scheduled workflow")
    assert not any(w["name"].lower().startswith("weekly") for w in bp["workflows"])           # what it offers is not there yet


def test_camera_log_keepers_get_the_read_tools():
    """Pre-existing (4e8cf02): a literal backspace stood where \b was meant in _CAM_READER, so "log" never matched and a
    camera log keeper in a camera team could not read a single event. "log" counts as a word, never inside another."""
    assert "\x08" not in DS._CAM_READER.pattern
    for yes in ("Visitor log keeper", "Logs every arrival at the gate", "Keeps the gate logbook", "Event logger", "Logged deliveries"):
        assert DS._CAM_READER.search(yes), yes
    for no in ("Catalog manager", "Blog writer", "Dialog designer", "Login helper", "Technologist", "Backlog groomer"):
        assert not DS._CAM_READER.search(no), no
    plain = lambda i, n, r: {"id": i, "name": n, "role": r, "goal": "", "tools": ["read_file"]}
    bp = DS.normalise({"agents": [{"id": "gate", "name": "Gate watcher", "role": "Camera watcher", "tools": ["camera_ask", "camera_events"]},
                                  plain("keeper", "Visitor log keeper", "Keeps the visitor log"),
                                  plain("cat", "Catalog manager", "Keeps the product catalog current"),
                                  plain("blog", "Blog writer", "Writes the weekly blog post")],
                       "cameras": [{"name": "gate", "source": "sample:none"}]})
    tools = {a["id"]: set(a["tools"]) for a in bp["agents"]}
    assert {"camera_events", "camera_ask"} <= tools["keeper"]
    assert not (tools["cat"] | tools["blog"]) & set(DS._CAM_TOOLS)
    assert {"camera_ask", "camera_events"} <= tools["gate"]


def test_a_camera_called_camera_reads_as_one():
    """Seen on desk 1 while porting the UI: its camera is named "camera", and the step read "Ask what camera saw today"."""
    opened = NS.desk_open("Acme", AGENTS, ["camera"], 0, WF)
    assert any(x["label"] == "Ask what the camera saw today" for x in opened)
    assert NS.camera_answer("q?", [], 24, ["front camera"])[1]["label"] == "Check the feed of the front camera"
    assert NS.camera_answer("q?", [], 24, ["Camera 1"])[1]["label"] == "Check the feed of Camera 1"
    assert NS.camera_answer("q?", [], 24, ["lobby"])[1]["label"] == "Check the feed of the lobby camera"


def test_the_workspace_draws_steps_as_text_and_switches_on_the_action():
    """The UI port: workspace.js renders every step through textContent (no innerHTML, no inline handler) and switches
    to paid models on action === "paid" only, never on the text a step or the owner sends."""
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "atlas" / "desk" / "static"
    js = (static / "workspace.js").read_text(encoding="utf-8")
    block = js[js.index("next steps (start)"):js.index("next steps (end)")]
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "onclick=", "document.write", "eval("):
        assert bad not in block, bad
    assert "textContent" in block and "case 'paid':" in block and "setTier('balanced')" in block
    assert not re.search(r"switch to the paid model", js, re.I)                     # no text test anywhere on the page
    assert 'id="sugg"' not in (static / "workspace.html").read_text(encoding="utf-8")
    # every action the server can send has an icon and a handler (no row that does nothing), and nothing else does
    icons = re.search(r"const NX_ACT = \{(.*?)\};", block, re.S).group(1)
    assert set(re.findall(r"(\w+): \['i-", icons)) == set(NS.ACTIONS)
    run = block[block.index("async function runStep"):block.index("function prefillJob")]
    assert set(re.findall(r"case '(\w+)':", run)) == set(NS.ACTIONS)
    # the page re-checks an open step's href with the server's own rule
    href = re.search(r"const DESK_HREF = /(.+?)/;", block).group(1).replace("\\/", "/")
    assert href == NS.HREF.pattern
    # a job step only fills the job bar: the run is the owner's Deploy (prefillJob never calls deploy)
    fill = block[block.index("function prefillJob"):]
    assert "deploy(" not in fill and "#job" in fill
    # an ask step about one camera asks that camera only
    assert "s.camera" in run[run.index("case 'ask':"):run.index("case 'paid':")] and "{question: text, hours, camera}" in js


def test_camera_follow_ups_do_what_they_say():
    """Found while exercising the UI on desk 13: (1) "Search the last 7 days instead" re-sent "What happened ... today?",
    and a time named in the question wins over the hours in the journal search, so the step searched today again;
    (2) "Ask the drive camera the same" said "only" but asked every camera, and asking it twice gave "On the drive
    camera: On the drive camera: ..."; (3) "Ask what happened around 23:32" for a note weeks old searched the latest
    23:32, a day with nothing in it."""
    q = "What happened on the cameras today? Give times."
    wide = NS.normalise([], NS.camera_answer(q, [], 20, ["carpark", "drive"], pinned=True), allow=NS.RUN)[0]
    assert wide == {"label": "Ask about the last 7 days", "detail": "A recap over the longer window, with times", "action": "ask",
                    "question": "What happened on the carpark and drive cameras in the last 7 days? Give times.", "hours": 168}
    plain = NS.normalise([], NS.camera_answer("Was the gate open?", [], 24, ["carpark"]), allow=NS.RUN)[0]
    assert plain["label"] == "Search the last 7 days instead" and plain["question"] == "Was the gate open?" and plain["hours"] == 168
    assert NS.camera_answer(q, [], 720, ["carpark"], pinned=True)[0]["action"] == "camera"      # nothing wider than 30 days
    other = NS.normalise([], NS.camera_answer("On the drive camera: " + q, [], 24, ["carpark", "drive"], camera="drive"), allow=NS.RUN)
    ask = next(x for x in other if x["label"] == "Ask the carpark camera the same")
    assert ask["question"] == "On the carpark camera: " + q and ask["camera"] == "carpark"           # one prefix, that camera only
    assert other[0]["camera"] == "drive" and other[1]["camera"] == "drive"                          # widen and the feed: the camera asked
    now = time.time()
    fresh = NS.camera_answer("Was anything left?", [{"id": 1, "camera": "till", "ts": now - 600}], 24, ["till"])
    stale = NS.camera_answer("Was anything left?", [{"id": 1, "camera": "till", "ts": now - 16 * 86400}], 720, ["till"])
    assert fresh[0]["label"].startswith("Ask what happened around") and fresh[0]["camera"] == "till"
    assert not any(x["label"].startswith("Ask what happened around") for x in stale)
    assert [x["action"] for x in NS.normalise([], stale, allow=NS.RUN)] == ["live", "job", "open"]
    # the ask step's camera: a name the desk has, plain text, at most 60 characters; blank or too long is no filter
    assert NS.clean({"label": "Ask the gate", "action": "ask", "question": "q?", "camera": "gate"})["camera"] == "gate"
    assert "camera" not in NS.clean({"label": "Ask the gate", "action": "ask", "question": "q?", "camera": "‌‍"})
    assert "camera" not in NS.clean({"label": "Ask the gate", "action": "ask", "question": "q?", "camera": "g" * 61})
    assert "camera" not in NS.camera_answer("q?", [], 24, ["yard"], camera="nope")[0]


def test_study_with_malformed_opportunities_still_ends_done(client, monkeypatch):
    """The study SSE ended in {t: error} when the model's profile had "opportunities": 3; now it ends done, seeded."""
    import atlas.study as ST
    _signup(client, "next-study@example.com", "Kite Co")
    monkeypatch.setattr(ST, "study", lambda links, **k: {"name": "Kite Yoga", "opening_message": "I read your site.", "source": "model",
                                                         "opportunities": 3, "suggestions": ["Something else"]})
    sid = J(client.post("/api/design/start", json={}))["sid"]
    ev = _done_event(client.post(f"/api/design/{sid}/study", json={"links": ["https://example.com"]}).data.decode())
    assert ev["t"] == "done" and len(ev["next"]) == 3 and ev["next"][0]["label"] == "Something else"
    assert J(client.get(f"/api/design/{sid}"))["next"] == ev["next"]
