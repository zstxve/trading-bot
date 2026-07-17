#!/usr/bin/env bash
# Install the watchdog as a cron job (every 5 minutes). This supervises the
# bot WITHOUT launchd agents — the fallback for machines where macOS
# background-item management refuses to run them (exit code 78).
# Idempotent: re-running replaces the existing entry.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-config_daily.yaml}"
ENTRY="*/5 * * * * $PROJECT_DIR/deploy/watchdog.sh $CONFIG"

# `|| true`: grep exits 1 on empty/no-match crontab, which set -e would
# otherwise turn into an abort before the new entry is emitted.
( crontab -l 2>/dev/null | grep -v "deploy/watchdog.sh" || true ; echo "$ENTRY" ) | crontab -

echo "installed cron watchdog:"
crontab -l | grep "deploy/watchdog.sh"
echo
echo "  starts/restarts bot + dashboard within 5 minutes, also after reboot"
echo "  activity log: $PROJECT_DIR/logs/watchdog.log"
echo "  remove:       $PROJECT_DIR/deploy/uninstall_cron.sh"
echo
echo "NOTE: if cron cannot touch this folder (macOS privacy), grant it access:"
echo "System Settings > Privacy & Security > Full Disk Access > add /usr/sbin/cron"
