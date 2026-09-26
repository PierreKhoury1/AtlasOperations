#!/usr/bin/env bash
# Nightly: pg_dump of the atlas database + tarball of the data dir. Keeps 30 days.
# Runs as user atlas with /etc/atlas/atlas.env loaded (DATABASE_URL, ATLAS_DATA_DIR).
set -euo pipefail
OUT=/srv/atlas/backups
STAMP=$(date -u +%Y%m%d-%H%M)
mkdir -p "$OUT"
pg_dump "$DATABASE_URL" --no-owner --no-privileges -Fc -f "$OUT/atlas-$STAMP.dump"
tar -czf "$OUT/data-$STAMP.tgz" -C "$(dirname "$ATLAS_DATA_DIR")" "$(basename "$ATLAS_DATA_DIR")" 2>/dev/null || true
find "$OUT" -type f -mtime +30 -delete
echo "backup: $OUT/atlas-$STAMP.dump ($(du -h "$OUT/atlas-$STAMP.dump" | cut -f1))"
