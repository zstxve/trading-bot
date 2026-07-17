#!/usr/bin/env bash
# Remove the watchdog cron entry (running processes are left alone).
set -euo pipefail
crontab -l 2>/dev/null | grep -v "deploy/watchdog.sh" | crontab -
echo "watchdog cron entry removed (bot/dashboard processes untouched)"
