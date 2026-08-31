"""Long-running soak test: run a simulated company against a real Atlas Desk server for hours or days.

    py -m atlas soak --hours 168 --port 8095 --leads-per-day 40 --fault-rate 0.05 --restart-every-h 24

What it does
  * starts its own portal server (demo provider, open mode, own data dir) so the real desk is untouched
  * creates a desk for a fictional company and fires realistic web-form leads at a Poisson rate with
    morning / evening peaks, a few malformed and duplicate submissions, plus a follow-up automation
  * plays the owner: approves / rejects pending drafts after a delay
  * injects provider faults (429 / 500 / stalls) at --fault-rate and restarts the server every N hours
  * every --snapshot-min minutes records /api/health/all, /api/stats and /api/metrics to snapshots.jsonl
    and rewrites report.md - open it any time; it is the deliverable
Stop early by creating a file named STOP in the output folder.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, WORKSPACE_DIR

COMPANY = {
    "name": "Northgate Plumbing", "template": "sales_desk", "tier": "free",
    "tagline": "Plumbing and heating across Greater Manchester",
    "description": "Six-engineer plumbing and heating firm. Emergency call-outs, boiler servicing, bathroom installs, landlord certificates.",
    "services": "Emergency plumbing, Boiler service and repair, Bathroom installation, Landlord gas safety certificates",
    "target_clients": "Homeowners and landlords in Greater Manchester", "tone": "Direct, friendly, reassuring. Short messages. One clear next step.",
    "sender_name": "Dana Whitfield, Northgate Plumbing", "availability": "Mon-Sat 7am-7pm, emergencies 24/7",
    "pricing_notes": "Call-out from 85; boiler service 95; quotes for installs after a visit", "no_money_figures": True,
}
FIRST = ["Priya", "Tom", "Hannah", "Marcus", "Aisha", "Oliver", "Chloe", "Ravi", "Emma", "Jade", "Ben", "Nadia", "Sam", "Leah", "Kwame", "Sofia", "Daniel", "Grace", "Yusuf", "Mia"]
LAST = ["Raman", "Okafor", "Weiss", "Bell", "Khan", "Grant", "Martin", "Patel", "Lund", "Owens", "Carter", "Ali", "Reid", "Novak", "Mensah", "Costa", "Hughes", "Doyle", "Ibrahim", "Walsh"]
POSTCODES = ["M1", "M4", "M14", "M20", "M21", "M33", "SK4", "SK8", "WA15", "OL9", "BL1", "M41"]
PROBLEMS = [
    "boiler has stopped working, no hot water since this morning, two kids in the house",
    "leak under the kitchen sink, getting worse, have put a bucket down",
    "want a quote for a full bathroom refit, currently a 1990s suite",
    "landlord gas safety certificate needed for a 2-bed flat before new tenants move in on the 1st",
    "radiators upstairs not heating, downstairs fine, bled them already",
    "outside tap needed for the garden, when could someone come",
    "annual boiler service, Worcester combi, last done two years ago",
    "toilet keeps running after flushing, tried adjusting the float",
    "smell of gas near the hob, have opened windows - is this urgent?",
    "moving into a house next week, want the whole system checked before we move",
    "shower pressure very low all of a sudden, everything else fine",
    "water tank in the loft overflowing through the overflow pipe outside",
]
SOURCES = ["website form", "website form", "website form", "Google ads form", "referral", "Checkatrade"]


def hour_weight(h: int) -> float:
    if 9 <= h < 12 or 17 <= h < 20:
        return 2.2
    if 7 <= h < 9 or 12 <= h < 17 or 20 <= h < 22:
        return 1.0
    return 0.15


def make_lead(rng: random.Random, seen: list[dict[str, str]]) -> tuple[dict[str, Any], str]:
    """Returns (payload, kind) where kind in normal | malformed | duplicate."""
    roll = rng.random()
    if roll < 0.02:
        return {"notes": "hello?? my boiler", "source": "website form"}, "malformed"          # no name / email
    if roll < 0.05 and seen:
        d = dict(rng.choice(seen)); d["notes"] = "Following up on my earlier message - any update?"
        return d, "duplicate"
    first, last = rng.choice(FIRST), rng.choice(LAST)
    email = f"{first}.{last}{rng.randint(1, 99)}@example.com".lower()
    lead = {"name": f"{first} {last}", "email": email, "phone": f"+44 7700 9{rng.randint(10000, 99999)}",
            "company": "" if rng.random() < 0.8 else f"{last} Lettings", "source": rng.choice(SOURCES),
            "notes": f"{rng.choice(PROBLEMS)}. Postcode {rng.choice(POSTCODES)}."}
    seen.append(lead)
    if len(seen) > 200:
        del seen[:100]
    return lead, "normal"


class Server:
    def __init__(self, port: int, data_dir: Path, fault_rate: float, log: Path):
        self.port, self.data_dir, self.fault_rate, self.log = port, data_dir, fault_rate, log
        self.proc: subprocess.Popen | None = None
        self.restarts = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = {**os.environ, "DESK_MODE": "demo", "DESK_OPEN": "1", "PORT": str(self.port), "ATLAS_DATA_DIR": str(self.data_dir),
               "DEMO_DELAY": "0.3", "DEMO_FAULT_RATE": str(self.fault_rate), "DEMO_FAULT_SLEEP": "20", "RUN_MAX_S": "240",
               "DESK_SECRET": "soak", "PYTHONIOENCODING": "utf-8"}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        out = open(self.log, "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "atlas.desk"], cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)
        for _ in range(60):
            time.sleep(1)
            try:
                if httpx.get(self.url + "/api/health", timeout=3).status_code == 200:
                    return
            except Exception:
                pass
        raise RuntimeError("soak server did not come up; see " + str(self.log))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)

    def restart(self) -> None:
        self.stop()
        self.restarts += 1
        self.start()

    def rss_mb(self) -> float | None:
        try:
            import psutil  # type: ignore
            return round(psutil.Process(self.proc.pid).memory_info().rss / 1e6, 1) if self.proc else None
        except Exception:
            return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas soak", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--leads-per-day", type=float, default=40)
    ap.add_argument("--fault-rate", type=float, default=0.05, help="fraction of model calls that fail or stall")
    ap.add_argument("--restart-every-h", type=float, default=24, help="0 = never restart the server")
    ap.add_argument("--snapshot-min", type=float, default=10)
    ap.add_argument("--owner-every-min", type=float, default=5, help="how often the simulated owner clears approvals")
    ap.add_argument("--out", default="")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) if a.out else WORKSPACE_DIR / "soak" / stamp
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    srv = Server(a.port, out / "data", a.fault_rate, out / "server.log")
    srv.start()
    c = httpx.Client(base_url=srv.url, timeout=30)
    desk = c.post("/api/desks", json=COMPANY).json()
    hook = c.get("/api/connectors").json()["hook_url"]
    c.post("/api/jobs", json={"kind": "followups", "name": "Follow-up chaser", "every_min": 60, "days": 0})
    c.post("/api/jobs", json={"kind": "inbox_watch", "name": "Inbox watcher (no IMAP - should back off)", "every_min": 2})

    t0 = time.time()
    deadline = t0 + a.hours * 3600
    stats: dict[str, Any] = collections.Counter()
    alerts_seen: collections.Counter = collections.Counter()
    seen_leads: list[dict[str, str]] = []
    next_owner = t0 + a.owner_every_min * 60
    next_snap = t0 + 60                       # first snapshot after a minute, then every --snapshot-min
    next_restart = t0 + a.restart_every_h * 3600 if a.restart_every_h > 0 else float("inf")
    next_lead = t0 + 5
    snaps = out / "snapshots.jsonl"
    max_zombies = 0
    detections: list[float] = []              # seconds between a failure being recorded and an alert showing

    def snapshot() -> dict[str, Any]:
        h = c.get("/api/health/all", params={"hours": 24}).json()
        s = c.get("/api/stats").json()
        m = c.get("/api/metrics").json()
        row = {"ts": time.time(), "elapsed_h": round((time.time() - t0) / 3600, 2), "health": h, "stats": s,
               "metrics": {k: m.get(k) for k in ("runs", "durations", "throughput", "tokens", "errors") if k in m},
               "leads_sent": dict(stats), "rss_mb": srv.rss_mb(), "restarts": srv.restarts}
        with snaps.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def report(row: dict[str, Any], final: bool = False) -> None:
        h = row["health"]; r = h["runs"]
        lines = [f"# Soak report - {COMPANY['name']} ({'FINAL' if final else 'in progress'})", "",
                 f"Started {time.strftime('%Y-%m-%d %H:%M', time.localtime(t0))} · elapsed {row['elapsed_h']} h of {a.hours} h · "
                 f"leads/day {a.leads_per_day} · fault rate {a.fault_rate} · server restarts {srv.restarts} · RSS {row['rss_mb']} MB", "",
                 "## Traffic", f"- leads posted: normal {stats['normal']}, duplicate {stats['duplicate']}, malformed {stats['malformed']} "
                 f"(rejected {stats['malformed_rejected']}, accepted {stats['malformed_accepted']})",
                 f"- hook errors (non-2xx on valid leads): {stats['hook_error']}",
                 f"- owner decisions: approved {stats['approved']}, rejected {stats['rejected']}",
                 "", "## Runs (last 24h window at snapshot)",
                 f"- total {r['total']} · by status {r['by_status']} · failure rate {round(r['failure_rate'] * 100, 1)}%",
                 f"- p50 {r['p50_s']}s · p90 {r['p90_s']}s · live now {r['active']} · stalled {r['stalled']} · zombies {r['zombies']} (max seen {max_zombies})",
                 f"- tokens {h['tokens']:,} (≈ £{h['cost_gbp']} on Sonnet)",
                 "", "## Queue & automations",
                 f"- pending approvals {h['queue']['pending']} · oldest {h['queue']['oldest_pending_h']} h",
                 ] + [f"- job '{j['name']}': {j['last_status'] or 'never'} - {j['last_result']}" for j in h["jobs"]] + [
                 "", "## Alerts seen (count of snapshots in which each alert was raised)"] + \
                ([f"- {k}: {v}" for k, v in alerts_seen.most_common()] or ["- none"]) + [
                 "", f"## Detection", f"- failures detected by the health check: {len(detections)}; mean time-to-detect "
                 f"{round(sum(detections) / len(detections)) if detections else 0}s (bounded by the {a.snapshot_min}-min snapshot interval)",
                 "", f"Last error: {h.get('last_error')}", "", f"Snapshots: {snaps}"]
        (out / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"soak: desk {desk.get('id')} at {srv.url}  out={out}")
    try:
        while time.time() < deadline and not (out / "STOP").exists():
            now = time.time()
            if now >= next_lead:
                lead, kind = make_lead(rng, seen_leads)
                try:
                    resp = httpx.post(hook, json=lead, timeout=30)
                    stats[kind] += 1
                    if kind == "malformed":
                        stats["malformed_rejected" if resp.status_code == 400 else "malformed_accepted"] += 1
                    elif resp.status_code >= 300:
                        stats["hook_error"] += 1
                except Exception:
                    stats["hook_error"] += 1
                rate_per_s = a.leads_per_day / 86400 * hour_weight(time.localtime().tm_hour) * (24 / sum(hour_weight(x) for x in range(24)))
                next_lead = now + min(rng.expovariate(max(rate_per_s, 1e-6)), 1800)
            if now >= next_owner:
                try:
                    for act in c.get("/api/actions", params={"status": "pending"}).json():
                        if now - (act.get("created") or now) < 120:
                            continue
                        roll = rng.random()
                        if roll < 0.7:
                            c.post(f"/api/actions/{act['id']}/decide", json={"status": "approved", "note": "ok"}); stats["approved"] += 1
                        elif roll < 0.9:
                            c.post(f"/api/actions/{act['id']}/decide", json={"status": "rejected", "note": "not now"}); stats["rejected"] += 1
                except Exception as exc:
                    stats["owner_error"] += 1
                    print("owner step failed:", exc)
                next_owner = now + a.owner_every_min * 60
            if now >= next_restart:
                print("soak: scheduled server restart")
                srv.restart(); c = httpx.Client(base_url=srv.url, timeout=30)
                next_restart = now + a.restart_every_h * 3600
            if now >= next_snap:
                try:
                    row = snapshot()
                    h = row["health"]
                    max_zombies = max(max_zombies, h["runs"]["zombies"])
                    for al in h["alerts"]:
                        alerts_seen[al["key"]] += 1
                    if h.get("last_error") and h["alerts"]:
                        detections.append(max(0.0, row["ts"] - h["last_error"]["ts"]))
                    report(row)
                    print(f"[{row['elapsed_h']}h] runs24h={h['runs']['total']} fail={h['runs']['failure_rate']} zombies={h['runs']['zombies']} "
                          f"pending={h['queue']['pending']} alerts={[x['key'] for x in h['alerts']]} rss={row['rss_mb']}")
                except Exception as exc:
                    print("snapshot failed:", exc)
                next_snap = now + a.snapshot_min * 60
            time.sleep(1)
        row = snapshot(); report(row, final=True)
        print("soak finished ->", out / "report.md")
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
