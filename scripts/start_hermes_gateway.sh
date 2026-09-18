#!/usr/bin/env bash
# Start the Hermes Agent gateway inside WSL if it is not already running.
# Kept in a file (not inline in AtlasDesk.bat) so the Windows PATH, which
# contains parentheses, is never re-parsed by bash, and so pgrep cannot
# match its own launcher command line.
if pgrep -f '[h]ermes gateway' >/dev/null; then
  exit 0
fi
nohup "$HOME/.local/bin/hermes" gateway > "$HOME/.hermes/gateway.atlas.log" 2>&1 &
sleep 2
