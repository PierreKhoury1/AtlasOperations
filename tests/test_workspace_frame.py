"""The workspace inside the app frame: one desk per page (X-Desk / ?desk=), one honest camera status, one model-cost
figure for Home and the workspace, failed runs that say so, run events that keep their data for a reload, and
/desk/workspace meaning "my desk" once there is one. The Usage page in the frame and the rail, and every page wording
a usage limit the same way (usage spec §4.7, §4.8, §6.1: T29, T30)."""
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas import templates

J = lambda r: json.loads(r.get_data(as_text=True))
STATIC = Path(__file__).resolve().parents[1] / "atlas" / "desk" / "static"


@pytest.fixture()
def fresh_limits(monkeypatch):
    """The suite signs up many owners from one address; give these tests their own login allowance. Accounts on:
    another test module switches the app into open mode and leaves it there. The usage ledger starts empty as well
    (its tables and its in-memory minute windows, pool snapshot and runs at once), so no earlier test's requests count
    against these (usage spec §8.1)."""
    from atlas import secure as SEC
    from atlas import usage
    from atlas.desk import app as A
    monkeypatch.setattr(A, "_RL_AUTH", SEC.RateLimiter(10000, 600))
    monkeypatch.setattr(A, "OPEN", False)
    usage.reset()
    if usage._store is A.store:
        with usage._tx() as c:
            for table in ("usage_events", "usage_counters", "usage_provider", "usage_overrides", "usage_deferred"):
                c.execute(f"DELETE FROM {table}")
    return A


def _owner(c, email):
    c.get("/logout")
    if c.post("/signup", json={"name": "W", "company": "W", "email": email, "password": "password1"}).status_code != 200:
        c.post("/login", json={"email": email, "password": "password1"})


def _desk(c, name):
    return J(c.post("/api/desks", json={"name": name, "template": "site_watch", "tier": "free"}))


