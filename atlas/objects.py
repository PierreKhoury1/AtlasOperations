"""Object catalogue: every distinct thing a camera saw, tracked, cross-checked, and open to questions.

Per camera, one worker reads the shared live decoder at the analysis rate (OBJ_FPS, default 2 a second) and runs

    precise detector (full frame + tiles)  ->  tracker (one identity per physical thing while it stays in view)
    ->  catalogue row once the track has earned it (>= MIN_HITS sightings, mean confidence >= MIN_CONF)
    ->  second opinion on the best crop (local CLIP zero-shot; a confident disagreement marks the object "disputed")
    ->  the owner can CALL an object: the vision model describes it, says whether the label is right, and keeps
        following it (a note every FOLLOW_S seconds while it stays in view); questions are answered from its crops.

Accuracy rules this module keeps:
  * nothing is catalogued from a single frame;
  * a label is a vote over the track's life, not the last frame's guess;
  * every object carries who vouched for it: detector only / clip agrees / vision model / the owner;
  * "same as" links across time or cameras are similarity suggestions with their score, never merged silently;
  * `py -m atlas audit` scores all of it against human ground truth (see audit.py).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import config as cfg

OBJ_DIR = cfg.DATA_DIR / "objects"
OBJ_FPS = float(os.environ.get("OBJ_FPS", "2"))
MIN_HITS = int(os.environ.get("OBJ_MIN_HITS", "3"))
MIN_CONF = float(os.environ.get("OBJ_MIN_CONF", "0.45"))
MAX_AGE_S = float(os.environ.get("OBJ_MAX_AGE_S", "6"))
FOLLOW_S = float(os.environ.get("OBJ_FOLLOW_S", "25"))
KEEP = int(os.environ.get("OBJ_KEEP", "3000"))              # catalogue rows kept per desk (watched ones are never pruned)
NAME_MATCH = float(os.environ.get("OBJ_NAME_MATCH", "0.86"))   # cosine to a named thing's exemplars before a sighting gets its name
NAME_SURE = float(os.environ.get("OBJ_NAME_SURE", "0.92"))     # above this the journal says the name plainly, below "appears to be"

# labels that are the same kind of thing for tracking (a van flips between car and truck frame to frame)
GROUPS = {"car": "vehicle", "truck": "vehicle", "bus": "vehicle", "motorcycle": "two-wheeler", "bicycle": "two-wheeler",
          "backpack": "bag", "handbag": "bag", "suitcase": "bag", "couch": "seat", "chair": "seat", "bench": "seat",
          "tv": "screen", "laptop": "screen", "cup": "drink", "bottle": "drink", "wine glass": "drink"}


def group(label: str) -> str:
    return GROUPS.get(label, label)


def iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def dedupe(dets: list[dict[str, Any]], thr: float = 0.7) -> list[dict[str, Any]]:
    """One box per physical thing: NMS is per class, so a van comes back as a car AND a truck. Best first wins."""
    out: list[dict[str, Any]] = []
    for d in sorted(dets, key=lambda d: -d["conf"]):
        if not any(group(o["label"]) == group(d["label"]) and iou(o["box"], d["box"]) >= thr for o in out):
            out.append(d)
    return out


# ---------------------------------------------------------------------------- tracking
class Track:
    def __init__(self, tid: int, det: dict[str, Any], ts: float):
        self.id = tid
        self.box = [float(v) for v in det["box"]]
        self.vel = [0.0, 0.0]                              # px / s, centre
        self.votes: dict[str, float] = {det["label"]: float(det["conf"])}
        self.hits = 1
        self.conf_sum = float(det["conf"])
        self.best_conf = float(det["conf"])
        self.first_ts = self.last_ts = ts
        self.first_box = list(self.box)
        self.path: list[list[float]] = [[round(ts, 2), *self.centre]]
        self.best_score = -1.0                             # best crop so far (scored by the worker)
        self.obj_id: int | None = None
        self.saved_ts = 0.0

    @property
    def centre(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    @property
    def label(self) -> str:
        return max(self.votes, key=self.votes.get)

    @property
    def label_conf(self) -> float:
        """Share of the confidence-weighted votes the winning label holds (1.0 = never called anything else)."""
        return self.votes[self.label] / (sum(self.votes.values()) or 1.0)

    @property
    def mean_conf(self) -> float:
        return self.conf_sum / self.hits

    def predicted(self, ts: float) -> list[float]:
        dt = min(3.0, max(0.0, ts - self.last_ts))
        dx, dy = self.vel[0] * dt, self.vel[1] * dt
        return [self.box[0] + dx, self.box[1] + dy, self.box[2] + dx, self.box[3] + dy]

    def absorb(self, det: dict[str, Any], ts: float) -> None:
        cx0, cy0 = self.centre
        self.box = [float(v) for v in det["box"]]
        dt = ts - self.last_ts
        if dt > 1e-3:
            cx, cy = self.centre
            self.vel = [0.6 * self.vel[0] + 0.4 * (cx - cx0) / dt, 0.6 * self.vel[1] + 0.4 * (cy - cy0) / dt]
        self.votes[det["label"]] = self.votes.get(det["label"], 0.0) + float(det["conf"])
        self.hits += 1
        self.conf_sum += float(det["conf"])
        self.best_conf = max(self.best_conf, float(det["conf"]))
        self.last_ts = ts
        if not self.path or ts - self.path[-1][0] >= 1.0:
            self.path.append([round(ts, 2), *[round(v, 1) for v in self.centre]])
            if len(self.path) > 240:
                self.path = self.path[::2]


class Tracker:
    """Greedy IoU tracker with a constant-velocity guess, made for a low analysis rate (a few frames a second), where
    a walking person's boxes still overlap frame to frame but a Kalman filter tuned for 30 fps does not help."""

    def __init__(self, max_age_s: float = MAX_AGE_S, min_hits: int = MIN_HITS, min_conf: float = MIN_CONF):
        self.max_age_s, self.min_hits, self.min_conf = max_age_s, min_hits, min_conf
        self.tracks: list[Track] = []
        self._next = 1

    def confirmed(self, t: Track) -> bool:
        return t.hits >= self.min_hits and t.mean_conf >= self.min_conf

    def reset(self) -> list[Track]:
        gone, self.tracks = self.tracks, []
        return gone

    def update(self, dets: list[dict[str, Any]], ts: float) -> tuple[list[Track], list[Track]]:
        """Returns (tracks that were seen in this frame, tracks that just ended)."""
        pairs: list[tuple[float, int, int]] = []
        for ti, t in enumerate(self.tracks):
            pb = t.predicted(ts)
            size = max(8.0, ((pb[2] - pb[0]) * (pb[3] - pb[1])) ** 0.5)
            pcx, pcy = (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2
            for di, d in enumerate(dets):
                if group(d["label"]) != group(t.label):
                    continue
                b = d["box"]
                score = iou(pb, b)
                if score < 0.15:                           # small or fast things: boxes stop overlapping, centres stay close
                    dist = (((b[0] + b[2]) / 2 - pcx) ** 2 + ((b[1] + b[3]) / 2 - pcy) ** 2) ** 0.5 / size
                    dsize = max(8.0, ((b[2] - b[0]) * (b[3] - b[1])) ** 0.5)
                    score = 0.14 * (1 - dist / 1.2) if dist < 1.2 and 0.5 < dsize / size < 2.0 else 0.0
                if score > 0.02:
                    pairs.append((score, ti, di))
        pairs.sort(reverse=True)
        used_t: set[int] = set()
        used_d: set[int] = set()
        seen: list[Track] = []
        for _score, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti); used_d.add(di)
            self.tracks[ti].absorb(dets[di], ts)
            seen.append(self.tracks[ti])
        for di, d in enumerate(dets):
            if di not in used_d:
                t = Track(self._next, d, ts)
                self._next += 1
                self.tracks.append(t)
                seen.append(t)
        ended = [t for t in self.tracks if ts - t.last_ts > self.max_age_s]
        self.tracks = [t for t in self.tracks if ts - t.last_ts <= self.max_age_s]
        return seen, ended


