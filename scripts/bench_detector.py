"""Detector benchmarks on WILDTRACK (python scripts/get_wildtrack.py --hd C1 C2 C3 C6): the live path per runtime, and the
record detector (PreciseDetector) before / after a change.

    python scripts/bench_detector.py live-speed [--cams 4,7] [--rounds 3] [--dur 6]
    python scripts/bench_detector.py live-accuracy
    python scripts/bench_detector.py precise [--before <git rev>]     # the tile fix: --before 9452f34

live-speed     one thread and one model per camera, each calling model.track(frame) exactly as atlas/live.py does, on that
               camera's 960x540 frames; torch and openvino runs interleaved (order reversed every round) so background
               load hits both alike. Reports median over rounds of process CPU-ms per frame (all threads) and aggregate fps.
live-accuracy  person P/R/F1 at IoU 0.5 after mcam_eval.roi_filter on the held-out instants 200-398 step 2, all 7 cameras:
               predict (the detector alone) and track (what the live picture shows), per runtime.
precise        PreciseDetector on the full-res frames of C1 C2 C3 C6, instants 200-390 step 10, scored the same way; with
               --before, the PreciseDetector of that revision too (loaded from git), on the same frames.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics as st
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas import live as LIVE          # noqa: E402
from atlas import mcam_eval as M        # noqa: E402
from atlas import vision as V           # noqa: E402

DATA = Path("~/AtlasDemo/wildtrack").expanduser()
RUNTIMES = ("torch", "openvino")


def _frames(cam: int, instants) -> list:
    import cv2
    return [cv2.imread(str(DATA / "frames" / M.CAMS[cam] / f"{5 * i:08d}.jpg")) for i in instants]


def _models(runtime: str, n: int) -> list:
    d = V.Detector(V.YOLO_WEIGHTS, runtime=runtime)
    out = [d.new_model() for _ in range(n)]
    if d.runtime != runtime:
        raise SystemExit(f"asked for {runtime}, got {d.runtime}: {d._ov_error}")
    return out


def _track(model, frame):
    return model.track(frame, conf=LIVE.DETECT_CONF, persist=True, verbose=False, tracker="bytetrack.yaml")[0]


def live_speed(a) -> dict:
    cams = [int(x) for x in a.cams.split(",")]
    feeds = {c: _frames(c, range(200, 400, 2)) for c in range(max(cams))}
    pool = {rt: _models(rt, max(cams)) for rt in RUNTIMES}
    for rt in RUNTIMES:                                    # warm-up: first calls compile / allocate
        for c, m in enumerate(pool[rt]):
            for f in feeds[c][:3]:
                _track(m, f)

    def run(rt: str, n: int) -> dict:
        counts = [0] * n
        stop = [0.0]
        bar = threading.Barrier(n + 1)

        def work(j: int) -> None:
            m, fr = pool[rt][j], feeds[j]
            bar.wait()
            k = 0
            while time.perf_counter() < stop[0]:
                _track(m, fr[k % len(fr)])
                k += 1
            counts[j] = k

        th = [threading.Thread(target=work, args=(j,)) for j in range(n)]
        stop[0] = time.perf_counter() + 1e9
        for t in th:
            t.start()
        stop[0] = time.perf_counter() + a.dur
        bar.wait()
        c0, w0 = time.process_time(), time.perf_counter()
        for t in th:
            t.join()
        wall, cpu, frames = time.perf_counter() - w0, time.process_time() - c0, sum(counts)
        return {"cpu_ms_per_frame": 1000 * cpu / max(1, frames), "fps": frames / wall, "frames": frames}

    runs: dict[str, list] = {}
    for r in range(a.rounds):
        order = [(rt, n) for n in cams for rt in RUNTIMES]
        for rt, n in (order[::-1] if r % 2 else order):
            m = run(rt, n)
            runs.setdefault(f"{rt} x{n}", []).append(m)
            print(f"round {r} {rt:>8} x{n}: {m['cpu_ms_per_frame']:6.1f} CPU-ms/frame  {m['fps']:6.1f} fps", flush=True)
    return {k: {"cpu_ms_per_frame": round(st.median(x["cpu_ms_per_frame"] for x in v), 1),
                "fps": round(st.median(x["fps"] for x in v), 1), "rounds": len(v)} for k, v in runs.items()}


def _persons(res) -> list[dict]:
    names = res.names
    return [{"box": [float(v) for v in b.xyxy[0].tolist()], "conf": float(b.conf)} for b in res.boxes if names[int(b.cls)] == "person"]


def _score(D, idx, preds) -> dict:
    Ds = dict(D, frames=[D["frames"][i] for i in idx], gt=[D["gt"][i] for i in idx], scene=[D["scene"][i] for i in idx],
              paths=[D["paths"][i] for i in idx])
    s = M.score_detection(Ds, M.roi_filter(Ds, preds), 0.5)
    return {k: s[k] for k in ("precision", "recall", "f1", "count_mae")}


def live_accuracy(a) -> dict:
    D = M.load(DATA)
    idx = list(range(200, 400, 2))
    out = {}
    for rt in RUNTIMES:
        models = _models(rt, 2 * len(M.CAMS))
        pred = [[[] for _ in M.CAMS] for _ in idx]
        trk = [[[] for _ in M.CAMS] for _ in idx]
        for c in range(len(M.CAMS)):
            for k, f in enumerate(_frames(c, idx)):
                pred[k][c] = _persons(models[c].predict(f, conf=LIVE.DETECT_CONF, verbose=False)[0])
                trk[k][c] = _persons(_track(models[len(M.CAMS) + c], f))
        out[rt] = {"predict": _score(D, idx, pred), "track": _score(D, idx, trk)}
        print(rt, out[rt], flush=True)
    return out


def _precise_class(rev: str):
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:atlas/vision.py"], capture_output=True, text=True, check=True).stdout
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vision_before.py"
        path.write_text(src)
        spec = importlib.util.spec_from_file_location("atlas.vision_before", path)   # inside the package: relative imports work
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod.PreciseDetector


def precise(a) -> dict:
    import cv2
    D = M.load(DATA)
    sub = [0, 1, 2, 5]
    idx = list(range(200, 400, 10))
    gt = [[(D["gt"][i][c] if c in sub else []) for c in range(len(M.CAMS))] for i in idx]
    Ds = dict(D, frames=[D["frames"][i] for i in idx], gt=gt, scene=[D["scene"][i] for i in idx], paths=[D["paths"][i] for i in idx])
    dets = {"after (this tree)": V.PreciseDetector()}
    if a.before:
        dets[f"before ({a.before})"] = _precise_class(a.before)()
    out = {}
    for name, det in dets.items():
        preds = [[[] for _ in M.CAMS] for _ in idx]
        t0, cpu0 = time.perf_counter(), time.process_time()
        for k, i in enumerate(idx):
            for c in sub:
                im = cv2.imread(str(DATA / "frames-hd" / M.CAMS[c] / f"{5 * i:08d}.jpg"))
                s = 960.0 / im.shape[1]                       # labels live in 960x540
                preds[k][c] = [{"box": [v * s for v in d["box"]], "conf": d["conf"]} for d in det.detect_bgr(im) if d["label"] == "person"]
        n = len(idx) * len(sub)
        sc = M.score_detection(Ds, M.roi_filter(Ds, preds), 0.5)
        out[name] = {"conf": det.conf, "tiles": list(det.tiles), **{k: sc[k] for k in ("precision", "recall", "f1", "count_mae")},
                     "s_per_frame": round((time.perf_counter() - t0) / n, 2), "cpu_s_per_frame": round((time.process_time() - cpu0) / n, 2),
                     "frames": n}
        print(name, out[name], flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["live-speed", "live-accuracy", "precise"])
    ap.add_argument("--cams", default="4,7", help="live-speed: camera counts to run")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--dur", type=float, default=6.0, help="live-speed: seconds per measurement")
    ap.add_argument("--before", default="", help="precise: also run the PreciseDetector of this git revision")
    ap.add_argument("--json", default="", help="write the result here")
    a = ap.parse_args()
    if not (DATA / "annotations_positions").is_dir():
        raise SystemExit(f"WILDTRACK not found at {DATA} (python scripts/get_wildtrack.py --hd C1 C2 C3 C6)")
    res = {"live-speed": live_speed, "live-accuracy": live_accuracy, "precise": precise}[a.what](a)
    print(json.dumps(res, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
