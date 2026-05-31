#!/usr/bin/env bash
# run-live.sh — launch the real curby-jarvis live run loop (pointer stream ->
# fusion -> reticle HUD -> utterance -> route -> confirm -> execute).
# Requires Accessibility + Microphone TCC grants, and the hand-signal gesture
# websocket running so the deixis pointer stream has a source.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Pick the python: prefer the project venv, else fall back to system python3.
if [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="python3"
fi

exec "$PY" -m curby_jarvis.app --live "$@"
