"""Accuracy audit: score the vision stack against HUMAN ground truth, not against itself.

Ground truth = the MEVA annotations for the campus sample clips (Kitware, CC BY 4.0): per-frame boxes for every person
and vehicle while they take part in an annotated activity. That makes it exact for RECALL ("of the people who were
really there, how many did we see") and blind for precision (people doing nothing annotated have no box), so precision
is reported two other ways: the share of catalogue objects that overlap any ground-truth actor (a floor, not the
value), and the agreement rate of the independent second opinion.

    py -m atlas audit                      # all cameras that have ground truth
    py -m atlas audit --camera lobby --seconds 120

What is measured
  detector     live (nano, 640) vs precise (tiles + full frame): recall per class, recall on small objects, ms/frame
  catalogue    the real ObjectWorker run over the clip at the analysis rate, with its real thresholds:
                 actor recall      ground-truth actors (>= 2 s in view) that got a catalogue object
                 label accuracy    matched objects whose voted label is the actor's type
                 fragmentation     catalogue objects per matched actor (1.0 = one identity per thing)
                 id swaps          times an object jumped to another actor while its own was still in view (0 = none)
                 gt-overlap floor  catalogue people/vehicles that overlap some ground-truth actor
  second look  how often the local second opinion agrees with a label that ground truth says is right / wrong

The report is written to data/audit/latest.json; the Objects page shows it next to the catalogue.
"""
from __future__ import annotations

import ast
import collections
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config as cfg

AUDIT_DIR = cfg.DATA_DIR / "audit"
GT_CAMERAS = {"campus-lobby": "G421", "campus-entrance": "G638", "campus-drive": "G336"}
VEHICLES = {"car", "truck", "bus"}


def gt_dir() -> Path:
    from . import designer as D
    return Path(os.environ.get("ATLAS_GT_DIR") or (D.sample_dir() / "raw" / "meva" / "gt"))


def kind(label: str) -> str:
    return "person" if label == "person" else ("vehicle" if label in VEHICLES else "")


def load_gt(code: str) -> tuple[dict[int, str], dict[int, list[tuple[int, str, list[int]]]]]:
    """({actor id: type}, {frame index: [(actor id, type, [x1, y1, x2, y2])]}) for one camera."""
    d = gt_dir()
    types: dict[int, str] = {}
    for line in (d / f"{code}.types.yml").read_text().splitlines():
        if "'types'" in line:
            t = ast.literal_eval(line.strip()[2:])["types"]
            types[t["id1"]] = max(t["cset3"], key=t["cset3"].get)
    frames: dict[int, list[tuple[int, str, list[int]]]] = collections.defaultdict(list)
    with (d / f"{code}.geom.yml").open() as f:
        for line in f:
            if "'geom'" in line:
                g = ast.literal_eval(line.strip()[2:])["geom"]
                ty = types.get(g["id1"], "")
                if ty in ("person", "vehicle"):
                    frames[g["ts0"]].append((g["id1"], ty, [int(v) for v in g["g0"].split()]))
    return types, frames


def available() -> list[str]:
    from . import designer as D
    clips = D.sample_clips()
    return [c for c, code in GT_CAMERAS.items() if c in clips and (gt_dir() / f"{code}.geom.yml").is_file()]


def _iou(a, b) -> float:
    from .objects import iou
    return iou(a, b)


# ---------------------------------------------------------------------------- detector recall
def detector_recall(clip: str, frames: dict, detect, every: int = 45, limit: int = 70, thr: float = 0.4) -> dict[str, Any]:
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(clip)
    hit: collections.Counter = collections.Counter()
    tot: collections.Counter = collections.Counter()
    ms: list[float] = []
    for idx in sorted(frames)[::every][:limit]:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        t0 = time.time()
        dets = [(kind(d["label"]), d["box"]) for d in detect(frame)]
        ms.append((time.time() - t0) * 1000)
        for _aid, ty, box in frames[idx]:
            small = (box[3] - box[1]) < 120
            found = any(k == ty and _iou(box, b) >= thr for k, b in dets)
            for key in (ty, "small" if small else ""):
                if key:
                    tot[key] += 1
                    hit[key] += found
    cap.release()
    return {"recall": {k: round(hit[k] / tot[k], 3) for k in tot}, "boxes": dict(tot),
            "ms_per_frame": round(float(np.median(ms[2:] or ms or [0])), 1), "frames": len(ms)}


