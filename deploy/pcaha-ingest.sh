#!/bin/bash
# Nightly PCAHA scoresheet ingest + rollup rebuild.
# Intended for Studio LaunchAgent (America/Vancouver ~02:15).

set -euo pipefail

ROOT="${PCAHA_ROOT:-$HOME/pcaha-schedule}"
LOG_DIR="${HOME}/Library/Logs"
LOG_FILE="${LOG_DIR}/pcaha-ingest.log"
DB="${PCAHA_DB:-$ROOT/data/pcaha.db}"
SEASON="${PCAHA_SEASON:-2026-27}"

mkdir -p "$LOG_DIR" "$(dirname "$DB")" "$ROOT/data/scoresheets"

# Prefer the app's venv / uv-managed python when present.
if [[ -x "$ROOT/web/.venv/bin/python" ]]; then
  PYTHON="$ROOT/web/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "No python3 found" >&2
  exit 1
fi

export PCAHA_DB="$DB"
export PCAHA_SCORESHEET_CACHE="${PCAHA_SCORESHEET_CACHE:-$ROOT/data/scoresheets}"

{
  echo "==== $(date '+%Y-%m-%d %H:%M:%S %Z') starting ingest ===="
  cd "$ROOT/web"
  # Fast path: yesterday's approved games, then full season catch-up for any misses.
  "$PYTHON" ingest.py --season "$SEASON" --yesterday
  "$PYTHON" ingest.py --season "$SEASON"
  echo "==== $(date '+%Y-%m-%d %H:%M:%S %Z') done ===="
} >>"$LOG_FILE" 2>&1
