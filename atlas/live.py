"""Live camera feeds: one decode loop per source, every frame through the detector, boxes drawn, served as MJPEG.

A `Feed` opens the source once (webcam, RTSP, or a recording that plays at wall-clock speed and loops), reads frames
continuously, runs the local YOLO detector (with ByteTrack ids when available) on every frame it can keep up with,
draws the boxes, and keeps the latest annotated JPEG for any number of viewers. Viewers pull the newest frame at
their own pace, so a slow browser never stalls the loop. A feed with no viewers stops itself after IDLE_S.

While a feed is running, `vision.grab(source)` returns its latest raw frame, so the journal, the rules and the
"look now" analyst all see exactly the picture the owner is watching, with no extra decode.
"""
from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path
from typing import Any

IDLE_S = float(os.environ.get("LIVE_IDLE_S", "45"))          # stop a feed this long after its last viewer left
MAX_W = int(os.environ.get("LIVE_MAX_W", "1920"))           # streamed frame width cap (source aspect kept)
JPEG_Q = int(os.environ.get("LIVE_JPEG_Q", "76"))
TARGET_FPS = float(os.environ.get("LIVE_FPS", "15"))
DETECT_CONF = 0.35
TRAIL_S = 2.5                                                # how long a track's trail stays on screen

_feeds: dict[str, "Feed"] = {}
_feeds_lock = threading.Lock()

PALETTE = [(80, 200, 120), (90, 160, 255), (255, 170, 60), (240, 90, 120), (170, 120, 255), (60, 220, 220),
           (255, 220, 80), (200, 200, 200)]


def _cv2():
    import cv2  # local import: opencv is optional on the server (Render has no camera)
    return cv2


