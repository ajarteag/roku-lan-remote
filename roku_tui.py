#!/usr/bin/env python3
"""Roku terminal remote — a curses TUI that talks directly to the TV over ECP.

Usage: python3 roku_tui.py [--ip 192.168.4.217]
Reads the default TV IP from config.json next to this script.
"""
import argparse
import curses
import json
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REFRESH_SECONDS = 2.0

KEY_LEGEND = [
    ("arrows", "navigate"),
    ("enter", "OK"),
    ("delete", "back"),
    ("h", "home"),
    ("space", "play/pause"),
    ("< >", "rew/fwd"),
    ("r", "replay"),
    ("i", "options"),
    ("+ -", "volume"),
    ("m", "mute"),
    ("p", "power"),
    ("a", "apps"),
    ("t", "type mode"),
    ("q", "quit"),
]


def load_default_ip():
    config = Path(__file__).resolve().parent / "config.json"
    try:
        return json.loads(config.read_text())["tv_ip"]
    except (OSError, ValueError, KeyError):
        return "192.168.4.217"


class Roku:
    def __init__(self, ip):
        self.ip = ip

    def _url(self, path):
        return f"http://{self.ip}:8060/{path}"

    def post(self, path):
        def fire():
            try:
                req = urllib.request.Request(self._url(path), data=b"", method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except OSError:
                pass
        threading.Thread(target=fire, daemon=True).start()

    def get_xml(self, path):
        with urllib.request.urlopen(self._url(path), timeout=3) as resp:
            return ET.fromstring(resp.read())

    def keypress(self, key):
        self.post(f"keypress/{key}")

    def type_char(self, char):
        self.post("keypress/Lit_" + urllib.parse.quote(char, safe=""))

    def status(self):
        info = self.get_xml("query/device-info")
        active = self.get_xml("query/active-app")
        player = self.get_xml("query/media-player")
        app = active.find("app")
        return {
            "name": info.findtext("friendly-device-name") or "Roku TV",
            "power": info.findtext("power-mode"),
            "app": (app.text or "").strip() if app is not None else "?",
            "state": player.get("state"),
            "position": player.findtext("position"),
        }

    def apps(self):
        entries = []
        for app in self.get_xml("query/apps"):
            entries.append(((app.text or "").strip(), app.get("id"), app.get("type")))
        return sorted(entries, key=lambda a: (a[2] != "tvin", a[0].lower()))


class StatusPoller(threading.Thread):
    def __init__(self, roku):
        super().__init__(daemon=True)
        self.roku = roku
        self.data = None

    def run(self):
        while True:
            try:
                self.data = self.roku.status()
            except OSError:
                self.data = None
            time.sleep(REFRESH_SECONDS)


def draw_header(stdscr, poller, width):
    status = poller.data
    if status is None:
        line = "TV unreachable — check power/network"
        color = curses.color_pair(3)
    else:
        power = "on" if status["power"] == "PowerOn" else "standby"
        line = f" {status['name']}  ·  {power}  ·  {status['app']}"
        if status["state"] and status["state"] != "none":
            pos = (status["position"] or "").replace(" ms", "")
            if pos.isdigit():
                sec = int(pos) // 1000
                line += f"  ·  {status['state']} {sec // 60}:{sec % 60:02d}"
            else:
                line += f"  ·  {status['state']}"
        color = curses.color_pair(2)
    stdscr.addnstr(0, 0, line.ljust(width - 1), width - 1, color | curses.A_BOLD)


def draw_legend(stdscr, top, width, mode_line):
    stdscr.addnstr(top, 0, mode_line.ljust(width - 1), width - 1, curses.color_pair(4))
    row, col = top + 2, 2
    for key, label in KEY_LEGEND:
        chunk = f"{key} {label}   "
        if col + len(chunk) >= width:
            row, col = row + 1, 2
        try:
            stdscr.addstr(row, col, key, curses.color_pair(1) | curses.A_BOLD)
            stdscr.addstr(row, col + len(key), f" {label}   ")
        except curses.error:
            break
        col += len(chunk)


def app_picker(stdscr, roku):
    try:
        entries = roku.apps()
    except OSError:
        return
    query, selected = "", 0
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        matches = [e for e in entries if query.lower() in e[0].lower()]
        selected = max(0, min(selected, len(matches) - 1))
        stdscr.addnstr(0, 0, f" Launch app — type to filter: {query}_".ljust(width - 1),
                       width - 1, curses.color_pair(2) | curses.A_BOLD)
        for idx, (name, _app_id, kind) in enumerate(matches[: height - 3]):
            label = f"  {'[input] ' if kind == 'tvin' else ''}{name}"
            attr = curses.color_pair(1) | curses.A_REVERSE if idx == selected else 0
            stdscr.addnstr(2 + idx, 0, label.ljust(width - 1), width - 1, attr)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (27, ord("q")) and not query:
            return
        if ch == 27:
            query = ""
        elif ch in (curses.KEY_UP,):
            selected -= 1
        elif ch in (curses.KEY_DOWN,):
            selected += 1
        elif ch in (10, 13, curses.KEY_ENTER):
            if matches:
                roku.post(f"launch/{matches[selected][1]}")
            return
        elif ch in (127, 8, curses.KEY_BACKSPACE):
            query = query[:-1]
        elif 32 <= ch < 127:
            query += chr(ch)
            selected = 0


def main(stdscr, roku):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_MAGENTA, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    stdscr.timeout(250)

    poller = StatusPoller(roku)
    poller.start()
    type_mode = False
    last_action = ""

    normal_keys = {
        curses.KEY_UP: "Up", curses.KEY_DOWN: "Down",
        curses.KEY_LEFT: "Left", curses.KEY_RIGHT: "Right",
        10: "Select", 13: "Select", curses.KEY_ENTER: "Select",
        127: "Back", 8: "Back", curses.KEY_BACKSPACE: "Back",
        ord(" "): "Play", ord("h"): "Home",
        ord("<"): "Rev", ord(","): "Rev", ord(">"): "Fwd", ord("."): "Fwd",
        ord("r"): "InstantReplay", ord("i"): "Info",
        ord("+"): "VolumeUp", ord("="): "VolumeUp",
        ord("-"): "VolumeDown", ord("_"): "VolumeDown",
        ord("m"): "VolumeMute", ord("p"): "Power",
    }

    while True:
        stdscr.erase()
        _height, width = stdscr.getmaxyx()
        draw_header(stdscr, poller, width)
        mode_line = (" TYPE MODE — keys go to the TV, esc to exit"
                     if type_mode else f" remote mode{('  ·  ' + last_action) if last_action else ''}")
        draw_legend(stdscr, 2, width, mode_line)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == -1:
            continue

        if type_mode:
            if ch == 27:
                type_mode = False
            elif ch in (127, 8, curses.KEY_BACKSPACE):
                roku.keypress("Backspace")
            elif ch in (10, 13, curses.KEY_ENTER):
                roku.keypress("Enter")
            elif 32 <= ch < 127:
                roku.type_char(chr(ch))
            continue

        if ch == ord("q"):
            return
        if ch == ord("t"):
            type_mode = True
            continue
        if ch == ord("a"):
            app_picker(stdscr, roku)
            continue
        key = normal_keys.get(ch)
        if key:
            roku.keypress(key)
            last_action = key


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Roku terminal remote")
    parser.add_argument("--ip", default=load_default_ip(), help="Roku TV IP address")
    args = parser.parse_args()
    curses.wrapper(main, Roku(args.ip))
