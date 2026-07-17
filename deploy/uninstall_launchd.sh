#!/usr/bin/env bash
# Stop and remove BOTH launchd agents (paper loop + dashboard server). The
# bot shuts down gracefully (SIGTERM), persisting its state; positions remain
# recorded in state/paper_state.json.
set -euo pipefail

for name in paperloop dashboard; do
    TARGET="$HOME/Library/LaunchAgents/com.tradingbot.$name.plist"
    launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "removed com.tradingbot.$name"
done
echo "state files in the project are untouched"
