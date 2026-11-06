#!/bin/sh
# Install (or remove) the Roku LAN Remote server as a container that starts
# at login and restarts if it crashes. Runs the server with Apple's `container`
# (macOS 26+, Apple silicon) or Docker Desktop, supervised by a launchd agent.
#
#   ./install-macos.sh                       build the image from this checkout, install
#   ./install-macos.sh --image ghcr.io/ajarteag/roku-lan-remote:latest
#                                            use the prebuilt image instead of building
#   ./install-macos.sh --runtime docker      force docker (default: container if present)
#   ./install-macos.sh --uninstall           stop and remove the service
#
# Earlier versions installed a root LaunchDaemon running python directly; the
# container replaces it (macOS Local Network Privacy blocked an unprivileged
# python from reaching the TV, but the container VM's network is not subject
# to it). Both install and --uninstall remove that old daemon if present,
# which is the only step that asks for sudo.
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.roku.remote"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OLD_DAEMON_PLIST="/Library/LaunchDaemons/$LABEL.plist"
NAME="roku-remote"
RUNTIME=""
IMAGE="roku-lan-remote"
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) UNINSTALL=1 ;;
        --runtime) RUNTIME="$2"; shift ;;
        --image) IMAGE="$2"; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then
    echo "Run as your normal user, not with sudo (it asks for sudo itself if needed)." >&2
    exit 1
fi

if [ -z "$RUNTIME" ]; then
    if command -v container > /dev/null 2>&1; then
        RUNTIME=container
    elif command -v docker > /dev/null 2>&1; then
        RUNTIME=docker
    elif [ "$UNINSTALL" -eq 0 ]; then
        echo "Need a container runtime: install Apple's container" >&2
        echo "(https://github.com/apple/container/releases) or Docker Desktop." >&2
        exit 1
    fi
fi

remove_old_root_daemon() {
    [ -f "$OLD_DAEMON_PLIST" ] || return 0
    echo "Removing the old root LaunchDaemon (sudo)..."
    if sudo sh -c "launchctl bootout 'system/$LABEL' 2>/dev/null; rm -f '$OLD_DAEMON_PLIST'"; then
        return 0
    fi
    echo "Could not remove it; it may still hold the port. Run by hand:" >&2
    echo "  sudo launchctl bootout system/$LABEL; sudo rm $OLD_DAEMON_PLIST" >&2
}

stop_existing() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$AGENT_PLIST"
    if [ -n "$RUNTIME" ]; then
        "$RUNTIME" rm -f "$NAME" > /dev/null 2>&1 || true
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    stop_existing
    remove_old_root_daemon
    echo "Removed $LABEL."
    exit 0
fi

PORT="$(python3 -c "import json; print(json.load(open('$REPO_DIR/config.json')).get('server_port', 8000))" 2>/dev/null || echo 8000)"

if [ "$RUNTIME" = "container" ]; then
    container system start > /dev/null 2>&1 || true
elif ! docker info > /dev/null 2>&1; then
    echo "Docker is not running. Start Docker Desktop (and enable 'Start Docker Desktop when you sign in')." >&2
    exit 1
fi

if [ "$IMAGE" = "roku-lan-remote" ]; then
    echo "Building $IMAGE with $RUNTIME..."
    "$RUNTIME" build -t "$IMAGE" "$REPO_DIR"
else
    echo "Pulling $IMAGE with $RUNTIME..."
    "$RUNTIME" pull "$IMAGE"
fi

echo "Installing as a login agent using $RUNTIME on port $PORT."
stop_existing
remove_old_root_daemon

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$REPO_DIR/macos/run-container.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>ROKU_RUNTIME</key><string>$RUNTIME</string>
    <key>ROKU_IMAGE</key><string>$IMAGE</string>
  </dict>
  <key>StandardOutPath</key><string>$REPO_DIR/server.log</string>
  <key>StandardErrorPath</key><string>$REPO_DIR/server.log</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
PLIST

# bootstrap takes <domain-target> <service-path> as SEPARATE arguments; glued
# together it silently does nothing and still exits 0.
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"

if ! launchctl print "gui/$(id -u)/$LABEL" > /dev/null 2>&1; then
    echo "FAILED: $LABEL did not register with launchd." >&2
    exit 1
fi

i=0
while [ "$i" -lt 45 ]; do
    if curl -s -o /dev/null --connect-timeout 3 "http://localhost:$PORT/api/health"; then
        echo "Installed and running on port $PORT ($RUNTIME, starts at login)."
        echo "Logs: $REPO_DIR/server.log"
        exit 0
    fi
    i=$((i + 1))
    sleep 1
done

echo "Registered with launchd but not answering on port $PORT after 45s." >&2
tail -20 "$REPO_DIR/server.log" 2>/dev/null >&2 || true
exit 1