class Feed:
    def __init__(self, source: str, name: str = "", max_w: int = MAX_W, fps: float = TARGET_FPS):
        from . import vision as V
        self.source = source
        self.name = name or source
        self.kind = V.source_kind(source)
        self.max_w = max_w
        self.fps_cap = fps
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._viewers = 0
        self._last_viewer = time.time()
        self.seq = 0
        self.jpeg = b""              # latest annotated frame (stream size)
        self._raw = None             # latest BGR frame (full decode size)
        self.ts = 0.0
        self.dets: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.fps = 0.0               # measured loop rate
        self.det_ms = 0.0
        self.error = ""
        self.size = (0, 0)
        self.pos_s = 0.0             # for recordings: position in the clip
        self._trails: dict[int, list[tuple[float, int, int]]] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "Feed":
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f"live:{self.name}", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def attach(self) -> None:
        with self._lock:
            self._viewers += 1
            self._last_viewer = time.time()

    def detach(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_viewer = time.time()

    @property
    def viewers(self) -> int:
        return self._viewers

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "running": self.running, "viewers": self._viewers, "fps": round(self.fps, 1),
                "detect_ms": round(self.det_ms, 1), "size": list(self.size), "counts": dict(self.counts), "error": self.error,
                "pos_s": round(self.pos_s, 1), "ts": self.ts}

    # ------------------------------------------------------------------ frames out
    def wait_frame(self, after_seq: int, timeout: float = 5.0) -> tuple[int, bytes]:
        """Block until a frame newer than after_seq exists (or timeout). Returns (seq, jpeg)."""
        with self._cond:
            if self.seq <= after_seq:
                self._cond.wait(timeout)
            return self.seq, self.jpeg

    def raw_jpeg(self, max_side: int = 0) -> bytes:
        """Latest unannotated frame as JPEG, for the analyst / journal. Empty bytes if nothing decoded yet."""
        cv2 = _cv2()
        with self._lock:
            frame = self._raw
        if frame is None:
            return b""
        if max_side:
            h, w = frame.shape[:2]
            s = max_side / max(w, h)
            if s < 1:
                frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        return buf.tobytes() if ok else b""

    # ------------------------------------------------------------------ the loop
    def _open(self):
        cv2 = _cv2()
        s = self.source.strip()
        if self.kind == "webcam":
            os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
            cap = cv2.VideoCapture(int(s), cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(int(s))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        elif self.kind == "video":
            if not Path(s).is_file():
                raise RuntimeError(f"camera source not found: {s[:120]}")
            cap = cv2.VideoCapture(s)
        else:
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            cap = cv2.VideoCapture(s, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError(f"could not open camera source ({self.kind})")
        return cap

    def _run(self) -> None:
        cv2 = _cv2()
        from . import vision as V
        try:
            cap = self._open()
        except Exception as exc:
            self.error = str(exc)[:200]
            return
        self.error = ""
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not (1.0 <= src_fps <= 120.0):
            src_fps = 25.0
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) if self.kind == "video" else 0
        clip_dur = n_frames / src_fps if n_frames > 0 else 0.0
        det = V.DETECTOR
        model = None
        if det.available and hasattr(det, "_load"):
            try:
                model = det._load()
            except Exception as exc:
                self.error = f"detector: {str(exc)[:120]}"
        use_track = model is not None
        det_lock = getattr(det, "_lock", threading.Lock())
        t0 = time.time()
        idx = 0                      # frames consumed from the source (recordings: playback position)
        shown = 0
        rate_t, rate_n = time.time(), 0
        while not self._stop.is_set():
            # a recording plays at its own speed: skip frames when we fall behind, wait when we are ahead
            if self.kind == "video":
                due_idx = int((time.time() - t0) * src_fps)
                while idx < due_idx - 1:                  # behind: drop frames without decoding
                    if not cap.grab():
                        break
                    idx += 1
                if idx > due_idx:
                    time.sleep(min(0.05, (idx - due_idx) / src_fps))
            ok, frame = cap.read()
            if not ok or frame is None:
                if self.kind == "video":                  # loop
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    t0, idx = time.time(), 0
                    if use_track:
                        try:
                            model.predictor.trackers[0].reset()   # fresh ids each loop
                        except Exception:
                            pass
                    continue
                self.error = "frame read failed, reconnecting"
                time.sleep(1.0)
                try:
                    cap.release()
                    cap = self._open()
                    self.error = ""
                except Exception as exc:
                    self.error = str(exc)[:200]
                continue
            idx += 1
            if clip_dur:
                self.pos_s = (idx / src_fps) % clip_dur
            h, w = frame.shape[:2]
            self.size = (w, h)
            dets: list[dict[str, Any]] = []
            det_t = time.time()
            if model is not None:
                try:
                    with det_lock:
                        if use_track:
                            res = model.track(frame, conf=DETECT_CONF, persist=True, verbose=False, tracker="bytetrack.yaml")[0]
                        else:
                            res = model.predict(frame, conf=DETECT_CONF, verbose=False)[0]
                    ids = res.boxes.id.int().tolist() if (use_track and res.boxes.id is not None) else [None] * len(res.boxes)
                    for b, tid in zip(res.boxes, ids):
                        x1, y1, x2, y2 = [int(v) for v in b.xyxy[0].tolist()]
                        dets.append({"label": model.names[int(b.cls)], "conf": round(float(b.conf), 2), "box": [x1, y1, x2, y2], "id": tid})
                except Exception as exc:
                    if use_track:                          # tracker unavailable (no lap): fall back to plain detection
                        use_track = False
                    else:
                        self.error = f"detector: {str(exc)[:120]}"
            elif det.available:                            # a detector without a YOLO model (tests, other backends)
                ok_j, jb = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok_j:
                    dets = [dict(d, id=None) for d in det.detect(jb.tobytes())]
            self.det_ms = (time.time() - det_t) * 1000
            dets.sort(key=lambda d: -d["conf"])
            counts = V.counts(dets)
            annotated = self._draw(frame, dets, counts)
            ok2, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_Q])
            if not ok2:
                continue
            shown += 1
            rate_n += 1
            if time.time() - rate_t >= 1.0:
                self.fps = rate_n / (time.time() - rate_t)
                rate_t, rate_n = time.time(), 0
            with self._cond:
                self._raw = frame
                self.jpeg = buf.tobytes()
                self.dets = dets
                self.counts = counts
                self.ts = time.time()
                self.seq += 1
                self._cond.notify_all()
            # nobody watching for a while: stop (the scheduler's grab() falls back to a one-off decode)
            if self._viewers == 0 and time.time() - self._last_viewer > IDLE_S:
                break
            if self.kind != "video" and self.fps_cap > 0:
                time.sleep(max(0.0, 1.0 / self.fps_cap - (time.time() - det_t)))
        cap.release()
        with _feeds_lock:
            if _feeds.get(self.source) is self:
                _feeds.pop(self.source, None)

    # ------------------------------------------------------------------ drawing
    def _draw(self, frame, dets: list[dict[str, Any]], counts: dict[str, int]):
        cv2 = _cv2()
        h, w = frame.shape[:2]
        scale = min(1.0, self.max_w / w)
        out = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else frame.copy()
        now = time.time()
        ow0 = out.shape[1]
        th = 1 if ow0 < 700 else 2
        fs = max(0.38, min(0.7, ow0 / 2000))                   # label type follows the frame width
        live_ids = set()
        for d in dets:
            x1, y1, x2, y2 = [int(v * scale) for v in d["box"]]
            tid = d.get("id")
            col = PALETTE[(tid if tid is not None else hash(d["label"])) % len(PALETTE)]
            cv2.rectangle(out, (x1, y1), (x2, y2), col, th)
            tag = f"{d['label']}" + (f" #{tid}" if tid is not None else "") + f" {int(d['conf'] * 100)}%"
            (tw, tth), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
            ty = y1 - 6 if y1 - tth - 10 > 0 else y2 + tth + 6
            cv2.rectangle(out, (x1, ty - tth - 6), (x1 + tw + 8, ty + 4), col, -1)
            cv2.putText(out, tag, (x1 + 4, ty - 1), cv2.FONT_HERSHEY_SIMPLEX, fs, (16, 16, 16), 1, cv2.LINE_AA)
            if tid is not None:
                live_ids.add(tid)
                cx, cy = (x1 + x2) // 2, y2
                tr = self._trails.setdefault(tid, [])
                tr.append((now, cx, cy))
                del tr[:-60]
                pts = [(x, y) for t, x, y in tr if now - t <= TRAIL_S]
                for i in range(1, len(pts)):
                    cv2.line(out, pts[i - 1], pts[i], col, 1, cv2.LINE_AA)
        for tid in [k for k, v in self._trails.items() if not v or now - v[-1][0] > TRAIL_S * 2]:
            self._trails.pop(tid, None)
        # HUD: name, clock, counts, loop rate
        from . import vision as V
        hud = f"{self.name}  {time.strftime('%H:%M:%S')}  {V.counts_text(counts) or 'no detections'}"
        rate = f"{self.fps:.0f} fps  det {self.det_ms:.0f} ms" + (f"  clip {self.pos_s:5.1f}s" if self.kind == "video" else "  live")
        oh, ow = out.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        fh, fr = (0.52, 0.48) if ow >= 900 else (0.44, 0.4)             # smaller type on small frames
        (hw, _), _ = cv2.getTextSize(hud, font, fh, 1)
        (rw, _), _ = cv2.getTextSize(rate, font, fr, 1)
        two_rows = hw + rw + 44 > ow                                     # not enough room side by side: rate goes on a row above
        while two_rows and hw + 20 > ow and len(hud) > 12:              # still too long alone: trim the counts text
            hud = hud[:-4] + "..."
            (hw, _), _ = cv2.getTextSize(hud, font, fh, 1)
        row = 26 if ow >= 900 else 22
        bar = row * (2 if two_rows else 1) + 4
        overlay = out.copy()
        cv2.rectangle(overlay, (0, oh - bar), (ow, oh), (12, 12, 12), -1)
        cv2.addWeighted(overlay, 0.62, out, 0.38, 0, out)
        cv2.putText(out, hud, (10, oh - 9), font, fh, (235, 235, 235), 1, cv2.LINE_AA)
        ry = oh - 9 - (row if two_rows else 0)
        cv2.putText(out, rate, (ow - rw - 10, ry), font, fr, (150, 220, 170), 1, cv2.LINE_AA)
        cv2.circle(out, (ow - rw - 22, ry - 5), 4, (60, 60, 240), -1)
        return out


