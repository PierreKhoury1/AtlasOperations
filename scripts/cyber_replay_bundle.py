"""Build a REPLAY bundle for the cyber page from a REAL portal database and run, after the fact.

The page's record mode (/desk/cyber?record=<name>) is the first choice for film footage (for a long take start the
portal with CYBER_REC_MAX_MB above the default 20). Use this script when no recording was made, when a save failed,
or for a clean cut of one session. It re-enacts what the page would have shown, through the portal's own routes,
from what the database recorded:

A throw-away portal runs in-process (scratch data folder, DESK_MODE=demo, CYBER_OFFLINE=1, no model is ever called).
The script walks the source desk's history in wall-clock time W, from --from to --to, on the page's poll cadence. At
every poll the scratch database holds what the source database says existed at W, with the ORIGINAL ids:
  - security events whose `ingested` time is <= W (`ts` stays the original event time);
  - detections, recomputed at W by the real rules (scheduler.cyber_detect) on the live throttle: one pass per 10 s
    while events arrive, and one right after an upload or the end of a replay; linked to their run once it started;
  - runs as of W (running until their recorded end, then their real status and summary) with their recorded activity;
  - approval-queue actions as of W (pending until their recorded decision, then the real status, decider and note);
  - connectors' name, kind and auto flag (never their config or secrets), camera events with ts <= W, and each
    log_replay job's progress as of W (events inserted, newest original time, done).
Each route response that changed becomes a frame at t = W - from, exactly as the page's recorder stores it.

Not instant-exact: polls and detection passes follow the live cadence, not the live portal's exact instants; a
containment decision's `ui` click frame sits at its recorded decision time; trigger_status (kept in memory only) is
null; rows deleted since (a removed connector, a reset desk) cannot come back. Every value shown is real data from the
database, processed by the real code.

    py scripts/cyber_replay_bundle.py --data-dir data --desk 3 --name maccdc-take1 --tz Europe/London \
        [--since 2012-03-17T14:30:00Z --until 2012-03-17T21:00:00Z] [--from ISO|epoch] [--to ISO|epoch] \
        [--run <run id>] [--mask | --no-mask] [--credit secrepo] [--timeline-s 2] [--out file.json] [--install]

--install also writes <data-dir>/cyber/recordings/<desk>/<name>.json (atomically): open /desk/cyber?replay=<name>.
DATABASE_URL in the environment selects a PostgreSQL source instead of <data-dir>/desk.db. The source is only read.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
POLL_S = {"timeline": 2.0, "containment": 1.5, "incident": 2.0, "graph": 3.0, "detections": 3.0, "config": 15.0}
WINDOWED = ("timeline", "graph", "detections")
DETECT_EVERY_S = 10.0           # the live throttle (scheduler.CYBER_DETECT_EVERY_S)
GRAPH_EVERY_S = 10.0            # the live graph rebuild throttle (app.CYBER_GRAPH_MIN_S)
NEVER = 32503680000.0           # next_run for re-enacted jobs: the scratch scheduler never runs them
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class _Recorded:
    """Stands in for a finished run's worker thread in the scratch portal: alive until the run's recorded end."""

    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


def _when(v: str | None, CY) -> float | None:
    if v in (None, ""):
        return None
    t = CY.parse_time(v)
    if t is None:
        raise SystemExit(f"not a time: {v!r} (ISO 8601 or epoch seconds)")
    return t


def _load_source(src, desk_id: int) -> dict:
    """Everything the re-enactment needs from the source desk (read only; connector configs are never read out)."""
    ds = src.for_desk(desk_id)
    events, after = [], 0
    while True:
        chunk = ds.sec_events(after_id=after, order="id", limit=100000)
        if not chunk:
            break
        events += chunk
        after = chunk[-1]["id"]
    runs = [src.run(r["id"]) for r in src.runs(10000, desk_id)]
    acts = ds.actions(limit=100000)
    jobs = [j for j in src.jobs(desk_id) if j["kind"] in ("log_replay", "log_watch")]
    conns = [{"name": c["name"], "kind": c["kind"], "auto": bool(c.get("auto")), "created": c.get("created") or 0}
             for c in ds.connectors()]
    return {"events": events, "dets": ds.sec_detections(limit=100000), "runs": [r for r in runs if r],
            "acts": acts, "jobs": jobs, "conns": conns, "vision": src.vision_events(desk_id, "", 0, "", 100000),
            "run_events": {r["id"]: src.events(r["id"]) for r in runs if r}}


def _default_span(data: dict, run: dict | None, since: float | None, until: float | None) -> tuple[float, float]:
    """From the first arrival of the window's events (or the run's start) to just after the last thing that happened."""
    evs = [e for e in data["events"] if (since is None or e["ts"] >= since) and (until is None or e["ts"] <= until)]
    starts = [min(e["ingested"] for e in evs)] if evs else []
    if run:
        starts.append(float(run["created"]) - 5)
    if not starts:
        raise SystemExit("nothing to replay: no events in the window and no run (pass --from/--to)")
    w0 = min(starts) - 1
    ends = [max(e["ingested"] for e in evs)] if evs else []
    ends += [float(r["ended"]) for r in data["runs"] if r.get("ended") and float(r["created"]) >= w0]
    ends += [float(a["decided_at"]) for a in data["acts"] if a.get("decided_at") and float(a["created"]) >= w0]
    ends += [float(a["created"]) for a in data["acts"] if float(a["created"]) >= w0]
    return w0, max(ends) + 3


def build(args) -> dict:
    # the scratch portal must be configured before anything from atlas is imported (config reads the environment)
    src_dir = Path(args.data_dir or os.environ.get("ATLAS_DATA_DIR") or ROOT / "data").resolve()
    src_url = os.environ.get("DATABASE_URL", "").strip() or None
    scratch = Path(tempfile.mkdtemp(prefix="atlas-replay-bundle-"))
    intel = os.environ.get("ATLAS_INTEL_DIR") or str(src_dir / "intel")      # the DB-IP notice follows the real portal
    if not Path(intel).is_dir():
        intel = str(scratch / "intel")              # never create folders in the source portal
    os.environ.update({"ATLAS_DATA_DIR": str(scratch), "ATLAS_INTEL_DIR": intel, "DESK_MODE": "demo",
                       "CYBER_OFFLINE": "1", "CYBER_GRAPH_MIN_S": "0", "DEMO_DELAY": "0",
                       "DESK_SECRET": secrets.token_hex(16)})
    for k in ("DATABASE_URL", "DESK_OPEN"):
        os.environ.pop(k, None)
    sys.path.insert(0, str(ROOT))
    from werkzeug.security import generate_password_hash
    from atlas import cyber as CY
    from atlas.store import Store
    from atlas.desk import scheduler as S
    import atlas.desk.app as A

    if src_url is None and not (src_dir / "desk.db").is_file():
        raise SystemExit(f"no portal database at {src_dir / 'desk.db'} (pass --data-dir, or set DATABASE_URL)")
    src = Store(src_dir / "desk.db", url=src_url)
    desk = src.desk(args.desk)
    if not desk:
        raise SystemExit(f"desk {args.desk} not found")
    data = _load_source(src, args.desk)
    since, until = _when(args.since, CY), _when(args.until, CY)
    det_runs = [r for r in data["runs"] if str(r.get("task") or "").startswith((S.TASK_PREFIX_DETECTION, S.TASK_PREFIX_REPORT))]
    run = next((r for r in data["runs"] if r["id"] == args.run), None) if args.run else (det_runs[0] if det_runs else None)
    if args.run and not run:
        raise SystemExit(f"run {args.run} is not on desk {args.desk}")
    w0, w1 = _default_span(data, run, since, until)
    w0, w1 = _when(args.start, CY) or w0, _when(args.end, CY) or w1
    if not 0 < w1 - w0 <= args.max_duration:
        raise SystemExit(f"span {w1 - w0:.0f} s is empty or longer than --max-duration {args.max_duration:.0f} s: "
                         "pass --from/--to (wall-clock times of the session)")
    print(f"desk {args.desk} '{desk['name']}': {len(data['events']):,} events, {len(data['dets'])} detections, "
          f"{len(data['runs'])} runs, {len(data['acts'])} actions; re-enacting {CY.fmt_ts(w0)} to {CY.fmt_ts(w1)} "
          f"({w1 - w0:.0f} s)", file=sys.stderr)

    # ---- the scratch portal: one user, one desk with the real desk's name, template and config
    gen_t0 = time.time()
    st, conn = A.store, A.store._conn               # explicit ids need plain INSERTs into the scratch SQLite file
    pw = secrets.token_urlsafe(16)
    user = st.add_user("replay-bundle@example.invalid", "Replay", "", generate_password_hash(pw))
    sd = st.add_desk(user["id"], desk["name"], desk["template"], desk.get("tier") or "free", desk.get("config") or {})
    sid = sd["id"]
    A.app.config["TESTING"] = True
    client = A.app.test_client()
    assert client.post("/login", json={"email": "replay-bundle@example.invalid", "password": pw}).status_code == 200
    assert client.post(f"/api/desks/{sid}/select").status_code == 200

    events = sorted(data["events"], key=lambda e: (e["ingested"], e["id"]))
    ingested = [e["ingested"] for e in events]
    # upload requests and replay ends force a detection pass right away (the live portal does the same)
    forced, i = set(), 0
    while i < len(events):
        j = i
        while j + 1 < len(events) and events[j + 1]["origin"] == events[i]["origin"]:
            j += 1
        if str(events[i]["origin"]).startswith(("upload:", "replay:")):
            forced.add(events[j]["ingested"])
        i = j + 1
    det_run = {d["id"]: d["run_id"] for d in data["dets"] if d.get("run_id")}
    rev = {r["id"]: sorted(data["run_events"].get(r["id"], []), key=lambda e: e["ts"]) for r in data["runs"]}
    job_pos = {j["id"]: [i for i, e in enumerate(events) if e["origin"] == f"replay:{j['id']}"] for j in data["jobs"]}
    state = {"ev": 0, "dirty": False, "last_pass": -1e18, "passes": 0, "runs": {}, "acts": {}, "conns": set(),
             "vision": set(), "jobs": {}, "graph_w": -1e18, "graph_ver": None}

    def advance(w: float) -> None:
        """Put into the scratch portal what existed at wall-clock time w."""
        n = bisect.bisect_right(ingested, w)
        if n > state["ev"]:
            rows = [(e["id"], sid, e["ts"], e["ingested"], e["source"], e["sensor"], e["kind"], e["src"], e["dst"],
                     e["user"], e["sig"], e["severity"], e["message"], "", json.dumps(e["attrs"], separators=(",", ":")),
                     e["origin"], 0, "") for e in events[state["ev"]:n]]
            with st._lock:
                conn.executemany("INSERT INTO sec_events(id,desk_id,ts,ingested,source,sensor,kind,src,dst,username,sig,"
                                 "severity,message,raw,attrs,origin,triggered,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.commit()
            state["ev"], state["dirty"] = n, True
        for c in data["conns"]:
            if c["created"] <= w and c["name"] not in state["conns"]:
                st.add_connector(sid, c["kind"], c["name"], {}, c["auto"])       # no config: no secrets leave the source
                state["conns"].add(c["name"])
        for v in data["vision"]:
            if (v.get("ts") or 0) <= w and v["id"] not in state["vision"]:
                with st._lock:
                    conn.execute("INSERT INTO vision_events(id,desk_id,camera,ts,counts,motion,backend,reason,question,answer,"
                                 "snapshot,triggered,run_id,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (v["id"], sid, v.get("camera") or "", v["ts"], json.dumps(v.get("counts") or {}),
                                  v.get("motion") or 0, v.get("backend") or "", v.get("reason") or "", v.get("question") or "",
                                  v.get("answer") or "", "", int(v.get("triggered") or 0), v.get("run_id") or "",
                                  v.get("source") or "camera"))
                    conn.commit()
                state["vision"].add(v["id"])
        for j in data["jobs"]:
            if (j.get("created") or 0) > w:
                continue
            spec = json.loads(j.get("task") or "{}") if str(j.get("task") or "").startswith("{") else {}
            done = False
            if j["kind"] == "log_replay":            # progress as of w: what the replay had inserted by then
                pos = job_pos[j["id"]]
                n = bisect.bisect_left(pos, state["ev"])
                newest = events[pos[n - 1]] if n else None
                done = bool((spec.get("state") or {}).get("done")) and bool(pos) and w >= events[pos[-1]]["ingested"]
                spec["state"] = {"inserted": n, "last_ts": newest["ts"] if newest else None, "done": done,
                                 "year_assumed": bool(newest and (newest.get("attrs") or {}).get("year_assumed"))}
            else:
                spec.pop("state", None)
            key = json.dumps(spec, sort_keys=True)
            if state["jobs"].get(j["id"]) != key:
                with st._lock:
                    conn.execute("INSERT OR REPLACE INTO jobs(id,desk_id,kind,name,task,every_min,next_run,last_run,last_result,"
                                 "enabled,created,last_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (j["id"], sid, j["kind"], j["name"], key, 0, NEVER, None, "", 0 if done else 1,
                                  j.get("created") or 0, ""))
                    conn.commit()
                state["jobs"][j["id"]] = key
        for r in data["runs"]:
            created, ended = float(r["created"] or 0), r.get("ended")
            if created > w:
                continue
            # a row from before runs had an `ended` column but with a final status: finished, time unknown
            finished = float(ended) <= w if ended is not None else r.get("status") not in ("running", None)
            with A._runs_lock:                      # register the holder first: the watchdog fails runs nobody owns
                h = A._runs.get(r["id"])
                if h is None and not finished:
                    h = A._runs[r["id"]] = {"desk_id": sid, "thread": _Recorded(), "events": [], "orch": None,
                                            "task": r.get("task") or "", "started": time.time()}
                if h is not None:
                    h["started"] = time.time()
                    h["events"] = [e for e in rev.get(r["id"], []) if e["ts"] <= w]
                    h["thread"].alive = not finished
            phase = "done" if finished else "running"
            if state["runs"].get(r["id"]) != phase:
                with st._lock:
                    conn.execute("INSERT OR REPLACE INTO runs(id,created,task,mode,status,summary,tokens_in,tokens_out,"
                                 "run_dir,desk_id,ended) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (r["id"], created, r.get("task") or "", r.get("mode") or "",
                                  r.get("status") if finished else "running", (r.get("summary") or "") if finished else "",
                                  r.get("tokens_in") or 0 if finished else 0, r.get("tokens_out") or 0 if finished else 0,
                                  "", sid, float(ended) if finished and ended is not None else None))
                    conn.commit()
                if r["id"] not in state["runs"]:
                    link(r["id"])
                state["runs"][r["id"]] = phase
        for a in data["acts"]:
            if float(a["created"] or 0) > w:
                continue
            decided = a.get("decided_at") is not None and float(a["decided_at"]) <= w
            phase = "decided" if decided else "pending"
            if state["acts"].get(a["id"]) != phase:
                with st._lock:
                    conn.execute('INSERT OR REPLACE INTO actions(id,run_id,created,agent,kind,"to",subject,body,reason,status,'
                                 "decided_at,decided_by,note,flags,desk_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (a["id"], a.get("run_id") or "", a["created"], a.get("agent") or "", a.get("kind") or "",
                                  a.get("to") or "", a.get("subject") or "", a.get("body") or "", a.get("reason") or "",
                                  a.get("status") if decided else "pending", a["decided_at"] if decided else None,
                                  (a.get("decided_by") or "") if decided else "", (a.get("note") or "") if decided else "",
                                  a.get("flags") or "", sid))
                    conn.commit()
                state["acts"][a["id"]] = phase

    def link(rid: str) -> None:
        """A run that has started owns the detections the source linked to it (when they exist yet)."""
        ids = [d for d, r in det_run.items() if r == rid]
        if ids:
            st.for_desk(sid).set_sec_detection_run(ids, rid)

    def detect_pass(w: float) -> None:
        S.cyber_detect(st, st.desk(sid), None, trigger=False, force=True)
        with st._lock:                              # the pass ran at w, not at generation time
            conn.execute("UPDATE sec_detections SET created=? WHERE desk_id=? AND created>=?", (w, sid, gen_t0))
            conn.execute("UPDATE sec_detections SET updated=? WHERE desk_id=? AND updated>=?", (w, sid, gen_t0))
            conn.commit()
        for rid in state["runs"]:
            link(rid)
        state["dirty"], state["last_pass"], state["passes"] = False, w, state["passes"] + 1

    # ---- the poll schedule: each kind on the page's cadence, plus containment + incident right after a decision
    polls = []
    for kind, every in POLL_S.items():
        every = args.timeline_s if kind == "timeline" else every
        k = 0
        while w0 + k * every <= w1:
            polls.append((w0 + k * every, kind))
            k += 1
    clicks = []
    for a in data["acts"]:
        d = a.get("decided_at")
        if a.get("kind") == "containment" and d is not None and w0 <= float(d) <= w1:
            clicks.append((float(d), "approve" if a.get("status") in ("approved", "sent", "failed") else "reject", a["id"]))
            polls += [(float(d) + 0.05, "containment"), (float(d) + 0.05, "incident")]
    polls += [(f + 0.05, "detect") for f in forced if w0 <= f <= w1]
    polls.sort(key=lambda p: (p[0], p[1] != "detect"))
    query = {k: v for k, v in (("since", args.since), ("until", args.until)) if v}
    frames, last, held = [], {}, {}
    for w, kind in polls:
        advance(w)
        if state["dirty"] and (kind == "detect" or w - state["last_pass"] >= DETECT_EVERY_S):
            detect_pass(w)
        if kind == "detect":
            continue
        ver = (state["ev"], state["passes"])
        if kind == "graph" and "graph" in last and ver != state["graph_ver"] and w - state["graph_w"] < GRAPH_EVERY_S:
            continue                                # live: a graph is rebuilt at most every 10 s while data arrives
        url = f"/api/cyber/{kind}"
        if kind in WINDOWED and query:
            url += "?" + urlencode(query)                # "+01:00" offsets must not arrive as spaces
        resp = client.get(url)
        if resp.status_code != 200:
            raise SystemExit(f"{url} -> HTTP {resp.status_code}: {resp.get_data(as_text=True)[:200]}")
        body = resp.get_json()
        if kind == "graph":
            state["graph_w"], state["graph_ver"] = w, ver
        if kind == "timeline":
            body["now"] = w                         # the response as the page would have had it at w
        if kind == "config" and isinstance(body.get("hook_url"), str):   # same rule as the page's recorder: no token
            body["hook_url"] = re.sub(r"/hook/[^/?#]+", "/hook/<token>", body["hook_url"])
        # the page's recorder rule: a frame per real change (a timeline's `now` alone is none), and for the timeline
        # also the poll just before a change, so the REPLAY clock moves only between two real observations
        s = json.dumps({**body, "now": 0} if kind == "timeline" else body, sort_keys=True)
        frame = {"t": round(w - w0, 3), "kind": kind, "data": body}
        if last.get(kind) == s:
            if kind == "timeline":
                held[kind] = frame
            continue
        last[kind] = s
        if kind in held:
            frames.append(held.pop(kind))
        frames.append(frame)
    frames += [{"t": round(t - w0, 3), "kind": "ui", "data": {"action": act, "id": aid}} for t, act, aid in clicks]
    frames.sort(key=lambda f: f["t"])

    # ---- consistency: the re-enacted detections at the end against what the source stored (same window)
    win = {k: v for k, v in (("since", since), ("until", until)) if v is not None}
    final = {d["id"]: d for d in st.for_desk(sid).sec_detections(limit=100000, **win)}
    stored = {d["id"]: d for d in src.for_desk(args.desk).sec_detections(limit=100000, **win)}
    same = [k for k in stored if k in final and final[k]["severity"] == stored[k]["severity"]
            and final[k]["evidence_total"] == stored[k]["evidence_total"]]
    print(f"detections in the window at the end: {len(same)} of the source's {len(stored)} re-enacted with the same "
          f"id, severity and evidence count ({len(final)} re-enacted)", file=sys.stderr)
    cfg = (desk.get("config") or {}).get("cyber") or {}
    mask = bool(cfg.get("mask_public_ips")) if args.mask is None else args.mask
    if args.credit:
        query["credit"] = args.credit
    bundle = {"kind": "atlas-cyber-recording", "version": 1, "name": args.name, "created": time.time(),
              "desk": {"id": args.desk, "name": desk["name"]}, "tz": args.tz, "viewport": [1920, 1080],
              "mask_public_ips": mask, "query": query, "duration": round(w1 - w0, 3), "frames": frames,
              "generator": {"script": "scripts/cyber_replay_bundle.py", "from": w0, "to": w1,
                            "run": run["id"] if run else None,
                            "note": ("Re-enacted from the portal database through the portal's routes: real events, real "
                                     "rule output, real run and approval records. Polls and detection passes follow the "
                                     "live cadence; a decision's click frame sits at its recorded decision time.")}}
    S._stop.set()                                   # the scratch portal's scheduler loop; then drop the scratch copy
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(scratch, ignore_errors=True)
    return {"bundle": bundle, "src_dir": src_dir}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", default="", help="the portal's ATLAS_DATA_DIR (default: env or ./data)")
    ap.add_argument("--desk", type=int, required=True, help="desk id")
    ap.add_argument("--name", required=True, help="recording name (a-z, 0-9, '-' and '_', at most 64)")
    ap.add_argument("--run", default="", help="center on this run (default: the newest detection or report run)")
    ap.add_argument("--since", default="", help="page window start, original event time (as ?since=)")
    ap.add_argument("--until", default="", help="page window end, original event time (as ?until=)")
    ap.add_argument("--from", dest="start", default="", help="wall-clock start of the re-enactment (ISO or epoch)")
    ap.add_argument("--to", dest="end", default="", help="wall-clock end of the re-enactment (ISO or epoch)")
    ap.add_argument("--tz", default="UTC", help="zone for the decision times, e.g. Europe/London")
    ap.add_argument("--mask", dest="mask", action="store_true", default=None, help="mask public IPs on the page")
    ap.add_argument("--no-mask", dest="mask", action="store_false", help="do not mask (default: the desk's setting)")
    ap.add_argument("--credit", default="", choices=["", "secrepo"], help="show the dataset credit in the page footer")
    ap.add_argument("--timeline-s", type=float, default=2.0, help="timeline poll period (bigger = smaller bundle)")
    ap.add_argument("--max-duration", type=float, default=3600.0, help="refuse longer spans (seconds)")
    ap.add_argument("--out", default="", help="output file (default: <name>.json in the current folder)")
    ap.add_argument("--install", action="store_true", help="also store it as a recording of the source portal's desk")
    a = ap.parse_args(argv)
    if not _NAME.match(a.name):
        ap.error("--name must be 1 to 64 of a-z, 0-9, '-' or '_', starting with a letter or digit")
    if a.timeline_s < 0.5:
        ap.error("--timeline-s must be at least 0.5")
    res = build(a)
    bundle, text = res["bundle"], json.dumps(res["bundle"], ensure_ascii=False, separators=(",", ":"))
    outs = [Path(a.out or f"{a.name}.json")]
    if a.install:
        outs.append(res["src_dir"] / "cyber" / "recordings" / str(int(a.desk)) / f"{a.name}.json")
    for p in outs:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    kinds: dict[str, int] = {}
    for f in bundle["frames"]:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print(f"{', '.join(str(p) for p in outs)}: {len(bundle['frames'])} frames {kinds}, duration {bundle['duration']:.1f} s, "
          f"{len(text) / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
