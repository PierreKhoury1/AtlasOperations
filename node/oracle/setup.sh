#!/usr/bin/env bash
# Atlas Vision Node - one-shot install on a fresh Oracle Cloud Always-Free VM (Ubuntu 22.04/24.04, Ampere A1 or x86).
# Run as the default `ubuntu` user:
#
#   curl -fsSL https://raw.githubusercontent.com/PierreKhoury1/hermes-ops/master/node/oracle/setup.sh | bash -s -- \
#       "https://atlas-ops.onrender.com/hook/YOUR_TOKEN/vision"
#
# Optional 2nd arg: path to a node.json to install (otherwise node.example.json is copied with the hook filled in,
# and you edit /opt/atlas-node/config/node.json afterwards, then: sudo docker restart atlas-node).
# Re-running the script pulls the latest code and rebuilds - safe to use as the update command too.
set -euo pipefail

HOOK="${1:-}"
CFG_SRC="${2:-}"
REPO="${ATLAS_REPO:-https://github.com/PierreKhoury1/hermes-ops.git}"
BRANCH="${ATLAS_BRANCH:-master}"
ROOT=/opt/atlas-node

echo "==> packages"
sudo apt-get update -y -qq
sudo apt-get install -y -qq ca-certificates curl git python3 >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  echo "==> docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

echo "==> source"
sudo mkdir -p "$ROOT/config" "$ROOT/models"
sudo chown -R "$USER":"$USER" "$ROOT"
if [ -d "$ROOT/src/.git" ]; then
  git -C "$ROOT/src" fetch --depth 1 origin "$BRANCH" && git -C "$ROOT/src" reset --hard "origin/$BRANCH"
else
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$ROOT/src"
fi

if [ -n "$CFG_SRC" ]; then
  cp "$CFG_SRC" "$ROOT/config/node.json"
elif [ ! -f "$ROOT/config/node.json" ]; then
  [ -n "$HOOK" ] || { echo "usage: setup.sh <hook url> [node.json]"; exit 2; }
  python3 - "$ROOT/src/node/node.example.json" "$ROOT/config/node.json" "$HOOK" <<'PY'
import json, sys
src, dst, hook = sys.argv[1:4]
cfg = json.load(open(src))
cfg["hook"] = hook
json.dump(cfg, open(dst, "w"), indent=2)
print("wrote", dst, "- edit the cameras list, then: sudo docker restart atlas-node")
PY
fi
chmod 600 "$ROOT/config/node.json"

echo "==> build (first time takes ~10 min on A1: torch + openvino wheels)"
sudo docker build -t atlas-node -f "$ROOT/src/node/Dockerfile" "$ROOT/src"

echo "==> run"
sudo docker rm -f atlas-node >/dev/null 2>&1 || true
sudo docker run -d --name atlas-node --restart unless-stopped \
  -v "$ROOT/config:/config" -v "$ROOT/models:/models" \
  -p 127.0.0.1:8765:8765 \
  --log-opt max-size=20m --log-opt max-file=5 \
  atlas-node

# Oracle Ubuntu images ship iptables rules that only allow SSH - fine, the node needs no inbound port.
# Keep the free VM from being reclaimed as idle: continuous inference already keeps CPU > 20%.

echo
echo "node running.  logs:   sudo docker logs -f atlas-node"
echo "               health: curl -s localhost:8765 | python3 -m json.tool"
echo "               config: $ROOT/config/node.json  (then: sudo docker restart atlas-node)"
echo "               update: re-run this script"
