"""Live multi-camera scenarios: several feeds, each with its own rule / question / job, running through the real desk.

    py -m atlas scenario corner-store --minutes 8 --port 8097

corner-store — a small shop watched by the Site desk:
    counter    laptop webcam           queue watch: 3+ waiting → SMS staff to open the second till
    street     Hikvision RTSP (real)   vehicles / loitering at the shutter → owner WhatsApp when it matters
    shelf      Vispera shelf photos    a feeder script rotates real shelf photos; scene change → gap check → reorder note
    back-door  simulated ESP32 PIR     a node script posts events to /hook/<token>/vision (delivery bell, after-hours motion)

"Scripts" that run concurrently: the portal (camera_watch jobs = the AI vision loop), the shelf feeder, the PIR node,
and a monitor that prints a live status line and writes the report. Everything the desk decides to send lands in the
approval queue — nothing goes out. Output: workspace/scenarios/<name>-<stamp>/report.md (+ data/ with DB and snapshots).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import config as cfg

ROOT = cfg.ROOT
OUT_ROOT = cfg.WORKSPACE_DIR / "scenarios"
VISPERA = Path(os.environ.get("VISPERA_DIR", r"C:\Users\pierr\vispera")) / "data" / "analyses"
HIKVISION = os.environ.get("SCENARIO_RTSP", "rtsp://admin:12345@178.214.74.98:554/Streaming/Channels/101")


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Desk:
    """Thin client for the portal API (DESK_OPEN=1 → no login)."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=120)

    def get(self, p: str, **params) -> Any:
        r = self.c.get("/api" + p, params=params or None)
        r.raise_for_status()
        return r.json()

    def post(self, p: str, body: dict | None = None, ok: tuple = (200,)) -> Any:
        r = self.c.post("/api" + p, json=body or {})
        if r.status_code not in ok:
            raise RuntimeError(f"POST {p} → {r.status_code} {r.text[:200]}")
        return r.json()

    def patch(self, p: str, body: dict) -> Any:
        r = self.c.patch("/api" + p, json=body)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    def hook(self, url: str, body: dict) -> Any:
        return self.c.post(url, json=body).json()

    def wait_idle(self, timeout: float = 240) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.get("/live")["runs"]:
                return True
            time.sleep(2)
        return False


