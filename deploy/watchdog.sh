#!/usr/bin/env bash
# Watchdog: ensure the paper loop and the dashboard server are running.
# Safe to run as often as you like — the runner's instance lock and the
# pgrep checks make duplicates impossible. Installed via install_cron.sh
# to run every 5 minutes (launchd-independent supervision).
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-config_daily.yaml}"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -qf "tradingbot paper"; then
    nohup .venv/bin/python -m tradingbot paper --config "$CONFIG" \
        >> logs/nohup.out.log 2>&1 &
    echo "$(date -u +%FT%TZ) watchdog: started paper loop (pid $!)" >> logs/watchdog.log
fi

if ! pgrep -qf "tradingbot dashboard-server"; then
    nohup .venv/bin/python -m tradingbot dashboard-server --config "$CONFIG" \
        >> logs/dashboard.nohup.log 2>&1 &
    echo "$(date -u +%FT%TZ) watchdog: started dashboard (pid $!)" >> logs/watchdog.log
fi

echo "$(date -u +%FT%TZ) watchdog: tick" >> logs/watchdog.log
