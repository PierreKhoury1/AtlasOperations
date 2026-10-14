"""Record the Review workspace (WILDTRACK: four synchronised cameras, our tracks over the human labels, the
multi-camera count) as a video for the website. Nothing is staged: the page plays the evaluation's own review.json.

    py scripts/record_review.py --base http://127.0.0.1:8099 --cams C1 C2 C3 C6 --seconds 24 --out site/video/review.mp4

The server must serve /desk/review without a login (DESK_OPEN=1) and point ATLAS_REVIEW_OUT at the run's folder.
Needs playwright (chromium) and ffmpeg (FFMPEG=path or on PATH).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALL = ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]


async def run(a) -> Path:
    from playwright.async_api import async_playwright
    tmp = Path(tempfile.mkdtemp(prefix="review-rec-"))
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": a.width, "height": a.height}, device_scale_factor=1,
                                  record_video_dir=str(tmp), record_video_size={"width": a.width, "height": a.height})
        pg = await ctx.new_page()
        await pg.goto(a.base + "/desk/review", wait_until="load")
        await pg.wait_for_function("() => typeof RV !== 'undefined' && RV && RV.instants && RV.instants.length > 0 && document.querySelector('.view canvas')", timeout=120000)
        hide = [c for c in ALL if c not in a.cams]
        await pg.evaluate("(hs) => { hs.forEach(c => hide(c)); }", hide)
        await pg.evaluate("(s) => { const e = document.getElementById('spd'); if (e) { e.value = String(s); e.dispatchEvent(new Event('change')); } }", a.speed)
        await pg.wait_for_timeout(2500)                               # first frames of every camera in the cache
        await pg.keyboard.press("Space")                               # play
        await pg.wait_for_timeout(int(a.seconds * 1000))
        await pg.keyboard.press("Space")
        await ctx.close()
        await b.close()
    webm = next(tmp.glob("*.webm"))
    return webm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--cams", nargs="*", default=["C1", "C2", "C3", "C6"])
    ap.add_argument("--seconds", type=float, default=24)
    ap.add_argument("--speed", type=int, default=2)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default=str(ROOT / "site" / "video" / "review.mp4"))
    a = ap.parse_args()
    webm = asyncio.run(run(a))
    ff = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # the first ~3 s are the page settling; keep the playback
    subprocess.run([ff, "-v", "error", "-y", "-ss", "3", "-i", str(webm), "-c:v", "libx264", "-preset", "slow", "-crf", "22",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)], check=True)
    subprocess.run([ff, "-v", "error", "-y", "-ss", "4", "-i", str(webm), "-frames:v", "1", "-q:v", "3", str(out.with_suffix(".jpg"))], check=True)
    shutil.rmtree(webm.parent, ignore_errors=True)
    print("wrote", out, out.stat().st_size // 1024, "KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
