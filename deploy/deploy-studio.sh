#!/bin/bash
# Fast deploy: rsync local checkout → Mac Studio and restart the web service.
# Run on a LAN Mac that can SSH to Studio.
#
# Usage (from repo root):
#   ./deploy/deploy-studio.sh
#   PCAHA_PORT=80 STUDIO_HOST=jasonbot@192.168.5.156 ./deploy/deploy-studio.sh

set -euo pipefail

STUDIO_HOST="${STUDIO_HOST:-jasonbot@192.168.5.156}"
# MUST be on internal disk — LaunchAgents cannot execute from /Volumes/External Drive
# (~/projects is often a symlink to the external volume on Studio).
REMOTE_ROOT="${REMOTE_ROOT:-/Users/jasonbot/pcaha-schedule}"
BRANCH="${BRANCH:-cursor/pcaha-stats-standings-22f9}"
SEASON="${PCAHA_SEASON:-2026-27}"
APP_PORT="${PCAHA_PORT:-8765}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Ensuring local branch $BRANCH"
cd "$LOCAL_ROOT"
git fetch origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || git pull --ff-only

echo "==> Rsync app → $STUDIO_HOST:$REMOTE_ROOT"
ssh -o ConnectTimeout=15 "$STUDIO_HOST" \
  "mkdir -p '$REMOTE_ROOT/data/scoresheets' '$REMOTE_ROOT/deploy' \"\$HOME/Library/LaunchAgents\" \"\$HOME/Library/Logs\""

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

echo "==> Restart web on Studio (port $APP_PORT)"
ssh -o ConnectTimeout=15 "$STUDIO_HOST" \
  "REMOTE_ROOT='$REMOTE_ROOT' SEASON='$SEASON' APP_PORT='$APP_PORT' bash -s" <<'EOF'
set -euo pipefail
ROOT="$REMOTE_ROOT"
OLD_ROOT="$HOME/pcaha-schedule"

chmod +x "$ROOT/deploy/"*.sh "$ROOT/web/ingest.py" 2>/dev/null || true

if [[ -d "$OLD_ROOT/data" && ! -f "$ROOT/data/pcaha.db" ]]; then
  echo "==> Migrating data from $OLD_ROOT/data"
  mkdir -p "$ROOT/data"
  cp -a "$OLD_ROOT/data/." "$ROOT/data/" || true
fi

# Install LaunchAgents (paths already baked for ~/projects/pcaha-schedule)
cp "$ROOT/deploy/com.jasonuy.pcaha-schedule.plist" "$HOME/Library/LaunchAgents/"
cp "$ROOT/deploy/com.jasonuy.pcaha-ingest.plist" "$HOME/Library/LaunchAgents/"
# Override port in the installed schedule plist when requested
if [[ "$APP_PORT" != "8765" ]]; then
  /usr/bin/sed -i '' "s#<string>8765</string>#<string>${APP_PORT}</string>#" \
    "$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist"
fi

if [[ -x "$ROOT/web/.venv/bin/python" ]]; then
  PYTHON="$ROOT/web/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi
"$PYTHON" -c "import fastapi, uvicorn, requests" 2>/dev/null || \
  "$PYTHON" -m pip install --user 'fastapi>=0.115' 'uvicorn>=0.32' 'requests>=2.32'

export PCAHA_ROOT="$ROOT"
export PCAHA_DB="$ROOT/data/pcaha.db"
export PCAHA_SCORESHEET_CACHE="$ROOT/data/scoresheets"
export PCAHA_HOST=0.0.0.0
export PCAHA_PORT="$APP_PORT"

cd "$ROOT/web"
if [[ ! -f "$PCAHA_DB" ]]; then
  echo "==> First-time full ingest..."
  "$PYTHON" ingest.py --season "$SEASON"
else
  echo "==> Refreshing recent games / rollups..."
  "$PYTHON" ingest.py --season "$SEASON" --yesterday || true
  "$PYTHON" ingest.py --rebuild-only --season "$SEASON" || true
fi

if command -v lsof >/dev/null 2>&1; then
  for p in 80 8765 "$APP_PORT"; do
    PIDS=$(lsof -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "${PIDS:-}" ]]; then
      echo "==> Stopping :$p ($PIDS)"
      kill $PIDS 2>/dev/null || true
    fi
  done
  sleep 1
fi

UID_NUM=$(id -u)
launchctl bootout "gui/$UID_NUM/com.jasonuy.pcaha-schedule" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist" 2>/dev/null \
  || launchctl load -w "$HOME/Library/LaunchAgents/com.jasonuy.pcaha-schedule.plist"
launchctl kickstart -k "gui/$UID_NUM/com.jasonuy.pcaha-schedule" 2>/dev/null \
  || launchctl start com.jasonuy.pcaha-schedule || true

launchctl bootout "gui/$UID_NUM/com.jasonuy.pcaha-ingest" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$HOME/Library/LaunchAgents/com.jasonuy.pcaha-ingest.plist" 2>/dev/null \
  || launchctl load -w "$HOME/Library/LaunchAgents/com.jasonuy.pcaha-ingest.plist" || true

sleep 2
for p in "$APP_PORT" 8765 80; do
  if curl -fsS "http://127.0.0.1:$p/" 2>/dev/null | grep -q Standings; then
    echo "==> Standings UI live on :$p  (root=$ROOT)"
    curl -fsS "http://127.0.0.1:$p/api/stats/meta" || true
    exit 0
  fi
done
echo "==> WARNING: Standings UI not confirmed. Check ~/Library/Logs/pcaha-schedule.err.log"
tail -n 40 "$HOME/Library/Logs/pcaha-schedule.err.log" 2>/dev/null || true
exit 1
EOF

echo "Done. Hard-refresh http://192.168.5.156/ or http://192.168.5.156:$APP_PORT"
