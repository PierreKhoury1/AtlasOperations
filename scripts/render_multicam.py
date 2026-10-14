"""Render the site's multi-camera showcase from the real pipeline: YOLO + ByteTrack (what the live view draws) over
four CAVIAR shop clips (two scenes, corridor + front view of the same people), tiled 2x2 with the per-feed count.
Every box is a real detection; nothing is drawn by hand.

    py scripts/render_multicam.py --out site/video/multicam.mp4 --seconds 14

Needs ultralytics + opencv (requirements-vision.txt) and an ffmpeg on PATH or FFMPEG=path.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CLIPS = [("OneShopOneWait1cor", "Corridor A"), ("OneShopOneWait1front", "Shop A · front"),
         ("TwoEnterShop1cor", "Corridor B"), ("TwoEnterShop1front", "Shop B · front")]
TILE_W, TILE_H = 640, 360
BLUE, GREEN, INK = (255, 141, 76), (140, 214, 61), (20, 11, 7)     # BGR: #4C8DFF, #3DD68C, #070B14


def label(img, text, x, y, color, scale=0.42):
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    cv2.rectangle(img, (x, y - h - 6), (x + w + 8, y + 2), INK, -1)
    cv2.putText(img, text, (x + 4, y - 2), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "site" / "video" / "multicam.mp4"))
    ap.add_argument("--seconds", type=float, default=14)
    ap.add_argument("--start", type=float, default=4, help="seconds into each clip")
    ap.add_argument("--model", default=os.environ.get("YOLO_MODEL", str(ROOT.parent / "hermes" / "data" / "models" / "yolo11n.pt")))
    ap.add_argument("--clips", default=str(ROOT / "data" / "theatre_clips"))
    a = ap.parse_args()
    from ultralytics import YOLO
    model = YOLO(a.model)
    fps = 25
    n = int(a.seconds * fps)
    tiles: list[list[np.ndarray]] = []
    for name, title in CLIPS:
        cap = cv2.VideoCapture(str(Path(a.clips) / f"{name}.mp4"))
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(a.start * fps))
        frames = []
        ids_seen: set[int] = set()
        for i in range(n):
            ok, fr = cap.read()
            if not ok:
                break
            res = model.track(fr, persist=True, classes=[0], conf=0.3, imgsz=640, tracker="bytetrack.yaml", verbose=False)[0]
            fr = cv2.resize(fr, (TILE_W, TILE_H), interpolation=cv2.INTER_CUBIC)
            sx, sy = TILE_W / res.orig_shape[1], TILE_H / res.orig_shape[0]
            count = 0
            if res.boxes is not None and len(res.boxes):
                ids = res.boxes.id.int().tolist() if res.boxes.id is not None else [None] * len(res.boxes)
                for (x1, y1, x2, y2), tid, cf in zip(res.boxes.xyxy.tolist(), ids, res.boxes.conf.tolist()):
                    x1, y1, x2, y2 = int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)
                    cv2.rectangle(fr, (x1, y1), (x2, y2), GREEN, 1, cv2.LINE_AA)
                    tag = f"person {tid}" if tid is not None else "person"
                    label(fr, f"{tag} {cf:.2f}", x1, max(y1, 14), GREEN, 0.38)
                    if tid is not None:
                        ids_seen.add(tid)
                    count += 1
            # the feed's HUD: name, live detector, count so far
            cv2.rectangle(fr, (0, 0), (TILE_W, 26), INK, -1)
            cv2.putText(fr, title, (10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 236, 230), 1, cv2.LINE_AA)
            right = f"people {count}  tracks {len(ids_seen)}"
            (w, _), _ = cv2.getTextSize(right, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            cv2.putText(fr, right, (TILE_W - w - 10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, BLUE, 1, cv2.LINE_AA)
            cv2.circle(fr, (TILE_W - w - 22, 13), 3, GREEN, -1, cv2.LINE_AA)
            frames.append(fr)
        cap.release()
        model.predictor = None          # a fresh tracker per feed
        tiles.append(frames)
        print(name, len(frames), "frames,", len(ids_seen), "tracks", file=sys.stderr)
    n = min(len(t) for t in tiles)
    tmp = Path(tempfile.mkdtemp(prefix="multicam-"))
    raw = tmp / "raw.mp4"
    vw = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), fps, (TILE_W * 2 + 4, TILE_H * 2 + 4))
    for i in range(n):
        canvas = np.full((TILE_H * 2 + 4, TILE_W * 2 + 4, 3), (64, 39, 27), np.uint8)   # #1B2740 gutters
        canvas[0:TILE_H, 0:TILE_W] = tiles[0][i]
        canvas[0:TILE_H, TILE_W + 4:] = tiles[1][i]
        canvas[TILE_H + 4:, 0:TILE_W] = tiles[2][i]
        canvas[TILE_H + 4:, TILE_W + 4:] = tiles[3][i]
        vw.write(canvas)
    vw.release()
    ff = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ff, "-v", "error", "-y", "-i", str(raw), "-c:v", "libx264", "-preset", "slow", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)], check=True)
    poster = out.with_suffix(".jpg")
    cv2.imwrite(str(poster), canvas, [cv2.IMWRITE_JPEG_QUALITY, 82])
    shutil.rmtree(tmp, ignore_errors=True)
    print("wrote", out, out.stat().st_size // 1024, "KB and", poster)
    return 0


if __name__ == "__main__":
    sys.exit(main())
