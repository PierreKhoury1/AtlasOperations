#!/usr/bin/env bash
# Pull-based deploy. Runs every minute from atlas-deploy.timer (as root).
# Fetches origin/<branch>; if it moved: reset, reinstall requirements when they changed,
# restart the portal, health-check, roll back on failure. Idle otherwise.
set -euo pipefail
ROOT=/srv/atlas
BRANCH="${ATLAS_BRANCH:-main}"
cd "$ROOT/src"
before=$(git rev-parse HEAD)
sudo -u atlas git fetch -q origin "$BRANCH"
after=$(git rev-parse "origin/$BRANCH")
[ "$before" = "$after" ] && exit 0

echo "deploy: $before -> $after"
sudo -u atlas git reset -q --hard "origin/$BRANCH"
if ! git diff --quiet "$before" "$after" -- requirements.txt; then
  sudo -u atlas "$ROOT/venv/bin/pip" install -q -r requirements.txt
fi
if ! git diff --quiet "$before" "$after" -- deploy/hetzner/deploy.sh deploy/hetzner/backup.sh; then
  install -m 755 deploy/hetzner/deploy.sh /usr/local/bin/atlas-deploy
  install -m 755 deploy/hetzner/backup.sh /usr/local/bin/atlas-backup
fi
systemctl restart atlas.service
for i in $(seq 1 10); do
  sleep 2
  if curl -fsS -m 10 http://127.0.0.1:8094/api/health >/dev/null; then
    echo "deploy: healthy at $after"; exit 0
  fi
done
echo "deploy: health check FAILED at $after, rolling back to $before"
sudo -u atlas git reset -q --hard "$before"
systemctl restart atlas.service
exit 1
