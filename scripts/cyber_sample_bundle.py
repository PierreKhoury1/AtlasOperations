"""Build a small REPLAY bundle for the cyber page (atlas/desk/static/cyber.html) from REAL parser output, no browser.

The real portal runs in-process (Flask test client) on a throw-away data folder with DESK_MODE=demo and
CYBER_OFFLINE=1: it signs up a scratch account, creates a soc_desk desk, feeds the SecRepo excerpts in
tests/fixtures/cyber through the real upload route in original time order (one slice per step), and after every step
reads each route the page polls. A response that changed becomes a frame, exactly as the page's record mode stores it.

What is real: every frame's data is the route's actual JSON over the parsed public log lines (Security Repo by Mike
Sconzo, CC BY 4.0; MACCDC 2012 via SecRepo). What is not: frame times `t` follow a fixed schedule (one step every
--step seconds) instead of a wall clock, and uploads use trigger=0, so no agent run starts: the incident card stays
empty and nothing is proposed for containment. Use it to check layout and REPLAY captures, never as film footage of a
run; film footage comes from ?record=<name> on a live desk.

    py scripts/cyber_sample_bundle.py maccdc  [--out samples/cyber/maccdc-sample.json] [--steps 10] [--step 2.5]
    py scripts/cyber_sample_bundle.py authlog [--out samples/cyber/authlog-sample.json] [--mask]

Open one without a portal: load cyber.html?replay=__test from disk and call window.__loadBundle(<the JSON>).
With a portal: POST it to /api/cyber/recordings as {name, bundle}, then open /desk/cyber?replay=<name>.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "cyber"

SCENARIOS = {
    # Scenario A shape: Zeek scans and password guessing, then Snort Meterpreter alerts (Snort-local = UTC-5, inferred).
    "maccdc": {"title": "SecRepo MACCDC 2012 excerpts", "files": [
        {"file": "zeek_notice_sample.log", "fmt": "zeek", "sensor": "zeek", "zeek_path": "notice"},
        {"file": "zeek_ssh_sample.log", "fmt": "zeek", "sensor": "zeek", "zeek_path": "ssh"},
        {"file": "snort_fast_sample.log", "fmt": "snort_fast", "sensor": "snort", "year": 2012, "tz": "-05:00"}]},
    # Scenario B shape: one 409-username list from four sources (no year in the log: times show without one).
    "authlog": {"title": "SecRepo auth.log excerpt", "files": [
        {"file": "auth_campaign.log.gz", "fmt": "sshd", "sensor": ""}]},
}
KINDS = ("timeline", "graph", "detections", "incident", "containment")


def _lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace").splitlines()


def build(scenario: str, steps: int, step_s: float, mask: bool) -> dict:
    from atlas import cyber as CY                          # imported after the environment points at a scratch folder
    import atlas.desk.app as A

    spec = SCENARIOS[scenario]
    A.app.config["TESTING"] = True
    c = A.app.test_client()
    r = c.post("/signup", json={"name": "Sample", "email": f"sample-{int(time.time())}@example.invalid", "password": "sample-bundle-1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    desk = c.post("/api/desks", json={"name": "Security operations", "template": "soc_desk"}).get_json()
    if mask:
        c.patch("/api/cyber/config", json={"mask_public_ips": True})

    # Parse each file once (B1's parser) to order its lines by original event time; only parsed lines are replayed.
    pieces = []
    for i, f in enumerate(spec["files"]):
        opts = {k: f[k] for k in ("sensor", "year", "tz", "zeek_path") if f.get(k)}
        events, stats = CY.parse(f["fmt"], _lines(FIX / f["file"]), **opts)
        pieces += [(ev["ts"], i, ev["raw"]) for ev in events]
        print(f"{f['file']}: {stats['lines']} lines, {stats['events']} events", file=sys.stderr)
    pieces.sort(key=lambda p: (p[0], p[1]))
    since, until = pieces[0][0], pieces[-1][0]
    q = {"since": CY.fmt_ts(since).replace(" ", "T"), "until": CY.fmt_ts(until + 1).replace(" ", "T")}
    windowed = "?since=%s&until=%s" % (q["since"], q["until"])

    frames, last = [], {}

    def poll(t: float, kind: str) -> None:
        url = f"/api/cyber/{kind}" + (windowed if kind in ("timeline", "graph", "detections") else "")
        resp = c.get(url)
        assert resp.status_code == 200, (url, resp.status_code, resp.get_data(as_text=True)[:200])
        data = resp.get_json()
        if kind == "config" and isinstance(data.get("hook_url"), str):     # same rule as the page's recorder: no token
            data["hook_url"] = re.sub(r"/hook/[^/?#]+", "/hook/<token>", data["hook_url"])
        s = json.dumps(data, sort_keys=True)
        if last.get(kind) != s:
            last[kind] = s
            frames.append({"t": round(t, 3), "kind": kind, "data": data})

    poll(0.0, "config")
    for kind in KINDS:
        poll(0.05, kind)
    n = len(pieces)
    for k in range(steps):
        part = pieces[k * n // steps:(k + 1) * n // steps]
        for i, f in enumerate(spec["files"]):
            lines = [p[2] for p in part if p[1] == i]
            if not lines:
                continue
            form = {"fmt": f["fmt"], "trigger": "0", "file": (io.BytesIO(("\n".join(lines) + "\n").encode("utf-8")), f["file"].replace(".gz", ""))}
            for key in ("sensor", "year", "tz", "zeek_path"):
                if f.get(key):
                    form[key] = str(f[key])
            up = c.post("/api/cyber/upload", data=form, content_type="multipart/form-data")
            assert up.status_code == 200, up.get_data(as_text=True)[:300]
        t0 = (k + 1) * step_s
        for j, kind in enumerate(KINDS):
            poll(t0 + 0.05 * (j + 1), kind)
    duration = round((steps + 1) * step_s, 3)
    return {"kind": "atlas-cyber-recording", "version": 1, "name": f"{scenario}-sample", "created": time.time(),
            "desk": {"id": desk["id"], "name": "Security operations"}, "tz": "UTC", "viewport": [1920, 1080],
            "mask_public_ips": mask, "query": dict(q, credit="secrepo"), "duration": duration, "frames": frames,
            "generator": {"script": "scripts/cyber_sample_bundle.py", "scenario": scenario, "source": spec["title"],
                          "note": ("Route responses captured in-process over SecRepo excerpts (CC BY 4.0). Frame times "
                                   "follow a fixed schedule; uploads ran with trigger=0, so no agent run and no "
                                   "containment proposal.")}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--out", default="")
    ap.add_argument("--steps", type=int, default=10)
    ap.add_argument("--step", type=float, default=2.5, help="seconds between steps in the bundle")
    ap.add_argument("--mask", action="store_true", help="store mask_public_ips (the page masks public IPs)")
    a = ap.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="atlas-cyber-sample-")
    os.environ.update({"ATLAS_DATA_DIR": scratch, "DESK_MODE": "demo", "CYBER_OFFLINE": "1", "DEMO_DELAY": "0",
                       "ATLAS_INTEL_DIR": os.path.join(scratch, "intel")})
    os.environ.pop("DESK_OPEN", None)
    os.environ.setdefault("DESK_SECRET", "sample-bundle-only")
    sys.path.insert(0, str(ROOT))
    bundle = build(a.scenario, max(1, a.steps), a.step, a.mask)
    out = Path(a.out or ROOT / "samples" / "cyber" / f"{a.scenario}-sample.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    kinds = {}
    for f in bundle["frames"]:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print(f"{out}: {len(bundle['frames'])} frames {kinds}, {out.stat().st_size / 1024:.0f} KB, scratch data in {scratch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
