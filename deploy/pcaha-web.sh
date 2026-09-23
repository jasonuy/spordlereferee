#!/bin/bash
# Start / keep-alive helper for the PCAHA web UI on Studio.
set -euo pipefail

ROOT="${PCAHA_ROOT:-$HOME/projects/pcaha-schedule}"
LOG_DIR="${HOME}/Library/Logs"
mkdir -p "$LOG_DIR" "$ROOT/data"

if [[ -x "$ROOT/web/.venv/bin/python" ]]; then
  PYTHON="$ROOT/web/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

export PCAHA_DB="${PCAHA_DB:-$ROOT/data/pcaha.db}"
export PCAHA_SCORESHEET_CACHE="${PCAHA_SCORESHEET_CACHE:-$ROOT/data/scoresheets}"
export PCAHA_HOST="${PCAHA_HOST:-0.0.0.0}"
# Default 8765. Use PCAHA_PORT=80 only when the process can bind privileged ports
# (e.g. root LaunchDaemon) or after `sudo` auth.
export PCAHA_PORT="${PCAHA_PORT:-8765}"

cd "$ROOT/web"
exec "$PYTHON" server.py
