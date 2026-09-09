#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p assets work/AjoNightWatcher.iconset
xcrun swift scripts/make-icon.swift assets/AjoNightWatcher.png
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" assets/AjoNightWatcher.png --out "work/AjoNightWatcher.iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" assets/AjoNightWatcher.png --out "work/AjoNightWatcher.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns work/AjoNightWatcher.iconset -o assets/AjoNightWatcher.icns