# ---------------------------------------------------------------------------- second opinion (local, free)
_SECOND_LABELS = ["person", "car", "truck", "bus", "bicycle", "motorcycle", "dog", "cat", "bird", "backpack", "handbag",
                  "suitcase", "chair", "armchair", "couch", "bench", "table", "potted plant", "tv screen", "laptop",
                  "bottle", "cup", "trash bin", "door", "sign", "lamp", "construction machine", "trailer", "wall or floor"]
_SECOND_ALIAS = {"tv screen": "tv", "armchair": "chair", "table": "dining table"}
_second_lock = threading.Lock()
_second_vecs = None


def second_opinion(jpeg: bytes, label: str, embedder=None) -> dict[str, Any]:
    """CLIP zero-shot on the crop: {"label", "score", "agrees", "emb"}. Empty dict when no image embedder is loaded."""
    global _second_vecs
    import numpy as np
    if embedder is None:
        from . import rag
        embedder = rag.get_embedder()
    if not str(getattr(embedder, "name", "")).startswith("clip"):      # only a real image-text model gets a vote
        return {}
    v = embedder.embed_images([jpeg])
    if v is None or not len(v):
        return {}
    names = list(_SECOND_LABELS)
    if label not in names and _SECOND_ALIAS.get(label, label) not in [_SECOND_ALIAS.get(n, n) for n in names]:
        names.append(label)
    with _second_lock:
        if _second_vecs is None or _second_vecs[0] != names:
            _second_vecs = (names, embedder.embed_texts([f"a photo of a {n}" for n in names]))
        tv = _second_vecs[1]
    logits = 100.0 * (tv @ v[0])
    p = np.exp(logits - logits.max())
    p /= p.sum()
    k = int(p.argmax())
    top = _SECOND_ALIAS.get(names[k], names[k])
    same = group(top) == group(label)
    p_label = float(sum(p[i] for i, n in enumerate(names) if group(_SECOND_ALIAS.get(n, n)) == group(label)))
    return {"label": top, "score": round(float(p[k]), 3), "p_label": round(p_label, 3), "agrees": same or p_label >= 0.35,
            "emb": v[0].astype("float32").tobytes()}


