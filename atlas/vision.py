"""Computer vision for the desk: cameras as a first-class input.

Pipeline per camera tick (see desk/scheduler.py `camera_watch`):

    grab()  →  motion()  →  detect()  →  rule()  →  vision_event row  →  (optional) describe()  →  desk run

- grab      one JPEG from a webcam index, an RTSP URL (ffmpeg), an HTTP snapshot URL or a file.
- motion    cheap frame-difference score against the previous frame (numpy) — gates the expensive steps.
- detect    local YOLO (ultralytics, optional) → [{label, conf, box}]. Falls back to the vision model when
            ultralytics is not installed (Render), or to nothing at all in demo mode.
- rule      "is this worth the desk's attention?" — watched labels, minimum count, hours, cooldown.
- describe  ask a vision-language model a question about the frame (OpenAI-compatible image message).
- annotate  draw boxes for the portal / snapshot archive (PIL).

Everything degrades: no ultralytics → VLM or motion-only; no model key → detections only; no cv2 → RTSP via
ffmpeg, webcams unavailable. Nothing here decides what to *do* — the orchestrator and approvals do that.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import config as cfg

MODELS_DIR = cfg.DATA_DIR / "models"
SNAP_DIR = cfg.DATA_DIR / "snapshots"
YOLO_WEIGHTS = os.environ.get("VISION_YOLO", str(MODELS_DIR / "yolov8n.pt"))
DEFAULT_VLM = os.environ.get("VISION_MODEL", "anthropic/claude-haiku-4.5")
DEFAULT_VLM_PROVIDER = os.environ.get("VISION_PROVIDER", "openrouter")
MAX_SIDE = 960                      # frames are downscaled to this before detection / VLM
FRAME_TIMEOUT = float(os.environ.get("VISION_GRAB_TIMEOUT", "12"))

# COCO labels the rule engine understands as synonyms
SYNONYMS = {"people": "person", "human": "person", "man": "person", "woman": "person", "customer": "person",
            "vehicle": "car", "van": "truck", "lorry": "truck", "bike": "bicycle", "phone": "cell phone"}


def _norm_label(s: str) -> str:
    s = s.strip().lower()
    return SYNONYMS.get(s, s)


# ---------------------------------------------------------------------------- frames
def _pil():
    from PIL import Image  # noqa: WPS433 (optional at import time, required at runtime)
    return Image


def _to_jpeg(img, quality: int = 82) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _shrink(jpeg: bytes, max_side: int = MAX_SIDE) -> bytes:
    Image = _pil()
    img = Image.open(io.BytesIO(jpeg))
    w, h = img.size
    if max(w, h) <= max_side:
        return jpeg
    s = max_side / float(max(w, h))
    img = img.resize((int(w * s), int(h * s)))
    return _to_jpeg(img)


def frame_size(jpeg: bytes) -> tuple[int, int]:
    return _pil().open(io.BytesIO(jpeg)).size


def source_kind(source: str) -> str:
    s = (source or "").strip()
    if re.fullmatch(r"\d+", s):
        return "webcam"
    if s.lower().startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    if s.lower().startswith(("http://", "https://")):
        return "http"
    return "file"


def _grab_webcam(index: int) -> bytes:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("webcam capture needs opencv-python (pip install opencv-python)") from exc
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(index)
    try:
        if not cap.isOpened():
            raise RuntimeError(f"webcam {index} not available")
        ok, frame = False, None
        for _ in range(6):                          # first frames from a cold webcam are dark / stale
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"webcam {index} returned no frame")
        ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return bytes(enc.tobytes())
    finally:
        cap.release()


def _grab_rtsp(url: str) -> bytes:
    ff = shutil.which("ffmpeg")
    if ff:
        cmd = [ff, "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url,
               "-frames:v", "1", "-f", "image2", "-q:v", "3", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"rtsp grab timed out after {FRAME_TIMEOUT:.0f}s") from exc
        if p.returncode == 0 and p.stdout[:2] == b"\xff\xd8":
            return p.stdout
        err = (p.stderr or b"").decode(errors="replace").strip().splitlines()
        last = err[-1] if err else f"ffmpeg exit {p.returncode}"
        # fall through to OpenCV only if ffmpeg failed to connect; otherwise report the real error
        if "Connection refused" not in last and "timed out" not in last.lower() and "401" not in last:
            raise RuntimeError(f"rtsp grab failed: {last[:200]}")
        cv_err = last
    else:
        cv_err = "ffmpeg not installed"
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(f"rtsp grab failed ({cv_err}); opencv fallback unavailable") from exc
    cap = cv2.VideoCapture(url)
    try:
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"rtsp grab failed: {cv_err}")
        ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return bytes(enc.tobytes())
    finally:
        cap.release()


def _grab_http(url: str) -> bytes:
    """Snapshot URL (Hikvision /ISAPI/Streaming/channels/101/picture, ESP32-CAM /capture, any JPEG URL).
    Credentials may be embedded (http://user:pass@host/...) — Basic and Digest are both tried."""
    u = httpx.URL(url)
    auth = None
    if u.username:
        auth = httpx.DigestAuth(u.username, u.password or "")
        url = str(u.copy_with(username=None, password=None))
    with httpx.Client(timeout=FRAME_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url, auth=auth)
        if r.status_code == 401 and auth is not None:
            r = c.get(url, auth=(u.username, u.password or ""))
        r.raise_for_status()
        data = r.content
    ctype = r.headers.get("content-type", "")
    if data[:2] != b"\xff\xd8":
        if "multipart/x-mixed-replace" in ctype or b"\xff\xd8" in data[:4096]:
            i = data.find(b"\xff\xd8"); j = data.find(b"\xff\xd9", i + 2)
            if i >= 0 and j > i:
                return data[i:j + 2]
        # PNG or anything PIL can read → re-encode
        try:
            return _to_jpeg(_pil().open(io.BytesIO(data)))
        except Exception as exc:
            raise RuntimeError(f"snapshot URL did not return an image ({ctype or 'no content-type'})") from exc
    return data


def grab(source: str) -> bytes:
    """One JPEG frame from any supported source. Raises RuntimeError with a human-readable reason."""
    kind = source_kind(source)
    s = source.strip()
    if kind == "webcam":
        jpeg = _grab_webcam(int(s))
    elif kind == "rtsp":
        jpeg = _grab_rtsp(s)
    elif kind == "http":
        jpeg = _grab_http(s)
    else:
        p = Path(s)
        if not p.is_file():
            raise RuntimeError(f"camera source not found: {s[:120]}")
        data = p.read_bytes()
        jpeg = data if data[:2] == b"\xff\xd8" else _to_jpeg(_pil().open(io.BytesIO(data)))
    return _shrink(jpeg)


# ---------------------------------------------------------------------------- motion
def _gray_small(jpeg: bytes, side: int = 64):
    import numpy as np
    Image = _pil()
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((side, side))
    return np.asarray(img, dtype="float32") / 255.0


def motion(prev_jpeg: bytes | None, cur_jpeg: bytes) -> float:
    """0..1 — mean absolute difference between two frames on a 64x64 grey thumbnail. ~0.02 is noise,
    >0.08 is somebody walking through, >0.3 is a scene change / camera moved."""
    if not prev_jpeg:
        return 1.0
    try:
        a, b = _gray_small(prev_jpeg), _gray_small(cur_jpeg)
        return float(abs(a - b).mean())
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------- detection
class Detector:
    """Local object detector (ultralytics YOLO). Loaded once, thread-safe, optional."""

    def __init__(self, weights: str = YOLO_WEIGHTS):
        self.weights = weights
        self._model = None
        self._lock = threading.Lock()
        self.error = ""

    @property
    def available(self) -> bool:
        if self._model is not None:
            return True
        if self.error:
            return False
        try:
            import ultralytics  # noqa: F401
            return True
        except Exception as exc:
            self.error = f"ultralytics not installed ({type(exc).__name__})"
            return False

    def _load(self):
        if self._model is None:
            from ultralytics import YOLO
            Path(self.weights).parent.mkdir(parents=True, exist_ok=True)
            self._model = YOLO(self.weights)          # downloads the standard weights on first use
        return self._model

    def detect(self, jpeg: bytes, conf: float = 0.35) -> list[dict[str, Any]]:
        if not self.available:
            return []
        Image = _pil()
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        with self._lock:
            try:
                model = self._load()
                res = model.predict(img, conf=conf, verbose=False)[0]
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
                return []
        out = []
        for b in res.boxes:
            x1, y1, x2, y2 = [int(v) for v in b.xyxy[0].tolist()]
            out.append({"label": model.names[int(b.cls)], "conf": round(float(b.conf), 2), "box": [x1, y1, x2, y2]})
        out.sort(key=lambda d: -d["conf"])
        return out


DETECTOR = Detector()


def counts(dets: list[dict[str, Any]]) -> dict[str, int]:
    c: dict[str, int] = {}
    for d in dets:
        c[d["label"]] = c.get(d["label"], 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def counts_text(c: dict[str, int]) -> str:
    if not c:
        return "nothing recognised"
    parts = []
    for k, n in c.items():
        parts.append(f"{n} {k}" + ("s" if n != 1 and not k.endswith("s") and k != "person" else "") if k != "person"
                     else (f"{n} person" if n == 1 else f"{n} people"))
    return ", ".join(parts)


def annotate(jpeg: bytes, dets: list[dict[str, Any]], banner: str = "") -> bytes:
    from PIL import ImageDraw
    Image = _pil()
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    dr = ImageDraw.Draw(img)
    for d in dets:
        x1, y1, x2, y2 = d["box"]
        col = (76, 144, 240) if d["label"] == "person" else (52, 211, 153)
        dr.rectangle([x1, y1, x2, y2], outline=col, width=2)
        tag = f"{d['label']} {int(d['conf'] * 100)}%"
        tw = 7 * len(tag) + 6
        dr.rectangle([x1, max(0, y1 - 14), x1 + tw, y1], fill=col)
        dr.text((x1 + 3, max(0, y1 - 13)), tag, fill=(10, 12, 16))
    if banner:
        dr.rectangle([0, img.size[1] - 16, img.size[0], img.size[1]], fill=(11, 14, 19))
        dr.text((4, img.size[1] - 14), banner[:120], fill=(230, 233, 238))
    return _to_jpeg(img, 80)


# ---------------------------------------------------------------------------- vision-language model
def _vlm_cfg(model: str = "") -> tuple[str, str, str]:
    """(base_url, api_key, model) for the vision model — an OpenAI-compatible provider from config/providers.json."""
    providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    pname = DEFAULT_VLM_PROVIDER
    pc = (providers.get("providers") or {}).get(pname) or cfg.DEFAULT_PROVIDERS["providers"]["openrouter"]
    key = (pc.get("api_key") or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    return pc.get("base_url", "https://openrouter.ai/api/v1").rstrip("/"), key, (model or DEFAULT_VLM)


def vlm_ready() -> bool:
    return bool(_vlm_cfg()[1])


def describe(jpeg: bytes, question: str, model: str = "", context: str = "", max_tokens: int = 400,
             transport: httpx.BaseTransport | None = None) -> str:
    """Ask the vision model one question about the frame. Plain-text answer, never invents what it can't see."""
    base, key, model = _vlm_cfg(model)
    if not key:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY or config/providers.json openrouter.api_key)")
    system = ("You are the vision analyst of a small business's operations desk. You look at one camera frame and "
              "answer the owner's question precisely. State only what is visible; say 'not visible' rather than guess. "
              "Count carefully. Keep it under 80 words unless asked for detail. Plain text only — no markdown, no headings, no asterisks. "
              "Never identify people by name.")
    if context:
        system += "\n\nContext: " + context[:800]
    payload = {
        "model": model, "max_tokens": max_tokens, "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": question.strip() or "Describe what is happening in this frame."},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
            ]},
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision"}
    with httpx.Client(timeout=90, transport=transport) as c:
        r = c.post(base + "/chat/completions", headers=headers, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"vision model HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    try:
        msg = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"vision model returned no answer: {json.dumps(j)[:200]}") from exc
    if isinstance(msg, list):
        msg = " ".join(p.get("text", "") for p in msg if isinstance(p, dict))
    return (msg or "").strip()