# ---------------------------------------------------------------------------- feeds ("scripts")
class ShelfFeeder(threading.Thread):
    """Rotates real shelf photos into feeds/shelf.jpg — stands in for a fixed shelf camera whose scene changes
    (restock, sell-through, planogram drift)."""

    def __init__(self, target: Path, every_s: float, stop: threading.Event, images: list[Path]):
        super().__init__(daemon=True, name="shelf-feeder")
        self.target, self.every_s, self.stop, self.images = target, every_s, stop, images
        self.shown: list[tuple[float, str]] = []

    def run(self):
        i = 0
        while not self.stop.is_set():
            src = self.images[i % len(self.images)]
            tmp = self.target.with_suffix(".tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, self.target)                  # atomic swap: the grabber never sees a half-written file
            self.shown.append((time.time(), src.name))
            _log(f"[shelf-feeder] showing {src.name}")
            i += 1
            self.stop.wait(self.every_s)


class PirNode(threading.Thread):
    """Simulated ESP32 door node: posts what a PIR + bell would post, on a fixed schedule from t0."""

    def __init__(self, desk: Desk, hook_url: str, stop: threading.Event, schedule: list[tuple[float, dict]]):
        super().__init__(daemon=True, name="pir-node")
        self.desk, self.hook_url, self.stop, self.schedule = desk, hook_url, stop, schedule
        self.posted: list[dict] = []

    def run(self):
        t0 = time.time()
        for at, body in self.schedule:
            while time.time() - t0 < at and not self.stop.is_set():
                self.stop.wait(1)
            if self.stop.is_set():
                return
            try:
                r = self.desk.hook(self.hook_url, body)
                _log(f"[pir-node] posted {body.get('camera')}: {body.get('note','')[:60]} → event {r.get('event_id')} run {r.get('run_id') or '-'}")
                self.posted.append({"t": time.time(), "body": body, "resp": r})
            except Exception as exc:
                _log(f"[pir-node] post failed: {exc}")


# ---------------------------------------------------------------------------- scenario
def corner_store(a: argparse.Namespace) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # type: ignore[attr-defined]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / f"corner-store-{stamp}"
    data = out / "data"
    feeds = out / "feeds"
    for d in (data, feeds):
        d.mkdir(parents=True, exist_ok=True)

    shelf_images = sorted(VISPERA.glob("*_raw.png"))
    if not shelf_images:
        _log(f"no shelf photos under {VISPERA} — shelf feed disabled")
    shelf_images = shelf_images[::max(1, len(shelf_images) // 8)][:8]
    shelf_path = feeds / "shelf.jpg"
    if shelf_images:
        shutil.copyfile(shelf_images[0], shelf_path)

    # 1. portal with its own data dir (fresh desk, own DB, own snapshots), live models, built-in site_watch desk
    import socket
    with socket.socket() as sk:
        if sk.connect_ex(("127.0.0.1", a.port)) == 0:
            _log(f"port {a.port} is already in use — stop that server or pass --port; refusing to talk to a stale portal")
            return 2
    env = {**os.environ, "ATLAS_DATA_DIR": str(data), "DESK_OPEN": "1", "DESK_MODE": "live", "DESK_PROVIDER": "openrouter",
           "DESK_TEMPLATE": "site_watch", "PORT": str(a.port), "SPEND_CAP_USD": str(a.spend_cap), "PYTHONIOENCODING": "utf-8",
           "VISION_YOLO": os.environ.get("VISION_YOLO", str(cfg.DATA_DIR / "models" / "yolov8n.pt"))}
    env.pop("DESK_DEFAULT_ENGINE", None)                  # specialists on the built-in loop: no WSL dependency
    log_f = open(out / "portal.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "atlas.desk"], cwd=str(ROOT), env=env, stdout=log_f, stderr=subprocess.STDOUT)
    desk = Desk(f"http://127.0.0.1:{a.port}")
    for _ in range(60):
        try:
            desk.get("/health")
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        _log("portal did not start — see portal.log")
        return 2
    cfg_ = desk.get("/config")
    desk_id = cfg_.get("desk_id") or 1
    _log(f"portal up on :{a.port} — desk {desk_id} ({cfg_.get('template')}), mode {cfg_.get('mode')}")

    # 2. the business
    biz = {"name": "Khoury Corner Store", "tagline": "Convenience store, one till, long hours",
           "description": "Independent corner shop: groceries, drinks, tobacco, newspapers. One main till, a second till opened when busy. "
                          "Deliveries to the rear door in the mornings. Shutter down outside opening hours.",
           "extra_context": "Opening hours 08:00-21:00 daily. Staff on shift phone: +44 7700 900123 (SMS). Owner Pierre: WhatsApp +44 7700 900100. "
                            "Second till should open when 3 or more customers are waiting. Expected deliveries: bakery 07:30, drinks van Tue/Fri morning. "
                            "Anyone at the shutter or rear door outside opening hours is unusual. Shelf gaps on fast movers (cereal, tuna, nappies) mean a reorder today.",
           "sender_name": "Atlas, Corner Store desk", "tone": "Calm, factual, specific. Times, counts, camera names."}
    desk.patch(f"/desks/{desk_id}", {"business": biz, "tier": a.tier})

    # 3. cameras — each with its own rule / question / job ("one script does this, one does that")
    cams: list[dict[str, Any]] = [
        {"name": "counter", "config": {"source": str(a.webcam), "watch_for": "person", "min_count": "1", "cooldown_min": "2", "motion_min": "0.03",
                                       "question": "How many customers are waiting at the counter, and is a staff member serving? Give counts and where people are.",
                                       "task": "Queue watch. If the analyst reports 3 or more customers waiting, queue an SMS (queue_action kind=sms) to the staff phone asking someone to open the second till, with the time and count. Otherwise log only — no message.",
                                       "notes": "Camera above the main till, looking along the counter towards the door. During the test this is the developer's desk webcam standing in for the counter."}},
        {"name": "shelf", "config": {"source": str(shelf_path), "watch_for": "bottle", "min_count": "1", "cooldown_min": "0", "alert_on_motion": "0.15", "motion_min": "0.02",
                                     "question": "This is a supermarket shelf photo. Which shelf sections have visible gaps or look out of stock, and which products can you read on the labels? Be specific per shelf row.",
                                     "task": "Shelf check. Compare with earlier shelf events (camera_events camera=shelf). If the analyst reports gaps on fast movers, save a short reorder note as a deliverable (product, shelf, gap size) and queue an email (queue_action kind=email) to the owner summarising the gaps. If shelves look full, log only.",
                                     "notes": "Fixed camera on the grocery aisle. Scene changes when staff restock or when products sell through."}},
    ]
    if a.rtsp and not a.no_street:
        cams.append({"name": "street", "config": {"source": a.rtsp, "watch_for": "person, car, truck, bicycle", "min_count": "1", "cooldown_min": "3",
                                                  "question": "Describe vehicles and people near the shop front. Is anything parked or stopped right outside? Anyone standing at the shutter or door?",
                                                  "task": "Street watch. Log every event. If a delivery vehicle (van/truck) is stopped outside, remember it and add a line to the day's delivery log via remember. If someone is standing at the shutter while the shop is closed, queue a WhatsApp (queue_action kind=whatsapp) to the owner. Otherwise no message.",
                                                  "notes": "Outdoor camera covering the pavement and shutter in front of the shop."}})
    created = []
    existing = {c["name"]: c for c in desk.get("/connectors")["connectors"]}
    for c in cams:
        if c["name"] in existing:                       # re-run against a kept data dir: update, don't duplicate
            r = desk.patch(f"/connectors/{existing[c['name']]['id']}", {"config": c["config"]})
        else:
            r = desk.post("/connectors", {"kind": "camera", "name": c["name"], "config": c["config"], "auto": False})
        created.append(r)
        _log(f"camera {c['name']} → connector {r['id']}")
    R = desk.get("/cameras")
    hook_url = R["hook_url"]
    _log(f"detector={R['detector']['available']} ({R['detector']['weights']}) vlm={R['vlm']['ready']} ({R['vlm']['model']}) hook={hook_url}")

    # first look at every camera (baseline frame, no alert wanted yet: the rule fires on change from now on)
    for r in created:
        try:
            lk = desk.post(f"/cameras/{r['id']}/look", {})
            _log(f"[{r['name']}] baseline: {_seen(lk['counts'])} motion {lk['motion']:.2f} backend {lk['backend']}" + (f" → ALERT run {lk['run_id']}" if lk.get("run_id") else ""))
        except Exception as exc:
            _log(f"[{r['name']}] baseline failed: {exc}")

    # 4. the watchers (the real-time AI vision loop) — different cadence per camera
    cadence = {"counter": a.counter_every, "shelf": a.shelf_every, "street": a.street_every}
    for r in created:
        desk.post(f"/cameras/{r['id']}/watch", {"on": True, "every_s": cadence.get(r["name"], 30)})
    _log("watch jobs on: " + ", ".join(f"{r['name']} every {cadence.get(r['name'], 30)}s" for r in created))

    # 5. the other scripts
    stop = threading.Event()
    feeder = ShelfFeeder(shelf_path, a.shelf_rotate, stop, shelf_images) if shelf_images else None
    pir = PirNode(desk, hook_url, stop, [
        (70, {"camera": "back-door", "labels": {"person": 1}, "motion": 0.35,
              "note": "PIR: motion at the rear door. Bell not pressed."}),
        (200, {"camera": "back-door", "labels": {"person": 1, "truck": 1}, "motion": 0.6,
               "note": "Rear door bell pressed. Driver waiting with a trolley (drinks van)."}),
        (330, {"camera": "back-door", "labels": {"person": 2}, "motion": 0.5,
               "note": "SIMULATED AFTER-HOURS TEST: treat the time as 23:12, shop closed. PIR motion at the rear door, two people, shutter down."}),
    ])
    if feeder:
        feeder.start()
    pir.start()

    # 6. monitor
    t_end = time.time() + a.minutes * 60
    status_lines: list[str] = []
    last_n = -1
    while time.time() < t_end:
        try:
            ev = desk.get("/vision/events", hours=1, limit=200)
            live = desk.get("/live")["runs"]
            pend = desk.get("/actions", status="pending")
            st = desk.get("/stats")
            line = (f"events {len(ev)} (alerts {sum(1 for e in ev if e['triggered'])}) · runs active {len(live)} · approvals pending {len(pend)} "
                    f"· tokens {st.get('tokens_in',0)+st.get('tokens_out',0):,} · shelf {feeder.shown[-1][1] if feeder and feeder.shown else '-'}")
            if len(ev) != last_n:
                last_n = len(ev)
                for e in reversed(ev[:3]):
                    _log(f"  · {e['camera']}: {e['seen']} m{e['motion']:.2f} {'ALERT ' if e['triggered'] else ''}{e['reason']}" + (f" | {e['answer'][:90]}" if e.get('answer') else ""))
            _log(line)
            status_lines.append(time.strftime("%H:%M:%S ") + line)
        except Exception as exc:
            _log(f"monitor: {exc}")
        time.sleep(a.tick)

    stop.set()
    _log("time up — stopping watchers, waiting for runs to finish")
    for r in created:
        try:
            desk.post(f"/cameras/{r['id']}/watch", {"on": False})
        except Exception:
            pass
    desk.wait_idle(240)

    # 7. ask the cameras + digest
    questions = ["How busy was the counter during this session, and did we ever need the second till?",
                 "What happened at the back door? List every event with its time.",
                 "Did the shelf camera see any gaps or out-of-stock sections, and on which products?",
                 "Was anything parked or stopped outside the shop?"]
    qa = []
    for q in questions:
        try:
            r = desk.post("/vision/ask", {"question": q, "hours": 2})
            qa.append({"q": q, "a": r.get("answer") or r.get("error", ""), "n": r.get("events_considered", 0)})
            _log(f"Q: {q}\n   A: {(r.get('answer') or r.get('error',''))[:300]}")
        except Exception as exc:
            qa.append({"q": q, "a": f"error {exc}", "n": 0})
    digest_run = None
    try:
        dr = desk.post("/runs", {"task": "Write the site digest for this test session (all cameras, all events, what was sent for approval).", "mode": "daily_digest"})
        digest_run = dr.get("run_id")
        _log(f"digest run {digest_run}")
        desk.wait_idle(240)
    except Exception as exc:
        _log(f"digest failed: {exc}")

    # 8. collect + report
    events = desk.get("/vision/events", hours=3, limit=500)
    runs = desk.get("/runs")
    run_detail = {}
    for r in runs:
        try:
            run_detail[r["id"]] = desk.get(f"/runs/{r['id']}")
        except Exception:
            pass
    actions = desk.get("/actions")
    stats = desk.get("/stats")
    mem = desk.get("/memory")
    report = _report(out, biz, created, cadence, events, runs, run_detail, actions, stats, mem, qa, digest_run, status_lines,
                     feeder.shown if feeder else [], pir.posted, a)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "raw.json").write_text(json.dumps({"events": events, "runs": runs, "run_detail": run_detail, "actions": actions, "stats": stats,
                                              "memory": mem, "qa": qa}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    proc.terminate()
    try:
        proc.wait(10)
    except Exception:
        proc.kill()
    log_f.close()
    _log(f"report → {out / 'report.md'}")
    print("\n" + report[:3000])
    return 0


def _seen(c: dict[str, int]) -> str:
    from . import vision as V
    return V.counts_text(c)


def _tools_used(detail: dict[str, Any]) -> list[str]:
    out = []
    for e in detail.get("events", []):
        if e["kind"] in ("tool", "approval"):
            out.append((e["text"] or "").split("(")[0].split(" →")[0][:40])
    return out


def _report(out, biz, cams, cadence, events, runs, run_detail, actions, stats, mem, qa, digest_run, status_lines, shown, posted, a) -> str:
    hms = lambda ts: time.strftime("%H:%M:%S", time.localtime(ts))
    L = [f"# Corner Store scenario — {time.strftime('%d %b %Y %H:%M')}", "",
         f"**{biz['name']}** · {a.minutes} min live · desk tier {a.tier} · portal :{a.port}", "",
         "## Feeds and their scripts", "",
         "| Camera | Source | Watch rule | Cadence | Standing question | What the desk does |", "|---|---|---|---|---|---|"]
    for c in cams:
        cf = c["config"]
        rule = f"{cf.get('watch_for')} ≥{cf.get('min_count')}, cooldown {cf.get('cooldown_min')}m" + (f", scene-change ≥{cf['alert_on_motion']}" if cf.get("alert_on_motion") else "")
        L.append(f"| {c['name']} | {str(cf.get('source'))[:40]} | {rule} | {cadence.get(c['name'],30)}s | {cf.get('question','')[:80]} | {cf.get('task','')[:90]} |")
    L += ["| back-door | simulated ESP32 PIR → /hook/…/vision | every post wakes the desk | scripted | — | assess, log, notify if it matters |", ""]
    if shown:
        L += ["Shelf feeder showed: " + ", ".join(f"{n} ({hms(t)})" for t, n in shown), ""]
    alerts = [e for e in events if e["triggered"]]
    L += ["## Numbers", "",
          f"- Vision events logged: **{len(events)}**, of which **{len(alerts)}** woke the desk",
          f"- Runs: **{len(runs)}** ({sum(1 for r in runs if r['status']=='done')} done, {sum(1 for r in runs if r['status'] not in ('done','running'))} failed/other)",
          f"- Approval queue: **{sum(1 for x in actions if x['status']=='pending')}** pending · nothing sent",
          f"- Tokens: {stats.get('tokens_in',0):,} in / {stats.get('tokens_out',0):,} out (≈ £{((stats.get('tokens_in',0)*3+stats.get('tokens_out',0)*15)/1e6*0.78):.2f} at Sonnet rates)",
          f"- Desk memory entries written: {len(mem)}", ""]
    L += ["## Timeline (what the cameras saw)", "", "| Time | Camera | Seen | Motion | Alert | Reason | Analyst |", "|---|---|---|---|---|---|---|"]
    for e in sorted(events, key=lambda e: e["ts"]):
        L.append(f"| {hms(e['ts'])} | {e['camera']} | {e['seen']} | {e['motion']:.2f} | {'**YES**' if e['triggered'] else ''} | {(e.get('reason') or '')[:60]} | {(e.get('answer') or '').replace(chr(10),' ')[:220]} |")
    L += ["", "## Runs (what Atlas did with each alert)", ""]
    for r in sorted(runs, key=lambda r: r["created"]):
        d = run_detail.get(r["id"], {})
        tools = _tools_used(d)
        L += [f"### {hms(r['created'])} · {r['id']} · {r['status']} · {r.get('tokens_in',0):,}/{r.get('tokens_out',0):,} tokens" + (" · DIGEST" if r["id"] == digest_run else ""),
              "", f"**Task:** {(r['task'] or '').splitlines()[0][:160]}", "",
              f"**Tools:** {', '.join(tools) if tools else '(none)'}", "",
              "**Outcome:** " + (r.get("summary") or "").replace("\n", " ")[:900], ""]
    L += ["## Approval queue (drafted, NOT sent)", ""]
    if actions:
        for x in actions:
            L += [f"- **#{x['id']} {x['kind']} → {x['to']}** [{x['status']}] {x.get('subject') or ''}", f"  > {(x.get('body') or '').replace(chr(10),' ')[:400]}", ""]
    else:
        L += ["- nothing queued (every event judged routine)", ""]
    L += ["## Ask the cameras (RAG over the event log)", ""]
    for x in qa:
        L += [f"**Q: {x['q']}** _(events considered: {x['n']})_", "", x["a"].strip(), ""]
    if mem:
        L += ["## Desk memory after the session", ""] + [f"- {m['key']}: {m['value'][:200]}" for m in mem] + [""]
    L += ["## Monitor log", "", "```"] + status_lines[-40:] + ["```", "",
          f"Data: `{out/'data'}` (desk.db, snapshots/), portal log `{out/'portal.log'}`, raw `{out/'raw.json'}`."]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas scenario", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?", default="corner-store", choices=["corner-store"])
    p.add_argument("--minutes", type=float, default=8)
    p.add_argument("--port", type=int, default=8097)
    p.add_argument("--tier", default="balanced")
    p.add_argument("--spend-cap", type=float, default=25.0, help="SPEND_CAP_USD for the scenario portal (OpenRouter account usage, not per-run)")
    p.add_argument("--webcam", default="0", help="counter camera source (webcam index / URL / file)")
    p.add_argument("--rtsp", default=HIKVISION, help="street camera RTSP URL ('' to disable)")
    p.add_argument("--no-street", action="store_true")
    p.add_argument("--counter-every", type=int, default=30)
    p.add_argument("--shelf-every", type=int, default=40)
    p.add_argument("--street-every", type=int, default=45)
    p.add_argument("--shelf-rotate", type=float, default=95, help="seconds between shelf photo changes")
    p.add_argument("--tick", type=float, default=15)
    a = p.parse_args(argv)
    return corner_store(a)


if __name__ == "__main__":
    raise SystemExit(main())
