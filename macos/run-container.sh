#!/bin/sh
# Run the Roku LAN Remote server container in the foreground.
#
# launchd (see install-macos.sh) supervises this script with KeepAlive, which
# is what restarts the server after a crash and brings it back at login:
# Apple's `container` has no restart policy of its own, and Docker Desktop's
# only applies while Docker Desktop is up.
#
#   ROKU_RUNTIME  container (default) or docker
#   ROKU_IMAGE    image to run (default: roku-lan-remote, built by the installer)
#
# The repo directory is mounted at /data so the container reads and writes
# the same config.json and macros.json as the TUI and hotkey window.
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="${ROKU_RUNTIME:-container}"
IMAGE="${ROKU_IMAGE:-roku-lan-remote}"
NAME="roku-remote"

PORT="$(python3 -c "import json; print(json.load(open('$REPO_DIR/config.json')).get('server_port', 8000))" 2>/dev/null || echo 8000)"

if [ "$RUNTIME" = "container" ]; then
    container system start > /dev/null 2>&1 || true
fi

# A previous run that launchd killed can leave the container behind (the CLI
# dies, the VM keeps going); the name must be free before we can start again.
"$RUNTIME" rm -f "$NAME" > /dev/null 2>&1 || true

echo "Starting $NAME with $RUNTIME ($IMAGE), host port $PORT -> container 8000"
exec "$RUNTIME" run --rm --name "$NAME" \
    -p "$PORT:8000" \
    -v "$REPO_DIR:/data" \
    "$IMAGE"