def vlm_detect(jpeg: bytes, labels: list[str], model: str = "", transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """Detector fallback when ultralytics is not installed: the vision model counts the watched labels.
    Boxes are unknown (empty); confidence is nominal."""
    want = ", ".join(labels) or "person"
    q = (f"Count how many of each of these are visible: {want}. Reply with JSON only, like "
         f'{{"person": 2, "car": 0}} — one integer per label, nothing else.')
    txt = describe(jpeg, q, model=model, max_tokens=120, transport=transport)
    m = re.search(r"\{.*\}", txt, re.S)
    out: list[dict[str, Any]] = []
    if not m:
        return out
    try:
        data = json.loads(m.group(0))
    except Exception:
        return out
    for k, v in data.items():
        try:
            n = int(v)
        except Exception:
            continue
        out.extend({"label": _norm_label(str(k)), "conf": 0.5, "box": [0, 0, 0, 0]} for _ in range(max(0, min(n, 50))))
    return out


# ---------------------------------------------------------------------------- rules
def parse_hours(spec: str) -> tuple[int, int] | None:
    """'20:00-07:00' → (1200, 420) minutes; blank → None (always)."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*", spec or "")
    if not m:
        return None
    a = int(m.group(1)) * 60 + int(m.group(2) or 0)
    b = int(m.group(3)) * 60 + int(m.group(4) or 0)
    return a, b


def in_hours(spec: str, now: datetime | None = None) -> bool:
    win = parse_hours(spec)
    if not win:
        return True
    now = now or datetime.now()
    t = now.hour * 60 + now.minute
    a, b = win
    return a <= t < b if a <= b else (t >= a or t < b)      # overnight windows wrap


def rule_config(config: dict[str, Any]) -> dict[str, Any]:
    labels = [_norm_label(x) for x in str(config.get("watch_for") or "person").split(",") if x.strip()]
    try:
        min_count = max(1, int(config.get("min_count") or 1))
    except Exception:
        min_count = 1
    try:
        cooldown = max(0, float(config.get("cooldown_min") or 10))
    except Exception:
        cooldown = 10.0
    try:
        motion_min = float(config.get("motion_min") or 0.03)
    except Exception:
        motion_min = 0.03
    try:
        alert_on_motion = float(config.get("alert_on_motion") or 0)      # 0 = off; e.g. 0.2 = wake the desk on any scene change
    except Exception:
        alert_on_motion = 0.0
    return {"labels": labels, "min_count": min_count, "cooldown_min": cooldown, "hours": str(config.get("hours") or ""),
            "motion_min": motion_min, "alert_on_motion": alert_on_motion,
            "question": str(config.get("question") or ""), "task": str(config.get("task") or "")}


def evaluate(config: dict[str, Any], dets: list[dict[str, Any]], prev_counts: dict[str, int] | None,
             last_trigger_ts: float | None, now_ts: float | None = None, mot: float = 0.0) -> tuple[bool, str]:
    """Should this frame wake the desk? Returns (triggered, reason). Rule:
    watched count >= min_count, inside the hours window, and either the watched count changed since the previous
    frame or the cooldown has passed (so a person standing still doesn't page the owner every tick)."""
    r = rule_config(config)
    now_ts = now_ts or time.time()
    c = counts(dets)
    n = sum(c.get(l, 0) for l in r["labels"])
    prev_n = sum((prev_counts or {}).get(l, 0) for l in r["labels"])
    cooled_now = last_trigger_ts is None or (now_ts - last_trigger_ts) >= r["cooldown_min"] * 60
    if r["alert_on_motion"] and mot >= r["alert_on_motion"] and prev_counts is not None             and in_hours(r["hours"], datetime.fromtimestamp(now_ts)) and cooled_now:
        return True, f"scene changed (motion {mot:.2f} ≥ {r['alert_on_motion']:g})"
    if n < r["min_count"]:
        return False, f"{n} {'/'.join(r['labels'])} (< {r['min_count']})"
    if not in_hours(r["hours"], datetime.fromtimestamp(now_ts)):
        return False, f"{n} {'/'.join(r['labels'])} but outside {r['hours']}"
    cooled = last_trigger_ts is None or (now_ts - last_trigger_ts) >= r["cooldown_min"] * 60
    if n != prev_n and cooled:
        return True, f"{'/'.join(r['labels'])} count {prev_n} → {n}"
    if n == prev_n and cooled and last_trigger_ts is not None:
        return True, f"{n} {'/'.join(r['labels'])} still present after cooldown"
    if n != prev_n and not cooled:
        return False, f"{'/'.join(r['labels'])} {prev_n} → {n} (cooldown)"
    return False, f"{n} {'/'.join(r['labels'])} unchanged"


# ---------------------------------------------------------------------------- snapshots
def save_snapshot(desk_id: int, camera: str, jpeg: bytes, keep: int = 600) -> str:
    d = SNAP_DIR / f"desk{desk_id}"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", camera)[:40] or "cam"
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{int((time.time() % 1) * 1000):03d}-{safe}.jpg"
    (d / name).write_bytes(jpeg)
    files = sorted(d.glob("*.jpg"))
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(d / name)


def event_text(camera: str, c: dict[str, int], mot: float, reason: str, answer: str = "") -> str:
    s = f"{camera}: {counts_text(c)} (motion {mot:.2f}) — {reason}"
    return s + (f". Analyst: {answer}" if answer else "")


def analyse(source: str, config: dict[str, Any], prev_jpeg: bytes | None = None, live_vlm: bool = False,
            question: str = "", detector: Detector | None = None) -> dict[str, Any]:
    """Grab + motion + detect (+ optional VLM answer). Pure function of the inputs; the caller decides what to store."""
    det = detector or DETECTOR
    jpeg = grab(source)
    mot = motion(prev_jpeg, jpeg)
    r = rule_config(config)
    dets: list[dict[str, Any]] = []
    backend = "none"
    if det.available:
        dets, backend = det.detect(jpeg), "yolo"
    elif live_vlm and vlm_ready() and mot >= r["motion_min"]:
        dets, backend = vlm_detect(jpeg, r["labels"], model=str(config.get("vlm_model") or "")), "vlm"
    c = counts(dets)
    answer = ""
    q = question or ""
    if q and live_vlm and vlm_ready():
        try:
            answer = describe(jpeg, q, model=str(config.get("vlm_model") or ""), context=str(config.get("notes") or ""))
        except Exception as exc:
            answer = f"(vision model unavailable: {str(exc)[:120]})"
    w, h = frame_size(jpeg)
    return {"jpeg": jpeg, "annotated": annotate(jpeg, dets, f"{time.strftime('%d %b %H:%M:%S')}  {counts_text(c)}"),
            "detections": dets, "counts": c, "motion": round(mot, 3), "backend": backend, "answer": answer,
            "size": [w, h], "detector_error": det.error}


# ---------------------------------------------------------------------------- video understanding
def _ffprobe_duration(path: str) -> float:
    fp = shutil.which("ffprobe")
    if not fp:
        return 0.0
    try:
        p = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, timeout=30)
        return float((p.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def sample_video_frames(source: str, n: int = 8) -> tuple[list[tuple[float, bytes]], float]:
    """Evenly sample up to n JPEG frames from a local video file (or any URL ffmpeg can read).
    Returns ([(timestamp_s, jpeg), ...], duration_s). Requires ffmpeg."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg is required for video understanding and is not installed")
    dur = _ffprobe_duration(source)
    n = max(1, min(int(n or 8), 10))
    if dur > 0:
        stamps = [dur * (i + 1) / (n + 1) for i in range(n)]
    else:                                            # duration unknown (stream/pipe): take the first frames spaced 2s
        stamps = [i * 2.0 for i in range(n)]
    frames: list[tuple[float, bytes]] = []
    for t in stamps:
        cmd = [ff, "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", source,
               "-frames:v", "1", "-f", "image2", "-q:v", "3", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT * 2)
        except subprocess.TimeoutExpired:
            continue
        if p.returncode == 0 and p.stdout[:2] == b"\xff\xd8":
            frames.append((t, p.stdout))
    if not frames:
        raise RuntimeError(f"could not extract any frames from {source}")
    return frames, dur


def describe_video(source: str, question: str = "", model: str = "", frames: int = 8, context: str = "",
                   transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Watch a video: sample frames evenly, send them all to the vision model in one call, get a timeline
    description + an answer to the question. Returns {answer, duration_s, frames:[{t, jpeg}]}."""
    sampled, dur = sample_video_frames(source, frames)
    base, key, model = _vlm_cfg(model)
    if not key:
        raise RuntimeError("no vision model key (set OPENROUTER_API_KEY or config/providers.json openrouter.api_key)")
    system = ("You are the vision analyst of a small business's operations desk. You are shown frames sampled evenly "
              "from ONE video, each labelled with its timestamp. Describe what happens over time as a short timeline "
              "(what changes between frames), then answer the owner's question. State only what is visible; say 'not "
              "visible' rather than guess. Never identify people by name. Plain text only.")
    if context:
        system += "\n\nContext: " + context[:800]
    content: list[dict[str, Any]] = [{"type": "text", "text":
        (question.strip() or "Describe this video.") + f"\n\nVideo duration ≈{dur:.0f}s; {len(sampled)} frames follow, in order."}]
    for t, jpeg in sampled:
        content.append({"type": "text", "text": f"[frame at {t:.1f}s]"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}})
    payload = {"model": model, "max_tokens": 700, "temperature": 0.1,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": "Atlas Desk vision"}
    with httpx.Client(timeout=180, transport=transport) as c:
        r = c.post(base + "/chat/completions", headers=headers, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"vision model HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    try:
        msg = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"vision model returned no answer: {json.dumps(j)[:200]}") from exc
    if isinstance(msg, list):
        msg = " ".join(p.get("text", "") for p in msg if isinstance(p, dict))
    return {"answer": (msg or "").strip(), "duration_s": dur,
            "frames": [{"t": t, "jpeg": jpeg} for t, jpeg in sampled]}
