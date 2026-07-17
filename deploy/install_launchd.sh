#!/usr/bin/env bash
# Install BOTH launchd user agents (macOS):
#   com.tradingbot.paperloop  — the paper-trading loop
#   com.tradingbot.dashboard  — live dashboard at http://127.0.0.1:8791/
# They start at login and restart on crash. Review before running.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-config_daily.yaml}"     # which config both services use

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "ERROR: $PROJECT_DIR/.venv/bin/python not found — create the venv first." >&2
    exit 1
fi
if [ ! -f "$PROJECT_DIR/$CONFIG" ]; then
    echo "ERROR: config '$CONFIG' not found in $PROJECT_DIR" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

for name in paperloop dashboard; do
    TEMPLATE="$PROJECT_DIR/deploy/com.tradingbot.$name.plist.template"
    TARGET="$HOME/Library/LaunchAgents/com.tradingbot.$name.plist"
    sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" -e "s|__CONFIG__|$CONFIG|g" \
        "$TEMPLATE" > "$TARGET"
    launchctl unload "$TARGET" 2>/dev/null || true
    launchctl load "$TARGET"
    echo "installed and started: com.tradingbot.$name"
done

echo
echo "  dashboard: http://127.0.0.1:8791/   (bookmark it)"
echo "  status:    launchctl list | grep tradingbot"
echo "  logs:      tail -f $PROJECT_DIR/logs/launchd.out.log"
echo "  stop all:  $PROJECT_DIR/deploy/uninstall_launchd.sh"
echo
echo "NOTE: launchd restarts the services, but it cannot keep a sleeping Mac"
echo "awake. Prevent sleep (System Settings > Energy) or use a VPS for a"
echo "clean multi-month record."
