"""Atlas Vision Node - continuous camera inference that feeds an Atlas desk.

Runs anywhere with a CPU (Oracle A1 ARM, Hetzner, a NUC, a laptop): reads *every* frame of each camera
(RTSP / HTTP / webcam / file), runs YOLO through OpenVINO with ByteTrack tracking, and turns tracks into events:

    entered / left        a tracked object appeared or disappeared            (logged, no agent run)
    dwell                 an object stayed longer than dwell_s                 (agent run)
    count                 watched-label count reached min_count (rising edge)  (agent run, then cooldown)
    after_hours           a watched object is present outside `hours`         (agent run, then cooldown)
    heartbeat             periodic snapshot with current counts                (logged, keeps the RAG log fresh)

Only events - with one annotated keyframe - are POSTed to the desk's /hook/<token>/vision. Video never leaves the node.
The desk's VLM and agents decide what matters; the node only decides what is *new*.

Config: node.json next to this file, or ATLAS_NODE_CONFIG=<path>. ATLAS_HOOK overrides the hook URL. See node.example.json.

    python atlas_node.py                 run forever
    python atlas_node.py --check         connect to every camera for 10 s, report fps, exit
    python atlas_node.py --seconds 60    run for a minute (smoke test)
    python atlas_node.py --dry           print payloads instead of posting
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STOP = threading.Event()
VERSION = "0.1"

DEFAULTS: dict[str, Any] = {
    "hook": "",
    "model": "yolov8n.pt",
    "device": "openvino",        # openvino | cpu   (openvino = Intel/AMD/ARM CPU plugin; falls back to torch CPU)
    "imgsz": 640,
    "conf": 0.35,
    "vid_stride": 2,             # decode every frame, infer every Nth
    "heartbeat_min": 30,
    "health_port": 8765,
    "health_bind": "127.0.0.1",
    "max_posts_per_min": 20,     # the desk hook allows 30/min per token+IP
    "keyframe_side": 960,
    "cameras": [],
}
CAMERA_DEFAULTS: dict[str, Any] = {
    "watch_for": ["person"],
    "min_count": 1,
    "dwell_s": 0,                # 0 = off
    "hours": "",                 # "08:00-20:00" or overnight "22:00-06:00"; empty = always inside hours
    "alert_outside_hours": True,
    "cooldown_s": 120,
    "confirm_s": 1.0,            # a track must persist this long before it counts (kills flicker)
    "gone_s": 3.0,               # a track missing this long has left
}


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------------------------------ config
def load_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path or os.environ.get("ATLAS_NODE_CONFIG") or HERE / "node.json")
    cfg = dict(DEFAULTS)
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    if os.environ.get("ATLAS_HOOK"):
        cfg["hook"] = os.environ["ATLAS_HOOK"].strip()
    if os.environ.get("ATLAS_CAMERAS"):                       # JSON list, handy for docker -e
        cfg["cameras"] = json.loads(os.environ["ATLAS_CAMERAS"])
    if os.environ.get("ATLAS_HEALTH_BIND"):                   # 0.0.0.0 inside Docker so -p 127.0.0.1:8765 works
        cfg["health_bind"] = os.environ["ATLAS_HEALTH_BIND"]
    cams = []
    for i, c in enumerate(cfg.get("cameras") or []):
        cc = dict(CAMERA_DEFAULTS)
        cc.update(c)
        cc["name"] = str(cc.get("name") or f"camera{i + 1}")[:60]
        cc["watch_for"] = [str(w).strip().lower() for w in (cc.get("watch_for") or []) if str(w).strip()]
        cams.append(cc)
    cfg["cameras"] = cams
    return cfg


def parse_hours(spec: str) -> tuple[int, int] | None:
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return None
    try:
        a, b = spec.split("-", 1)
        h1, m1 = [int(x) for x in a.strip().split(":")]
        h2, m2 = [int(x) for x in b.strip().split(":")]
        return h1 * 60 + m1, h2 * 60 + m2
    except ValueError:
        return None


def in_hours(spec: str, now: datetime | None = None) -> bool:
    w = parse_hours(spec)
    if not w:
        return True
    start, end = w
    t = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    return start <= t < end if start <= end else (t >= start or t < end)


# ------------------------------------------------------------------------------------------ state machine
@dataclass
class Track:
    label: str
    first: float
    last: float
    confirmed: bool = False
    dwell_sent: bool = False


@dataclass
class Event:
    kind: str                       # entered | left | dwell | count | after_hours | heartbeat
    note: str
    trigger: bool
    counts: dict[str, int]
    track_id: int | None = None


@dataclass
class CameraState:
    """Turns per-frame tracks into events. Pure: no YOLO, no network, so it is unit-tested without a camera."""
    cfg: dict[str, Any]
    tracks: dict[int, Track] = field(default_factory=dict)
    last_alert: dict[str, float] = field(default_factory=dict)
    prev_counts: dict[str, int] = field(default_factory=dict)
    last_heartbeat: float = -1e18        # first heartbeat fires on connect: "node online" snapshot
    frames: int = 0

    @property
    def name(self) -> str:
        return self.cfg["name"]

    def _watched(self, label: str) -> bool:
        return not self.cfg["watch_for"] or label in self.cfg["watch_for"]

    def _cool(self, key: str, now: float) -> bool:
        return now - self.last_alert.get(key, -1e9) >= float(self.cfg["cooldown_s"])

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for t in self.tracks.values():
            if t.confirmed:
                c[t.label] = c.get(t.label, 0) + 1
        return dict(sorted(c.items()))

    def update(self, now: float, dets: list[dict[str, Any]], when: datetime | None = None) -> list[Event]:
        self.frames += 1
        events: list[Event] = []
        seen: set[int] = set()
        for d in dets:
            tid = d.get("id")
            if tid is None:
                continue
            seen.add(tid)
            label = str(d["label"]).lower()
            t = self.tracks.get(tid)
            if t is None:
                self.tracks[tid] = Track(label, now, now)
                continue
            t.last = now
            t.label = label
        # confirm / dwell
        for tid, t in self.tracks.items():
            if not t.confirmed and now - t.first >= float(self.cfg["confirm_s"]) and tid in seen:
                t.confirmed = True
                if self._watched(t.label):
                    events.append(Event("entered", f"{t.label} entered (track {tid})", False, {}, tid))
            dwell = float(self.cfg["dwell_s"] or 0)
            if t.confirmed and dwell > 0 and not t.dwell_sent and now - t.first >= dwell and self._watched(t.label):
                t.dwell_sent = True
                events.append(Event("dwell", f"{t.label} present for {int(now - t.first)}s (track {tid})", True, {}, tid))
        # gone
        for tid in [k for k, t in self.tracks.items() if now - t.last > float(self.cfg["gone_s"])]:
            t = self.tracks.pop(tid)
            if t.confirmed and self._watched(t.label):
                events.append(Event("left", f"{t.label} left after {int(now - t.first)}s (track {tid})", False, {}, tid))
        # counts / hours
        c = self.counts()
        watched = {k: v for k, v in c.items() if self._watched(k)}
        total = sum(watched.values())
        prev_total = sum(v for k, v in self.prev_counts.items() if self._watched(k))
        need = int(self.cfg["min_count"] or 1)
        if total >= need and self._cool("count", now):          # rising edge, then again every cooldown while it holds
            who = ", ".join(f"{v} {k}" for k, v in watched.items())
            still = "" if prev_total < need else " (still)"
            events.append(Event("count", f"{who} at once{still}", True, c))
            self.last_alert["count"] = now
        if total and self.cfg.get("hours") and self.cfg.get("alert_outside_hours", True) and not in_hours(self.cfg["hours"], when) and self._cool("after_hours", now):
            who = ", ".join(f"{v} {k}" for k, v in watched.items())
            events.append(Event("after_hours", f"{who} outside hours {self.cfg['hours']}", True, c))
            self.last_alert["after_hours"] = now
        self.prev_counts = c
        for e in events:
            if not e.counts:
                e.counts = c
        # heartbeat
        hb = float(self.cfg.get("heartbeat_min") or 0) * 60
        if hb > 0 and now - self.last_heartbeat >= hb:
            self.last_heartbeat = now
            events.append(Event("heartbeat", ", ".join(f"{v} {k}" for k, v in c.items()) or "nothing tracked", False, c))
        return events


# ------------------------------------------------------------------------------------------ posting
class Poster:
    """Background queue -> HTTP POST to the desk hook, with retries and a token bucket. Non-trigger events are
    dropped first when the bucket is empty; trigger events wait."""

    def __init__(self, hook: str, per_min: int = 20, dry: bool = False):
        self.hook, self.per_min, self.dry = hook, per_min, dry
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        self.sent = self.failed = self.dropped = 0
        self.last: dict[str, Any] = {}
        self._stamps: list[float] = []
        self._th = threading.Thread(target=self._run, name="poster", daemon=True)

    def start(self) -> "Poster":
        self._th.start()
        return self

    def _allow(self, now: float) -> bool:
        self._stamps = [t for t in self._stamps if now - t < 60]
        return len(self._stamps) < self.per_min

    def submit(self, payload: dict[str, Any]) -> bool:
        now = time.time()
        if not payload.get("trigger") and (not self._allow(now) or self.q.qsize() > 50):
            self.dropped += 1
            return False
        try:
            self.q.put_nowait(payload)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.dry or not self.hook:
            log("DRY", payload["camera"], payload["note"], "trigger" if payload["trigger"] else "log", payload["labels"])
            return {"ok": True, "dry": True}
        import httpx
        r = httpx.post(self.hook, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def _run(self) -> None:
        while not STOP.is_set() or not self.q.empty():
            try:
                p = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            while not self._allow(time.time()) and not STOP.is_set():
                time.sleep(1)
            for attempt in range(3):
                try:
                    res = self.post_once(p)
                    self._stamps.append(time.time())
                    self.sent += 1
                    self.last = {"camera": p["camera"], "note": p["note"], "at": time.time(), "event_id": res.get("event_id"), "run_id": res.get("run_id")}
                    log("POST", p["camera"], p["note"], "->", res.get("event_id", "ok"), ("run " + res["run_id"]) if res.get("run_id") else "")
                    break
                except Exception as exc:
                    log("post failed", attempt + 1, type(exc).__name__, str(exc)[:120])
                    time.sleep(2 * (attempt + 1))
            else:
                self.failed += 1


def keyframe(img_bgr, dets: list[dict[str, Any]], banner: str, max_side: int = 960) -> bytes:
    import cv2
    im = img_bgr.copy()
    for d in dets:
        x1, y1, x2, y2 = [int(v) for v in d["box"]]
        cv2.rectangle(im, (x1, y1), (x2, y2), (60, 220, 120), 2)
        tag = f"{d['label']}{' #' + str(d['id']) if d.get('id') is not None else ''} {d['conf']:.2f}"
        cv2.putText(im, tag, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 220, 120), 1, cv2.LINE_AA)
    h, w = im.shape[:2]
    if max(h, w) > max_side:
        s = max_side / max(h, w)
        im = cv2.resize(im, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    if banner:
        cv2.rectangle(im, (0, 0), (im.shape[1], 22), (0, 0, 0), -1)
        cv2.putText(im, banner, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", im, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else b""


def payload(cam: dict[str, Any], ev: Event, jpeg: bytes, backend: str) -> dict[str, Any]:
    return {"camera": cam["name"], "labels": ev.counts, "note": f"{ev.kind}: {ev.note}", "trigger": ev.trigger,
            "image": base64.b64encode(jpeg).decode() if jpeg else "", "backend": backend, "kind": ev.kind, "node": VERSION}


# ------------------------------------------------------------------------------------------ model
def prepare_model(cfg: dict[str, Any]) -> tuple[str, str]:
    """Returns (weights path to load, backend name). Exports to OpenVINO once and caches next to the weights."""
    from ultralytics import YOLO
    weights = cfg["model"]
    models_dir = Path(os.environ.get("ATLAS_MODELS") or HERE / "models")
    models_dir.mkdir(parents=True, exist_ok=True)
    wp = Path(weights) if Path(weights).is_absolute() else models_dir / weights
    if not wp.exists() and wp.suffix == ".pt":
        log("downloading", wp.name)
        YOLO(wp.name)                                     # ultralytics fetches the standard weights into cwd
        src = Path(wp.name)
        if src.exists():
            src.replace(wp)
    if cfg.get("device", "openvino") != "openvino":
        return str(wp), "node/torch-cpu"
    ov_dir = wp.with_name(wp.stem + "_openvino_model")
    if not (ov_dir / "metadata.yaml").exists():
        try:
            import openvino  # noqa: F401
            log("exporting", wp.name, "to OpenVINO (one-off)")
            YOLO(str(wp)).export(format="openvino", imgsz=int(cfg["imgsz"]), half=False, dynamic=False)
        except Exception as exc:
            log("OpenVINO export failed, using torch CPU:", type(exc).__name__, str(exc)[:160])
            return str(wp), "node/torch-cpu"
    return str(ov_dir), "node/openvino"


def parse_result(r) -> list[dict[str, Any]]:
    b = r.boxes
    if b is None or len(b) == 0:
        return []
    ids = b.id.int().tolist() if b.id is not None else [None] * len(b)
    xyxy = b.xyxy.tolist()
    cls = b.cls.int().tolist()
    conf = b.conf.tolist()
    return [{"id": ids[i], "label": r.names[cls[i]], "conf": round(conf[i], 2), "box": [int(v) for v in xyxy[i]]} for i in range(len(cls))]


# ------------------------------------------------------------------------------------------ camera loop
STATS: dict[str, Any] = {"started": time.time(), "cameras": {}, "backend": "", "version": VERSION}


def run_camera(cam: dict[str, Any], weights: str, backend: str, cfg: dict[str, Any], poster: Poster, until: float = 0) -> None:
    import logging
    from ultralytics import YOLO
    from ultralytics.utils import LOGGER as _ul
    _ul.setLevel(logging.ERROR)                           # "Waiting for stream" spam is normal for remote RTSP
    st = STATS["cameras"].setdefault(cam["name"], {"fps": 0.0, "frames": 0, "tracks": 0, "counts": {}, "events": 0, "errors": 0, "connected": False, "last_error": ""})
    state = CameraState({**cam, "heartbeat_min": cfg["heartbeat_min"]})
    src: Any = cam["source"]
    if isinstance(src, str) and src.isdigit():
        src = int(src)
    backoff = 2.0
    model = YOLO(weights, task="detect")
    while not STOP.is_set():
        t0, n = time.time(), 0
        try:
            log(cam["name"], "connecting", str(src).split("@")[-1] if isinstance(src, str) else src)
            gen = model.track(source=src, stream=True, tracker="bytetrack.yaml", conf=float(cfg["conf"]), imgsz=int(cfg["imgsz"]),
                              vid_stride=int(cfg["vid_stride"]), verbose=False)
            for r in gen:
                if STOP.is_set() or (until and time.time() > until):
                    break
                st["connected"], backoff = True, 2.0
                now = time.time()
                dets = parse_result(r)
                events = state.update(now, dets)
                n += 1
                st["frames"] += 1
                st["tracks"] = len(state.tracks)
                st["counts"] = state.counts()
                if now - t0 >= 5:
                    st["fps"] = round(n / (now - t0), 1)
                    t0, n = now, 0
                for ev in events:
                    banner = f"{time.strftime('%d %b %H:%M:%S')}  {cam['name']}  {ev.kind}"
                    if poster.submit(payload(cam, ev, keyframe(r.orig_img, dets, banner, int(cfg["keyframe_side"])), backend)):
                        st["events"] += 1
            if until and time.time() > until:
                break
            log(cam["name"], "stream ended, reconnecting")
        except Exception as exc:
            st["errors"] += 1
            st["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            log(cam["name"], "error:", st["last_error"])
        st["connected"] = False
        if STOP.wait(backoff):
            break
        backoff = min(backoff * 2, 60)
    st["connected"] = False


# ------------------------------------------------------------------------------------------ health
class _Health(BaseHTTPRequestHandler):
    poster: Poster | None = None

    def do_GET(self):  # noqa: N802
        p = self.poster
        body = json.dumps({**STATS, "uptime_s": int(time.time() - STATS["started"]), "hook": bool(p and p.hook),
                           "posted": p.sent if p else 0, "failed": p.failed if p else 0, "dropped": p.dropped if p else 0,
                           "last_post": p.last if p else {}}, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


def serve_health(cfg: dict[str, Any], poster: Poster) -> None:
    _Health.poster = poster
    try:
        srv = ThreadingHTTPServer((cfg["health_bind"], int(cfg["health_port"])), _Health)
    except OSError as exc:
        log("health server not started:", exc)
        return
    threading.Thread(target=srv.serve_forever, daemon=True, name="health").start()
    log(f"health on http://{cfg['health_bind']}:{cfg['health_port']}/")


# ------------------------------------------------------------------------------------------ main
def check(cfg: dict[str, Any], seconds: int = 10) -> int:
    """Open every camera for a few seconds and report fps. Exit code = number of cameras that failed."""
    weights, backend = prepare_model(cfg)
    STATS["backend"] = backend
    poster = Poster("", dry=True)
    threads = [threading.Thread(target=run_camera, args=(c, weights, backend, cfg, poster, time.time() + seconds), daemon=True) for c in cfg["cameras"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 30)
    bad = 0
    print(f"\nbackend: {backend}")
    for name, s in STATS["cameras"].items():
        ok = s["frames"] > 0
        bad += 0 if ok else 1
        print(f"  {name:20s} {'OK ' if ok else 'FAIL'}  {s['fps']} fps  frames={s['frames']}  {s['counts'] or ''}  {s['last_error']}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Atlas Vision Node")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true", help="test camera connections for 10 s and exit")
    ap.add_argument("--seconds", type=int, default=0, help="run for N seconds then exit (smoke test)")
    ap.add_argument("--dry", action="store_true", help="print payloads instead of posting")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if not cfg["cameras"]:
        print("no cameras configured - see node.example.json", file=sys.stderr)
        return 2
    if a.check:
        return check(cfg)
    if not cfg["hook"] and not a.dry:
        print("no hook URL (config 'hook' or env ATLAS_HOOK) - use --dry to run without one", file=sys.stderr)
        return 2
    weights, backend = prepare_model(cfg)
    STATS["backend"] = backend
    log(f"atlas node {VERSION}  backend={backend}  cameras={[c['name'] for c in cfg['cameras']]}  hook={'set' if cfg['hook'] else 'DRY'}")
    poster = Poster(cfg["hook"], int(cfg["max_posts_per_min"]), dry=a.dry).start()
    serve_health(cfg, poster)
    until = time.time() + a.seconds if a.seconds else 0
    threads = [threading.Thread(target=run_camera, args=(c, weights, backend, cfg, poster, until), daemon=True, name=c["name"]) for c in cfg["cameras"]]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    STOP.set()
    for t in threads:
        t.join(5)
    time.sleep(1.5)                                       # let the poster drain
    log(f"done: posted={poster.sent} failed={poster.failed} dropped={poster.dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
