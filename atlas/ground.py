"""People on the ground plane, seen by several calibrated cameras at once: one tracker for all cameras, and a head count.

A sighting is a detection's foot point on the ground (z = 0, in cm, through its camera's calibration). At each instant
the sightings are merged into people by where the feet land (one camera sees a person once); people are then followed
over time on the ground, so one person carries the SAME id on every camera that sees them. Nothing here knows a dataset:
callers pass foot points per camera (or boxes and a `feet_fn`), confidences, and the area to count in.

    trk = GroundTracker()
    ids = trk.update([feet_cam0, feet_cam1, ...], [conf_cam0, conf_cam1, ...], t)   # per camera: an id per sighting, or None
    people = scene_count([feet_cam0, ...], [conf_cam0, ...], roi=(x0, x1, y0, y1))  # (n, 2) centres of the people counted
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment


def cluster(pts: np.ndarray, cams: Sequence[int], R: float, w: np.ndarray | None = None) -> list[list[int]]:
    """Greedy agglomeration by ground distance: merge the closest pair of clusters while their centres are within R cm and
    they share no camera (one camera sees a person once). Centres are weighted by `w` (default: one per sighting).
    Returns the members (indices into pts) of each cluster."""
    n = len(pts)
    if not n:
        return []
    C = np.asarray(pts, float).reshape(-1, 2).copy()
    W = np.ones(n) if w is None else np.asarray(w, float).copy()
    cams = list(cams)
    mem, cs, alive = [[i] for i in range(n)], [{cams[i]} for i in range(n)], np.ones(n, bool)
    d = np.linalg.norm(C[:, None] - C[None], axis=2)
    d[np.equal.outer(cams, cams)] = np.inf                       # also the diagonal
    while True:
        i, j = divmod(int(np.argmin(d)), n)
        if not d[i, j] < R:
            break
        C[i] = (C[i] * W[i] + C[j] * W[j]) / (W[i] + W[j]); W[i] += W[j]
        mem[i] += mem[j]; cs[i] |= cs[j]; alive[j] = False
        d[j, :] = d[:, j] = np.inf
        live = np.flatnonzero(alive)
        nd = np.linalg.norm(C[live] - C[i], axis=1)
        nd[[bool(cs[i] & cs[k]) for k in live]] = np.inf
        d[i, live] = d[live, i] = nd
    return [mem[i] for i in np.flatnonzero(alive)]


def gather(feet: Sequence[np.ndarray], conf: Sequence[Sequence[float]] | None = None) -> tuple[np.ndarray, list[int], np.ndarray]:
    """Per-camera foot points (and confidences) -> one list of sightings: points (n, 2), camera of each, confidence of each."""
    pts = [np.asarray(f, float).reshape(-1, 2) for f in feet]
    cams = [ci for ci, p in enumerate(pts) for _ in range(len(p))]
    if conf is not None and [len(np.asarray(c).reshape(-1)) for c in conf] != [len(p) for p in pts]:
        raise ValueError("one confidence per foot point, camera by camera")
    cf = np.concatenate([np.asarray(c, float).reshape(-1) for c in conf]) if conf is not None and len(cams) else np.ones(len(cams))
    # weights for the cluster centres: a zero-confidence sighting must not zero a whole cluster's weight
    return (np.concatenate(pts) if pts else np.zeros((0, 2))), cams, np.clip(cf, 1e-6, None)


def boxes_to_feet(calib: Sequence[Any], boxes: Sequence[Sequence[Sequence[float]]], feet_fn: Callable) -> list[np.ndarray]:
    """Per camera: feet_fn(calibration, boxes) -> (n, 2) ground points."""
    return [np.asarray(feet_fn(cal, list(b)), float).reshape(-1, 2) if len(b) else np.zeros((0, 2)) for cal, b in zip(calib, boxes)]


def scene_count(feet: Sequence[np.ndarray], conf: Sequence[Sequence[float]], roi: tuple[float, float, float, float] | None = None,
                R: float = 150.0, min_cams: int = 3, single_conf: float = 0.7) -> np.ndarray:
    """How many people are in the scene: sightings merged at R cm; a person counts when min_cams cameras saw them or one
    detection of them is at least single_conf sure (a false detection is rarely confirmed by other cameras), and when the
    centre lies inside roi (x0, x1, y0, y1). Returns the (n, 2) centres of the people counted."""
    pts, cams, cf = gather(feet, conf)
    keep = []
    for m in cluster(pts, cams, R):
        if len({cams[i] for i in m}) >= min_cams or cf[m].max() >= single_conf:
            keep.append(pts[m].mean(0))
    P = np.array(keep).reshape(-1, 2)
    if roi is not None and len(P):
        x0, x1, y0, y1 = roi
        P = P[(P[:, 0] >= x0) & (P[:, 0] <= x1) & (P[:, 1] >= y0) & (P[:, 1] <= y1)]
    return P


class Kalman:
    """Constant-velocity Kalman filter on the ground: state (x, y, vx, vy) in cm and cm/s; q is the acceleration noise
    (cm/s^2), r the measurement noise (cm)."""

    H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], float)

    def __init__(self, xy: Sequence[float], q: float = 15.0, r: float = 30.0):
        self.x = np.array([xy[0], xy[1], 0.0, 0.0])
        self.P = np.diag([r ** 2, r ** 2, 100.0 ** 2, 100.0 ** 2])
        self.q, self.Rm = q, np.eye(2) * r ** 2

    def predict(self, dt: float) -> np.ndarray:
        F = np.eye(4); F[0, 2] = F[1, 3] = dt
        G = np.array([[dt ** 2 / 2, 0], [0, dt ** 2 / 2], [dt, 0], [0, dt]])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + G @ G.T * self.q ** 2
        return self.x[:2]

    def update(self, z: Sequence[float]) -> None:
        S = self.H @ self.P @ self.H.T + self.Rm
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ (np.asarray(z, float) - self.H @ self.x)
        self.P = (np.eye(4) - K @ self.H) @ self.P


class GroundTracker:
    """One tracker for every calibrated camera. Per instant: sightings merged into people (cluster, R cm, centres weighted
    by confidence); people linked to tracks by Hungarian assignment on the ground against the Kalman prediction (within
    `gate` cm; confirmed tracks first); a track survives `max_miss` unseen instants; a new track starts only from a person
    seen by `birth_cams` cameras and is confirmed after `min_hits` consecutive instants.

    update() is online: a sighting of a track not yet confirmed gets None. With retro=True, the instants a track waited
    for confirmation are labelled afterwards: `self.backfill` then lists, for this update, (step, camera, index, id) of
    those past sightings and `self.ground_backfill` (step, id, x, y). `self.ground` holds this instant's (id, x, y) for
    confirmed people seen by at least `ground_min_cams` cameras.
    Defaults were tuned on WILDTRACK instants 0-199 (7 cameras, 2 instants a second)."""

    def __init__(self, R: float = 120.0, gate: float = 120.0, max_miss: int = 3, min_hits: int = 3, birth_cams: int = 2,
                 q: float = 15.0, r: float = 30.0, dt: float = 0.5, retro: bool = False, ground_min_cams: int = 2):
        self.R, self.gate, self.max_miss, self.min_hits, self.birth_cams = R, gate, max_miss, min_hits, birth_cams
        self.q, self.r, self.dt, self.retro, self.ground_min_cams = q, r, dt, retro, ground_min_cams
        self.tracks: list[dict] = []
        self.next_id, self.step, self.t = 1, -1, None
        self.ground: list[tuple[int, float, float]] = []
        self.backfill: list[tuple[int, int, int, int]] = []
        self.ground_backfill: list[tuple[int, int, float, float]] = []

    def update(self, feet: Sequence[np.ndarray], conf: Sequence[Sequence[float]] | None = None, t: float | None = None) -> list[list[int | None]]:
        """feet: per camera (n_c, 2) ground points; conf: per camera n_c confidences; t: seconds (default: one dt later).
        Returns per camera the track id of each sighting, None when it belongs to no confirmed track."""
        dt = self.dt if t is None or self.t is None else max(1e-3, t - self.t)
        self.t, self.step = t, self.step + 1
        self.ground, self.backfill, self.ground_backfill = [], [], []
        pts, cams, cf = gather(feet, conf)
        ref = [(ci, j) for ci, f in enumerate(feet) for j in range(len(np.asarray(f).reshape(-1, 2)))]
        cls = cluster(pts, cams, self.R, cf)
        cent = np.array([np.average(pts[m], axis=0, weights=cf[m]) for m in cls]).reshape(-1, 2)
        ncams = [len({cams[i] for i in m}) for m in cls]
        ids: list[list[int | None]] = [[None] * len(np.asarray(f).reshape(-1, 2)) for f in feet]
        for tr in self.tracks:
            tr["pred"] = tr["kf"].predict(dt)
        used: dict[int, int] = {}                                  # cluster -> track index
        for stage in (True, False):
            T = [k for k, tr in enumerate(self.tracks) if tr["confirmed"] == stage and k not in used.values()]
            U = [k for k in range(len(cls)) if k not in used]
            if not T or not U:
                continue
            D = np.linalg.norm(np.array([self.tracks[k]["pred"] for k in T])[:, None] - cent[U][None], axis=2)
            cost = np.where(D <= self.gate, D, 1e6)
            for a, b in zip(*linear_sum_assignment(cost)):
                if cost[a, b] < 1e6:
                    used[U[b]] = T[a]
        matched = {v: k for k, v in used.items()}
        keep = []
        for k, tr in enumerate(self.tracks):
            c = matched.get(k)
            if c is None:
                tr["miss"] += 1
                if tr["confirmed"] and tr["miss"] <= self.max_miss:
                    keep.append(tr)
                continue
            tr["kf"].update(cent[c]); tr["miss"] = 0; tr["hits"] += 1
            if not tr["confirmed"] and tr["hits"] >= self.min_hits:
                tr["confirmed"] = True
                if self.retro:
                    for step, members, xy, nc in tr["pending"]:
                        self.backfill += [(step, ci, j, tr["id"]) for ci, j in members]
                        if nc >= self.ground_min_cams:
                            self.ground_backfill.append((step, tr["id"], float(xy[0]), float(xy[1])))
                tr["pending"] = []
            self._emit(tr, cls[c], cent[c], ncams[c], ref, ids)
            keep.append(tr)
        for c in range(len(cls)):
            if c in used or ncams[c] < self.birth_cams:
                continue
            tr = {"id": self.next_id, "kf": Kalman(cent[c], self.q, self.r), "miss": 0, "hits": 1, "confirmed": self.min_hits <= 1, "pending": []}
            self.next_id += 1
            self._emit(tr, cls[c], cent[c], ncams[c], ref, ids)
            keep.append(tr)
        self.tracks = keep
        return ids

    def _emit(self, tr: dict, members: list[int], xy: np.ndarray, nc: int, ref: list[tuple[int, int]], ids: list[list[int | None]]) -> None:
        if not tr["confirmed"]:
            tr["pending"].append((self.step, [ref[i] for i in members], xy.copy(), nc))
            return
        for i in members:
            ci, j = ref[i]
            ids[ci][j] = tr["id"]
        if nc >= self.ground_min_cams:
            self.ground.append((tr["id"], float(xy[0]), float(xy[1])))


def track(feet: Sequence[Sequence[np.ndarray]], conf: Sequence[Sequence[Sequence[float]]] | None = None, times: Sequence[float] | None = None,
          **params: Any) -> tuple[list[list[list[int | None]]], list[list[tuple[int, float, float]]]]:
    """A whole recording at once: feet[instant][camera] -> (ids[instant][camera][sighting], ground[instant] -> [(id, x, y)]),
    with the retro-labelling applied (GroundTracker(retro=True) unless params say otherwise)."""
    trk = GroundTracker(**{"retro": True, **params})
    ids, ground = [], []
    for k, f in enumerate(feet):
        ids.append(trk.update(f, conf[k] if conf is not None else None, times[k] if times is not None else None))
        ground.append(list(trk.ground))
        for step, ci, j, tid in trk.backfill:
            ids[step][ci][j] = tid
        for step, tid, x, y in trk.ground_backfill:
            ground[step].append((tid, x, y))
    return ids, ground
