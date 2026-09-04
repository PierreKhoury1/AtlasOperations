"""One-click local test: portal up -> node pointed at its hook -> Cameras page open -> node running in this window.

    py node/launch_local.py            (Desktop\\AtlasVisionNode.bat does exactly this)

Env: ATLAS_PORTAL (default http://127.0.0.1:8094), ATLAS_DESK_BAT (default Desktop\\AtlasDesk.bat, started if the
portal is down). Cameras come from node/node.json; when it does not exist one is created from node.example.json
plus the sample shop clips in Desktop\\shop-cams (two angles per scene) so there is something to watch immediately.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORTAL = os.environ.get("ATLAS_PORTAL", "http://127.0.0.1:8094").rstrip("/")
DESK_BAT = Path(os.environ.get("ATLAS_DESK_BAT", Path.home() / "Desktop" / "AtlasDesk.bat"))
SHOP_CLIPS = Path.home() / "Desktop" / "shop-cams"
CFG = HERE / "node.json"


def say(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def health() -> dict | None:
    try:
        import httpx
        r = httpx.get(PORTAL + "/api/health", timeout=4)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def ensure_portal() -> None:
    if health():
        say("portal up:", PORTAL)
        return
    if not DESK_BAT.exists():
        sys.exit(f"portal {PORTAL} is down and {DESK_BAT} does not exist - start the desk first")
    say("portal down, starting", DESK_BAT.name)
    subprocess.Popen(["cmd", "/c", "start", "", str(DESK_BAT)], shell=False)
    for _ in range(45):
        time.sleep(2)
        if health():
            say("portal up")
            return
    sys.exit("portal did not come up in 90 s - check the Atlas Desk window")


def hook_url() -> str:
    import httpx
    r = httpx.get(PORTAL + "/api/cameras", timeout=15)
    if r.status_code != 200:
        sys.exit(f"cannot read the hook URL ({r.status_code}) - is the portal open (DESK_OPEN=1) or are you logged in?")
    return r.json()["hook_url"].replace("://localhost", "://127.0.0.1")


def default_cameras() -> list[dict]:
    cams: list[dict] = [{"name": "laptop", "source": "0", "watch_for": ["person"], "min_count": 1, "cooldown_s": 600}]
    if SHOP_CLIPS.exists():
        scene = {"TwoEnterShop1": "shopA", "OneShopOneWait1": "shopB", "ShopAssistant1": "shopC"}
        for f in sorted(SHOP_CLIPS.glob("*.mp4")):
            m = re.match(r"(.+?)(front|cor)\.mp4$", f.name)
            if not m:
                continue
            tag = scene.get(m.group(1), m.group(1))
            angle = "front" if m.group(2) == "front" else "corridor"
            cams.append({"name": f"{tag}-{angle}", "source": str(f).replace("\\", "/"), "watch_for": ["person"],
                         "min_count": 2, "cooldown_s": 300, "confirm_s": 0.5, "gone_s": 2})
    return cams


def write_config(hook: str) -> dict:
    if CFG.exists():
        cfg = json.loads(CFG.read_text(encoding="utf-8"))
    else:
        cfg = json.loads((HERE / "node.example.json").read_text(encoding="utf-8"))
        cfg["cameras"] = default_cameras()
        cfg["heartbeat_min"] = 10
        say("created", CFG.name, "with", len(cfg["cameras"]), "cameras")
    cfg["hook"] = hook
    CFG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def stop_previous(port: int) -> None:
    """A node already running on the health port is stopped so the button always means 'restart'."""
    if os.name != "nt":
        return
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    except Exception:
        return
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = line.split()[-1]
            if pid.isdigit() and int(pid) != os.getpid():
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
                say("stopped previous node (pid", pid + ")")
                time.sleep(1)


def main() -> int:
    os.chdir(HERE.parent)
    ensure_portal()
    hook = hook_url()
    cfg = write_config(hook)
    stop_previous(int(cfg.get("health_port", 8765)))
    say("hook:", re.sub(r"/hook/[^/]+", "/hook/<token>", hook))
    say("cameras:", ", ".join(c["name"] for c in cfg["cameras"]))
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")     # Windows webcams open reliably through DirectShow
    webbrowser.open(PORTAL + "/desk#cameras")
    sys.path.insert(0, str(HERE))
    import atlas_node
    return atlas_node.main(["--config", str(CFG)])


if __name__ == "__main__":
    sys.exit(main())
