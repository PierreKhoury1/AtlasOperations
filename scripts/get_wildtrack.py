"""Fetch WILDTRACK for `py -m atlas mcam-eval` and the Review page: 7 synchronised cameras, 400 labelled instants.

    python scripts/get_wildtrack.py [out_dir]          # default ~/AtlasDemo/wildtrack  (~250 MB kept, ~6.8 GB streamed)
    python scripts/get_wildtrack.py ~/wt C1 C4         # only some cameras
    python scripts/get_wildtrack.py --hd C1 C2 C3 C6   # also keep full 1920x1080 frames under frames-hd/ (Review prefers them)

Streams the official archive (a Hugging Face mirror of EPFL's Wildtrack_dataset_full.zip) with HTTP range requests and
keeps only what is needed: annotations_positions/*.json, calibrations/, and every frame downscaled to 960x540 JPEG under
frames/C1..C7/. The archive was written with a malformed ZIP64 end record, so Python's zipfile adds a phantom 2**32 to
every offset: each entry is located by trying both offsets and checking the local header's signature and name.

Licence: WILDTRACK (Chavdarova et al., CVPR 2018) is for non-commercial research use. Do not commit it or ship it.
Needs: pip install remotezip httpx pillow
"""
from __future__ import annotations

import io
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

URL = "https://huggingface.co/datasets/ShantyCam/wildtrack/resolve/main/Wildtrack_dataset_full.zip"


def main(argv: list[str]) -> int:
    import httpx
    from PIL import Image
    from remotezip import RemoteZip
    hd = "--hd" in argv
    argv = [a for a in argv if a != "--hd"]
    out = Path(argv[0]).expanduser() if argv and not argv[0].startswith("C") else Path.home() / "AtlasDemo" / "wildtrack"
    only = [a for a in argv if a.startswith("C")]
    c = httpx.Client(follow_redirects=True, timeout=120, limits=httpx.Limits(max_connections=16))

    def rng(off: int, n: int) -> bytes:
        return c.get(URL, headers={"Range": f"bytes={off}-{off + n - 1}"}).content

    def fetch(info) -> bytes:
        for off in (info.header_offset - 2**32, info.header_offset):
            if off < 0:
                continue
            h = rng(off, 30 + len(info.filename.encode()) + 4)
            if h[:4] != b"PK\x03\x04":
                continue
            nl, el = struct.unpack("<HH", h[26:30])
            if h[30:30 + nl].decode(errors="replace") != info.filename:
                continue
            raw = rng(off + 30 + nl + el, info.compress_size)
            return raw if info.compress_type == 0 else zlib.decompress(raw, -15)
        raise RuntimeError("no local header for " + info.filename)

    with RemoteZip(URL) as z:
        infos = [i for i in z.infolist() if not i.filename.startswith("__MACOSX") and not i.is_dir() and not i.filename.endswith(".DS_Store")]
    meta = [i for i in infos if "/annotations_positions/" in i.filename or "/calibrations/" in i.filename]
    imgs = [i for i in infos if "/Image_subsets/" in i.filename and i.filename.endswith(".png")]
    if only:
        imgs = [i for i in imgs if i.filename.split("/")[-2] in only]
    print(f"{len(meta)} label/calibration files, {len(imgs)} frames -> {out}", flush=True)

    def meta_one(i) -> None:
        p = out / i.filename.split("Wildtrack_dataset/", 1)[1]
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(fetch(i))

    def img_one(i) -> int:
        cam, fn = i.filename.split("/")[-2:]
        p = out / "frames" / cam / (fn[:-4] + ".jpg")
        q = out / "frames-hd" / cam / (fn[:-4] + ".jpg")
        if p.exists() and (not hd or q.exists()):
            return 0
        p.parent.mkdir(parents=True, exist_ok=True)
        im = Image.open(io.BytesIO(fetch(i))).convert("RGB")
        if hd:
            q.parent.mkdir(parents=True, exist_ok=True)
            im.save(q, "JPEG", quality=90)
        if not p.exists():
            im.resize((960, 540), Image.LANCZOS).save(p, "JPEG", quality=86)
        return i.compress_size

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(meta_one, meta))
    t0, done, byt = time.time(), 0, 0
    with ThreadPoolExecutor(12) as ex:
        for f in as_completed([ex.submit(img_one, i) for i in imgs]):
            byt += f.result()
            done += 1
            if done % 200 == 0:
                print(f"{done}/{len(imgs)}  {byt / 1e9:.2f} GB streamed", flush=True)
    print(f"done: {done} frames in {time.time() - t0:.0f}s. Next: py -m atlas mcam-eval --data {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
