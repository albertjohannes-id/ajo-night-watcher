#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
APP="$PWD/dist/Ajo Night Watcher.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -target "$(uname -m)-apple-macosx13.0" -O -swift-version 5 Sources/AjoNightWatcher.swift -o "$APP/Contents/MacOS/AjoNightWatcher" -framework AppKit -framework SwiftUI -framework UserNotifications
cp assets/AjoNightWatcher.icns "$APP/Contents/Resources/AjoNightWatcher.icns"
cp assets/AjoNightWatcher.png "$APP/Contents/Resources/AjoNightWatcher.png"
cp backend/codex_usage.py "$APP/Contents/Resources/codex_usage.py"
cp backend/watcher.py "$APP/Contents/Resources/watcher.py"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.ajo.night-watcher</string>
<key>CFBundleName</key><string>Ajo Night Watcher</string>
<key>CFBundleDisplayName</key><string>Ajo Night Watcher</string>
<key>CFBundleExecutable</key><string>AjoNightWatcher</string>
<key>CFBundleIconFile</key><string>AjoNightWatcher</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
printf 'Built %s\n' "$APP"
