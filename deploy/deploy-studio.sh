#!/bin/bash
# Deploy PCAHA schedule + standings to Mac Studio and restart the web service.
# Run from a machine that can reach Studio (LAN / SSH), e.g. Jason's MacBook.
#
# Usage:
#   ./deploy/deploy-studio.sh
#   STUDIO_HOST=jasonbot@192.168.5.156 BRANCH=cursor/pcaha-stats-standings-22f9 ./deploy/deploy-studio.sh

set -euo pipefail

STUDIO_HOST="${STUDIO_HOST:-jasonbot@192.168.5.156}"
REMOTE_ROOT="${REMOTE_ROOT:-/Users/jasonbot/pcaha-schedule}"
BRANCH="${BRANCH:-cursor/pcaha-stats-standings-22f9}"
REPO_URL="${REPO_URL:-https://github.com/jasonuy/spordlereferee.git}"
SEASON="${PCAHA_SEASON:-2026-27}"

echo "==> Deploying $BRANCH to $STUDIO_HOST:$REMOTE_ROOT"

ssh -o ConnectTimeout=15 "$STUDIO_HOST" bash -s <<EOF
set -euo pipefail
ROOT="$REMOTE_ROOT"
BRANCH="$BRANCH"
REPO_URL="$REPO_URL"
SEASON="$SEASON"

mkdir -p "\$ROOT" "\$HOME/Library/LaunchAgents" "\$HOME/Library/Logs" "\$ROOT/data/scoresheets" "\$ROOT/deploy"

if [[ ! -d "\$ROOT/.git" ]]; then
  git clone "\$REPO_URL" "\$ROOT"
fi

cd "\$ROOT"
git fetch origin
git checkout "\$BRANCH"
git pull --ff-only origin "\$BRANCH"

# Prefer internal-disk python (LaunchAgents cannot use /Volumes/External Drive)
if [[ -x "\$ROOT/web/.venv/bin/python" ]]; then
  PYTHON="\$ROOT/web/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="\$(command -v python3)"
else
  echo "No python3 on Studio" >&2
  exit 1
fi

# Ensure deps
"\$PYTHON" -c "import fastapi, uvicorn, requests" 2>/dev/null || \
  "\$PYTHON" -m pip install --user 'fastapi>=0.115' 'uvicorn>=0.32' 'requests>=2.32'

export PCAHA_DB="\$ROOT/data/pcaha.db"
export PCAHA_SCORESHEET_CACHE="\$ROOT/data/scoresheets"
export PCAHA_HOST=0.0.0.0
export PCAHA_PORT=8765

# Install LaunchAgents
cp "\$ROOT/deploy/pcaha-ingest.sh" "\$ROOT/deploy/pcaha-ingest.sh"
chmod +x "\$ROOT/deploy/pcaha-ingest.sh" "\$ROOT/web/ingest.py"
cp "\$ROOT/deploy/com.jasonuy.pcaha-ingest.plist" "\$HOME/Library/LaunchAgents/"
cp "\$ROOT/deploy/com.jasonuy.pcaha-schedule.plist" "\$HOME/Library/LaunchAgents/"
chmod +x "\$ROOT/deploy/pcaha-web.sh"

# Ingest / refresh rollups (uses disk cache; polite to API for new games)
cd "\$ROOT/web"
if [[ ! -f "\$PCAHA_DB" ]]; then
  echo "==> First-time full ingest..."
  "\$PYTHON" ingest.py --season "\$SEASON"
else
  echo "==> Incremental ingest (yesterday + catch-up)..."
  "\$PYTHON" ingest.py --season "\$SEASON" --yesterday || true
  "\$PYTHON" ingest.py --season "\$SEASON"
fi

# Restart web LaunchAgent
launchctl bootout "gui/\$(id -u)/com.jasonuy.pcaha-schedule" 2>/dev/null || true
launchctl bootstrap "gui/\$(id -u)" "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist" 2>/dev/null \
  || launchctl load "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist"
launchctl kickstart -k "gui/\$(id -u)/com.jasonuy.pcaha-schedule" 2>/dev/null \
  || launchctl start com.jasonuy.pcaha-schedule || true

# Also load ingest agent (does not run until schedule)
launchctl bootout "gui/\$(id -u)/com.jasonuy.pcaha-ingest" 2>/dev/null || true
launchctl bootstrap "gui/\$(id -u)" "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-ingest.plist" 2>/dev/null \
  || launchctl load "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-ingest.plist" || true

sleep 2
curl -fsS "http://127.0.0.1:8765/api/stats/meta" || curl -fsS "http://127.0.0.1:8765/" >/dev/null
echo "==> Studio ready at http://192.168.5.156:8765"
EOF

echo "==> Verifying from this machine..."
curl -fsS --connect-timeout 5 "http://192.168.5.156:8765/api/stats/meta" && echo
curl -fsS --connect-timeout 5 "http://192.168.5.156:8765/" | head -c 200
echo
echo "Done. Open http://192.168.5.156:8765 — tabs: Schedule | Standings"