def test_x_desk_pins_a_page_to_its_desk(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-pin@example.com")
    a, b = _desk(c, "Pin A"), _desk(c, "Pin B")
    assert J(c.get("/api/desks"))["current"] == b["id"]                       # the session follows the newest desk
    assert J(c.get("/api/config", headers={"X-Desk": str(a["id"])}))["desk"]["id"] == a["id"]
    assert J(c.get(f"/api/config?desk={a['id']}"))["desk"]["id"] == a["id"]   # images and event streams name it in the query
    assert J(c.get("/api/desks"))["current"] == b["id"]                       # reading another desk never switches the session
    _owner(c, "ws-pin-other@example.com")
    other = _desk(c, "Other")
    assert J(c.get("/api/config", headers={"X-Desk": str(a["id"])}))["desk"]["id"] == other["id"]   # not yours: ignored


def test_workspace_means_my_desk_once_there_is_one(app_client, fresh_limits):
    c = app_client
    c.get("/logout")
    r = c.get("/desk/workspace?desk=5")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login?next=/desk/workspace%3Fdesk%3D5")   # the link survives the login
    _owner(c, "ws-door@example.com")
    c.get("/logout")
    r = c.post("/login?next=/desk/workspace%3Fdesk%3D5", data={"email": "ws-door@example.com", "password": "password1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace?desk=5")
    assert c.get("/desk/workspace").status_code == 200                         # no desk yet: the conversation that builds one
    d = _desk(c, "Door")
    r = c.get("/desk/workspace")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/desk/workspace?desk={d['id']}")
    assert c.get("/desk/workspace?new=1").status_code == 200                   # a new desk, on purpose


def test_the_link_survives_a_google_sign_in_too(app_client, fresh_limits, monkeypatch):
    """The login page hands ?next= to both ways in: the password form (then the mailed code) and Continue with Google."""
    import re
    from urllib.parse import quote
    from atlas.desk import auth as AUTH
    c = app_client
    c.get("/logout")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
    monkeypatch.setenv("TWO_FACTOR", "off")
    page = c.get("/login?next=/desk/workspace%3Fdesk%3D5").get_data(as_text=True)
    assert 'class="gbtn on"' in page and 'href="/auth/google"' in page
    assert "action='/login?next='+encodeURIComponent(n)" in page and "href='/auth/google?next='+encodeURIComponent(n)" in page
    monkeypatch.setattr(AUTH, "google_exchange", lambda code, redirect: {"sub": "g-ws-next", "email": "ws-gnext@example.com", "name": "G"})
    r = c.get("/auth/google?next=" + quote("/desk/workspace?desk=5", safe=""))
    state = re.search(r"state=([^&]+)", r.headers["Location"]).group(1)
    r = c.get(f"/auth/google/callback?state={state}&code=x")
    assert r.status_code == 302 and r.headers["Location"].endswith("/desk/workspace?desk=5")


def test_stats_carry_the_one_cost_figure(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-cost@example.com")
    _desk(c, "Cost")
    s = J(c.get("/api/stats"))
    cost = s["cost"]
    assert cost["basis"] == "estimate" and cost["scope"] == "desk" and cost["window"] == "all time"
    assert cost["tokens"] == s["tokens_in"] + s["tokens_out"]
    assert cost["gbp"] == round((s["tokens_in"] * 5 + s["tokens_out"] * 25) / 1e6 * 0.78, 2)


def test_camera_status_is_live_only_while_watched_and_fresh():
    from atlas.desk.app import _camera_status
    now = 1_000_000.0
    cam = {"config": {"source": "rtsp://cam.local/1"}}
    task = json.dumps({"connector": "door", "every_s": 8})
    on, off = {"enabled": 1, "task": task}, {"enabled": 0, "task": task}
    assert _camera_status(cam, {"ts": now - 5}, None, on, now)["state"] == "live"
    assert _camera_status(cam, {}, {"ts": now - 15 * 86400}, on, now)["state"] == "waiting"      # watched, newest frame is old
    st = _camera_status(cam, {}, {"ts": now - 15 * 86400}, off, now)
    assert st["state"] == "off" and not st["watching"] and st["frame_ts"] == now - 15 * 86400 and st["every_s"] == 8
    assert _camera_status({"config": {"_decrypt_error": "x"}}, {}, None, on, now)["problem"].startswith("stored settings")
    assert _camera_status({"config": {}}, {}, None, None, now) == {"state": "off", "watching": False, "every_s": 30.0,
                                                                     "frame_ts": 0.0, "problem": "no stream address yet"}


def test_last_vision_notes_pick_each_cameras_newest_written_note(store):
    t0 = time.time() - 100
    store.add_vision_event(1, "door", {"person": 1}, answer="first note", source="journal", ts=t0)
    store.add_vision_event(1, "door", {"person": 2}, answer="", source="camera", ts=t0 + 1)            # a detection, not a note
    store.add_vision_event(1, "door", {"person": 1}, answer="second note", source="journal", ts=t0 + 2)
    store.add_vision_event(1, "door", {"person": 3}, answer="asked: 3 people", source="camera", ts=t0 + 3)   # an answer, not a note
    store.add_vision_event(1, "yard", {}, answer="the summary", source="digest", ts=t0 + 4)
    store.add_vision_event(2, "door", {}, answer="another desk", source="journal", ts=t0 + 5)
    notes = store.last_vision_notes(1, 0)
    assert set(notes) == {"door", "yard"}
    assert notes["door"]["text"] == "second note" and notes["door"]["ts"] == t0 + 2 and notes["yard"]["source"] == "digest"
    assert store.last_vision_notes(1, t0 + 3) == {"yard": {"text": "the summary", "ts": t0 + 4, "source": "digest"}}


def test_run_events_keep_their_data_for_a_reload(store):
    store.add_event("r1", "agent_start", "writer", "Writer <- task", json.dumps({"inst": "writer#2", "assignment": "draft it"}))
    store.add_event("r1", "log", "writer", "the text", "")
    ev = store.events("r1")
    assert ev[0]["data"] == {"inst": "writer#2", "assignment": "draft it"} and ev[1]["data"] == {}
    assert [e["kind"] for e in ev] == ["agent_start", "log"]


def _configs(template="sales_desk"):
    t = templates.get(template)
    for a in t["agents"]:
        a["provider"] = "demo"; a["model"] = ""
    return {"providers": {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": 0}}},
            "orchestration": {"max_iterations": 12, "max_delegation_depth": 2, "specialist_max_iterations": 4},
            "business": t["business"], "agents": t["agents"], "workflows": t["workflows"], "ui": {}}


def test_a_run_whose_lead_never_ran_is_failed_not_done(store, monkeypatch):
    from atlas.orchestrator import Orchestrator
    d = store.add_desk(1, "Acme", "sales_desk", "free", templates.build_desk("sales_desk", {}))
    ds = store.for_desk(d["id"])
    evs = []
    orch = Orchestrator(_configs(), ds, evs.append)
    monkeypatch.setattr(orch, "run_agent", lambda *a, **k: "ERROR from Atlas: RuntimeError: HTTP 404: This model is unavailable for free")
    res = orch.run("Summarise the day", "auto")
    assert res.status == "error" and ds.runs()[0]["status"] == "error"
    assert [e.data.get("status") for e in evs if e.kind == "done"] == ["error"]
    assert "Verified by the desk" not in res.summary                             # no success footer on a failed run


def test_cost_leads_with_its_basis(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-cost-words@example.com")
    _desk(c, "Cost words")
    cost = J(c.get("/api/stats"))["cost"]
    assert cost["note"].startswith("at paid rates")                       # the qualifier first: a truncated line still says it
    assert cost["tier"] == "free" and "not a bill" in cost["title"] and "free models" in cost["title"]
    # a free-tier desk leads with what its models cost (nothing); the paid-rate figure stays as context, never as the headline
    assert cost["value"] == "Free" and cost["label"] == "Model cost" and f"£{cost['gbp']:.2f}" in cost["note"]


def test_sample_footage_needs_no_desk(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-samples@example.com")
    assert J(c.get("/api/desks"))["desks"] == []
    assert c.get("/api/cameras").status_code == 409                       # a desk's cameras need a desk...
    r = c.get("/api/cameras/samples")                                     # ...the server's sample clips do not
    assert r.status_code == 200 and isinstance(J(r)["samples"], list)
    assert all({"name", "label", "camera"} <= set(x) for x in J(r)["samples"])   # Use offers the clip's usual camera name
    c.get("/logout")
    assert c.get("/api/cameras/samples").status_code == 401


def test_sample_clips_carry_a_label_and_a_camera_name():
    from atlas import designer as DS
    assert DS.sample_label("corner-store_ezymart-1080p") == DS.SAMPLE_LABELS["corner-store_ezymart"] + ", 1080p"   # a copy reads as its original
    assert DS.sample_camera_name("corner-store_ezymart-1080p") == "till" and DS.sample_camera_name("campus-gym") == "gym"
    assert DS.sample_label("no-such_clip") == "no such clip" and DS.sample_camera_name("no-such_clip") == ""


def test_health_prices_its_tokens_like_the_cost_tile(app_client, fresh_limits):
    """Home showed two estimates for one desk: the cost tile (paid rates, $5 in / $25 out) and System health (every token
    at $5). Both now use the paid rates; health keeps its own window (24 h) and says so."""
    from atlas.desk import app as A
    c = app_client
    _owner(c, "ws-health-cost@example.com")
    d = _desk(c, "Health cost")
    A.store.create_run("hc-1", "price me", "auto", "", d["id"])
    A.store.finish_run("hc-1", "done", "ok", 1_000_000, 100_000)
    h = A._health(d["id"])
    assert h["tokens"] == 1_100_000 and h["cost_gbp"] == round((1_000_000 * 5 + 100_000 * 25) / 1e6 * 0.78, 2)


def test_an_unfinished_draft_follows_its_owner_and_nobody_else(app_client, fresh_limits):
    c = app_client
    _owner(c, "ws-draft-a@example.com")
    assert J(c.get("/api/design/mine")) == {}                             # nothing started yet
    sid = J(c.post("/api/design/start", json={"tier": "free"}))["sid"]
    assert J(c.get("/api/design/mine")) == {}                             # a greeting alone is not a draft
    c.post(f"/api/design/{sid}/say", json={"text": "We are a small dental clinic; enquiries wait too long"}).get_data()
    mine = J(c.get("/api/design/mine"))
    assert mine["sid"] == sid and mine["turn"] == 1                        # another tab or device resumes it
    _owner(c, "ws-draft-b@example.com")
    assert J(c.get("/api/design/mine")) == {}
    assert c.get(f"/api/design/{sid}").status_code == 404                 # someone else's draft, even with its id
    _owner(c, "ws-draft-a@example.com")
    assert J(c.get(f"/api/design/{sid}"))["sid"] == sid
    bp = J(c.get(f"/api/design/{sid}"))["blueprint"]
    J(c.post(f"/api/design/{sid}/build", json={"blueprint": bp, "tier": "free"}))
    assert J(c.get("/api/design/mine")) == {}                             # built: no longer a draft


def test_a_failed_run_keeps_the_desks_record_of_what_it_queued(store, monkeypatch):
    from atlas.orchestrator import Orchestrator
    d = store.add_desk(1, "Acme", "sales_desk", "free", templates.build_desk("sales_desk", {}))
    ds = store.for_desk(d["id"])
    orch = Orchestrator(_configs(), ds, lambda e: None)

    def lead_fails_late(*a, **k):                                         # a specialist queued a message, then the lead's model went away
        orch._counts["queued"] += 1
        return "ERROR from Atlas: RuntimeError: HTTP 503"
    monkeypatch.setattr(orch, "run_agent", lead_fails_late)
    res = orch.run("Reply to the newest enquiry", "auto")
    assert res.status == "error" and res.summary.startswith("ERROR from Atlas")
    assert "Verified by the desk (not model-claimed): 1 action(s) queued for approval" in res.summary


def test_cameras_carry_the_journals_newest_note_of_any_age(app_client, fresh_limits):
    """Desk 1 had 500 notes (the newest five weeks old, from cameras since removed) and the workspace said "no notes
    yet": /api/cameras only read 30 days per camera. It now names the newest written note of the whole record."""
    from atlas.desk import app as A
    c = app_client
    _owner(c, "ws-oldnote@example.com")
    d = _desk(c, "Old notes")
    assert J(c.get("/api/cameras"))["last_note_ts"] == 0                   # nothing written yet: the page may say so
    old = time.time() - 40 * 86400
    A.store.add_vision_event(d["id"], "gone-cam", {"person": 1}, answer="two people at the door", source="video", ts=old)
    A.store.add_vision_event(d["id"], "gone-cam", {"person": 2}, answer="", source="hook", ts=old + 60)   # a detection, not a note
    A.store.add_vision_event(d["id"] + 1000, "other", {}, answer="another desk", source="journal", ts=old + 120)
    assert abs(J(c.get("/api/cameras"))["last_note_ts"] - old) < 1


# ---------------------------------------------------------------------------- the Usage page (usage spec §6, §4.8)
THEME_LIGHT = 'document.documentElement.dataset.theme=t||"light"'


def test_usage_page_is_in_the_frame_and_rail(app_client, fresh_limits):
    """T29 (usage spec §6.1): /desk/usage needs a sign-in and is a page of the app frame (shell.css, shell.js, Inter,
    IBM Plex Mono, its own tokens first); the rail has Usage in the full menu and in the menu of an owner with no desk
    yet, marks it, and badges it from /api/usage/brief; every page with the theme line starts light."""
    c = app_client
    c.get("/logout")
    r = c.get("/desk/usage")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login?next=/desk/usage")
    _owner(c, "ws-usage-page@example.com")
    r = c.get("/desk/usage")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "<title>Atlas Desk · Usage</title>" in page
    for ref in ("/desk/static/shell.css", "/desk/static/shell.js", "family=Inter", "IBM+Plex+Mono",
                "/desk/static/usage.css", "/desk/static/usage.js"):
        assert ref in page, ref
    assert page.index("/desk/static/usage.css") < page.index("/desk/static/shell.css")
    shell = (STATIC / "shell.js").read_text(encoding="utf-8")
    nav = shell.split("const NAV = [", 1)[1].split("];", 1)[0]
    setup = shell.split("const NAV_SETUP = [", 1)[1].split("];", 1)[0]
    entry = "['usage', 'Usage', '/desk/usage', 'gauge']"
    assert entry in nav and entry in setup and "gauge: '<path" in shell
    assert "if (p === '/desk/usage') return 'usage';" in shell
    assert "api('/usage/brief')" in shell and "if (SETUP) return usageBadge();" in shell
    assert "shellPage" not in (STATIC / "usage.js").read_text(encoding="utf-8")   # the rail's Ask Atlas behaves as on Home
    for name in ("usage.html", "index.html", "objects.html", "ops.html", "workspace.html"):
        line = next(x for x in (STATIC / name).read_text(encoding="utf-8").splitlines() if "atlas-theme" in x and "<script>" in x)
        assert THEME_LIGHT in line and "prefers-color-scheme" not in line, name


def _fn(src: str, head: str) -> str:
    """One top-level function's source, from its head to the next top-level function."""
    i = src.index(head)
    ends = [k for k in (src.find("\nasync function ", i + 1), src.find("\nfunction ", i + 1)) if k > 0]
    return src[i:min(ends) if ends else len(src)]


def test_limit_rendering_is_wired():
    """T30 (usage spec §4.7, §4.8, §5.5): the design chat in the workspace and in the studio reads the status before
    the stream, renders a refused turn's limit and a done event's limit; all five api() helpers turn usage_limit and
    outside_window bodies into the formatted message; the live theatre handles a 429, any meta line and a hidden tab;
    the Ask panel's run watcher stops at `limit`; runs stopped at a limit say so; the cost figure links Usage."""
    ws = (STATIC / "workspace.js").read_text(encoding="utf-8")
    send = _fn(ws, "async function send(")
    assert send.index("if (!r.ok || !r.body)") < send.index("getReader()") and "isLimit(j)" in send
    assert "result.refused" in send and "limitHtml(j)" in send and "j.next" in send
    assert "if (result.limit) d.append(limitLink(result.limit));" in send
    studio = (STATIC / "studio.js").read_text(encoding="utf-8")
    for head in ("async function dsSend(", "async function dsStudy("):
        f = _fn(studio, head)
        assert f.index("if (!r.ok)") < f.index("getReader()") and "dsLimit(" in f and "ev.limit" in f, head
    # the five api() helpers
    shell = (STATIC / "shell.js").read_text(encoding="utf-8")
    assert "const KINDS = ['usage_limit', 'outside_window', 'rate_limited'];" in shell
    assert "try { return window.atlasLimit.map(await r.json()); }" in shell
    assert "const LIMIT_KINDS = ['usage_limit', 'outside_window', 'rate_limited'];" in ws
    assert "try { return limitBody(await r.json()); }" in ws
    kinds = "['usage_limit', 'outside_window', 'rate_limited'].includes(j.error)"
    for name, head in (("index.html", "const api = async (p, opt) =>"), ("objects.html", "async function api(path, opts={})"),
                       ("ops.html", "async function api(path, opt)")):
        text = (STATIC / name).read_text(encoding="utf-8")
        helper = text[text.index(head):].split("\n}", 1)[0].split("};", 1)[0]
        assert "const limitBody = j => (j && " + kinds in text and "limitBody(" in helper, name
    # the surfaces
    assert "if (r && r.limited) { toast(r.error); limitLine(r); return; }" in _fn(ws, "async function deploy(")
    assert "r && r.limited ? limitLine(r)" in _fn(ws, "async function askCams(")
    assert "r.limited ||" in _fn(ws, "async function decide(") and "'stopped at a limit'" in _fn(ws, "function runState(")
    assert "Run stopped at a limit" in _fn(ws, "function showSummary(")
    home = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "r.limited ||" in home.split("const heldSend = ", 1)[1].split("\n", 1)[0]
    assert "held[0].limited" in _fn(home, "async function seed(") and "'stopped at a limit'" in _fn(home, "function homeRuns(")
    assert """"location.href='/desk/usage'",""" in home and "onclick=\"location.assign('/desk/usage')\"" in (STATIC / "workspace.html").read_text(encoding="utf-8")
    m = re.search(r"if \(/([^/]+)/i\.test\(st\)\)", shell)             # T39: the Ask panel's run watcher
    assert m and re.search(m.group(1), "limit", re.I) and not re.search(m.group(1), "running", re.I)
    assert "Stopped at a limit: " in shell
    th = (STATIC / "theatre.html").read_text(encoding="utf-8")
    an = _fn(th, "async function analyse(")
    assert "if(!r.ok || !r.body)" in an and "j.error==='usage_limit'" in an and "pauseFeed(feed, held, div)" in an
    assert "while((i=buf.indexOf('\\x01'))>=0)" in an and "startsWith('\\x01')" not in th   # every meta line, not only the first
    assert "addEventListener('visibilitychange'" in th and "document.hidden" in _fn(th, "async function tick(")
    assert "!(feed.pausedUntil>Date.now())" in _fn(th, "async function tick(")


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not on PATH")
def test_atlas_limit_words_every_limit_the_same_way(tmp_path):
    """Usage spec §4.8, §4.9: shell.js's one formatter: numbers first, the reset recomputed now in UTC (the
    server's "(in 6 h 27 min)" was true when it was written), the viewer's local time in the tooltip, a link to the
    Usage page; a reply window has no reset; a rate limit says when to try again and links nothing; every value is
    escaped; an api() body keeps its numbers, with the message in `error`."""
    shell = (STATIC / "shell.js").read_text(encoding="utf-8")
    start = shell.index("(() => {\n  if (window.atlasLimit) return;")
    src = shell[start:shell.index("})();", start) + len("})();")]
    now = int(time.time())
    resets = now + 3 * 3600 + 120
    day = {"error": "usage_limit", "limit_id": "ai_requests_day", "label": "AI requests today", "used": 250, "limit": 250,
           "resets_at": resets, "retry_at": resets, "usage_url": "/desk/usage",
           "message": "AI requests today: 250 of 250 used. Resets 00:00 UTC (in 6 h 27 min)."}
    win = {"error": "outside_window", "limit_id": "reply_window", "channel": "whatsapp", "retry_at": None,
           "message": "Outside WhatsApp's 24-hour reply window: a template message is needed, which Atlas can't send yet."}
    rate = {"error": "rate_limited", "message": "Too many design messages from this address in a minute.", "retry_after_s": 23}
    hostile = {**day, "message": "<img src=x onerror=alert(1)>: 1 of 1 used."}
    probe = tmp_path / "probe.js"
    probe.write_text("global.window = {};\n" + src + "\nconst L = window.atlasLimit;\nconst B = " + json.dumps([day, win, rate, hostile])
                     + ";\nconst M = L.map(B[0]);\nprocess.stdout.write(JSON.stringify({text: B.map(b => L.text(b)), html: B.map(b => L.html(b)), "
                       "map: M, again: L.map(M), mapped_html: L.html(M), mapped_text: L.text(M), plain: L.map({ok: true}), "
                       "is: B.map(b => L.is(b)).concat(L.is({error: 'no_desk'}), L.is(M))}));\n",
                     encoding="utf-8")
    r = subprocess.run([NODE, str(probe)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    hm = datetime.fromtimestamp(resets, timezone.utc).strftime("%H:%M")
    assert re.fullmatch(rf"AI requests today: 250 of 250 used\. Resets {hm} UTC \(in 3 h [12] min\)\.", out["text"][0]), out["text"][0]
    assert out["text"][1] == win["message"]
    assert out["text"][2] == "Too many design messages from this address in a minute. Try again in 23 s."
    h0 = out["html"][0]
    assert h0.startswith('<span class="al-msg">AI requests today: 250 of 250 used.</span> <span class="al-reset" title="')
    assert '<a class="al-link" href="/desk/usage">See usage</a>' in h0 and "your time" in h0
    assert "See usage" in out["html"][1] and "See usage" not in out["html"][2]
    assert "<img" not in out["html"][3] and "&lt;img src=x onerror=alert(1)&gt;" in out["html"][3]
    m = out["map"]
    assert (m["limited"], m["code"], m["used"], m["limit_id"]) == (True, "usage_limit", 250, "ai_requests_day")
    assert m["error"].startswith("AI requests today: 250 of 250 used. Resets ")
    assert out["plain"] == {"ok": True} and out["is"] == [True, True, True, True, False, True]
    # a body an api() helper already mapped (the Ask panel, the job bar) still reads with its reset and link
    assert out["again"] == m and out["mapped_text"].startswith("AI requests today: 250 of 250 used. Resets ")
    assert out["mapped_html"].startswith('<span class="al-msg">AI requests today: 250 of 250 used.</span> <span class="al-reset" title="')