def verdict_from(second: dict[str, Any]) -> tuple[str, str]:
    """(verdict, by). Local opinion only confirms or disputes; rejecting is left to the vision model or the owner."""
    if not second:
        return "", ""
    if second["agrees"]:
        return "confirmed", "clip"
    if second["score"] >= 0.6 and second.get("p_label", 0) < 0.15:
        return "disputed", "clip"
    return "", ""


# ---------------------------------------------------------------------------- crops on disk
def _crop(frame, box, margin: float = 0.18):
    h, w = frame.shape[:2]
    bw, bh = box[2] - box[0], box[3] - box[1]
    x1, y1 = max(0, int(box[0] - bw * margin)), max(0, int(box[1] - bh * margin))
    x2, y2 = min(w, int(box[2] + bw * margin)), min(h, int(box[3] + bh * margin))
    return frame[y1:y2, x1:x2]


def _jpeg(img, q: int = 90, min_side: int = 0) -> bytes:
    import cv2
    if min_side and min(img.shape[:2]) < min_side:          # tiny far-away crops: enlarge so a vision model can read them
        s = min(4.0, min_side / max(1, min(img.shape[:2])))
        img = cv2.resize(img, (int(img.shape[1] * s), int(img.shape[0] * s)), interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    return buf.tobytes() if ok else b""


def scene_jpeg(frame, box, label: str, max_w: int = 1280) -> bytes:
    """The whole frame with this one object outlined: context for the vision model and the page."""
    import cv2
    out = frame.copy()
    x1, y1, x2, y2 = [int(v) for v in box]
    t = max(2, out.shape[1] // 480)
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), t)
    cv2.putText(out, label, (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, max(0.6, out.shape[1] / 1600), (0, 0, 255), t)
    if out.shape[1] > max_w:
        s = max_w / out.shape[1]
        out = cv2.resize(out, (max_w, int(out.shape[0] * s)), interpolation=cv2.INTER_AREA)
    return _jpeg(out, 85)


def obj_dir(desk_id: int) -> Path:
    d = OBJ_DIR / f"desk{desk_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------- the per-camera worker
_workers: dict[tuple[int, str], "ObjectWorker"] = {}
_workers_lock = threading.Lock()


class ObjectWorker:
    def __init__(self, store, desk_id: int, camera: str, source: str, vlm_model: str = "", live: bool = True,
                 detector=None, fps: float = OBJ_FPS):
        self.store, self.desk_id, self.camera, self.source = store, desk_id, camera, source
        self.vlm_model, self.live, self.fps = vlm_model, live, fps
        self.ds = store.for_desk(desk_id)
        self.detector = detector
        self.tracker = Tracker()
        self.touched = time.time()
        self.stats = {"frames": 0, "detect_ms": 0.0, "active": 0, "catalogued": 0, "error": ""}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_pos = 0.0
        self._follow: dict[int, float] = {}               # object id -> last follow-up ts
        self._latest: dict[int, tuple[Any, list[float]]] = {}   # object id -> (frame, box) for calls that arrive mid-life

    # -- lifecycle
    def start(self) -> "ObjectWorker":
        self._thread = threading.Thread(target=self._run, name=f"objects:{self.camera}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        from . import live as LIVE
        from . import vision as V
        try:
            det = self.detector or V.PreciseDetector()
            feed = LIVE.open(self.source, self.camera)
        except Exception as exc:
            self.stats["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return
        self.ds.close_vision_objects(self.camera)          # rows left "active" by a previous process
        who = f"objects:{self.desk_id}:{self.camera}"
        last_seq = -1
        while not self._stop.is_set() and time.time() - self.touched < 180:
            t0 = time.time()
            feed.hold(who, 30)
            if not feed.running:
                feed = LIVE.open(self.source, self.camera)
            seq, frame, pos = feed.raw()
            if frame is None or seq == last_seq:
                time.sleep(0.1)
                continue
            last_seq = seq
            try:
                self.step(frame, time.time(), pos, det)
                self.stats["error"] = ""
            except Exception as exc:
                self.stats["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(max(0.0, 1.0 / self.fps - (time.time() - t0)))
        self._end(self.tracker.reset())
        with _workers_lock:
            if _workers.get((self.desk_id, self.camera)) is self:
                _workers.pop((self.desk_id, self.camera), None)

    # -- one analysed frame (also what the audit drives, frame by frame, with its own clock)
    def step(self, frame, ts: float, pos: float, det) -> list[Track]:
        if pos + 1.0 < self._last_pos:                     # a recording looped: everything in view is a new sighting
            self._end(self.tracker.reset())
        self._last_pos = pos
        t0 = time.time()
        dets = dedupe(det.detect_bgr(frame))
        self.stats["detect_ms"] = round((time.time() - t0) * 1000, 1)
        self.stats["frames"] += 1
        seen, ended = self.tracker.update(dets, ts)
        h, w = frame.shape[:2]
        for t in seen:
            if not self.tracker.confirmed(t):
                continue
            edge = t.box[0] <= 2 or t.box[1] <= 2 or t.box[2] >= w - 2 or t.box[3] >= h - 2
            score = t.best_conf * ((t.box[2] - t.box[0]) * (t.box[3] - t.box[1])) ** 0.5 * (0.5 if edge else 1.0)
            better = score > t.best_score * 1.15
            if t.obj_id is None:
                self._create(t, frame, pos, score)
            elif better or ts - t.saved_ts >= 2.0:
                self._save(t, frame if better else None, score if better else None)
            if t.obj_id is not None:
                self._latest[t.obj_id] = (frame, list(t.box))
        self._end(ended)
        self.stats["active"] = sum(1 for t in self.tracker.tracks if t.obj_id is not None)
        if self.live:
            self._follow_up(ts)
        return seen

    def _fields(self, t: Track) -> dict[str, Any]:
        return {"label": t.label, "label_conf": round(t.label_conf, 3), "votes": {k: round(v, 2) for k, v in t.votes.items()},
                "last_ts": t.last_ts, "hits": t.hits, "best_conf": round(t.best_conf, 3), "box": [int(v) for v in t.box],
                "path": t.path, "status": "active"}

    def _write_crops(self, oid: int, t: Track, frame) -> dict[str, Any]:
        d = obj_dir(self.desk_id)
        crop = _jpeg(_crop(frame, t.box), 92, min_side=160)
        (d / f"{oid}.jpg").write_bytes(crop)
        (d / f"{oid}-scene.jpg").write_bytes(scene_jpeg(frame, t.box, f"{t.label} #{oid}"))
        out: dict[str, Any] = {"crop": str(d / f"{oid}.jpg")}
        try:
            so = second_opinion(crop, t.label)
        except Exception:
            so = {}
        if so:
            verdict, by = verdict_from(so)
            out.update({"second_label": so["label"], "second_score": so["score"], "emb": so["emb"]})
            cur = self.ds.vision_object(oid) or {}
            if cur.get("verdict_by") not in ("vlm", "owner"):          # a stronger judge already spoke: keep it
                out.update({"verdict": verdict, "verdict_by": by})
            if cur.get("name_by") != "owner":                          # the owner's word on a name is final; matches may improve
                m = match_named(self.store, self.desk_id, so["emb"], t.label)
                if m:
                    out.update({"name_id": m["id"], "name_score": m["score"], "name_by": "match"})
                    if not cur.get("name_id"):
                        self.store.update_named(m["id"], sightings=int(m.get("sightings") or 0) + 1)
                elif cur.get("name_id"):
                    out.update({"name_id": 0, "name_score": 0.0, "name_by": ""})
        return out

    def _create(self, t: Track, frame, pos: float, score: float) -> None:
        h, w = frame.shape[:2]
        t.obj_id = self.ds.add_vision_object(self.camera, track_id=t.id, first_ts=t.first_ts, frame_w=w, frame_h=h,
                                             clip_pos=round(pos, 2), attrs={"first_box": [int(v) for v in t.first_box]},
                                             **self._fields(t))
        t.best_score, t.saved_ts = score, t.last_ts
        self.ds.update_vision_object(t.obj_id, **self._write_crops(t.obj_id, t, frame))
        self.stats["catalogued"] += 1
        if self.stats["catalogued"] % 50 == 0:
            prune(self.store, self.desk_id)

    def _save(self, t: Track, frame, score: float | None) -> None:
        f = self._fields(t)
        if frame is not None and score is not None:
            t.best_score = score
            f.update(self._write_crops(t.obj_id, t, frame))
        t.saved_ts = t.last_ts
        self.ds.update_vision_object(t.obj_id, **f)

    def _end(self, tracks: list[Track]) -> None:
        for t in tracks:
            if t.obj_id is not None:
                self.ds.update_vision_object(t.obj_id, **{**self._fields(t), "status": "gone"})
                self._latest.pop(t.obj_id, None)
                self._follow.pop(t.obj_id, None)

    # -- called objects: keep analysing while they stay in view
    def _follow_up(self, ts: float) -> None:
        for t in self.tracker.tracks:
            oid = t.obj_id
            if oid is None or ts - t.last_ts > 1.5 or ts - self._follow.get(oid, 0) < FOLLOW_S:
                continue
            o = self.ds.vision_object(oid)
            if not o or not o.get("watch"):
                continue
            self._follow[oid] = ts
            frame, box = self._latest.get(oid, (None, None))
            if frame is not None:
                threading.Thread(target=_safe, args=(follow, self.store, o, frame.copy(), box, self.vlm_model),
                                 daemon=True, name=f"follow:{oid}").start()

    def latest(self, oid: int):
        return self._latest.get(oid, (None, None))


def _safe(fn, *a) -> None:
    try:
        fn(*a)
    except Exception as exc:
        try:
            store, o = a[0], a[1]
            store.for_desk(o["desk_id"]).add_object_note(o["id"], "error", f"vision model unavailable: {str(exc)[:160]}")
        except Exception:
            pass


def ensure_worker(store, desk_id: int, conn: dict[str, Any], live: bool = True) -> ObjectWorker | None:
    """Called from every camera tick: start this camera's worker if its catalogue is on, keep it alive otherwise."""
    c = conn.get("config") or {}
    on = str(c.get("objects", c.get("journal", ""))).lower() in ("1", "true", "on", "yes")
    key = (desk_id, conn["name"])
    with _workers_lock:
        w = _workers.get(key)
        if not on:
            if w:
                w.stop()
            return None
        if w and w.running and w.source == str(c.get("source", "")):
            w.touched = time.time()
            w.vlm_model = str(c.get("vlm_model") or "")
            return w
        if w:
            w.stop()
        w = ObjectWorker(store, desk_id, conn["name"], str(c.get("source", "")), str(c.get("vlm_model") or ""), live).start()
        _workers[key] = w
        return w


def worker(desk_id: int, camera: str) -> ObjectWorker | None:
    return _workers.get((desk_id, camera))


def statuses(desk_id: int) -> dict[str, dict[str, Any]]:
    return {cam: {**w.stats, "running": w.running} for (d, cam), w in list(_workers.items()) if d == desk_id}


def stop_all() -> None:
    for w in list(_workers.values()):
        w.stop()


def prune(store, desk_id: int, keep: int = KEEP) -> int:
    rows = store.vision_objects(desk_id, limit=keep + 2000)
    n = 0
    for o in rows[keep:]:
        if o.get("watch") or o.get("verdict_by") == "owner":
            continue
        for suffix in ("", "-scene"):
            try:
                (obj_dir(desk_id) / f"{o['id']}{suffix}.jpg").unlink(missing_ok=True)
            except OSError:
                pass
        store._conn.execute("DELETE FROM vision_object_notes WHERE object_id=?", (o["id"],))
        store._conn.execute("DELETE FROM vision_objects WHERE id=?", (o["id"],))
        n += 1
    if n:
        store._conn.commit()
    return n


# ---------------------------------------------------------------------------- the vision model on one object
CALL_SYSTEM = """You examine ONE object that a camera's detector picked out. You get a close crop of it and the whole
scene with the object outlined in red. The detector's label can be wrong: say what the outlined thing really is.
Answer with one JSON object and nothing else:
{"is_label": true or false, "actual": "what it really is, two or three words",
 "description": "what it looks like: colours, clothing or make/type, anything carried or attached, size, condition",
 "doing": "what it is doing or its state right now, and where in the scene it is",
 "confidence": "high" | "medium" | "low"}
Only what is visible. Never guess identity, name, ethnicity or exact age. If the crop is too small or blurred to be
sure, say so in "description" and use "confidence": "low"."""

FOLLOW_SYSTEM = """You are following ONE object on a camera (outlined in red in the scene). You get your previous note
about it and a fresh crop plus the scene. In at most 45 words say what it is doing NOW and what changed since the
previous note (moved where, picked up or put down what, joined or left whom, stopped, started). If nothing changed say
"No change" and restate its position in a few words. Only what is visible. Plain text."""

ASK_SYSTEM = """You answer a question about ONE object seen by a camera. You get its crops (best view, latest view), the
scene with it outlined in red, what the detector and earlier notes say, and the question. Answer from what is visible
in the images; use the notes only for history. If the images cannot answer it, say that plainly. Under 80 words."""


def _loose_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _images(store, o: dict[str, Any], frame=None, box=None) -> list[tuple[str, bytes]]:
    d = obj_dir(o["desk_id"])
    imgs: list[tuple[str, bytes]] = []
    best = d / f"{o['id']}.jpg"
    if frame is not None and box is not None:
        imgs.append(("CROP NOW", _jpeg(_crop(frame, box), 92, min_side=160)))
        imgs.append(("SCENE NOW (object outlined in red)", scene_jpeg(frame, box, o["label"])))
        if best.is_file():
            imgs.append(("BEST EARLIER CROP", best.read_bytes()))
    else:
        if best.is_file():
            imgs.append(("CROP", best.read_bytes()))
        scene = d / f"{o['id']}-scene.jpg"
        if scene.is_file():
            imgs.append(("SCENE (object outlined in red)", scene.read_bytes()))
    return imgs


def _facts(o: dict[str, Any]) -> str:
    dur = max(0.0, (o.get("last_ts") or 0) - (o.get("first_ts") or 0))
    named = ""
    if o.get("name"):
        how = "named by the owner" if o.get("name_by") == "owner" else f"matched by appearance, {o.get('name_score', 0):.0%}"
        named = f" Known as: {o['name']} ({o.get('name_kind') or 'object'}; {how})."
    return (named + f"Camera: {o['camera']}. Detector label: {o['label']} (best confidence {o.get('best_conf', 0):.0%}, "
            f"{o.get('hits', 0)} sightings over {dur:.0f}s, label held {o.get('label_conf', 0):.0%} of the votes). "
            f"Local second opinion: {o.get('second_label') or 'none'} ({o.get('second_score', 0):.0%}). "
            f"First seen {time.strftime('%H:%M:%S', time.localtime(o.get('first_ts') or 0))}, "
            f"last seen {time.strftime('%H:%M:%S', time.localtime(o.get('last_ts') or 0))}, status {o.get('status')}.")


def call(store, o: dict[str, Any], model: str = "", frame=None, box=None, transport=None) -> dict[str, Any]:
    """The owner called this object: the vision model says what it really is and describes it. Sets the verdict."""
    from . import vision as V
    ds = store.for_desk(o["desk_id"])
    imgs = _images(store, o, frame, box)
    if not imgs:
        raise RuntimeError("no picture of this object is stored")
    raw = V.chat_images(CALL_SYSTEM, _facts(o) + "\n\nExamine the outlined object.", imgs, model=model, max_tokens=300, transport=transport)
    j = _loose_json(raw)
    if not j:
        ds.add_object_note(o["id"], "describe", raw.strip()[:900])
        return {"text": raw.strip()}
    fields: dict[str, Any] = {"description": str(j.get("description") or "")[:600],
                              "attrs": {**(o.get("attrs") or {}), "doing": str(j.get("doing") or "")[:300],
                                        "actual": str(j.get("actual") or "")[:60], "vlm_confidence": str(j.get("confidence") or "")[:10]}}
    if o.get("verdict_by") != "owner" and str(j.get("confidence") or "").lower() != "low":
        fields.update({"verdict": "confirmed" if j.get("is_label") else "rejected", "verdict_by": "vlm"})
    ds.update_vision_object(o["id"], **fields)
    text = (("" if j.get("is_label") else f"NOT a {o['label']}: it is {j.get('actual') or 'something else'}. ")
            + f"{j.get('description') or ''} {j.get('doing') or ''}".strip())
    ds.add_object_note(o["id"], "describe", text[:900])
    return {**j, "text": text}


def follow(store, o: dict[str, Any], frame, box, model: str = "", transport=None) -> str:
    from . import vision as V
    ds = store.for_desk(o["desk_id"])
    prev = next((n["text"] for n in ds.object_notes(o["id"], 10) if n["kind"] in ("follow", "describe")), "")
    text = V.chat_images(FOLLOW_SYSTEM, _facts(o) + f"\nPrevious note: {prev or 'none yet'}\n\nWhat is it doing now?",
                         _images(store, o, frame, box)[:2], model=model, max_tokens=140, transport=transport).strip()
    if text:
        ds.add_object_note(o["id"], "follow", text[:600])
    return text


def ask(store, o: dict[str, Any], question: str, model: str = "", frame=None, box=None, transport=None) -> str:
    from . import vision as V
    ds = store.for_desk(o["desk_id"])
    notes = "\n".join(f"- {time.strftime('%H:%M:%S', time.localtime(n['ts']))} {n['kind']}: {n['text']}"
                      for n in reversed(ds.object_notes(o["id"], 12)) if n["kind"] != "error")
    text = V.chat_images(ASK_SYSTEM, _facts(o) + (f"\nNotes so far:\n{notes}" if notes else "") + f"\n\nQuestion: {question}",
                         _images(store, o, frame, box), model=model, max_tokens=260, transport=transport).strip()
    ds.add_object_note(o["id"], "answer", text[:900], question=question[:300])
    return text


def similar(store, o: dict[str, Any], limit: int = 6, min_score: float = 0.8) -> list[dict[str, Any]]:
    """Other catalogue objects that look alike (CLIP on the best crops): candidates for "the same one, seen again or
    on another camera". A suggestion with its score, never a merge."""
    import numpy as np
    full = store.vision_object(o["id"], emb=True)
    if not full or not full.get("emb"):
        return []
    v = np.frombuffer(full["emb"], dtype="float32")
    out = []
    for c in store.vision_objects(o["desk_id"], limit=1500, emb=True):
        if c["id"] == o["id"] or not c.get("emb") or group(c["label"]) != group(o["label"]) or c.get("verdict") == "rejected":
            continue
        cv = np.frombuffer(c["emb"], dtype="float32")
        if cv.shape != v.shape:
            continue
        s = float(v @ cv)
        if s >= min_score:
            c.pop("emb", None)
            out.append({**c, "similarity": round(s, 3)})
    out.sort(key=lambda c: -c["similarity"])
    return out[:limit]


# ---------------------------------------------------------------------------- named things: the owner's registry
def _vec(b: bytes | None):
    import numpy as np
    if not b:
        return None
    v = np.frombuffer(b, dtype="float32")
    n = float(np.linalg.norm(v))
    return v / n if n else None


def match_named(store, desk_id: int, emb: bytes | None, label: str = "", min_score: float = NAME_MATCH) -> dict[str, Any] | None:
    """The named thing whose exemplars this sighting looks most like: {id, name, kind, score, sightings} or None.
    Appearance only (CLIP on the crop): clothing, build, colour, make, never a face. Same label group only, so a
    person is never matched to a van."""
    import numpy as np
    v = _vec(emb)
    if v is None:
        return None
    best, best_s = None, 0.0
    for n in store.named_things(desk_id, emb=True):
        if n.get("label") and label and group(n["label"]) != group(label):
            continue
        nv = _vec(n.get("emb"))
        if nv is None:
            continue
        s = float(np.dot(v, nv))
        if s > best_s:
            best, best_s = n, s
    if best is None or best_s < min_score:
        return None
    return {"id": best["id"], "name": best["name"], "kind": best["kind"], "score": round(best_s, 3), "sightings": best.get("sightings", 0)}


def _mean_emb(embs: list[bytes]) -> tuple[bytes | None, int]:
    import numpy as np
    vs = [_vec(e) for e in embs]
    vs = [v for v in vs if v is not None]
    if not vs:
        return None, 0
    m = np.mean(np.stack(vs), axis=0)
    n = float(np.linalg.norm(m))
    m = (m / n) if n else m
    return m.astype("float32").tobytes(), int(m.shape[0])


def name_object(store, o: dict[str, Any], name: str, kind: str = "", notes: str = "") -> dict[str, Any]:
    """The owner names this sighting: a new named thing, or one more exemplar for an existing name. The sighting's
    crop and embedding become an exemplar; the registry's embedding is the mean of its exemplars. Returns the thing."""
    ds = store.for_desk(o["desk_id"])
    full = store.vision_object(o["id"], emb=True) or o
    kind = kind or ("person" if o["label"] == "person" else "vehicle" if group(o["label"]) == "vehicle" else "object")
    thing = ds.named_by_name(name)
    ex = {"object_id": o["id"], "camera": o["camera"], "ts": o.get("last_ts"), "crop": o.get("crop") or "", "label": o["label"]}
    if thing is None:
        thing = ds.add_named(name, kind=kind, label=o["label"], notes=notes, exemplars=[ex], emb=full.get("emb"),
                             dim=len(full["emb"]) // 4 if full.get("emb") else 0)
    else:
        exemplars = [e for e in thing["exemplars"] if e.get("object_id") != o["id"]] + [ex]
        embs = [full["emb"]] if full.get("emb") else []
        for e in exemplars[:-1]:
            prev = store.vision_object(e["object_id"], emb=True)
            if prev and prev.get("emb"):
                embs.append(prev["emb"])
        emb, dim = _mean_emb(embs)
        f = {"exemplars": exemplars[-12:], "emb": emb, "dim": dim}
        if notes:
            f["notes"] = notes
        ds.update_named(thing["id"], **f)
        thing = ds.named(thing["id"])
    ds.update_vision_object(o["id"], name_id=thing["id"], name_score=1.0, name_by="owner")
    ds.add_object_note(o["id"], "verify", f"Owner named this {o['label']}: {thing['name']} ({thing['kind']}).")
    return thing


def unname_object(store, o: dict[str, Any]) -> None:
    """Not that one: the sighting loses its name and stops being an exemplar for it."""
    ds = store.for_desk(o["desk_id"])
    if o.get("name_id"):
        thing = ds.named(o["name_id"])
        if thing:
            exemplars = [e for e in thing["exemplars"] if e.get("object_id") != o["id"]]
            if len(exemplars) != len(thing["exemplars"]):
                embs = []
                for e in exemplars:
                    prev = store.vision_object(e["object_id"], emb=True)
                    if prev and prev.get("emb"):
                        embs.append(prev["emb"])
                emb, dim = _mean_emb(embs)
                ds.update_named(thing["id"], exemplars=exemplars, emb=emb, dim=dim)
            ds.add_object_note(o["id"], "verify", f"Owner says this is not {thing['name']}.")
    ds.update_vision_object(o["id"], name_id=0, name_score=0.0, name_by="owner-no")


def known_in_view(store, desk_id: int, camera: str, since_s: float = 20.0, now: float | None = None) -> list[dict[str, Any]]:
    """Named things seen on this camera in the last `since_s` seconds, for the journal: [{name, kind, score, sure, by}]."""
    now = now or time.time()
    ds = store.for_desk(desk_id)
    out: dict[int, dict[str, Any]] = {}
    for o in ds.vision_objects(camera=camera, since=now - since_s, named=True, limit=50):
        if o.get("name_by") == "owner-no":
            continue
        n = ds.named(o["name_id"])
        if not n:
            continue
        score = 1.0 if o.get("name_by") == "owner" else float(o.get("name_score") or 0)
        cur = out.get(n["id"])
        if cur is None or score > cur["score"]:
            out[n["id"]] = {"name": n["name"], "kind": n["kind"], "score": round(score, 2), "sure": score >= NAME_SURE,
                            "by": o.get("name_by") or "match", "notes": n.get("notes") or ""}
    return sorted(out.values(), key=lambda x: -x["score"])
