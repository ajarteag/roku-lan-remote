#!/bin/sh
# Build "Roku Remote.app" (the hotkey window, roku_gui.py) and start it at
# login. No sudo needed.
#
#   ./install-gui-macos.sh              build/update the app, start it, run at login
#   ./install-gui-macos.sh --uninstall  stop it and remove the app + login agent
#
# The app is a tiny compiled launcher (macos/launcher.swift) that runs this
# repo's roku_gui.py with your python3, so edits to the Python take effect on
# the next restart without rebuilding. The bundle exists so macOS can grant
# it Local Network access (to reach the TV) and Microphone/Speech (dictation);
# a bare launchd python is silently denied. First launch shows a "find
# devices on your local network" prompt -- click Allow.
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Roku Remote.app"
BUNDLE_ID="com.roku.remote.gui"
LABEL="$BUNDLE_ID"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON3="$(command -v python3)"
LOG="$REPO_DIR/gui.log"

stop_existing() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$AGENT_PLIST"
    pkill -f "$REPO_DIR/roku_gui.py" 2>/dev/null || true
    pkill -f "$APP/Contents/MacOS/RokuRemote" 2>/dev/null || true
}

if [ "$(id -u)" -eq 0 ]; then
    echo "Run without sudo: the window belongs to your user session." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    stop_existing
    rm -rf "$APP"
    echo "Removed Roku Remote.app and its login agent."
    exit 0
fi

command -v swiftc > /dev/null || { echo "swiftc not found: run xcode-select --install" >&2; exit 1; }

stop_existing
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$HOME/Library/LaunchAgents"

swiftc -O "$REPO_DIR/macos/launcher.swift" -o "$APP/Contents/MacOS/RokuRemote"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>Roku Remote</string>
  <key>CFBundleExecutable</key><string>RokuRemote</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Roku Remote talks to your Roku TV over the local network.</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Dictation sends what you say to the TV as text.</string>
  <key>NSSpeechRecognitionUsageDescription</key>
  <string>Dictation transcribes speech on-device to send to the TV.</string>
  <key>RokuPython</key><string>$PYTHON3</string>
  <key>RokuScript</key><string>$REPO_DIR/roku_gui.py</string>
</dict>
</plist>
EOF

codesign --force --sign - "$APP" > /dev/null 2>&1 || true

# `open` launches through LaunchServices so the bundle is the responsible app.
cat > "$AGENT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-g</string>
    <string>-a</string>
    <string>$APP</string>
    <string>--stdout</string><string>$LOG</string>
    <string>--stderr</string><string>$LOG</string>
    <string>--args</string>
    <string>--background</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"

HOTKEY="$("$PYTHON3" -c "import json; print(json.load(open('$REPO_DIR/config.json')).get('hotkey', 'cmd+shift+u'))" 2>/dev/null || echo cmd+shift+u)"
echo "Installed $APP (starts at login)."
echo "Press $HOTKEY anywhere to show/hide the remote. Logs: $LOG"
echo "If macOS asks to find devices on your local network, click Allow."
