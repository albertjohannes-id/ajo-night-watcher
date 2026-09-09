#!/bin/bash
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.ajo.night-watcher.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
if [[ -f "$PLIST" ]]; then rm "$PLIST"; fi
printf 'Launch at login disabled. Saved tasks are retained.\n'
