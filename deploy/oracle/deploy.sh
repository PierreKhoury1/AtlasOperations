#!/usr/bin/env bash
# Update the Atlas portal on the VM: pull, install any new requirements, restart, health-check.
# Run on the VM (ssh ubuntu@VM 'bash /opt/atlas/src/deploy/oracle/deploy.sh') - the GitHub Action does exactly this.
set -euo pipefail
ROOT=/opt/atlas
cd "$ROOT/src"
git fetch -q origin
git reset -q --hard "origin/$(git rev-parse --abbrev-ref HEAD)"
"$ROOT/venv/bin/pip" install -q -r requirements.txt
sudo systemctl restart atlas
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  if curl -sf -m 5 http://127.0.0.1:8094/api/health >/dev/null; then
    echo "deployed $(git rev-parse --short HEAD): $(curl -s http://127.0.0.1:8094/api/health)"
    exit 0
  fi
done
echo "portal did not come back - last log lines:"; sudo journalctl -u atlas -n 40 --no-pager
exit 1
