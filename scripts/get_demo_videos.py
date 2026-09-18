"""Download the camera-journal demo pack: real CCTV-style clips of a corner store, a hotel lobby and a sushi counter,
converted to small 960 px H.264 MP4s that play as live, looping cameras (any .mp4 path works as a camera source).

    py scripts/get_demo_videos.py [out_dir]          # default C:/Users/<you>/AtlasDemo/videos (or ~/AtlasDemo/videos)

Sources and licences
  Wikimedia Commons, public domain: Ezymart convenience-store CCTV (wallet taken from a backpack), iProx retail-store
    CCTV, X-Tragos liquor-store counter CCTV
  Wikimedia Commons, CC BY 3.0: "2026-0120 Hiden sushi making Seijun Okano" (credit the uploader if you show it)
  EC Funded CAVIAR project / IST 2001 37540 (INRIA lobby): Browse4, LeftBag, Meet_Crowd, Browse_WhileWaiting2
    - free for research use; cite CAVIAR if published
Requires ffmpeg on PATH.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

UA = {"User-Agent": "AtlasDesk demo downloader (https://atlas-ops.onrender.com)"}
COMMONS = "https://upload.wikimedia.org/wikipedia/commons/"
CAVIAR = "https://homepages.inf.ed.ac.uk/rbf/CAVIARDATA1/"
CLIPS = {
    "corner-store_ezymart": COMMONS + "1/17/CCTV_footage_of_wallet_theft_at_Ezymart_in_Newtown%2C_Sydney%2C_NSW.webm",
    "retail-store": COMMONS + "7/77/HD_CCTV_Camera_video_3MP_4MP_iProx_CCTV_HDCCTVCameras.net_retail_store.webm",
    "liquor-store-delivery": COMMONS + "e/e7/X-Tragos-Licoreria-delivery_75166_%28CCTV-Footage_15_June_2025%29.webm",
    "restaurant-sushi-counter": COMMONS + "5/5b/2026-0120_Hiden_sushi_making_Seijun_Okano.webm",
    "hotel-lobby_Browse4": CAVIAR + "Browse4/Browse4.mpg",
    "hotel-lobby_LeftBag": CAVIAR + "LeftBag/LeftBag.mpg",
    "hotel-lobby_Meet_Crowd": CAVIAR + "Meet_Crowd/Meet_Crowd.mpg",
    "hotel-lobby_Browse_WhileWaiting2": CAVIAR + "Browse_WhileWaiting2/Browse_WhileWaiting2.mpg",
}


def main(argv: list[str]) -> int:
    ff = shutil.which("ffmpeg")
    if not ff:
        print("ffmpeg is required (winget install ffmpeg / apt install ffmpeg)")
        return 2
    out = Path(argv[0]) if argv else Path.home() / "AtlasDemo" / "videos"
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for name, url in CLIPS.items():
        mp4 = out / f"{name}.mp4"
        if mp4.exists():
            print(f"have {mp4.name}")
            continue
        src = raw / (name + Path(url.split("?")[0]).suffix)
        if not src.exists():
            print(f"downloading {name} ...", flush=True)
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
        subprocess.run([ff, "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-vf", "scale='min(960,iw)':-2", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart", str(mp4)], check=True)
        print(f"ready {mp4}")
    print(f"\nAdd any of these as a camera source on the Cameras page, e.g.\n  {out / 'hotel-lobby_LeftBag.mp4'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