# ---------------------------------------------------------------------------- the catalogue, end to end
def catalogue_run(clip: str, frames: dict, seconds: float = 0, fps: float = 0, detector=None) -> dict[str, Any]:
    """Drive the real ObjectWorker over the clip on the clip's own clock and compare its catalogue with the actors."""
    import cv2
    from . import objects as O
    from . import vision as V
    from .store import Store
    tmp = Path(tempfile.mkdtemp(prefix="atlas-audit-"))
    old_dir, O.OBJ_DIR = O.OBJ_DIR, tmp / "objects"
    try:
        store = Store(str(tmp / "audit.db"))
        w = O.ObjectWorker(store, 1, "audit", clip, live=False, fps=fps or O.OBJ_FPS)
        det = detector or V.PreciseDetector()
        cap = cv2.VideoCapture(clip)
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if seconds:
            n = min(n, int(seconds * src_fps))
        stride = max(1, int(round(src_fps / w.fps)))
        t_base = 1_700_000_000.0
        per_frame: dict[int, list[tuple[int, str, list[float]]]] = {}       # frame -> [(object id, label, box)]
        idx = 0
        t0 = time.time()
        while idx < n:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % stride == 0:
                seen = w.step(frame, t_base + idx / src_fps, idx / src_fps, det)
                per_frame[idx] = [(t.obj_id, t.label, list(t.box)) for t in seen if t.obj_id is not None]
            idx += 1
        cap.release()
        w._end(w.tracker.reset())
        wall = time.time() - t0
        objs = {o["id"]: o for o in store.vision_objects(1, limit=100000)}
    finally:
        O.OBJ_DIR = old_dir

    # ground-truth actors that were in view long enough to be catalogued at all (>= 2 s inside the audited span)
    actor_frames: dict[int, list[int]] = collections.defaultdict(list)
    actor_type: dict[int, str] = {}
    for fi, rows in frames.items():
        if fi < idx:
            for aid, ty, _b in rows:
                actor_frames[aid].append(fi)
                actor_type[aid] = ty
    actors = {a for a, fr in actor_frames.items() if len(fr) >= 2 * src_fps}

    match: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)    # actor -> {object: frames}
    obj_actor: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)  # object -> {actor: frames}
    obj_seq: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)                  # object -> [(frame, actor)]
    for fi, rows in sorted(per_frame.items()):
        for aid, ty, gbox in frames.get(fi, []):
            best, best_iou = None, 0.4
            for oid, label, box in rows:
                v = _iou(gbox, box)
                if v >= best_iou:
                    best, best_iou = oid, v
            if best is not None:
                match[aid][best] += 1
                obj_actor[best][aid] += 1
                obj_seq[best].append((fi, aid))
    # identity swaps. Ground truth re-numbers a person for each activity, so "one object, several actor ids" is normal;
    # a SWAP is an object jumping to another actor while the actor it was on is still in view.
    swaps = 0
    for oid, seq in obj_seq.items():
        cur = None
        for fi, aid in seq:
            if cur is not None and aid != cur and any(a == cur for a, _t, _b in frames.get(fi, [])):
                swaps += 1
            cur = aid

    by_type: dict[str, dict[str, Any]] = {}
    for ty in ("person", "vehicle"):
        acts = [a for a in actors if actor_type[a] == ty]
        found = [a for a in acts if match.get(a)]
        right = [a for a in found if kind(objs[match[a].most_common(1)[0][0]]["label"]) == ty]
        frag = [len(match[a]) for a in found]
        by_type[ty] = {"actors": len(acts), "found": len(found), "recall": round(len(found) / len(acts), 3) if acts else None,
                       "label_accuracy": round(len(right) / len(found), 3) if found else None,
                       "fragmentation": round(sum(frag) / len(frag), 2) if frag else None}
    pv = [o for o in objs.values() if kind(o["label"])]
    overlap = [o for o in pv if o["id"] in obj_actor]
    # second opinion vs ground truth: objects matched to an actor have a known-true type
    so = collections.Counter()
    for oid, c in obj_actor.items():
        o = objs[oid]
        if not o.get("second_label"):
            continue
        truth_ok = kind(o["label"]) == actor_type[c.most_common(1)[0][0]]
        so[("label right" if truth_ok else "label wrong", o.get("verdict") or "no verdict")] += 1
    labels = collections.Counter(o["label"] for o in objs.values())
    verdicts = collections.Counter(o.get("verdict") or "unverified" for o in objs.values())
    return {"seconds": round(idx / src_fps, 1), "analysis_fps": w.fps, "wall_s": round(wall, 1),
            "realtime_factor": round((idx / src_fps) / wall, 2) if wall else None,
            "objects": len(objs), "labels": dict(labels.most_common()), "verdicts": dict(verdicts),
            "actors": by_type, "id_swaps": swaps, "objects_matched": len(obj_seq),
            "gt_overlap_floor": round(len(overlap) / len(pv), 3) if pv else None,
            "second_opinion_vs_truth": {f"{a} / {b}": n for (a, b), n in sorted(so.items())},
            "disputed": [{"id": o["id"], "label": o["label"], "second": o["second_label"], "score": o["second_score"]}
                         for o in objs.values() if o.get("verdict") == "disputed"][:20]}


