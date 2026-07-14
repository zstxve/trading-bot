#!/usr/bin/env bash
# Stop and remove the launchd agent. The bot shuts down gracefully (SIGTERM),
# persisting its state; positions remain recorded in state/paper_state.json.
set -euo pipefail

TARGET="$HOME/Library/LaunchAgents/com.tradingbot.paperloop.plist"
launchctl unload "$TARGET" 2>/dev/null || true
rm -f "$TARGET"
echo "removed com.tradingbot.paperloop (state files in the project are untouched)"
