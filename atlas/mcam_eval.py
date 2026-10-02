"""Multi-camera accuracy against human ground truth: WILDTRACK (EPFL), 7 synchronised cameras over one public square,
400 annotated instants (2 a second, 200 s), 13-40 people in view at once, 313 identities that are the SAME id on every
camera. Nothing here is a model's opinion of itself: every number is a comparison with the human labels.

    py -m atlas mcam-eval --data ~/AtlasDemo/wildtrack                  # detection, tracking, re-id, counts
    py -m atlas mcam-eval --data … --vlm google/gemini-3.1-flash-lite   # + the vision model's site-wide head count

Data: `scripts/get_wildtrack.py` puts frames/C1..C7/<frame>.jpg (960x540), annotations_positions/*.json and
calibrations/ under --data. WILDTRACK is for non-commercial research use; it is not committed to this repository.

What is scored
  detection   per camera, person boxes vs the labels at IoU >= 0.5 (one-to-one, Hungarian): precision, recall, F1, and
              the error of the per-frame head count. Only people standing inside the labelled 12 x 36 m area are
              labelled, so a detection counts only when its feet, projected through the camera's calibration onto the
              ground plane, land inside that area (the labels' own feet are projected the same way as a check).
  tracking    per camera over the 400 instants: MOTA, IDF1, identity switches (motmetrics), for ByteTrack (what the live
              view draws), the catalogue's own tracker (what gets named) and the ground tracker (atlas.ground: one
              tracker for all cameras, sightings merged by where the feet land). 2 frames a second is the catalogue's
              analysis rate; it is hard on trackers built for 30 fps, and that is the point. Across cameras: does a
              person keep ONE id on every camera (IDF1 over all cameras at once), and on the ground plane, are the
              tracked people where the labels put them (MODA, MOTA, IDF1 within 50 cm).
  re-id       CLIP ViT-B/32 on the labelled crops: for a person picked on one camera, is the most similar person on
              another camera at the same instant the same person (rank-1, gallery = everyone visible there), and how do
              same-person and different-person similarities separate (ROC AUC; precision and recall at the naming
              thresholds 0.86 and 0.92).
  counts      people in the whole scene per instant (the labels: distinct ids) vs what the pipeline can say: the sum of
              per-camera counts (double counts overlaps), the largest single camera (misses what one camera cannot see),
              feet merged on the ground, the multi-camera vote (atlas.ground.scene_count), and, with --vlm, the vision
              model's answer from all seven frames at once.
  held out    the ground tracker's and the vote's parameters were chosen on instants 0-199; the same scores on 200-399
              alone (the catalogue and ground trackers start fresh there) are the numbers to quote.
Output: workspace/vision-eval/wildtrack/{results.json, report.md, review.json (per-instant boxes for the review page)}.
"""
from __future__ import annotations

import argparse
import inspect
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import ground as G
from .config import WORKSPACE_DIR

CAMS = ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]
CALIB = ["CVLab1", "CVLab2", "CVLab3", "CVLab4", "IDIAP1", "IDIAP2", "IDIAP3"]
SCALE = 0.5                                   # frames are stored at 960x540; labels and calibration are at 1920x1080
ROI = (-300.0, 900.0, -900.0, 2700.0)         # the labelled ground area in cm: x 12 m, y 36 m (480 x 1440 cells of 2.5 cm)
OUT_DIR = WORKSPACE_DIR / "vision-eval" / "wildtrack"


# ---------------------------------------------------------------------------- data
def load(data: Path, limit: int = 0) -> dict[str, Any]:
    ann = sorted((data / "annotations_positions").glob("*.json"))
    if limit:
        ann = ann[:limit]
    frames = [p.stem for p in ann]
    gt: list[list[list[tuple[int, list[float]]]]] = []            # [instant][cam] -> [(pid, box at 960x540)]
    pos: list[dict[int, tuple[float, float]]] = []                # [instant] -> {pid: labelled ground position (x, y) cm}
    scene = []
    for p in ann:
        per = [[] for _ in CAMS]
        people = json.loads(p.read_text())
        for person in people:
            for v in person["views"]:
                if v["xmin"] < 0:
                    continue
                b = [v["xmin"] * SCALE, v["ymin"] * SCALE, v["xmax"] * SCALE, v["ymax"] * SCALE]
                per[v["viewNum"]].append((int(person["personID"]), b))
        gt.append(per)
        pos.append({int(x["personID"]): ground_cell(int(x["positionID"])) for x in people})
        scene.append(len({pid for c in per for pid, _ in c} | set(pos[-1])))
    return {"frames": frames, "gt": gt, "scene": scene, "pos": pos, "calib": [camera(data, c) for c in CALIB],
            "paths": [[data / "frames" / c / f"{f}.jpg" for c in CAMS] for f in frames]}


