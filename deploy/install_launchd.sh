#!/usr/bin/env bash
# Install the paper-trading loop as a launchd user agent (macOS).
# It starts at login and restarts on crash. Review before running.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$PROJECT_DIR/deploy/com.tradingbot.paper.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.tradingbot.paper.plist"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "ERROR: $PROJECT_DIR/.venv/bin/python not found — create the venv first." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$TEMPLATE" > "$TARGET"

launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "installed and started: com.tradingbot.paper"
echo "  status:    launchctl list | grep tradingbot"
echo "  logs:      tail -f $PROJECT_DIR/logs/launchd.out.log"
echo "  stop:      $PROJECT_DIR/deploy/uninstall_launchd.sh"
echo
echo "NOTE: launchd restarts the bot, but it cannot keep a sleeping Mac awake."
echo "For a real 3-month paper run use a VPS/always-on machine, or prevent"
echo "sleep explicitly (System Settings > Energy, or 'caffeinate')."
