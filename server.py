#!/usr/bin/env python3
"""Roku LAN remote — zero-dependency web remote + status API for Roku TVs.

Serves the web UI from ./static and proxies/parses the Roku External
Control Protocol (ECP, port 8060) so any browser on the LAN can control
and observe the TV. Run with: python3 server.py

Environment (all optional; set by the container image, unused when run bare):
  DATA_DIR  directory holding config.json and macros.json (default: this dir)
  TV_IP     overrides tv_ip from config.json
  PORT      overrides server_port from config.json
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# In a container DATA_DIR is a volume (/data) so config and automations outlive
# the image; the Mac installer bind-mounts the repo itself there so the TUI and
# hotkey window keep sharing the same two files.
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT)
IN_CONTAINER = DATA_DIR != ROOT
CONFIG_PATH = DATA_DIR / "config.json"
MACROS_PATH = DATA_DIR / "macros.json"

SAFE_ID = re.compile(r"^[A-Za-z0-9_.%-]+$")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


CONFIG = load_json(CONFIG_PATH, {"tv_ip": "192.168.4.217", "server_port": 8000})
if os.environ.get("TV_IP", "").strip():
    CONFIG["tv_ip"] = os.environ["TV_IP"].strip()
if os.environ.get("PORT", "").strip():
    CONFIG["server_port"] = int(os.environ["PORT"])

if not MACROS_PATH.exists() and (ROOT / "macros.json").exists():
    # Fresh volume: start from the bundled example automations so the
    # Automations panel isn't empty.
    try:
        MACROS_PATH.write_bytes((ROOT / "macros.json").read_bytes())
    except OSError:
        pass

ICON_CACHE = {}


def ecp(path):
    return f"http://{CONFIG['tv_ip']}:8060/{path}"


def ecp_get(path, timeout=4):
    with urllib.request.urlopen(ecp(path), timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def ecp_post(path, timeout=4):
    req = urllib.request.Request(ecp(path), data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def parse_ms(text):
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def query_status():
    status = {"reachable": True, "tv_ip": CONFIG["tv_ip"]}
    try:
        info_xml, _ = ecp_get("query/device-info", timeout=3)
        info = ET.fromstring(info_xml)
        status["device"] = {
            "name": info.findtext("friendly-device-name"),
            "model": info.findtext("friendly-model-name"),
            "power": info.findtext("power-mode"),
        }

        app_xml, _ = ecp_get("query/active-app", timeout=3)
        active = ET.fromstring(app_xml)
        app = active.find("app")
        screensaver = active.find("screensaver")
        status["app"] = {
            "id": app.get("id") if app is not None else None,
            "name": (app.text or "").strip() if app is not None else None,
            "screensaver": screensaver is not None,
        }

        player_xml, _ = ecp_get("query/media-player", timeout=3)
        player = ET.fromstring(player_xml)
        status["player"] = {
            "state": player.get("state"),
            "position_ms": parse_ms(player.findtext("position")),
            "duration_ms": parse_ms(player.findtext("duration")),
        }
    except Exception:
        return {"reachable": False, "tv_ip": CONFIG["tv_ip"]}
    return status


def query_apps():
    apps_xml, _ = ecp_get("query/apps")
    apps = []
    for app in ET.fromstring(apps_xml):
        apps.append({
            "id": app.get("id"),
            "name": (app.text or "").strip(),
            "type": app.get("type"),
        })
    return apps


def run_macro(steps):
    for step in steps:
        kind = step.get("type")
        value = str(step.get("value", ""))
        if kind == "delay":
            time.sleep(min(int(value or 0), 30000) / 1000)
        elif kind == "keypress" and SAFE_ID.match(value):
            ecp_post(f"keypress/{value}")
        elif kind == "launch" and SAFE_ID.match(value):
            ecp_post(f"launch/{value}")


def send_text(text):
    for char in text:
        ecp_post("keypress/Lit_" + urllib.parse.quote(char, safe=""))
        time.sleep(0.05)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def fail(self, exc):
        """Record a route failure, then report it to the client as a 502.

        Without this the traceback is lost: the client gets an error body and
        the server keeps no record of what actually broke. A client that has
        already hung up makes send_json raise, so log first.
        """
        print(f"{self.command} {self.path} failed: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        self.send_json({"error": str(exc)}, 502)

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/api/health":
                # Liveness only (never touches the TV): container HEALTHCHECK
                # and the installer's readiness loop.
                self.send_json({"ok": True, "tv_ip": CONFIG["tv_ip"]})
            elif route == "/api/status":
                self.send_json(query_status())
            elif route == "/api/apps":
                self.send_json(query_apps())
            elif route == "/api/macros":
                self.send_json(load_json(MACROS_PATH, []))
            elif route == "/api/config":
                self.send_json({"tv_ip": CONFIG["tv_ip"]})
            elif route.startswith("/api/icon/"):
                app_id = route.rsplit("/", 1)[1]
                if not SAFE_ID.match(app_id):
                    self.send_json({"error": "bad id"}, 400)
                    return
                if app_id not in ICON_CACHE:
                    ICON_CACHE[app_id] = ecp_get(f"query/icon/{app_id}")
                data, ctype = ICON_CACHE[app_id]
                self.send_response(200)
                self.send_header("Content-Type", ctype or "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            else:
                super().do_GET()
        except Exception as exc:
            self.fail(exc)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route.startswith("/api/keypress/"):
                key = route.rsplit("/", 1)[1]
                if not SAFE_ID.match(key):
                    self.send_json({"error": "bad key"}, 400)
                    return
                ecp_post(f"keypress/{key}")
                self.send_json({"ok": True})
            elif route.startswith("/api/launch/"):
                app_id = route.rsplit("/", 1)[1]
                if not SAFE_ID.match(app_id):
                    self.send_json({"error": "bad id"}, 400)
                    return
                ecp_post(f"launch/{app_id}")
                self.send_json({"ok": True})
            elif route == "/api/text":
                payload = json.loads(self.read_body() or b"{}")
                send_text(str(payload.get("text", ""))[:200])
                self.send_json({"ok": True})
            elif route.startswith("/api/macro/"):
                name = urllib.parse.unquote(route.split("/api/macro/", 1)[1])
                macros = load_json(MACROS_PATH, [])
                macro = next((m for m in macros if m.get("name") == name), None)
                if macro is None:
                    self.send_json({"error": "unknown macro"}, 404)
                    return
                run_macro(macro.get("steps", []))
                self.send_json({"ok": True})
            elif route == "/api/macros":
                macros = json.loads(self.read_body() or b"[]")
                if not isinstance(macros, list):
                    self.send_json({"error": "expected a JSON array"}, 400)
                    return
                MACROS_PATH.write_text(json.dumps(macros, indent=2) + "\n")
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.fail(exc)


class Server(ThreadingHTTPServer):
    """Threading server that doesn't log clients hanging up mid-request.

    Browsers routinely abort idle keep-alive connections, which surfaces as
    ConnectionResetError/BrokenPipeError from the socket read or write. Those
    are normal and were burying real failures in the log; anything else still
    gets the default traceback.
    """

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def lan_ips():
    """This machine's LAN IPv4 addresses (what other devices should browse to)."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    addrs = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return [a for a in addrs if not a.startswith(("127.", "169.254."))]


def main():
    port = int(CONFIG.get("server_port", 8000))
    handler = partial(Handler, directory=str(ROOT / "static"))
    server = Server(("0.0.0.0", port), handler)
    suffix = "" if port == 80 else f":{port}"
    # flush=True so the banner reaches server.log immediately when stdout is a
    # file (launchd) rather than sitting in a block buffer until the process dies.
    print(f"Roku remote -> TV {CONFIG['tv_ip']}, reachable at:", flush=True)
    if IN_CONTAINER:
        # The interfaces visible here belong to the container, not the LAN.
        print(f"  http://<host-ip>:<published port> (container port {port})",
              flush=True)
    else:
        for ip in lan_ips() or ["<this-machine's-ip>"]:
            print(f"  http://{ip}{suffix}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
