#!/bin/bash
# Fast deploy: rsync local checkout → Mac Studio and restart the web service.
# Run on a LAN Mac (e.g. MacBook) that can SSH to Studio.
#
# Usage (from repo root):
#   ./deploy/deploy-studio.sh
#   STUDIO_HOST=jasonbot@192.168.5.156 ./deploy/deploy-studio.sh

set -euo pipefail

STUDIO_HOST="${STUDIO_HOST:-jasonbot@192.168.5.156}"
REMOTE_ROOT="${REMOTE_ROOT:-/Users/jasonbot/pcaha-schedule}"
BRANCH="${BRANCH:-cursor/pcaha-stats-standings-22f9}"
REPO_URL="${REPO_URL:-https://github.com/jasonuy/spordlereferee.git}"
SEASON="${PCAHA_SEASON:-2026-27}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Ensuring local branch $BRANCH"
cd "$LOCAL_ROOT"
git fetch origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || git pull --ff-only

echo "==> Rsync app → $STUDIO_HOST:$REMOTE_ROOT"
ssh -o ConnectTimeout=15 "$STUDIO_HOST" "mkdir -p '$REMOTE_ROOT' '$REMOTE_ROOT/data/scoresheets' '$REMOTE_ROOT/deploy' '\$HOME/Library/LaunchAgents' '\$HOME/Library/Logs'"

rsync -az --delete \
  --exclude '.git/' \
  --exclude 'data/pcaha.db' \
  --exclude 'data/pcaha.db-*' \
  --exclude 'data/scoresheets/' \
  --exclude 'web/.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  "$LOCAL_ROOT/" "$STUDIO_HOST:$REMOTE_ROOT/"

echo "==> Restart web + optional ingest on Studio"
ssh -o ConnectTimeout=15 "$STUDIO_HOST" bash -s <<EOF
set -euo pipefail
ROOT="$REMOTE_ROOT"
SEASON="$SEASON"

chmod +x "\$ROOT/deploy/"*.sh "\$ROOT/web/ingest.py" 2>/dev/null || true
cp "\$ROOT/deploy/com.jasonuy.pcaha-ingest.plist" "\$HOME/Library/LaunchAgents/" 2>/dev/null || true
cp "\$ROOT/deploy/com.jasonuy.pcaha-schedule.plist" "\$HOME/Library/LaunchAgents/"

if [[ -x "\$ROOT/web/.venv/bin/python" ]]; then
  PYTHON="\$ROOT/web/.venv/bin/python"
else
  PYTHON="\$(command -v python3)"
fi
"\$PYTHON" -c "import fastapi, uvicorn, requests" 2>/dev/null || \
  "\$PYTHON" -m pip install --user 'fastapi>=0.115' 'uvicorn>=0.32' 'requests>=2.32'

export PCAHA_DB="\$ROOT/data/pcaha.db"
export PCAHA_SCORESHEET_CACHE="\$ROOT/data/scoresheets"
export PCAHA_HOST=0.0.0.0
export PCAHA_PORT=8765

cd "\$ROOT/web"
if [[ ! -f "\$PCAHA_DB" ]]; then
  echo "==> First-time full ingest (several minutes)..."
  "\$PYTHON" ingest.py --season "\$SEASON"
else
  echo "==> Refreshing rollups / recent games..."
  "\$PYTHON" ingest.py --season "\$SEASON" --yesterday || true
  "\$PYTHON" ingest.py --rebuild-only --season "\$SEASON" || \
    "\$PYTHON" ingest.py --season "\$SEASON"
fi

# Stop any ad-hoc server on 8765, then KeepAlive LaunchAgent
if command -v lsof >/dev/null 2>&1; then
  PIDS=\$(lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "\${PIDS:-}" ]]; then
    echo "==> Stopping old process(es) on :8765: \$PIDS"
    kill \$PIDS 2>/dev/null || true
    sleep 1
  fi
fi

UID_NUM=\$(id -u)
launchctl bootout "gui/\$UID_NUM/com.jasonuy.pcaha-schedule" 2>/dev/null || true
launchctl bootstrap "gui/\$UID_NUM" "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist" 2>/dev/null \
  || launchctl load -w "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist"
launchctl kickstart -k "gui/\$UID_NUM/com.jasonuy.pcaha-schedule" 2>/dev/null \
  || launchctl start com.jasonuy.pcaha-schedule || true

launchctl bootout "gui/\$UID_NUM/com.jasonuy.pcaha-ingest" 2>/dev/null || true
launchctl bootstrap "gui/\$UID_NUM" "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-ingest.plist" 2>/dev/null \
  || launchctl load -w "\$HOME/Library/LaunchAgents/com.jasonuy.pcaha-ingest.plist" || true

sleep 2
curl -fsS "http://127.0.0.1:8765/" | grep -q 'Standings' && echo "==> HTML has Standings tab"
curl -fsS "http://127.0.0.1:8765/api/stats/meta" || true
echo "==> Studio ready at http://192.168.5.156:8765"
EOF

echo "==> Verifying from this machine..."
html=\$(curl -fsS --connect-timeout 5 "http://192.168.5.156:8765/" || true)
echo "\$html" | grep -o 'Schedule\|Standings' | sort | uniq -c || echo "(could not fetch HTML)"
curl -fsS --connect-timeout 5 "http://192.168.5.156:8765/api/stats/meta" && echo
echo "Done. Hard-refresh the browser (Cmd+Shift+R) on http://192.168.5.156:8765"
