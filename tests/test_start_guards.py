"""One press, one thing: the server side of the workspace's double-start guards.

- POST /api/runs: the same job on the same desk again while the first is starting or running (within a few seconds) is
  refused with 409, so a double click, a second Enter or a second tab never starts two runs (two costs, duplicate
  outbound drafts). Another job, another desk, a finished run or a start that failed are never blocked.
- POST /api/design/<sid>/build twice at once makes one desk (the second build updates it), never two."""
import json
import threading
import time

import pytest


def J(r):
    return json.loads(r.data)


@pytest.fixture()
def client(app_client, monkeypatch):
    import atlas.desk.app as A
    monkeypatch.setattr(A, "OPEN", False)        # accounts on, whatever an earlier test switched
    return app_client


def _signup(c, email: str, company: str) -> None:
    import atlas.desk.app as A
    A._RL_AUTH._hits.clear()                     # the whole suite signs up from one IP: stay under the auth rate limit
    assert J(c.post("/signup", json={"name": company, "company": company, "email": email, "password": "password1"}))["ok"]


def _second_client(email: str):
    """The same owner in a second tab / browser: its own session cookie."""
    import atlas.desk.app as A
    A._RL_AUTH._hits.clear()
    c2 = A.app.test_client()
    assert J(c2.post("/login", json={"email": email, "password": "password1"})).get("ok")
    return c2


class _Alive:
    """A run holder whose thread runs until released (stands in for a run still going)."""

    def __init__(self):
        self.ev = threading.Event()
        self.thread = threading.Thread(target=self.ev.wait, args=(10,), daemon=True)
        self.thread.start()


def test_claim_start_refuses_the_same_job_while_it_starts_or_runs(monkeypatch):
    import atlas.desk.app as A
    monkeypatch.setattr(A, "_starts", {})
    k = A._claim_start(901, "Draft a reply to the newest enquiry")
    assert k is not None
    assert A._claim_start(901, "  draft a reply   to the NEWEST enquiry ") is None      # still starting: refused (spacing, case)
    assert A._claim_start(902, "Draft a reply to the newest enquiry") is not None        # another desk
    assert A._claim_start(901, "Prepare tomorrow's staff briefing") is not None          # another job
    alive = _Alive()
    monkeypatch.setitem(A._runs, "guard-run-1", {"events": [], "thread": alive.thread, "desk_id": 901, "task": "x", "started": time.time()})
    try:
        A._release_start(k, "guard-run-1")
        assert A._claim_start(901, "Draft a reply to the newest enquiry") is None        # running: refused
        alive.ev.set(); alive.thread.join(2)
        assert A._claim_start(901, "Draft a reply to the newest enquiry") is not None    # finished: a new run may start
    finally:
        alive.ev.set()
    k2 = A._claim_start(903, "Chase the Jones invoice")
    A._release_start(k2, "")                                                             # the start failed: the next press may try
    assert A._claim_start(903, "Chase the Jones invoice") is not None
    monkeypatch.setattr(A, "START_DEDUPE_S", 0.05)
    k3 = A._claim_start(904, "Write the weekly report")
    assert k3 is not None and A._claim_start(904, "Write the weekly report") is None
    time.sleep(0.08)
    assert A._claim_start(904, "Write the weekly report") is not None                    # only within the window


def test_two_identical_starts_at_once_make_one_run(client, monkeypatch):
    """The workspace's double-Deploy race, seen from the server: two POSTs with the same job arrive together."""
    import atlas.desk.app as A
    c = client
    email = "guard-runs@example.com"
    _signup(c, email, "Guard Runs")
    J(c.post("/api/desks", json={"name": "Guard Runs", "template": "sales_desk", "tier": "free"}))
    c2 = _second_client(email)
    calls, alive = [], []

    def slow_start(desk, task, mode, lead_id=None, case_id=None):                       # a start that takes a moment
        calls.append(task)
        time.sleep(0.4)
        h = _Alive(); alive.append(h)
        rid = f"guard-run-{len(calls)}"
        A._runs[rid] = {"events": [], "thread": h.thread, "desk_id": desk["id"], "task": task, "started": time.time()}
        return rid

    monkeypatch.setattr(A, "_start_run", slow_start)
    monkeypatch.setattr(A, "_starts", {})
    out = []
    body = {"task": "Draft a reply to the newest enquiry", "mode": "auto"}
    ts = [threading.Thread(target=lambda cl=cl: out.append(cl.post("/api/runs", json=body))) for cl in (c, c2)]
    try:
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        codes = sorted(r.status_code for r in out)
        assert codes == [200, 409], codes
        refused = next(J(r) for r in out if r.status_code == 409)
        assert refused["error"] == "duplicate_run" and "one run" in refused["message"]
        assert calls == [body["task"]]                                                    # one run started
        assert c.post("/api/runs", json=body).status_code == 409                          # still running: still one
        assert c.post("/api/runs", json={"task": "Prepare tomorrow's staff briefing"}).status_code == 200   # another job is fine
    finally:
        for h in alive:
            h.ev.set()
        for rid in [k for k in A._runs if k.startswith("guard-run-")]:
            A._runs.pop(rid, None)
        c.get("/logout"); c2.get("/logout")


def test_two_builds_of_one_draft_at_once_make_one_desk(client, monkeypatch):
    import atlas.desk.app as A
    c = client
    email = "guard-build@example.com"
    _signup(c, email, "Guard Build")
    sid = J(c.post("/api/design/start", json={}))["sid"]
    c.post(f"/api/design/{sid}/say", json={"text": "We are a lettings agency and answer Rightmove enquiries far too slowly."}).get_data()
    bp = J(c.get(f"/api/design/{sid}"))["blueprint"]
    assert bp and bp.get("agents")
    c2 = _second_client(email)
    uid = J(c.get("/api/me"))["id"]
    before = len(A.store.desks_for(uid))
    real_add = A.store.add_desk

    def slow_add(*a, **k):                                                                # widen the window two builds race in
        time.sleep(0.3)
        return real_add(*a, **k)

    monkeypatch.setattr(A.store, "add_desk", slow_add)
    out = []
    ts = [threading.Thread(target=lambda cl=cl: out.append(cl.post(f"/api/design/{sid}/build", json={"blueprint": bp, "tier": "free"})))
          for cl in (c, c2)]
    try:
        [t.start() for t in ts]
        [t.join(15) for t in ts]
        assert [r.status_code for r in out] == [200, 200]
        ids = {J(r)["desk"]["id"] for r in out}
        assert len(ids) == 1, ids                                                          # the second build updated the first's desk
        assert len(A.store.desks_for(uid)) == before + 1
    finally:
        c.get("/logout"); c2.get("/logout")