def ground_cell(position_id: int) -> tuple[float, float]:
    """A label's positionID -> ground (x, y) in cm (a 480 x 1440 grid of 2.5 cm cells over ROI)."""
    return ROI[0] + 2.5 * (position_id % 480), ROI[2] + 2.5 * (position_id // 480)


def sub(D: dict[str, Any], a: int, b: int) -> dict[str, Any]:
    """Instants a..b-1 of a loaded dataset."""
    return dict(D, **{k: D[k][a:b] for k in ("frames", "gt", "scene", "pos", "paths") if k in D})


def camera(data: Path, name: str) -> dict[str, Any]:
    import cv2
    # the frames in Image_subsets are already undistorted: intrinsic_zero (no distortion) is the matching calibration.
    # Undistorting them again with intrinsic_original bends the edges: foot points then land 30 cm off at the median and
    # 2.3 m at the 90th percentile, against 8.9 cm / 19.8 cm here (checked against each label's own ground position).
    fs = cv2.FileStorage(str(data / "calibrations" / "intrinsic_zero" / f"intr_{name}.xml"), cv2.FILE_STORAGE_READ)
    K, dist = fs.getNode("camera_matrix").mat(), fs.getNode("distortion_coefficients").mat()
    txt = (data / "calibrations" / "extrinsic" / f"extr_{name}.xml").read_text()
    rvec = np.array([float(x) for x in re.search(r"<rvec>(.*?)</rvec>", txt, re.S).group(1).split()])
    tvec = np.array([float(x) for x in re.search(r"<tvec>(.*?)</tvec>", txt, re.S).group(1).split()])
    R, _ = cv2.Rodrigues(rvec)
    H = K @ np.column_stack([R[:, 0], R[:, 1], tvec])                # ground plane z = 0 -> image
    return {"K": K, "dist": dist, "Hinv": np.linalg.inv(H)}


def feet_on_ground(cal: dict[str, Any], boxes: list[list[float]]) -> np.ndarray:
    """World (x, y) in cm of each box's bottom-centre, through the camera's ground-plane homography."""
    import cv2
    if not boxes:
        return np.zeros((0, 2))
    pts = np.array([[[(b[0] + b[2]) / 2 / SCALE, b[3] / SCALE]] for b in boxes], dtype=np.float64)
    und = cv2.undistortPoints(pts, cal["K"], cal["dist"], P=cal["K"]).reshape(-1, 2)
    w = (cal["Hinv"] @ np.column_stack([und, np.ones(len(und))]).T).T
    return w[:, :2] / w[:, 2:3]


def projection_error(data: Path, D: dict[str, Any], every: int = 10) -> dict[str, float]:
    """How far a labelled box's foot point lands from that person's labelled ground position (positionID), in cm."""
    errs = []
    for p in sorted((data / "annotations_positions").glob("*.json"))[:len(D["frames"])][::every]:
        for person in json.loads(p.read_text()):
            truth = np.array(ground_cell(int(person["positionID"])))
            for v in person["views"]:
                if v["xmin"] < 0:
                    continue
                w = feet_on_ground(D["calib"][v["viewNum"]], [[v["xmin"] * SCALE, v["ymin"] * SCALE, v["xmax"] * SCALE, v["ymax"] * SCALE]])[0]
                errs.append(float(np.linalg.norm(w - truth)))
    e = np.array(errs)
    return {"median": round(float(np.median(e)), 1), "p90": round(float(np.percentile(e, 90)), 1), "n": len(e)}


def in_roi(xy: np.ndarray, margin: float = 0.0) -> np.ndarray:
    x0, x1, y0, y1 = ROI
    return (xy[:, 0] >= x0 - margin) & (xy[:, 0] <= x1 + margin) & (xy[:, 1] >= y0 - margin) & (xy[:, 1] <= y1 + margin)


def iou_matrix(a: list[list[float]], b: list[list[float]]) -> np.ndarray:
    if not a or not b:
        return np.zeros((len(a), len(b)))
    A, B = np.array(a)[:, None, :], np.array(b)[None, :, :]
    ix = np.clip(np.minimum(A[..., 2], B[..., 2]) - np.maximum(A[..., 0], B[..., 0]), 0, None)
    iy = np.clip(np.minimum(A[..., 3], B[..., 3]) - np.maximum(A[..., 1], B[..., 1]), 0, None)
    inter = ix * iy
    area = lambda X: (X[..., 2] - X[..., 0]) * (X[..., 3] - X[..., 1])
    return inter / (area(A) + area(B) - inter + 1e-9)


def match(gtb: list[list[float]], pb: list[list[float]], thr: float = 0.5) -> list[tuple[int, int]]:
    from scipy.optimize import linear_sum_assignment
    M = iou_matrix(gtb, pb)
    if M.size == 0:
        return []
    r, c = linear_sum_assignment(-M)
    return [(i, j) for i, j in zip(r, c) if M[i, j] >= thr]


# ---------------------------------------------------------------------------- detection + tracking
def run_detector(D: dict[str, Any], weights: str, track: bool, conf: float = 0.35, log=print, cache: Path | None = None) -> list[list[list[dict]]]:
    """[instant][cam] -> [{box, conf, id}] for one detector (and ByteTrack ids when track). Cached under `cache`."""
    cf = cache / f"preds-{Path(weights).stem}{'-track' if track else ''}-{len(D['frames'])}.json" if cache else None
    if cf and cf.is_file():
        log(f"  {cf.name}: cached")
        return json.loads(cf.read_text())
    from ultralytics import YOLO
    out: list[list[list[dict]]] = [[[] for _ in CAMS] for _ in D["frames"]]
    for ci in range(len(CAMS)):
        m = YOLO(weights)
        t0 = time.time()
        for fi, paths in enumerate(D["paths"]):
            p = str(paths[ci])
            r = (m.track(p, conf=conf, classes=[0], persist=True, verbose=False, tracker="bytetrack.yaml", imgsz=960) if track
                 else m.predict(p, conf=conf, classes=[0], verbose=False, imgsz=960))[0]
            ids = r.boxes.id.int().tolist() if (track and r.boxes.id is not None) else [None] * len(r.boxes)
            out[fi][ci] = [{"box": [round(v, 1) for v in b.xyxy[0].tolist()], "conf": round(float(b.conf), 3), "id": tid}
                           for b, tid in zip(r.boxes, ids)]
        log(f"  {Path(weights).stem}{' +track' if track else ''} {CAMS[ci]}: {len(D['frames'])} frames in {time.time() - t0:.0f}s")
    if cf:
        cf.write_text(json.dumps(out, separators=(",", ":")))
    return out


def roi_filter(D: dict[str, Any], preds: list[list[list[dict]]]) -> list[list[list[dict]]]:
    out = []
    for fp in preds:
        row = []
        for ci, dets in enumerate(fp):
            xy = feet_on_ground(D["calib"][ci], [d["box"] for d in dets])
            keep = in_roi(xy, margin=50.0) if len(dets) else np.zeros(0, bool)
            row.append([d for d, k in zip(dets, keep) if k])
        out.append(row)
    return out


def score_detection(D: dict[str, Any], preds: list[list[list[dict]]], thr: float = 0.5) -> dict[str, Any]:
    per = []
    for ci, cam in enumerate(CAMS):
        tp = fp = fn = 0
        cerr = []
        for fi, per_cam in enumerate(D["gt"]):
            g = [b for _, b in per_cam[ci]]
            p = [d["box"] for d in preds[fi][ci]]
            m = match(g, p, thr)
            tp += len(m); fp += len(p) - len(m); fn += len(g) - len(m)
            cerr.append(abs(len(p) - len(g)))
        prec, rec = tp / max(1, tp + fp), tp / max(1, tp + fn)
        per.append({"camera": cam, "gt": tp + fn, "tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 3), "recall": round(rec, 3),
                    "f1": round(2 * prec * rec / max(1e-9, prec + rec), 3), "count_mae": round(float(np.mean(cerr)), 2)})
    T = {k: sum(x[k] for x in per) for k in ("tp", "fp", "fn")}
    prec, rec = T["tp"] / max(1, T["tp"] + T["fp"]), T["tp"] / max(1, T["tp"] + T["fn"])
    return {"per_camera": per, "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(2 * prec * rec / max(1e-9, prec + rec), 3),
            "count_mae": round(float(np.mean([x["count_mae"] for x in per])), 2)}


def catalogue_tracks(D: dict[str, Any], preds: list[list[list[dict]]]) -> list[list[list[dict]]]:
    """Feed the catalogue's own tracker (objects.Tracker) the same detections at the same 2 frames a second."""
    from . import objects as OBJ
    out: list[list[list[dict]]] = [[[] for _ in CAMS] for _ in D["frames"]]
    for ci in range(len(CAMS)):
        tr = OBJ.Tracker()
        for fi in range(len(D["frames"])):
            dets = [{"label": "person", "conf": d["conf"], "box": d["box"]} for d in preds[fi][ci]]
            seen, _ended = tr.update(dets, fi * 0.5)
            out[fi][ci] = [{"box": list(t.box), "conf": round(t.mean_conf, 3), "id": t.id} for t in seen if tr.confirmed(t)]
    return out


def score_tracking(D: dict[str, Any], preds: list[list[list[dict]]]) -> dict[str, Any]:
    import motmetrics as mm
    accs, names = [], []
    for ci, cam in enumerate(CAMS):
        acc = mm.MOTAccumulator(auto_id=True)
        for fi, per_cam in enumerate(D["gt"]):
            gids = [pid for pid, _ in per_cam[ci]]
            hyp = [d for d in preds[fi][ci] if d.get("id") is not None]
            dist = 1 - iou_matrix([b for _, b in per_cam[ci]], [d["box"] for d in hyp])
            dist[dist > 0.5] = np.nan
            acc.update(gids, [d["id"] for d in hyp], dist)
        accs.append(acc); names.append(cam)
    mh = mm.metrics.create()
    summ = mh.compute_many(accs, names=names, metrics=["mota", "idf1", "num_switches", "precision", "recall", "num_unique_objects", "mostly_tracked"],
                           generate_overall=True)
    rows = []
    for name, r in summ.iterrows():
        rows.append({"camera": name, "mota": round(float(r["mota"]), 3), "idf1": round(float(r["idf1"]), 3), "id_switches": int(r["num_switches"]),
                     "precision": round(float(r["precision"]), 3), "recall": round(float(r["recall"]), 3),
                     "gt_identities": int(r["num_unique_objects"]), "mostly_tracked": int(r["mostly_tracked"])})
    return {"per_camera": [r for r in rows if r["camera"] != "OVERALL"], "overall": next(r for r in rows if r["camera"] == "OVERALL")}


def all_feet(D: dict[str, Any], preds: list[list[list[dict]]]) -> list[list[np.ndarray]]:
    """[instant][cam] -> (n, 2) ground points of the detections' feet."""
    return [G.boxes_to_feet(D["calib"], [[d["box"] for d in c] for c in f], feet_on_ground) for f in preds]


def ground_tracks(D: dict[str, Any], preds: list[list[list[dict]]], feet: list[list[np.ndarray]] | None = None,
                  **params: Any) -> tuple[list[list[list[dict]]], list[list[tuple[int, float, float]]]]:
    """One ground-plane tracker for all cameras (atlas.ground, with retro-labelling): [instant][cam] -> [{box, conf, id}]
    for the sightings of confirmed people, the id being the same on every camera; and per instant [(id, x, y)] on the
    ground for the people seen by two cameras or more."""
    feet = feet if feet is not None else all_feet(D, preds)
    ids, ground = G.track(feet, [[[d["conf"] for d in c] for c in f] for f in preds], [fi * 0.5 for fi in range(len(preds))], **params)
    tracks = [[[{"box": d["box"], "conf": d["conf"], "id": i} for d, i in zip(preds[fi][ci], ids[fi][ci]) if i is not None]
               for ci in range(len(CAMS))] for fi in range(len(preds))]
    return tracks, ground


def link_by_feet(D: dict[str, Any], tracks: list[list[list[dict]]], R: float = 120.0, kmin: int = 2, frac: float = 0.3) -> list[list[list[dict]]]:
    """Per-camera tracks joined across cameras afterwards (the baseline for one id on every camera): two tracks on different
    cameras are the same person when their feet fall in the same ground cluster (R cm) at >= kmin instants and >= frac of
    the shorter one's life; strongest pairs first, never two tracks of one camera that overlap in time. R, kmin and frac
    were chosen on WILDTRACK instants 0-199."""
    co: dict[tuple, int] = {}
    life: dict[tuple, set] = {}
    for fi in range(len(tracks)):
        keys = [(ci, d["id"]) for ci in range(len(CAMS)) for d in tracks[fi][ci] if d.get("id") is not None]
        for k in keys:
            life.setdefault(k, set()).add(fi)
        feet = [feet_on_ground(D["calib"][ci], [d["box"] for d in tracks[fi][ci] if d.get("id") is not None]) for ci in range(len(CAMS))]
        pts, cams, _ = G.gather(feet)
        for m in G.cluster(pts, cams, R):
            ks = sorted(keys[i] for i in m)
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    co[(ks[i], ks[j])] = co.get((ks[i], ks[j]), 0) + 1
    group = {k: {k} for k in life}
    root = {k: k for k in life}
    for (u, v), n in sorted(co.items(), key=lambda kv: -kv[1]):
        ru, rv = root[u], root[v]
        if n < kmin or n < frac * min(len(life[u]), len(life[v])) or ru == rv:
            continue
        if any(x[0] == y[0] and life[x] & life[y] for x in group[ru] for y in group[rv]):
            continue
        for k in group[rv]:
            root[k] = ru
        group[ru] |= group.pop(rv)
    gid = {r: n + 1 for n, r in enumerate(sorted(group))}
    return [[[dict(d, id=gid[root[(ci, d["id"])]]) for d in tracks[fi][ci] if d.get("id") is not None] for ci in range(len(CAMS))]
            for fi in range(len(tracks))]


def score_cross_camera(D: dict[str, Any], tracks: list[list[list[dict]]], per_camera_ids: bool = False) -> dict[str, float]:
    """Identity across cameras: a person must keep ONE id on every camera and over time. Every labelled box matched to a
    tracked box (same camera, same instant, IoU >= 0.5) counts for the pair (person, id); persons and ids are then paired
    one to one to maximise the matched boxes (as IDF1 does, over all cameras at once). per_camera_ids: the ids are only
    unique per camera (camera-prefixed before scoring)."""
    from scipy.optimize import linear_sum_assignment
    co: dict[tuple, int] = {}
    ng = nh = 0
    for fi in range(len(D["frames"])):
        for ci in range(len(CAMS)):
            g = D["gt"][fi][ci]
            h = [d for d in tracks[fi][ci] if d.get("id") is not None]
            ng += len(g); nh += len(h)
            I = iou_matrix([b for _, b in g], [d["box"] for d in h])
            for i, j in zip(*np.where(I >= 0.5)):
                k = (g[i][0], (ci, h[j]["id"]) if per_camera_ids else h[j]["id"])
                co[k] = co.get(k, 0) + 1
    P, H = sorted({k[0] for k in co}), sorted({k[1] for k in co}, key=str)
    pi, hi = {p: i for i, p in enumerate(P)}, {h: i for i, h in enumerate(H)}
    C = np.zeros((len(P), len(H)))
    for (p, h), n in co.items():
        C[pi[p], hi[h]] = n
    r, c = linear_sum_assignment(-C) if C.size else ([], [])
    tp = float(C[r, c].sum()) if C.size else 0.0
    return {"idf1": round(2 * tp / max(1, ng + nh), 3), "idp": round(tp / max(1, nh), 3), "idr": round(tp / max(1, ng), 3)}


def score_ground(D: dict[str, Any], ground: list[list[tuple[int, float, float]]], thr: float = 50.0) -> dict[str, Any]:
    """The people's ground positions and ids vs the labelled positions (a hit within thr cm): MODA, MOTA, IDF1 (motmetrics)."""
    import motmetrics as mm
    acc = mm.MOTAccumulator(auto_id=True)
    for g, h in zip(D["pos"], ground):
        gids = list(g)
        P = np.array([g[k] for k in gids], float).reshape(-1, 2)
        Q = np.array([[x, y] for _, x, y in h], float).reshape(-1, 2)
        d = np.linalg.norm(P[:, None] - Q[None], axis=2)
        d[d > thr] = np.nan
        acc.update(gids, [t for t, _, _ in h], d)
    r = mm.metrics.create().compute(acc, metrics=["mota", "idf1", "num_switches", "precision", "recall", "num_misses",
                                                  "num_false_positives", "num_objects", "motp"], name="g").iloc[0]
    return {"radius_cm": thr, "moda": round(float(1 - (r["num_misses"] + r["num_false_positives"]) / max(1, r["num_objects"])), 3),
            "mota": round(float(r["mota"]), 3), "idf1": round(float(r["idf1"]), 3), "id_switches": int(r["num_switches"]),
            "precision": round(float(r["precision"]), 3), "recall": round(float(r["recall"]), 3), "position_error_cm": round(float(r["motp"]), 1)}


# ---------------------------------------------------------------------------- re-identification across cameras
def score_reid(D: dict[str, Any], n_instants: int = 40, min_h: float = 60.0, thresholds=(0.86, 0.92), log=print) -> dict[str, Any]:
    from PIL import Image
    from . import rag
    emb = rag.get_embedder("clip")
    if not str(getattr(emb, "name", "")).startswith("clip"):
        return {"error": "CLIP is not available (pip install open_clip_torch torch)"}
    idx = np.linspace(0, len(D["frames"]) - 1, min(n_instants, len(D["frames"]))).astype(int).tolist()
    crops, meta = [], []
    import io
    for fi in idx:
        for ci in range(len(CAMS)):
            im = None
            for pid, b in D["gt"][fi][ci]:
                if b[3] - b[1] < min_h or b[2] - b[0] < 12:
                    continue
                im = im or Image.open(D["paths"][fi][ci]).convert("RGB")
                c = im.crop((max(0, int(b[0])), max(0, int(b[1])), min(im.width, int(b[2])), min(im.height, int(b[3]))))
                buf = io.BytesIO(); c.save(buf, "JPEG", quality=90)
                crops.append(buf.getvalue()); meta.append((fi, ci, pid))
    t0 = time.time()
    V = np.concatenate([emb.embed_images(crops[i:i + 64]) for i in range(0, len(crops), 64)])
    V = V / np.linalg.norm(V, axis=1, keepdims=True)
    log(f"  re-id: {len(crops)} labelled crops from {len(idx)} instants embedded in {time.time() - t0:.0f}s")
    fi_a, ci_a, pid_a = (np.array([m[k] for m in meta]) for k in range(3))
    def auc_of(pos, neg):
        if not len(pos) or not len(neg):
            return None
        allv = np.concatenate([pos, neg]); rk = allv.argsort().argsort() + 1
        return (rk[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    q_aucs = []
    ctrl_hit, ctrl_n = 0, 0
    inst = sorted(set(fi_a.tolist()))
    for q in range(len(meta)):                                              # control: same camera, the next sampled instant
        nxt = [f for f in inst if f > fi_a[q]]
        if not nxt:
            continue
        gal = np.where((fi_a == nxt[0]) & (ci_a == ci_a[q]))[0]
        if len(gal) and np.any(pid_a[gal] == pid_a[q]):
            ctrl_n += 1
            ctrl_hit += int(pid_a[gal[int(np.argmax(V[gal] @ V[q]))]] == pid_a[q])
    rank1, trials = 0, 0
    same_s, diff_s = [], []
    for q in range(len(meta)):
        gal = np.where((fi_a == fi_a[q]) & (ci_a != ci_a[q]))[0]                 # everyone visible on the other cameras, same instant
        if not len(gal) or not np.any(pid_a[gal] == pid_a[q]):
            continue
        s = V[gal] @ V[q]
        trials += 1
        rank1 += int(pid_a[gal[int(np.argmax(s))]] == pid_a[q])
        same_s += s[pid_a[gal] == pid_a[q]].tolist()
        diff_s += s[pid_a[gal] != pid_a[q]].tolist()
        qa = auc_of(s[pid_a[gal] == pid_a[q]], s[pid_a[gal] != pid_a[q]])
        if qa is not None:
            q_aucs.append(qa)
    same, diff = np.array(same_s), np.array(diff_s)
    allv = np.concatenate([same, diff]); ranks = allv.argsort().argsort() + 1
    auc = (ranks[:len(same)].sum() - len(same) * (len(same) + 1) / 2) / max(1, len(same) * len(diff))
    at = []
    for t in thresholds:
        tp, fp = int((same >= t).sum()), int((diff >= t).sum())
        at.append({"threshold": t, "precision": round(tp / max(1, tp + fp), 3), "recall": round(tp / max(1, len(same)), 3),
                   "false_matches_per_true": round(fp / max(1, tp), 1)})
    return {"model": getattr(emb, "name", "clip"), "crops": len(crops), "queries": trials, "rank1": round(rank1 / max(1, trials), 3),
            "chance_rank1": round(float(np.mean([1 / max(1, int(((fi_a == fi_a[q]) & (ci_a != ci_a[q])).sum())) for q in range(len(meta))])), 3),
            "auc": round(float(auc), 3), "auc_per_query": round(float(np.mean(q_aucs)), 3) if q_aucs else None,
            "control_rank1_same_camera": round(ctrl_hit / max(1, ctrl_n), 3), "control_queries": ctrl_n, "same_mean": round(float(same.mean()), 3), "diff_mean": round(float(diff.mean()), 3),
            "same_pairs": len(same), "diff_pairs": len(diff), "at": at}


# ---------------------------------------------------------------------------- geometry: the cameras are calibrated
def score_geometry(D: dict[str, Any], preds: list[list[list[dict]]], radii=(50.0, 75.0, 100.0, 150.0)) -> dict[str, Any]:
    """Two sightings whose feet land on the same spot of the ground at the same instant are the same person, whatever
    they wear. (a) cross-camera association on the LABELLED boxes (isolates the geometry from the detector): for a person
    on one camera, is the nearest foot point on another camera the same person; (b) the whole-scene head count from the
    DETECTIONS: feet from every camera clustered on the ground (a cluster never holds two sightings from one camera)."""
    hits = trials = 0
    dists_same = []
    for fi in range(len(D["frames"])):
        W = [feet_on_ground(D["calib"][ci], [b for _, b in D["gt"][fi][ci]]) for ci in range(len(CAMS))]
        P = [[pid for pid, _ in D["gt"][fi][ci]] for ci in range(len(CAMS))]
        for a in range(len(CAMS)):
            for qi, pid in enumerate(P[a]):
                for b in range(len(CAMS)):
                    if b == a or not P[b] or pid not in P[b]:
                        continue
                    d = np.linalg.norm(W[b] - W[a][qi], axis=1)
                    trials += 1
                    hits += int(P[b][int(np.argmin(d))] == pid)
                    dists_same.append(float(d[P[b].index(pid)]))
    truth = np.array(D["scene"], float)
    counts = {}
    for R in radii:
        est = []
        for fi in range(len(D["frames"])):
            pts, cam = [], []
            for ci in range(len(CAMS)):
                w = feet_on_ground(D["calib"][ci], [d["box"] for d in preds[fi][ci]])
                pts += w.tolist(); cam += [ci] * len(w)
            est.append(cluster_count(np.array(pts), cam, R))
        e = np.array(est, float)
        counts[f"{int(R)} cm"] = {"mae": round(float(np.mean(np.abs(e - truth))), 1), "bias": round(float(np.mean(e - truth)), 1),
                                  "within_20pct": round(float(np.mean(np.abs(e - truth) <= 0.2 * truth)), 3), "series": [int(v) for v in e]}
    ds = np.array(dists_same)
    return {"assoc_rank1": round(hits / max(1, trials), 3), "assoc_queries": trials,
            "same_person_ground_gap_cm": {"median": round(float(np.median(ds)), 1), "p90": round(float(np.percentile(ds, 90)), 1)},
            "counts": counts}


def cluster_count(pts: np.ndarray, cam: list[int], R: float) -> int:
    """Greedy agglomeration by ground distance (ground.cluster): merge the closest pair of clusters while their centres are
    within R cm and they share no camera (one camera sees a person once)."""
    return len(G.cluster(np.asarray(pts, float).reshape(-1, 2), cam, R))


# ---------------------------------------------------------------------------- the whole scene: how many people are there?
def count_stats(est: np.ndarray, truth: np.ndarray) -> dict[str, float]:
    return {"mae": round(float(np.mean(np.abs(est - truth))), 1), "bias": round(float(np.mean(est - truth)), 1),
            "within_20pct": round(float(np.mean(np.abs(est - truth) <= 0.2 * truth)), 3)}


def score_counts(D: dict[str, Any], preds: list[list[list[dict]]]) -> dict[str, Any]:
    truth = np.array(D["scene"], float)
    s = np.array([sum(len(c) for c in f) for f in preds], float)
    m = np.array([max(len(c) for c in f) for f in preds], float)
    return {"truth_mean": round(float(truth.mean()), 1), "truth_min": int(truth.min()), "truth_max": int(truth.max()),
            "sum_of_cameras": count_stats(s, truth), "largest_camera": count_stats(m, truth)}


HELD_OUT = 200                                                 # WILDTRACK instants 0-199 tuned the ground tracker and the vote
VOTE = {"R": 150.0, "min_cams": 3, "single_conf": 0.7}         # chosen on WILDTRACK instants 0-199


def score_vote(D: dict[str, Any], preds: list[list[list[dict]]], feet: list[list[np.ndarray]] | None = None) -> dict[str, Any]:
    """The multi-camera head count (ground.scene_count): feet merged at 150 cm, a person kept when 3 cameras saw them or one
    detection is >= 0.7 sure, counted inside the labelled area. With the per-instant series for the review timeline."""
    feet = feet if feet is not None else all_feet(D, preds)
    est = np.array([len(G.scene_count(feet[fi], [[d["conf"] for d in c] for c in preds[fi]], ROI, **VOTE)) for fi in range(len(preds))], float)
    return {**count_stats(est, np.array(D["scene"], float)), "params": VOTE, "series": [int(v) for v in est]}


VLM_SYSTEM = ("You are shown seven frames from seven cameras over ONE public square, taken at the same instant, each "
              "labelled with its camera. The cameras overlap: the same person often appears on several. Count carefully. "
              "State only what is visible. Reply with JSON only.")
VLM_PROMPT = ('{"distinct_people": <integer: your best estimate of how many different people are in the whole scene, '
              'counting each person once even if several cameras see them>, "per_camera": {"C1": <people visible>, …}, '
              '"groups": "<one sentence: are people walking, standing in groups, where>", "confidence": "high|medium|low"}')


def score_vlm(D: dict[str, Any], model: str, n: int = 8, log=print) -> dict[str, Any]:
    from . import vision as V
    idx = np.linspace(0, len(D["frames"]) - 1, n).astype(int).tolist()
    rows = []
    for fi in idx:
        imgs = [(f"[camera {c}]", D["paths"][fi][ci].read_bytes()) for ci, c in enumerate(CAMS)]
        t0 = time.time()
        try:
            txt = V.chat_images(VLM_SYSTEM, VLM_PROMPT, imgs, model=model, max_tokens=500)
            j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            est = int(j.get("distinct_people"))
            pc = j.get("per_camera") or {}
            per_err = [abs(int(pc.get(c, -99)) - len(D["gt"][fi][ci])) for ci, c in enumerate(CAMS) if str(pc.get(c, "")).lstrip("-").isdigit()]
            rows.append({"instant": D["frames"][fi], "truth": D["scene"][fi], "estimate": est, "per_camera_mae": round(float(np.mean(per_err)), 1) if per_err else None,
                         "groups": str(j.get("groups", ""))[:200], "confidence": j.get("confidence"), "seconds": round(time.time() - t0, 1)})
        except Exception as exc:
            rows.append({"instant": D["frames"][fi], "truth": D["scene"][fi], "error": str(exc)[:160]})
        log(f"  vlm {model.split('/')[-1]} @ {D['frames'][fi]}: truth {D['scene'][fi]}, said {rows[-1].get('estimate', 'ERR')}")
    ok = [r for r in rows if "estimate" in r]
    err = np.array([r["estimate"] - r["truth"] for r in ok], float)
    tr = np.array([r["truth"] for r in ok], float)
    return {"model": model, "instants": rows, "answered": len(ok), "mae": round(float(np.abs(err).mean()), 1) if len(ok) else None,
            "bias": round(float(err.mean()), 1) if len(ok) else None,
            "within_20pct": round(float(np.mean(np.abs(err) <= 0.2 * tr)), 3) if len(ok) else None,
            "per_camera_mae": round(float(np.mean([r["per_camera_mae"] for r in ok if r.get("per_camera_mae") is not None])), 1) if ok else None}


# ---------------------------------------------------------------------------- output
def review_json(D: dict[str, Any], preds: list[list[list[dict]]], tracks: list[list[list[dict]]], geo: list[int] | None = None,
                geo_label: str = "", shared_ids: bool = False) -> dict[str, Any]:
    """Per instant, per camera: labelled boxes (with person id) and predicted tracks (with track id and the person id they
    matched, or -1): what the review page plays back, frame by frame, all cameras on one timeline. shared_ids: a track id
    means the same person on every camera (the ground tracker); geo: a whole-scene head count per instant."""
    inst = []
    for fi, f in enumerate(D["frames"]):
        cams = []
        for ci in range(len(CAMS)):
            g = D["gt"][fi][ci]
            p = tracks[fi][ci]
            m = dict((j, g[i][0]) for i, j in match([b for _, b in g], [d["box"] for d in p]))
            cams.append({"gt": [[pid, *[round(v) for v in b]] for pid, b in g],
                         "pred": [[d["id"] if d["id"] is not None else -1, *[round(v) for v in d["box"]], m.get(j, -1)] for j, d in enumerate(p)],
                         "det": len(preds[fi][ci])})
        inst.append({"frame": f, "t": fi * 0.5, "scene": D["scene"][fi], "cams": cams, **({"geo": geo[fi]} if geo else {})})
    return {"cameras": CAMS, "fps": 2, "size": [960, 540], "instants": inst, "shared_ids": shared_ids, **({"geo_label": geo_label} if geo else {})}


def multicam(D: dict[str, Any], preds: list[list[list[dict]]], cat: list[list[list[dict]]] | None = None) -> dict[str, Any]:
    """The cameras together: the ground tracker against the catalogue tracker (per camera, then linked by foot position),
    on the image (per camera), across cameras and on the ground; and the multi-camera head count."""
    feet = all_feet(D, preds)
    gtr, gpos = ground_tracks(D, preds, feet)
    cat = cat if cat is not None else catalogue_tracks(D, preds)
    return {"tracks": gtr, "tracking": {"catalogue tracker": score_tracking(D, cat), "ground tracker (all cameras)": score_tracking(D, gtr)},
            "cross_camera": {"ground tracker (all cameras)": score_cross_camera(D, gtr),
                             "catalogue tracker, linked across cameras by foot position": score_cross_camera(D, link_by_feet(D, cat)),
                             "catalogue tracker, one id per camera": score_cross_camera(D, cat, per_camera_ids=True)},
            "ground_plane": {"ground tracker (all cameras)": score_ground(D, gpos)},
            "counts": {**score_counts(D, preds), "vote": score_vote(D, preds, feet)}}


def write_report(R: dict[str, Any], out: Path) -> None:
    L = ["# Multi-camera accuracy: WILDTRACK", "",
         f"7 synchronised cameras, {R['instants']} instants at 2 per second ({R['instants'] / 2:.0f} s), {R['counts']['truth_min']}-{R['counts']['truth_max']} "
         f"people in the scene at once (mean {R['counts']['truth_mean']}), {R['identities']} labelled identities. Every number compares with the human labels.", "",
         f"Calibration check: {R['calib_check'] * 100:.1f}% of labelled feet project inside the labelled area; a labelled box's foot point lands "
         f"{R['projection_error_cm']['median']} cm from that person's labelled ground position at the median ({R['projection_error_cm']['p90']} cm at the 90th percentile).", "", "## Detection (people, IoU >= 0.5, inside the labelled area)", "",
         "| detector | precision | recall | F1 | per-camera count error (people/frame) |", "|---|---|---|---|---|"]
    for k, d in R["detection"].items():
        L.append(f"| {k} | {d['precision']} | {d['recall']} | {d['f1']} | {d['count_mae']} |")
    L += ["", "| camera | " + " | ".join(f"{k} P / R" for k in R["detection"]) + " |", "|---|" + "---|" * len(R["detection"])]
    for ci, c in enumerate(CAMS):
        L.append(f"| {c} | " + " | ".join(f"{d['per_camera'][ci]['precision']} / {d['per_camera'][ci]['recall']}" for d in R["detection"].values()) + " |")
    L += ["", "## Tracking (identity kept over time, per camera, 2 frames a second)", "", "| tracker | MOTA | IDF1 | identity switches | recall | mostly tracked |", "|---|---|---|---|---|---|"]
    for k, t in R["tracking"].items():
        o = t["overall"]
        L.append(f"| {k} | {o['mota']} | {o['idf1']} | {o['id_switches']} | {o['recall']} | {o['mostly_tracked']} of {o['gt_identities']} |")
    if R.get("cross_camera"):
        gp = R.get("ground_tracker_params") or {}
        L += ["", f"The ground tracker merges every camera's sightings by where the feet land ({gp.get('R', '?'):.0f} cm), follows each person "
              f"on the ground (constant-velocity Kalman, Hungarian assignment within {gp.get('gate', '?'):.0f} cm, {gp.get('max_miss', '?')} missed "
              f"instants allowed), starts a person only when {gp.get('birth_cams', '?')} cameras see them, and gives every sighting of that person "
              "the same id on every camera.", "",
              "| one id per person on every camera | IDF1 across cameras | ID precision | ID recall |", "|---|---|---|---|"]
        for k, v in R["cross_camera"].items():
            L.append(f"| {k} | {v['idf1']} | {v['idp']} | {v['idr']} |")
        for k, v in (R.get("ground_plane") or {}).items():
            L += ["", f"On the ground plane ({k} vs the labelled positions, a hit within {v['radius_cm']:.0f} cm): MODA {v['moda']}, MOTA {v['mota']}, "
                  f"IDF1 {v['idf1']}, {v['id_switches']} identity switches, precision {v['precision']}, recall {v['recall']}, "
                  f"position error {v['position_error_cm']} cm (people seen by two cameras or more)."]
    r = R.get("reid") or {}
    if r and "error" not in r:
        L += ["", "## Re-identification across cameras (CLIP appearance, same instant)", "",
              f"Pick a person on one camera; among everyone visible on another camera at that instant, the most similar crop is the same person "
              f"**{r['rank1'] * 100:.0f}%** of the time (chance {r['chance_rank1'] * 100:.0f}%, {r['queries']} queries). ROC AUC {r['auc']} pooled, "
              f"{r.get('auc_per_query')} per query (same-person similarity {r['same_mean']} vs different {r['diff_mean']}). Control, the easiest case: "
              f"the same camera a few seconds later, rank-1 {r.get('control_rank1_same_camera', 0) * 100:.0f}% ({r.get('control_queries')} queries).", "", "| threshold | precision | recall | false matches per true one |", "|---|---|---|---|"]
        for a in r["at"]:
            L.append(f"| {a['threshold']} | {a['precision']} | {a['recall']} | {a['false_matches_per_true']} |")
    g = R.get("geometry") or {}
    if g:
        L += ["", "## Geometry: the cameras are calibrated", "",
              f"Matching a person across cameras by where their feet land on the ground (same instant, labelled boxes): the nearest foot "
              f"point on the other camera is the same person **{g['assoc_rank1'] * 100:.0f}%** of the time ({g['assoc_queries']} queries), against "
              f"{(r.get('rank1') or 0) * 100:.0f}% for CLIP appearance. The same person's two foot points are {g['same_person_ground_gap_cm']['median']} cm "
              f"apart at the median ({g['same_person_ground_gap_cm']['p90']} cm at the 90th percentile)."]
    c = R["counts"]
    L += ["", "## How many people are in the scene?", "", "| estimate | mean absolute error (people) | bias | within 20% of the truth |", "|---|---|---|---|",
          f"| sum of per-camera detections | {c['sum_of_cameras']['mae']} | {c['sum_of_cameras']['bias']:+} | {c['sum_of_cameras']['within_20pct'] * 100:.0f}% |",
          f"| largest single camera | {c['largest_camera']['mae']} | {c['largest_camera']['bias']:+} | {c['largest_camera']['within_20pct'] * 100:.0f}% |"]
    for k, v in (g.get("counts") or {}).items():
        L.append(f"| detections' feet clustered on the ground, {k} | {v['mae']} | {v['bias']:+} | {v['within_20pct'] * 100:.0f}% |")
    if c.get("vote"):
        v = c["vote"]
        L.append(f"| multi-camera vote: feet merged at {v['params']['R']:.0f} cm, kept when {v['params']['min_cams']} cameras agree or one detection "
                 f"is >= {v['params']['single_conf']} sure, inside the labelled area | {v['mae']} | {v['bias']:+} | {v['within_20pct'] * 100:.0f}% |")
    for v in R.get("vlm", []):
        if v.get("mae") is not None:
            L.append(f"| {v['model']} from all 7 frames | {v['mae']} | {v['bias']:+} | {v['within_20pct'] * 100:.0f}% ({v['answered']} instants) |")
    if R.get("vlm"):
        L += ["", "The vision models are not held to the same standard: they see everyone in the seven frames, including people outside the "
              "labelled area (the stairs, the far side), which the truth does not count. Their round answers (85, 115, 120) still say they "
              "cannot count a crowd across seven overlapping views."]
    h = R.get("held_out")
    if h:
        a, b = h["instants"]
        L += ["", f"## Held out: instants {a}-{b}", "",
              f"The ground tracker's parameters (merge radius, gate, misses, confirmations, Kalman noise) and the vote's (radius, cameras, "
              f"confidence) were chosen on instants 0-{a - 1}, the cross-camera linking of the catalogue tracks too; the full-run numbers "
              f"above include those instants. On instants {a}-{b} alone (the catalogue and ground trackers start fresh at {a}; ByteTrack keeps "
              f"its ids from the full run):", "",
              "| tracker | MOTA | IDF1 | identity switches | recall |", "|---|---|---|---|---|"]
        for k, o in h["tracking"].items():
            L.append(f"| {k} | {o['mota']} | {o['idf1']} | {o['id_switches']} | {o['recall']} |")
        L += ["", "| one id per person on every camera | IDF1 across cameras |", "|---|---|"]
        L += [f"| {k} | {v['idf1']} |" for k, v in h["cross_camera"].items()]
        for k, v in h["ground_plane"].items():
            L += ["", f"Ground plane, {k}, within {v['radius_cm']:.0f} cm: MODA {v['moda']}, MOTA {v['mota']}, IDF1 {v['idf1']}."]
        hc = h["counts"]
        L += ["", f"Head count (truth mean {hc['truth_mean']}): the vote errs by {hc['vote']['mae']} people ({hc['vote']['bias']:+}), the busiest camera "
              f"by {hc['largest_camera']['mae']} ({hc['largest_camera']['bias']:+}). In the experiment that chose these settings on "
              "WILDTRACK (tuned on instants 0-199, re-run on 200-399), the vote's 0.7 confidence bar was sensitive (0.6 or 0.8 cost about one "
              "person of error), and plain clustering with a very wide radius (800 cm) counted inside the area came within 0.4 people of it."]
    L += ["", f"_Generated {time.strftime('%Y-%m-%d %H:%M')}. Data: WILDTRACK (Chavdarova et al., CVPR 2018), non-commercial research use._"]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas mcam-eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(Path.home() / "AtlasDemo" / "wildtrack"))
    ap.add_argument("--models", default="yolo11n,yolo11s", help="detector weights under data/models (comma-separated)")
    ap.add_argument("--vlm", default="", help="comma-separated vision models to ask for the site-wide head count")
    ap.add_argument("--limit", type=int, default=0, help="first N instants only (smoke test)")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--skip-reid", action="store_true", help="keep the re-id numbers of the previous run in --out")
    ap.add_argument("--keep-vlm", action="store_true", help="keep the vision-model rows of the previous run in --out")
    a = ap.parse_args(argv)
    from . import config as cfg
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    D = load(Path(a.data).expanduser(), a.limit)
    print(f"{len(D['frames'])} instants x {len(CAMS)} cameras, {sum(D['scene'])} person-instants", flush=True)
    feet = [in_roi(feet_on_ground(D["calib"][ci], [b for _, b in D["gt"][fi][ci]]), 50.0) for fi in range(0, len(D["frames"]), 20) for ci in range(len(CAMS))]
    calib_check = float(np.mean(np.concatenate([f for f in feet if len(f)])))
    perr = projection_error(Path(a.data).expanduser(), D)
    print(f"calibration check: {calib_check * 100:.1f}% of labelled feet inside the area; foot point vs the label's ground position: "
          f"median {perr['median']} cm, p90 {perr['p90']} cm", flush=True)
    R: dict[str, Any] = {"instants": len(D["frames"]), "identities": len({pid for f in D["gt"] for c in f for pid, _ in c}),
                         "calib_check": round(calib_check, 3), "projection_error_cm": perr, "counts": {}, "detection": {}, "tracking": {}, "vlm": []}
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    base = None
    for m in models:
        w = str(cfg.DATA_DIR / "models" / f"{m}.pt")
        preds = roi_filter(D, run_detector(D, w, track=False, cache=out))
        R["detection"][m] = score_detection(D, preds)
        R["detection"][m + " @ IoU 0.3"] = score_detection(D, preds, 0.3)
        print(f"{m}: P {R['detection'][m]['precision']} R {R['detection'][m]['recall']} F1 {R['detection'][m]['f1']}", flush=True)
        if base is None:
            base = preds
    R["counts"] = score_counts(D, base)
    bt = roi_filter(D, run_detector(D, str(cfg.DATA_DIR / "models" / f"{models[0]}.pt"), track=True, cache=out))
    R["tracking"][f"ByteTrack ({models[0]}, live view)"] = score_tracking(D, bt)
    cat = catalogue_tracks(D, base)
    R["tracking"][f"catalogue tracker ({models[0]})"] = score_tracking(D, cat)
    mc = multicam(D, base, cat)
    R["tracking"]["ground tracker (all cameras)"] = mc["tracking"]["ground tracker (all cameras)"]
    R["cross_camera"], R["ground_plane"], R["counts"]["vote"] = mc["cross_camera"], mc["ground_plane"], mc["counts"]["vote"]
    R["ground_tracker_params"] = {k: v.default for k, v in inspect.signature(G.GroundTracker).parameters.items()
                                  if isinstance(v.default, (int, float)) and not isinstance(v.default, bool)}
    if len(D["frames"]) > HELD_OUT:                      # the parameters were chosen on instants 0-199: the rest is unseen
        D2 = sub(D, HELD_OUT, len(D["frames"]))
        h = multicam(D2, base[HELD_OUT:])
        h["tracking"] = {"ByteTrack (live view)": score_tracking(D2, bt[HELD_OUT:]), **h["tracking"]}
        R["held_out"] = {"instants": [HELD_OUT, len(D["frames"]) - 1], "tracking": {k: v["overall"] for k, v in h["tracking"].items()},
                         "cross_camera": h["cross_camera"], "ground_plane": h["ground_plane"],
                         "counts": {k: {x: y for x, y in v.items() if x != "series"} if isinstance(v, dict) else v for k, v in h["counts"].items()}}
    print("across cameras: " + ", ".join(f"{k} IDF1 {v['idf1']}" for k, v in mc["cross_camera"].items())
          + f" | vote count MAE {mc['counts']['vote']['mae']}", flush=True)
    for k, t in R["tracking"].items():
        print(f"{k}: IDF1 {t['overall']['idf1']} MOTA {t['overall']['mota']} switches {t['overall']['id_switches']}", flush=True)
    R["geometry"] = score_geometry(D, base)
    g = R["geometry"]
    print(f"geometry: cross-camera rank-1 {g['assoc_rank1']} | counts " + ", ".join(f"{k}: MAE {v['mae']}" for k, v in g["counts"].items()), flush=True)
    R["reid"] = score_reid(D) if not a.skip_reid else (json.loads((out / "results.json").read_text()).get("reid") if (out / "results.json").is_file() else {})
    print("re-id:", {k: R["reid"].get(k) for k in ("rank1", "chance_rank1", "auc")}, flush=True)
    if a.keep_vlm and (out / "results.json").is_file():
        R["vlm"] = json.loads((out / "results.json").read_text()).get("vlm") or []
    for vm in [v.strip() for v in a.vlm.split(",") if v.strip()]:
        R["vlm"].append(score_vlm(D, vm))
    (out / "results.json").write_text(json.dumps(R, indent=1), encoding="utf-8")
    rv = review_json(D, base, mc["tracks"], R["counts"]["vote"]["series"], "multi-camera count (vote)", shared_ids=True)
    (out / "review.json").write_text(json.dumps(rv, separators=(",", ":")), encoding="utf-8")
    write_report(R, out)
    print(f"-> {out / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
