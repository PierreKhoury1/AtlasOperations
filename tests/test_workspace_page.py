"""The workspace page itself, in a real headless Chromium: the shipped workspace.html / .css / .js and the frame
(shell.js / shell.css) against a scripted API, so what the owner sees is checked, not just what the server sends.

- Nothing fake while it loads: until /actions, /runs, /cameras and /stats answer, their figures read "-" with nothing
  under them; no "0 jobs", no "Nothing waiting", no "no run yet", no "ready" (seen on every load in prod at 40-80 ms).
- The Runs tab never squeezes a panel to its border on a short screen (auto rows, overflow:hidden panels).
- The Add camera dialog keeps Test and Attach on screen at 1280x720.
- A desk with no run yet: its workflows, jobs to try and outbound rules; what a job runs through, from its own setup.
- Escape closes the frame's desk menu; two desks with one name say which is which.
- Fitted org chart cards never cut a tool chip; a run's chips name each tool once.
- Activity shows whole rows; Atlas's opening status and its three next steps fit the panel; the first screen lists the
  ready-made desks.
- The Team tab's chart is never drawn below full size and lays a big team out in columns; a small draft's chart is as
  tall as it is, with the draft plan under it.
- The Cameras tab of a desk or draft with no camera is a setup surface (the sources, the sample clips with Use, what the
  journal will keep); a draft has no journal to read and says so, without a request.
- Until the page knows its draft it says it is opening and sends nothing (never to /api/design/null).
- The name dialog's buttons never cover its hint; the job line is cut by whole lines; Ctrl K never puts a workflow run
  above where the owner was going.
Skipped when Playwright or its Chromium is missing."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

STATIC = Path(__file__).resolve().parents[1] / "atlas" / "desk" / "static"
ORIGIN = "http://atlas.test"
CT = {".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}
NOW = time.time()
DASH = "–"


def _agents():
    return [
        {"id": "atlas", "name": "Atlas", "role": "Orchestrator", "goal": "Briefs the team, reviews every result.",
         "tools": ["delegate", "save_deliverable", "read_file", "list_agents", "finish", "crm_update", "crm_lookup", "queue_action", "web_fetch"]},
        {"id": "research", "name": "Researcher", "role": "Lead & company research", "goal": "Find who the enquirer is.", "reports_to": "atlas",
         "tools": ["web_fetch", "read_file", "list_files", "record_find", "crm_lookup", "save_deliverable"], "instructions": ["Cite sources."]},
        {"id": "writer", "name": "Reply writer", "role": "First-response drafting", "goal": "Draft a short reply.", "reports_to": "atlas",
         "tools": ["read_file", "list_files", "record_find", "queue_action", "save_deliverable"]},
        {"id": "crm", "name": "CRM keeper", "role": "Pipeline hygiene", "goal": "Keep every contact staged.", "reports_to": "atlas",
         "tools": ["read_file", "list_files", "crm_update", "crm_lookup"]},
    ]


def _run(rid, task, ago, events=0):
    evs = [{"kind": "agent_start", "agent": "research", "ts": NOW - ago, "text": "", "data": {"inst": "research", "assignment": "look the lead up"}}]
    evs += [{"kind": "tool", "agent": "atlas", "ts": NOW - ago + 1 + i / 10, "text": f"delegate -> research: step {i}", "data": {}} for i in range(events)]
    evs += [{"kind": "agent_end", "agent": "research", "ts": NOW - ago + 2, "text": "", "data": {"inst": "research"}},
            {"kind": "done", "agent": "system", "ts": NOW - ago + 3, "text": "Done. Researched the lead.\n\n---\nVerified by the desk (not model-claimed): 1 action(s) queued for approval",
             "data": {"status": "done"}}]
    row = {"id": rid, "task": task, "status": "done", "created": NOW - ago, "tokens_in": 120, "tokens_out": 40, "active": False,
           "summary": "Done. Researched the lead.\n\n---\nVerified by the desk (not model-claimed): 1 action(s) queued for approval"}
    return row, {**row, "events": evs, "deliverables": [{"name": "reply.md", "content": "Hello, thanks for getting in touch."}]}


class FakeDesk:
    """A scripted API. `hold` names paths that wait until release(); `data` is what each path answers."""

    def __init__(self, runs=1, events=0, desks=None, built=True):
        self.held, self.hold = [], set()
        rows, details = [], {}
        for i in range(runs):
            r, d = _run(f"r{i}", f"Write report {i}", 600 + i * 900, events)
            rows.append(r); details[r["id"]] = d
        desk = {"id": 7, "name": "Harbour Physio", "business_name": "Harbour Physio", "tier": "free", "template": "custom", "created": NOW - 86400}
        self.data = {
            "/api/desks": {"desks": desks if desks is not None else ([desk] if built else []), "current": 7 if built else None},
            "/api/me": {"id": 1, "name": "QA", "email": "qa@example.test"},
            "/api/config": {"desk": desk if built else None, "business": {"name": "Harbour Physio", "policy": {"max_words": 180, "no_money_figures": True, "banned_phrases": ["guaranteed"]}},
                            "agents": _agents() if built else [], "mode": "demo", "template": "custom", "tier": "free", "live_reason": "",
                            "workflows": [{"id": "inbound", "name": "Inbound lead to researched reply", "description": "research -> writer -> crm"}] if built else []},
            "/api/health": {"ok": True}, "/api/health/full": {"alerts": []}, "/api/agents/links": {"agents": {}},
            "/api/cameras": {"cameras": [], "vlm": {"ready": False}, "last_note_ts": 0},
            "/api/actions": [{"id": i + 1, "kind": "email", "to": f"lead{i}@example.test", "subject": "Re: your enquiry", "body": "Hello, thanks for getting in touch.",
                              "created": NOW - 3600, "agent": "writer", "status": "pending"} for i in range(3)],
            "/api/runs": rows, "/api/stats": {"pending": 3, "active_runs": 0, "cost": {"value": "Free", "small": "models", "note": "at paid rates £0.03", "label": "Model cost", "gbp": 0.03}},
            "/api/next": {"next": [{"label": "Review 3 pending approvals", "action": "approvals"}, {"label": "Run a test job", "detail": "Prefilled, runs only when you press Deploy", "action": "job", "job": "Test the inbound workflow"},
                                   {"label": "Connect a camera", "detail": "RTSP, a recording or sample footage", "action": "camera"}]},
            "/api/vision/events": [], "/api/cases": {"cases": []}, "/api/cameras/samples": {"samples": []},
            "/api/templates": {"desks": [{"id": t, "label": t.title() + " desk", "tagline": f"What the {t} desk does.", "does": ["a", "b"], "for": "Small firms"}
                                         for t in ("sales", "watch", "security", "consultancy", "agency", "saas", "shop")] + [{"id": "blank", "label": "Blank", "tagline": "", "does": [], "for": ""}],
                               "tiers": [], "mode": "demo"},
            "/api/design/mine": {},
            "/api/design/start": {"sid": "s1", "mode": "demo", "tier": "free", "transcript": [{"role": "assistant", "text": "Describe the business and the work."}],
                                  "next": [{"label": "Answer enquiries faster", "detail": "A team that researches each lead and drafts the reply for your approval", "action": "say", "say": "x"}]},
        }
        self.details = details

    def answer(self, route):
        url = route.request.url
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
        if not url.startswith(ORIGIN):
            return route.abort()                                             # web fonts: the page must stand without them
        if path.startswith("/desk/static/"):
            f = STATIC / path.rsplit("/", 1)[1]
            return route.fulfill(status=200, body=f.read_bytes(), headers={"Content-Type": CT.get(f.suffix, "text/plain")})
        if path == "/desk/workspace":
            return route.fulfill(status=200, body=(STATIC / "workspace.html").read_bytes(), headers={"Content-Type": "text/html"})
        if path.startswith("/api/"):
            key = "/api/runs" if path == "/api/runs" else path
            if key in self.hold:
                return self.held.append(route)
            return self.fulfill(route)
        return route.fulfill(status=404, body="")

    def fulfill(self, route):
        path = re.sub(r"^https?://[^/]+", "", route.request.url).split("?")[0]
        if path.startswith("/api/runs/"):
            body = self.details.get(path.rsplit("/", 1)[1])
            return route.fulfill(status=200 if body else 404, body=json.dumps(body or {"error": "not found"}), headers={"Content-Type": "application/json"})
        body = self.data.get(path)
        if body is None:
            return route.fulfill(status=404, body=json.dumps({"error": "not found"}), headers={"Content-Type": "application/json"})
        return route.fulfill(status=200, body=json.dumps(body), headers={"Content-Type": "application/json"})

    def release(self):
        self.hold = set()
        held, self.held = self.held, []
        for r in held:
            self.fulfill(r)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - no Chromium downloaded on this machine
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


def _open(browser, fake, path="/desk/workspace?desk=7", w=1600, h=900, theme="light", ls=None, reqs=None):
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
    init = {"ws_tut_done": "1", "atlas-theme": theme, **(ls or {})}
    ctx.add_init_script("".join(f"try{{localStorage.setItem({json.dumps(k)},{json.dumps(v)})}}catch(e){{}}" for k, v in init.items()))
    ctx.route("**/*", fake.answer)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if reqs is not None:
        page.on("request", lambda r: reqs.append(r.method + " " + re.sub(r"^https?://[^/]+", "", r.url)))
    page.goto(ORIGIN + path, wait_until="domcontentloaded")
    return ctx, page, errors


def _text(page, sel):
    return page.evaluate(f"(document.querySelector({json.dumps(sel)}) || {{}}).innerText || ''")


def test_nothing_is_claimed_before_its_source_answers(browser):
    fake = FakeDesk(runs=1)
    fake.hold = {"/api/cameras", "/api/actions", "/api/runs", "/api/stats"}
    ctx, page, errors = _open(browser, fake)
    try:
        page.wait_for_function("() => document.getElementById('wk').dataset.phase === 'run'")
        page.wait_for_timeout(400)
        claims = {s: _text(page, s) for s in ("#ph-sum", "#ap-body", "#run-body", "#run-meta", "#kpis", "#tab-n-ap", "#tab-n-cams", "#phase-label", "#msgs")}
        assert not re.search(r"jobs? in 24 h|ready|No approvals", claims["#ph-sum"]), claims["#ph-sum"]
        assert "Nothing waiting" not in claims["#ap-body"] and "Reading" in claims["#ap-body"]
        assert not re.search(r"no run yet|Jobs to try|Workflows", claims["#run-body"] + claims["#run-meta"], re.I), claims["#run-body"]
        assert claims["#tab-n-ap"] == "" and claims["#tab-n-cams"] == ""
        assert _text(page, "#run-n").strip() == DASH                         # no "0 running" before /stats answers
        assert "READY" not in claims["#phase-label"].upper()
        for k in ("#k-cams", "#k-journal", "#k-ap", "#k-spend"):                # "-" and nothing under it
            assert _text(page, k + " .v").strip() == DASH and _text(page, k + " .s").strip() == "", (k, _text(page, k))
        assert "none attached" not in claims["#kpis"] and "nothing outbound" not in claims["#kpis"] and "Last job" not in claims["#msgs"]
        assert page.evaluate("[...document.querySelectorAll('#team-rows .st')].every(s => s.textContent.trim() === '–')")   # no agent state yet
        fake.release()
        page.wait_for_function("() => /ready/.test(document.getElementById('ph-sum').textContent)")
        page.wait_for_timeout(300)
        assert "3 approvals waiting" in _text(page, "#ph-sum") and "1 job in 24 h" in _text(page, "#ph-sum")
        assert "Write report 0" in _text(page, "#run-body") and _text(page, "#tab-n-ap") == "3"
        assert _text(page, "#k-ap .v").strip() == "3" and "Last job: done" in _text(page, "#msgs")
        assert not errors, errors
    finally:
        ctx.close()


def test_a_desk_with_no_run_offers_its_own_workflows_and_rules(browser):
    fake = FakeDesk(runs=0)
    ctx, page, errors = _open(browser, fake, w=1920, h=1080)
    try:
        page.wait_for_function("() => /no run yet/.test(document.getElementById('run-meta').textContent)")
        body = _text(page, "#run-body")
        assert "Inbound lead to researched reply" in body and "Researcher" in body and "jobs to try" in body.lower()
        assert "at most 180 words" in body and "never says \"guaranteed\"" in body
        assert "Give the team its first job" not in body                    # the next steps are the server's, under Atlas's message
        page.click("#tabs [data-tab=runs]"); page.wait_for_timeout(500)
        assert page.is_hidden("#summary-panel") and page.is_visible("#flow-panel")
        flow = _text(page, "#flow-panel")
        assert "Reply writer" in flow and "3 messages waiting now" in flow and "at most 180 words" in flow
        assert page.evaluate("document.querySelectorAll('#runs-try .nstep').length") >= 2
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("size", [(1366, 768), (1280, 720)])
def test_runs_tab_never_squeezes_a_panel(browser, size):
    fake = FakeDesk(runs=6, events=24)                                      # a short screen: Activity joins the Runs tab
    ctx, page, errors = _open(browser, fake, w=size[0], h=size[1])
    try:
        page.wait_for_function("() => document.querySelectorAll('#feed > div').length > 10 && document.getElementById('run-id').textContent")
        page.click("#tabs [data-tab=runs]"); page.wait_for_timeout(600)
        assert page.evaluate("document.getElementById('act-panel').parentElement.id") == "pane-runs"
        wf = page.evaluate("(e => [e.offsetHeight, e.scrollHeight])(document.getElementById('wf-panel'))")
        assert wf[0] >= wf[1] - 1 and wf[0] > 60, wf                       # the whole panel, header and Run buttons
        assert page.is_visible("#wf-list .wfrow button")
        assert not errors, errors
    finally:
        ctx.close()


def test_add_camera_keeps_its_buttons_on_a_short_screen(browser):
    fake = FakeDesk(runs=1)
    ctx, page, errors = _open(browser, fake, w=1280, h=720)
    try:
        page.wait_for_function("() => document.getElementById('run-id').textContent")
        page.click("#cam-btn"); page.wait_for_timeout(400)
        r = page.evaluate("""() => { const m = document.querySelector('#attach .mbox').getBoundingClientRect(), b = document.getElementById('at-go').getBoundingClientRect(),
                                   t = document.getElementById('at-test').getBoundingClientRect(), pv = document.getElementById('at-pv').getBoundingClientRect();
                                   return {mb: m.bottom, gb: b.bottom, gt: b.top, tb: t.bottom, vh: innerHeight, pvh: pv.height, pvw: pv.width, mw: m.width}; }""")
        assert r["gb"] <= r["mb"] and r["gb"] <= r["vh"] and r["tb"] <= r["mb"], r
        assert r["pvh"] < 80 and r["pvw"] > r["mw"] * .8, r                # no black 16:9 box before a frame: one full-width line
        page.evaluate("atPreview('data:image/gif;base64,R0lGODlhAQABAAAAACw=', 'Connected')"); page.wait_for_timeout(200)
        r2 = page.evaluate("(() => { const m = document.querySelector('#attach .mbox').getBoundingClientRect(), b = document.getElementById('at-go').getBoundingClientRect(); return {mb: m.bottom, gb: b.bottom, vh: innerHeight}; })()")
        assert r2["gb"] <= r2["mb"] + 0.5 and r2["gb"] <= r2["vh"], r2    # with a frame shown, the footer still sits in view
        assert not errors, errors
    finally:
        ctx.close()


def test_escape_closes_the_desk_menu_and_twins_say_which(browser):
    twins = [{"id": 7, "name": "Bright Smile Dental", "business_name": "Bright Smile Dental", "template": "sales_desk", "created": NOW - 86400 * 40},
             {"id": 9, "name": "Bright Smile Dental", "business_name": "Bright Smile Dental", "template": "custom", "created": NOW - 86400 * 2},
             {"id": 11, "name": "Nour Pharmacy", "business_name": "Nour Pharmacy", "template": "blank", "created": NOW}]
    fake = FakeDesk(runs=1, desks=twins)
    ctx, page, errors = _open(browser, fake)
    try:
        page.wait_for_selector("#sh-desk")
        page.wait_for_function("() => document.getElementById('sh-dname').textContent !== '…'")
        page.click("#sh-desk"); page.wait_for_timeout(200)
        subs = page.evaluate("[...document.querySelectorAll('.sh-menu [data-d] small')].map(s => s.textContent)")
        assert len(subs) == 2 and subs[0].startswith("#7 · sales desk") and subs[1].startswith("#9 · custom"), subs
        page.keyboard.press("Escape"); page.wait_for_timeout(150)
        assert page.evaluate("document.querySelectorAll('.sh-menu').length") == 0
        assert "atlas-open" not in page.evaluate("document.getElementById('ws').className")   # one Escape, one thing closed
        assert not errors, errors
    finally:
        ctx.close()


def test_fitted_chart_cards_never_cut_a_chip(browser):
    fake = FakeDesk(runs=1)
    ctx, page, errors = _open(browser, fake, ls={"ws_teamview": "chart"})
    try:
        page.wait_for_function("() => document.getElementById('run-id').textContent && document.querySelector('#canvas.fit .node')")
        page.wait_for_timeout(500)
        cut = page.evaluate("""() => [...document.querySelectorAll('#canvas.fit .node')].flatMap(n => { const c = n.querySelector('.chips');
            return [...c.children].filter(x => x.style.display !== 'none' && x.offsetWidth && x.offsetLeft - c.offsetLeft + x.offsetWidth > c.clientWidth + .5)
              .map(x => n.dataset.id + ':' + x.textContent); })""")
        assert cut == [], cut
        assert page.evaluate("[...document.querySelectorAll('#canvas.fit .node .chips .more')].length") >= 1   # what did not fit is counted
        # a run's tool calls: each tool once, newest last
        page.evaluate("""() => { const el = nodeEl('atlas'); ['delegate', 'delegate', 'crm_update', 'delegate'].forEach(t => addChip(el, t)); }""")
        names = page.evaluate("[...nodeEl('atlas').querySelectorAll('.chips span:not(.more)')].map(s => s.textContent)")
        assert names == ["crm_update", "delegate"], names
        assert not errors, errors
    finally:
        ctx.close()


def test_activity_shows_whole_rows_and_the_status_its_heading(browser):
    fake = FakeDesk(runs=1, events=20)
    ctx, page, errors = _open(browser, fake, w=1920, h=1080)
    try:
        page.wait_for_function("() => document.querySelectorAll('#feed > div').length > 10 && W.greetEl")
        page.wait_for_timeout(700)
        rows = page.evaluate("""() => { const f = document.getElementById('feed'), fr = f.getBoundingClientRect();
            const vis = [...f.children].map(c => c.getBoundingClientRect()).filter(r => r.bottom > fr.top + .5 && r.top < fr.bottom - .5);
            return [Math.round(vis[0].top - fr.top), Math.round(fr.bottom - vis[vis.length - 1].bottom)]; }""")
        assert rows == [0, 0], rows
        assert not errors, errors
    finally:
        ctx.close()
    fake = FakeDesk(runs=1)
    ctx, page, errors = _open(browser, fake, w=1366, h=768)                 # a short panel: the status with its steps
    try:
        page.wait_for_function("() => W.greetEl && document.querySelector('#msgs .nx')")
        page.wait_for_timeout(600)
        g = page.evaluate("(() => { const m = document.getElementById('msgs'), r = W.greetEl.getBoundingClientRect(), mr = m.getBoundingClientRect(); return {tall: m.scrollHeight > m.clientHeight, top: r.top - mr.top}; })()")
        assert not g["tall"] and g["top"] >= 0, g                           # it fits: the desk's name, its time and the steps in view
        assert not errors, errors
    finally:
        ctx.close()


def test_the_first_screen_lists_the_ready_made_desks_on_a_tall_screen(browser):
    fake = FakeDesk(built=False)
    ctx, page, errors = _open(browser, fake, path="/desk/workspace", w=1920, h=1080)
    try:
        page.wait_for_function("() => document.querySelectorAll('#others button.tp').length > 0")
        page.wait_for_timeout(300)
        assert page.evaluate("document.querySelectorAll('#others button.tp').length") == 7       # all of them fit
        pane = page.evaluate("(p => [p.clientHeight, p.scrollHeight])(document.getElementById('pane-overview'))")
        assert pane[1] <= pane[0] + 1, pane                                 # and nothing is pushed off the screen
        rest = page.evaluate("(() => { const m = document.getElementById('msgs'), l = m.lastElementChild.getBoundingClientRect(); return m.getBoundingClientRect().bottom - l.bottom; })()")
        assert rest < 40, rest                                              # the conversation rests on the composer
        assert not errors, errors
    finally:
        ctx.close()
    ctx, page, errors = _open(browser, FakeDesk(built=False), path="/desk/workspace", w=1366, h=768)
    try:
        page.wait_for_function("() => typeof W !== 'undefined' && W.tpls")
        page.wait_for_timeout(300)
        pane = page.evaluate("(p => [p.clientHeight, p.scrollHeight])(document.getElementById('pane-overview'))")
        assert pane[1] <= pane[0] + 1, pane                                 # a short screen keeps the one line for them
        assert not errors, errors
    finally:
        ctx.close()


def _rows_in_view(page):
    return page.evaluate("""() => { const m = document.getElementById('msgs'), mr = m.getBoundingClientRect();
        const rows = [...m.querySelectorAll('.nx-r')].map(r => r.getBoundingClientRect());
        return [rows.length, rows.filter(r => r.top >= mr.top - .5 && r.bottom <= mr.bottom + .5).length]; }""")


@pytest.mark.parametrize("size", [(1600, 900), (1366, 768), (1280, 720)])
def test_the_opening_status_and_its_three_steps_fit(browser, size):
    """Approvals and Activity took the right column, so the status's next steps (the panel's actions) fell below the fold.
    A held, flagged approval and two agent gaps are the tallest case."""
    fake = FakeDesk(runs=1, events=12)
    fake.data["/api/actions"][0].update({"flags": "missing sign-off 'Harbour Physio'", "reason": "Outbound message to a lead needs owner approval."})
    fake.data["/api/agents/links"] = {"agents": {"writer": {"gaps": ["no email connector"]}, "crm": {"gaps": ["no CRM connected"]}}}
    ctx, page, errors = _open(browser, fake, w=size[0], h=size[1])
    try:
        page.wait_for_function("() => W.greetEl && document.querySelector('#msgs .nx') && W.ld.last && /Gaps/.test(W.greetEl.textContent)")
        page.wait_for_timeout(700)
        assert _rows_in_view(page) == [3, 3], (size, _rows_in_view(page))
        cut = page.evaluate("""() => { const b = document.getElementById('ap-body').getBoundingClientRect();
            return [...document.querySelectorAll('#ap-body .ap-acts .btn, #ap-body .ap-more .link')].map(x => x.getBoundingClientRect())
              .filter(r => r.height && (r.bottom > b.bottom + .5 || r.top < b.top - .5)).length; }""")
        assert cut == 0, size                                                # Approve & send, Edit and Reject whole
        assert page.evaluate("[...W.greetEl.querySelectorAll('li')].every(l => l.getBoundingClientRect().height <= 22)")   # one line per fact
        assert not errors, errors
    finally:
        ctx.close()


def test_team_tab_chart_is_full_size_and_lays_a_big_team_out_in_columns(browser):
    fake = FakeDesk(runs=1)
    fake.data["/api/config"]["agents"] = _agents() + [{"id": f"watch{i}", "name": f"Watcher {i}", "role": "Camera monitor", "goal": "Watch the feed.",
                                                       "reports_to": "atlas", "tools": ["camera_ask", "camera_events"]} for i in range(3)]
    ctx, page, errors = _open(browser, fake, ls={"ws_teamview": "chart"})
    try:
        page.wait_for_function("() => document.getElementById('run-id').textContent")
        page.evaluate("showTab('team')"); page.wait_for_timeout(900)
        r = page.evaluate("""() => { const c = document.getElementById('canvas').getBoundingClientRect();
            const ns = [...document.querySelectorAll('#nodes .node')].filter(n => getComputedStyle(n).display !== 'none');
            let small = 99; ns.forEach(n => n.querySelectorAll('*').forEach(e => { if (e.offsetWidth && [...e.childNodes].some(t => t.nodeType === 3 && t.textContent.trim()))
              small = Math.min(small, parseFloat(getComputedStyle(e).fontSize) * W.k); }));
            const rs = ns.map(n => n.getBoundingClientRect());
            return {k: W.k, n: ns.length, cols: W.tfit.cols.length, small, out: rs.filter(r => r.left < c.left - 1 || r.right > c.right + 1 || r.top < c.top - 1 || r.bottom > c.bottom + 1).length}; }""")
        assert r["k"] >= 1 and r["small"] >= 11, r                         # never drawn below the cards' real size
        assert r["n"] == 7 and r["cols"] >= 2 and r["out"] == 0, r          # Atlas + 6 in columns, every card whole
        assert not errors, errors
    finally:
        ctx.close()


DRAFT = {"agents": [{"id": "research", "name": "Researcher", "role": "Lead & company research", "goal": "Find who the enquirer is.", "reports_to": "atlas",
                     "tools": ["web_fetch", "read_file", "list_files"]},
                    {"id": "writer", "name": "Reply writer", "role": "First-response drafting", "goal": "Draft a short reply.", "reports_to": "atlas",
                     "tools": ["read_file", "list_files", "record_find", "record_get"]}],
         "workflows": [{"id": "inbound", "name": "Inbound lead to researched reply", "steps": ["research", "writer"], "trigger": {"kind": "webhook", "detail": "Website form"}}],
         "connectors": [{"name": "Website form", "kind": "webhook", "required": True, "purpose": "New enquiries in"}],
         "policy": {"max_words": 180, "no_money_figures": True}, "business": {"name": ""}, "cameras": []}


def _draft(browser, fake, w, h, reqs=None):
    ctx, page, errors = _open(browser, fake, path="/desk/workspace", w=w, h=h, reqs=reqs)
    page.wait_for_function("() => typeof W !== 'undefined' && W.sid")
    page.evaluate("bp => applyBlueprint(bp, true)", DRAFT)
    page.wait_for_function("() => document.getElementById('wk').dataset.phase === 'design'")
    page.wait_for_timeout(400)
    return ctx, page, errors


def test_a_small_drafts_team_tab_is_as_tall_as_its_chart(browser):
    """The draft's Team tab opened on a big dotted canvas holding three small cards. The chart is now as tall as it is,
    drawn a little larger, and the rest of the draft (workflows, connections, outbound rules) sits under it."""
    ctx, page, errors = _draft(browser, FakeDesk(built=False), 1920, 1080)
    try:
        page.evaluate("showTab('team')"); page.wait_for_timeout(900)
        r = page.evaluate("""() => { const c = document.getElementById('canvas').getBoundingClientRect(), pane = document.getElementById('pane-team').getBoundingClientRect();
            const ns = [...document.querySelectorAll('#nodes .node')].filter(n => getComputedStyle(n).display !== 'none').map(n => n.getBoundingClientRect());
            const bottom = Math.max(...ns.map(r => r.bottom));
            return {k: W.k, snug: document.getElementById('pane-team').classList.contains('snug'), plan: document.getElementById('plan-panel').offsetHeight,
                    canvasShare: c.height / pane.height, below: c.bottom - bottom, n: ns.length}; }""")
        assert r["snug"] and r["plan"] > 150 and r["canvasShare"] < .7, r
        assert 1 <= r["k"] <= 1.25 and r["below"] < 40 and r["n"] == 3, r  # no empty canvas under the cards
        plan = _text(page, "#plan-panel")
        assert "Researcher" in plan and "Reply writer" in plan and "at most 180 words" in plan, plan   # the hand-offs, as the draft's agents
        assert not errors, errors
    finally:
        ctx.close()


def test_cameras_tab_with_no_camera_is_a_setup_surface(browser):
    fake = FakeDesk(built=False)
    fake.data["/api/cameras/samples"] = {"samples": [{"name": "campus-lobby", "label": "campus building, lobby", "camera": "lobby"},
                                                     {"name": "retail-store", "label": "retail shop floor", "camera": "shop-floor"}]}
    reqs = []
    ctx, page, errors = _draft(browser, fake, 1600, 900, reqs)
    try:
        reqs.clear()
        page.evaluate("showTab('cameras')"); page.wait_for_timeout(800)
        j = _text(page, "#journal-list")
        assert "starts once the desk is built" in j and "Loading" not in j and "Reading" not in j, j      # nothing to read, said so
        assert not [x for x in reqs if "/api/vision" in x], reqs                                         # ...and nothing requested
        assert page.is_hidden("#journal-page")                                                           # no journal to print
        assert page.evaluate("document.querySelectorAll('#cam-setup .srcs button').length") == 5
        assert page.evaluate("document.querySelectorAll('#smp-list .smp').length") == 2
        blank = page.evaluate("""() => { const p = document.getElementById('pane-cameras').getBoundingClientRect();
            return p.bottom - Math.max(...[...document.querySelectorAll('#pane-cameras > .panel')].filter(e => e.offsetHeight).map(e => e.getBoundingClientRect().bottom)); }""")
        assert blank < 2, blank                                                                          # no half-empty tab
        page.click("#smp-list .smp:first-child button"); page.wait_for_timeout(400)
        assert page.is_visible("#attach") and page.evaluate("AT.tab") == "sample" and page.input_value("#at-cam-name") == "lobby"
        assert page.evaluate("AT.sample") == "sample:campus-lobby"
        assert not errors, errors
    finally:
        ctx.close()


def test_nothing_is_sent_before_the_draft_is_known(browser):
    fake = FakeDesk(built=False)
    fake.hold = {"/api/design/start"}
    reqs = []
    ctx, page, errors = _open(browser, fake, path="/desk/workspace", reqs=reqs)
    try:
        page.wait_for_function("() => typeof W !== 'undefined' && document.getElementById('phase-label').textContent.trim()")
        page.wait_for_timeout(300)
        assert _text(page, "#phase-label").strip().lower() == "loading" and "Opening the workspace" in _text(page, "#ph-sum")
        assert "No draft yet" not in _text(page, "#ph-sum") and page.is_disabled("#send")
        page.click("#say"); page.keyboard.type("Hi"); page.keyboard.press("Enter"); page.wait_for_timeout(200)
        assert page.input_value("#say") == "Hi" and not [x for x in reqs if "/say" in x], reqs        # kept, never posted to a null draft
        fake.release()
        page.wait_for_function("() => W.sid && !document.getElementById('send').disabled")
        assert _text(page, "#phase-label").strip().lower() == "brief" and "No draft yet" in _text(page, "#ph-sum")
        assert not [x for x in reqs if "/null" in x], reqs
        assert not errors, errors
    finally:
        ctx.close()


@pytest.mark.parametrize("size", [(1600, 900), (390, 844)])
def test_the_name_dialogs_buttons_never_cover_its_hint(browser, size):
    ctx, page, errors = _open(browser, FakeDesk(runs=1), w=size[0], h=size[1])
    try:
        page.wait_for_function("() => typeof W !== 'undefined' && W.ld.last")
        page.evaluate("W.me = {company: ''}; askDeskName()"); page.wait_for_timeout(300)
        gap = page.evaluate("(() => { const b = document.querySelector('#namedlg .mbox'); return b.querySelector('.mrow').getBoundingClientRect().top - b.querySelector('.hint').getBoundingClientRect().bottom; })()")
        assert gap >= 8, (size, gap)
        assert not errors, errors
    finally:
        ctx.close()


def test_the_job_line_is_cut_by_whole_lines(browser):
    fake = FakeDesk(runs=1)
    long = ("Write a short report from the camera journal on this question: what happened on the cameras in the last 30 days? "
            "Give times. Cite times, camera names and the journal entries used, and queue the reply for my approval.")
    fake.data["/api/runs"][0]["task"] = long
    fake.details["r0"]["task"] = long
    ctx, page, errors = _open(browser, fake, w=1280, h=720)
    try:
        page.wait_for_function("() => document.querySelector('#run-body .run-task')")
        r = page.evaluate("(() => { const t = document.querySelector('#run-body .run-task'), x = t.querySelector('.rtx'), cs = getComputedStyle(x); return {clamp: cs.webkitLineClamp, title: t.title, h: x.clientHeight, lh: parseFloat(cs.lineHeight)}; })()")
        assert r["clamp"] == "3" and r["title"] == long and abs(r["h"] - 3 * r["lh"]) < 2, r
        assert not errors, errors
    finally:
        ctx.close()


def test_ctrl_k_never_puts_a_workflow_run_first(browser):
    """Typing "cam" to reach Cameras put "Campus Safety Summary · run now" first, and Enter started a whole team run."""
    fake = FakeDesk(runs=1)
    fake.data["/api/config"]["workflows"] = [{"id": "camsum", "name": "Campus Safety Summary", "description": "research -> writer"}]
    reqs = []
    ctx, page, errors = _open(browser, fake, reqs=reqs)
    try:
        page.wait_for_function("() => W.ld.last && document.getElementById('sh-rail')")
        page.keyboard.press("Control+k"); page.wait_for_timeout(250); page.keyboard.type("cam"); page.wait_for_timeout(250)
        items = page.evaluate("[...document.querySelectorAll('#sh-pal .it > span')].map(s => s.textContent)")
        assert items and items[0] != "Campus Safety Summary" and items.index("Campus Safety Summary") > items.index("Add camera"), items
        page.keyboard.type(" safety"); page.wait_for_timeout(200); page.keyboard.press("Enter"); page.wait_for_timeout(400)
        assert not [x for x in reqs if x.startswith("POST /api/runs")], reqs                            # it only fills the job bar
        assert "Campus Safety Summary" in page.input_value("#job") and page.evaluate("W.jobWf && W.jobWf.id") == "camsum"
        assert not errors, errors
    finally:
        ctx.close()


def test_drafted_cards_fit_their_chips_from_the_start(browser):
    """A card drafted in front of the owner had its tool chips fitted before it was on the canvas, so the fitted chart
    showed a chip cut through at the card's edge until something else re-laid it out."""
    ctx, page, errors = _draft(browser, FakeDesk(built=False), 1920, 1080)
    try:
        page.wait_for_function("() => document.querySelector('#canvas.fit .node:not(.atlas)')")
        cut = page.evaluate("""() => [...document.querySelectorAll('#canvas.fit .node')].flatMap(n => { const c = n.querySelector('.chips');
            return [...c.children].filter(x => x.style.display !== 'none' && x.offsetWidth && x.offsetLeft - c.offsetLeft + x.offsetWidth > c.clientWidth + .5)
              .map(x => n.dataset.id + ':' + x.textContent); })""")
        assert cut == [], cut
        assert not errors, errors
    finally:
        ctx.close()
