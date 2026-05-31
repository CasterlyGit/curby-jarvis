#!/usr/bin/env bash
# demo.sh — drive all 8 golden point-and-say demos through the REAL curby-jarvis
# CLI in --dry-run mode (zero side effects) and pretty-print each routing decision
# so the whole hybrid-router decision table is visible live.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Pick the python: prefer the project venv, else fall back to system python3.
if [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="python3"
fi

# Headless: never touch a real display server.
export QT_QPA_PLATFORM=offscreen

# Make the package importable even on a fresh clone (before `pip install -e .`):
# src-layout means curby_jarvis lives under src/.
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"

# Colors (fall back to no-op if not a tty).
if [[ -t 1 ]]; then
  CYAN=$'\033[36m'; RED=$'\033[31m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  CYAN=''; RED=''; BOLD=''; RESET=''
fi

# Pretty-printer: jq if present, else python's json.tool.
if command -v jq >/dev/null 2>&1; then
  pretty() { jq .; }
else
  pretty() { "$PY" -m json.tool; }
fi

# Each demo is: "label|utterance|extra-flags".
DEMOS=(
  "GD1|open Spotify|"
  "GD2|mute|"
  "GD3|close this window|"
  "GD4|next tab|"
  "GD5|play this|--pointer 700,400"
  "GD6|move that there|--pointer 300,300 --pointer2 800,600"
  "GD7|play this|"
  "GD8|reorganize my entire downloads folder by file type and date|"
)

routed=0
for entry in "${DEMOS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  utter="${rest%%|*}"
  flags="${rest#*|}"

  echo
  echo "${CYAN}${BOLD}${label}: ${utter}${RESET}"

  # Build argv; only append pointer flags when present (word-split intended).
  # shellcheck disable=SC2086
  if "$PY" -m curby_jarvis.app --say "$utter" --dry-run $flags | pretty; then
    routed=$((routed + 1))
  else
    echo "${RED}FAIL: ${label} (${utter}) exited non-zero${RESET}"
  fi
done

echo
echo "${BOLD}${routed}/8 demos routed.${RESET}"
