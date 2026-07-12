#!/usr/bin/env python3
"""Roku LAN remote — zero-dependency web remote + status API for Roku TVs.

Serves the web UI from ./static and proxies/parses the Roku External
Control Protocol (ECP, port 8060) so any browser on the LAN can control
and observe the TV. Run with: python3 server.py
"""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
MACROS_PATH = ROOT / "macros.json"

SAFE_ID = re.compile(r"^[A-Za-z0-9_.%-]+$")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


CONFIG = load_json(CONFIG_PATH, {"tv_ip": "192.168.4.217", "server_port": 8000})

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

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/api/status":
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
            self.send_json({"error": str(exc)}, 502)

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
            self.send_json({"error": str(exc)}, 502)


def main():
    port = int(CONFIG.get("server_port", 8000))
    handler = partial(Handler, directory=str(ROOT / "static"))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"Roku remote serving on http://0.0.0.0:{port} -> TV {CONFIG['tv_ip']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
