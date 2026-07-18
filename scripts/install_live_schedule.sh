#!/bin/bash
# Install (or reinstall) the launchd job that runs `live_cross.py cycle`
# every 10 minutes. Copy trading itself is server-side at Bullpen and does
# not depend on this machine being awake; this schedule drives the arb
# scanner, the seller, settlement, redemption and the kill switch.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.pma.live-cycle"
PLIST_SRC="$REPO/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "installed: $LABEL (every 10 min)"
echo "logs:      $REPO/data/live_cycle.log"
echo "remove:    launchctl unload $PLIST_DST && rm $PLIST_DST"