def run(cameras: list[str] | None = None, seconds: float = 0, catalogue: bool = True, log=print) -> dict[str, Any]:
    from . import designer as D
    from . import vision as V
    clips = D.sample_clips()
    cams = [c for c in (cameras or available()) if c in available()]
    if not cams:
        raise RuntimeError(f"no ground truth found in {gt_dir()} (expected <code>.geom.yml / .types.yml for {', '.join(GT_CAMERAS)})")
    import cv2
    live = V.DETECTOR._load()
    precise = V.PreciseDetector()

    def live_detect(frame):
        small = cv2.resize(frame, (V.MAX_SIDE, int(frame.shape[0] * V.MAX_SIDE / frame.shape[1])), interpolation=cv2.INTER_AREA)
        s = frame.shape[1] / small.shape[1]                # the record path shrinks to MAX_SIDE before detecting
        return [{"label": live.names[int(b.cls)], "box": [float(v) * s for v in b.xyxy[0]]} for b in live.predict(small, conf=0.35, verbose=False)[0].boxes]

    report: dict[str, Any] = {"ts": time.time(), "ground_truth": "MEVA (Kitware) human annotations, CC BY 4.0",
                              "precise": {"weights": Path(precise.weights).name, "tiles": list(precise.tiles),
                                          "tile_size": precise.tile_size, "full_size": precise.full_size, "conf": precise.conf},
                              "cameras": {}}
    for cam in cams:
        log(f"[{cam}] loading ground truth")
        _types, frames = load_gt(GT_CAMERAS[cam])
        r: dict[str, Any] = {}
        log(f"[{cam}] detector recall: live")
        r["detector_live"] = detector_recall(clips[cam], frames, live_detect)
        log(f"[{cam}] detector recall: precise")
        r["detector_precise"] = detector_recall(clips[cam], frames, precise.detect_bgr)
        if catalogue:
            log(f"[{cam}] catalogue run ({seconds or 'full clip'} s)")
            r["catalogue"] = catalogue_run(clips[cam], frames, seconds=seconds, detector=precise)
        report["cameras"][cam] = r
        log(json.dumps(r, indent=2))
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (AUDIT_DIR / time.strftime("audit-%Y%m%d-%H%M%S.json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def latest() -> dict[str, Any] | None:
    p = AUDIT_DIR / "latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="atlas audit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", action="append", help="sample clip name (campus-lobby ...); repeatable; default: all with ground truth")
    ap.add_argument("--seconds", type=float, default=0, help="audit only the first N seconds of each clip (default: whole clip)")
    ap.add_argument("--no-catalogue", action="store_true", help="detector recall only")
    a = ap.parse_args(argv)
    rep = run(a.camera, a.seconds, not a.no_catalogue)
    print(f"\nreport: {AUDIT_DIR / 'latest.json'}")
    return 0 if rep["cameras"] else 1