# ---------------------------------------------------------------------- registry
def get(source: str) -> Feed | None:
    with _feeds_lock:
        f = _feeds.get(source)
    return f if (f and f.running) else None


def open(source: str, name: str = "") -> Feed:   # noqa: A001 - mirrors the file API on purpose
    """The running feed for this source, started if needed."""
    with _feeds_lock:
        f = _feeds.get(source)
        if f is None or not f.running:
            f = Feed(source, name)
            _feeds[source] = f
    f.start()
    return f


def stop_all() -> None:
    with _feeds_lock:
        feeds = list(_feeds.values())
        _feeds.clear()
    for f in feeds:
        f.stop()


def statuses() -> list[dict[str, Any]]:
    with _feeds_lock:
        return [f.status() for f in _feeds.values()]


def mjpeg(feed: Feed, max_fps: float = 0.0, boundary: str = "atlasframe"):
    """Generator of a multipart/x-mixed-replace body: the newest frame, never a stale one, at the viewer's pace."""
    feed.attach()
    seq = 0
    min_dt = 1.0 / max_fps if max_fps > 0 else 0.0
    last = 0.0
    try:
        deadline = time.time() + 20
        while True:
            new_seq, jpeg = feed.wait_frame(seq, timeout=2.0)
            if new_seq == seq or not jpeg:                 # nothing new yet
                if not feed.running or (not jpeg and time.time() > deadline):
                    break
                continue
            seq = new_seq
            if min_dt and time.time() - last < min_dt:
                continue
            last = time.time()
            yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"
    finally:
        feed.detach()
