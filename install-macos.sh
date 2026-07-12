#!/bin/sh
# Install (or remove) the Roku LAN Remote server as a macOS launchd service
# so it starts on boot and restarts if it crashes.
#
#   ./install-macos.sh              install/update using this repo's location
#   ./install-macos.sh --uninstall  stop and remove the service
#
# Uses the port from config.json. Ports below 1024 (e.g. 80, for a bare
# http://tv URL) need root, so those install as a system
# LaunchDaemon via sudo; otherwise it's a per-user LaunchAgent.
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.roku.remote"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DAEMON_PLIST="/Library/LaunchDaemons/$LABEL.plist"
PYTHON3="$(command -v python3)"

stop_existing() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    [ -f "$AGENT_PLIST" ] && rm -f "$AGENT_PLIST"
    if [ -f "$DAEMON_PLIST" ]; then
        sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
        sudo rm -f "$DAEMON_PLIST"
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    stop_existing
    echo "Removed $LABEL."
    exit 0
fi

PORT="$("$PYTHON3" -c "import json; print(json.load(open('$REPO_DIR/config.json')).get('server_port', 8000))" 2>/dev/null || echo 8000)"

if [ "$PORT" -lt 1024 ]; then
    PLIST="$DAEMON_PLIST"
    SUDO="sudo"
    DOMAIN="system"
    echo "Port $PORT is privileged; installing as a system LaunchDaemon (needs sudo)."
else
    PLIST="$AGENT_PLIST"
    SUDO=""
    DOMAIN="gui/$(id -u)"
fi

stop_existing

$SUDO mkdir -p "$(dirname "$PLIST")"
$SUDO tee "$PLIST" > /dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON3</string>
    <string>$REPO_DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>StandardOutPath</key><string>$REPO_DIR/server.log</string>
  <key>StandardErrorPath</key><string>$REPO_DIR/server.log</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

$SUDO launchctl bootstrap "$DOMAIN" "$PLIST"

sleep 1
if curl -s -o /dev/null --connect-timeout 3 "http://localhost:$PORT/api/status"; then
    echo "Installed and running: http://$(hostname -s | tr '[:upper:]' '[:lower:]').local:$PORT"
    echo "Logs: $REPO_DIR/server.log"
    echo "If macOS asks to allow Python to accept local network connections, approve it."
else
    echo "Installed, but the server isn't answering yet. Check $REPO_DIR/server.log"
fi
