"""scripts/cyber_replay_bundle.py: a REPLAY bundle re-enacted from a portal database through the portal's own routes.

The source database is built here from the SecRepo auth.log campaign excerpt (Security Repo by Mike Sconzo, CC BY 4.0)
with a run, its activity and a containment decision at known wall-clock times. The script runs as a subprocess, as an
operator runs it: its scratch portal must not share this process's app module. Offline: demo mode, CYBER_OFFLINE=1."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures" / "cyber"
T0 = 1790900000.0                                    # wall clock when the recorded session started


def _source(tmp: Path) -> dict:
    from atlas import cyber as CY
    from atlas import templates
    from atlas.store import Store
    src = Store(tmp / "desk.db")
    desk = src.add_desk(1, "Security operations", "soc_desk", "free", templates.build_desk("soc_desk", {}))
    ds = src.for_desk(desk["id"])
    evs, _ = CY.parse("sshd", CY.open_lines(FX / "auth_campaign.log.gz"), now=CY.parse_time("2026-10-02T12:00:00Z"))
    ids = ds.add_sec_events(evs, origin="upload:auth_campaign.log.gz")
    c = src._conn                                    # the session's real times, set after the fact
    c.execute("UPDATE sec_events SET ingested=? WHERE id<=?", (T0 + 10, ids[799]))
    c.execute("UPDATE sec_events SET ingested=? WHERE id>?", (T0 + 30, ids[799]))
    dets = CY.detect(ds.sec_events(order="asc", limit=10000))
    for d in dets:
        ds.upsert_sec_detection(d)
    rid = "20261002-000000-bundle"
    src.create_run(rid, "SECURITY DETECTION — 1 new detection(s) on Security operations\n- [MEDIUM] ...", "alert_triage",
                   "", desk["id"])
    ds.set_sec_detection_run(["wordlist_fingerprint:e3503990"], rid)
    ev = ds.sec_events(src="61.197.203.243", order="asc", limit=1)[0]["id"]
    src.add_event(rid, "tool", "triage", "log_search(src=61.197.203.243) -> 409 events")
    src.finish_run(rid, "done", f"Replay of a public dataset: one wordlist campaign [#{ev}] [#999999].\n\n---\n"
                                "Verified by the desk (not model-claimed): 1 action(s) queued for approval", 10, 20)
    c.execute("UPDATE runs SET created=?, ended=? WHERE id=?", (T0 + 40, T0 + 70, rid))
    c.execute("UPDATE events SET ts=? WHERE run_id=?", (T0 + 45, rid))
    spec = {"targets": [{"kind": "ip", "value": "61.197.203.243", "action": "block"}], "action": "", "evidence": [ev],
            "connector": "", "justification": "Synthetic justification for the test."}
    aid = ds.add_action(rid, "responder", "containment", "61.197.203.243", "Block 61.197.203.243?", json.dumps(spec),
                        "containment needs a person's approval")
    src.decide_action(aid, "sent", by="Pierre",
                      note="[simulated containment — no connector named in the action; nothing was blocked: block 61.197.203.243]")
    c.execute("UPDATE actions SET created=?, decided_at=? WHERE id=?", (T0 + 60, T0 + 80, aid))
    c.commit()
    c.close()
    return {"desk": desk["id"], "events": len(ids), "run": rid, "action": aid, "ev": ev, "dets": len(dets)}


def test_bundle_reenacts_events_detections_run_and_decision(tmp_path):
    s = _source(tmp_path)
    out = tmp_path / "b.json"
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "ATLAS_INTEL_DIR")}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "cyber_replay_bundle.py"), "--data-dir", str(tmp_path),
                        "--desk", str(s["desk"]), "--name", "t-1", "--from", str(T0), "--to", str(T0 + 90),
                        "--tz", "Europe/London", "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=600, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    n = s["dets"]                                     # the real rules over the same events give the stored detections
    assert n >= 5 and f"{n} of the source's {n} re-enacted with the same id, severity and evidence count" in r.stderr
    b = json.loads(out.read_text(encoding="utf-8"))
    assert b["kind"] == "atlas-cyber-recording" and b["version"] == 1 and b["name"] == "t-1" and b["duration"] == 90
    assert b["desk"] == {"id": s["desk"], "name": "Security operations"} and b["tz"] == "Europe/London"
    fr = b["frames"]
    assert [f["t"] for f in fr] == sorted(f["t"] for f in fr) and all(0 <= f["t"] <= 90 for f in fr)

    def at(kind, t):                                  # what the page shows at t: the last frame of that kind
        x = [f for f in fr if f["kind"] == kind and f["t"] <= t]
        return x[-1]["data"] if x else None

    total = lambda d: sum(l["total"] for l in d["lanes"])           # noqa: E731
    assert total(at("timeline", 9)) == 0 and total(at("timeline", 12)) == 800 and total(at("timeline", 90)) == s["events"]
    tl = [f for f in fr if f["kind"] == "timeline"]
    assert all(abs(f["data"]["now"] - (T0 + f["t"])) < 0.01 for f in tl)   # each answer as the page had it then
    assert len(tl) <= 8                                               # real changes only, not one frame per poll

    def wordlist(t):
        return [d for d in at("detections", t)["detections"] if d["id"] == "wordlist_fingerprint:e3503990"]

    assert at("detections", 9)["total"] == 0
    assert wordlist(36)[0]["counts"] == {"attempts": 1636, "length": 409, "sources": 4} and wordlist(36)[0]["run_id"] == ""
    assert wordlist(90)[0]["run_id"] == s["run"]                     # linked once the run had started

    assert at("incident", 39) == {"run": None}
    live = at("incident", 50)
    assert live["run"]["id"] == s["run"] and live["run"]["active"] is True and live["run"]["status"] == "running"
    assert [a["text"] for a in live["activity"]] == ["log_search(src=61.197.203.243) -> 409 events"] and live["summary"] == ""
    done = at("incident", 90)
    assert done["run"]["active"] is False and done["run"]["status"] == "done" and done["run"]["ended"] == T0 + 70
    assert done["summary"].startswith("Replay of a public dataset") and done["verified"].startswith("Verified by the desk")
    assert [(c["id"], c["found"]) for c in done["citations"]] == [(s["ev"], True), (999999, False)]
    assert done["actions"] == [s["action"]] and "wordlist_fingerprint:e3503990" in done["detections"]

    assert at("containment", 59)["actions"] == []
    pend = at("containment", 75)["actions"]
    assert [(a["id"], a["status"], a["question"]) for a in pend] == [(s["action"], "pending", "Block 61.197.203.243?")]
    assert pend[0]["policy"]["ok"] is True and pend[0]["connector_status"] == "none" and pend[0]["decided_at"] is None
    ui = [f for f in fr if f["kind"] == "ui"]
    assert [(f["t"], f["data"]) for f in ui] == [(80.0, {"action": "approve", "id": s["action"]})]
    after = at("containment", 80.1)["actions"][0]
    assert after["status"] == "sent" and after["decided_by"] == "Pierre" and after["decided_at"] == T0 + 80
    assert after["note"].startswith("[simulated containment")
    assert at("config", 0)["hook_url"].endswith("/hook/<token>/logs")
    assert b["generator"]["script"] == "scripts/cyber_replay_bundle.py" and b["generator"]["run"] == s["run"]
