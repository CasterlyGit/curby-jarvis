#!/usr/bin/env bash
# ensure-running.sh — keep curby-jarvis live run running (respawn if crashed)
# Call from cron / login hook / or manually. Requires the terminal session
# context for Microphone + Accessibility TCC grants.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$HOME/.curby/live.pid"
mkdir -p "$(dirname "$PIDFILE")"

# If already running, exit early
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  exit 0
fi

# Launch in background and record PID
cd "$REPO"
bash scripts/run-live.sh &
echo $! > "$PIDFILE"
wait
