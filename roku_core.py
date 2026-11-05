"""Shared Roku remote core used by both the terminal (roku_tui.py) and the
macOS window (roku_gui.py) front ends.

- Roku: tiny ECP client (keypress, text, launch, status, app list).
- Dictation: live speech-to-text via the macOS `hear` CLI.
- StatusPoller: background thread refreshing TV status.
- Remote: the UI-agnostic controller. Front ends translate their native key
  events into logical key names (see Remote.handle) and render Remote's
  state; all key bindings and mode logic live here, so every front end has
  exactly the same shortcuts.

Standard library only.
"""
import json
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_IP = "192.168.4.217"
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
    ("^d", "dictate"),
    ("q", "quit"),
]

# Logical key name -> ECP key, for remote mode.
REMOTE_KEYS = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "enter": "Select", "backspace": "Back",
    " ": "Play", "h": "Home",
    "<": "Rev", ",": "Rev", ">": "Fwd", ".": "Fwd",
    "r": "InstantReplay", "i": "Info",
    "+": "VolumeUp", "=": "VolumeUp",
    "-": "VolumeDown", "_": "VolumeDown",
    "m": "VolumeMute", "p": "Power",
}


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


class Roku:
    def __init__(self, ip):
        self.ip = ip
        self._type_lock = threading.Lock()  # keeps bulk text sends from interleaving

    def _url(self, path):
        return f"http://{self.ip}:8060/{path}"

    def post_sync(self, path):
        try:
            req = urllib.request.Request(self._url(path), data=b"", method="POST")
            urllib.request.urlopen(req, timeout=3).read()
            return True
        except OSError:
            return False

    def post(self, path):
        threading.Thread(target=self.post_sync, args=(path,), daemon=True).start()

    def get_xml(self, path):
        with urllib.request.urlopen(self._url(path), timeout=3) as resp:
            return ET.fromstring(resp.read())

    def keypress(self, key):
        self.post(f"keypress/{key}")

    def launch(self, app_id):
        self.post(f"launch/{app_id}")

    def type_char(self, char):
        self.post("keypress/Lit_" + urllib.parse.quote(char, safe=""))

    def type_text(self, text):
        # Chars must arrive in order, so send sequentially on one thread
        # (same 50 ms pacing as server.py's send_text). Non-daemon so an
        # in-flight send finishes even if the UI quits; bails on the first
        # failed request so an unreachable TV can't stall exit.
        def worker():
            with self._type_lock:
                for char in text:
                    if not self.post_sync("keypress/Lit_" + urllib.parse.quote(char, safe="")):
                        return
                    time.sleep(0.05)
        threading.Thread(target=worker).start()

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


class Dictation:
    """Live speech-to-text via the macOS `hear` CLI (on-device recognition)."""

    HINT_INSTALL = "dictation needs the 'hear' CLI — https://sveinbjorn.org/hear"
    HINT_OS = "dictation is only supported on macOS"

    def __init__(self):
        self.proc = None
        self.transcript = ""
        self.error = None
        self._stderr = []
        self._reader = None

    @staticmethod
    def unavailable_reason():
        if sys.platform != "darwin":
            return Dictation.HINT_OS
        if Dictation.hear_path() is None:
            return Dictation.HINT_INSTALL
        return None

    @staticmethod
    def hear_path():
        # launchd agents get a minimal PATH, so also look in Homebrew's prefixes.
        return (shutil.which("hear")
                or shutil.which("hear", path="/opt/homebrew/bin:/usr/local/bin"))

    def start(self):
        try:
            self.proc = subprocess.Popen(
                [self.hear_path() or "hear", "-d", "-p", "-m"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self.error = f"could not start hear: {exc}"
            return False
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return True

    def _read_stdout(self):
        # Single-line mode rewrites the transcript after \r, so keep a rolling
        # buffer and treat the last \r/\n-delimited segment as the current text.
        data = b""
        while True:
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                break
            data = (data + chunk)[-65536:]
            parts = [p.strip() for p in data.replace(b"\r", b"\n").split(b"\n")]
            parts = [p for p in parts if p]
            if parts:
                self.transcript = parts[-1].decode("utf-8", "replace")

    def _read_stderr(self):
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr.append(line)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return self.transcript
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=1.5)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()
        if self._reader is not None:
            self._reader.join(timeout=0.5)
        if not self.transcript and self._stderr:
            self.error = self._stderr[0]
        return self.transcript

    def cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass


class StatusPoller(threading.Thread):
    def __init__(self, roku):
        super().__init__(daemon=True)
        self.roku = roku
        self.data = None
        self.active = True  # set False to pause polling (e.g. window hidden)
        self._wake = threading.Event()

    def run(self):
        while True:
            if self.active:
                try:
                    self.data = self.roku.status()
                except OSError:
                    self.data = None
            self._wake.wait(REFRESH_SECONDS)
            self._wake.clear()

    def refresh_now(self):
        self._wake.set()

    def header(self):
        """Returns (text, ok) for the one-line status header."""
        status = self.data
        if status is None:
            return "TV unreachable — check power/network", False
        power = "on" if status["power"] == "PowerOn" else "standby"
        line = f" {status['name']}  ·  {power}  ·  {status['app']}"
        if status["state"] and status["state"] != "none":
            pos = (status["position"] or "").replace(" ms", "")
            if pos.isdigit():
                sec = int(pos) // 1000
                line += f"  ·  {status['state']} {sec // 60}:{sec % 60:02d}"
            else:
                line += f"  ·  {status['state']}"
        return line, True


class Remote:
    """UI-agnostic remote controller: all modes and key bindings.

    Front ends call handle(key) with a logical key name:
      "up" "down" "left" "right" "enter" "backspace" "esc" "ctrl-d",
      or a single printable ASCII character ("a", " ", "+", ...).
    handle() returns "quit" when the user asked to quit (q in remote mode),
    "esc" for an escape with nothing to cancel, else None.
    Call tick() regularly (every ~250 ms) so dictation can finish on its own.

    Render from: mode ("remote" | "type" | "picker" | "recording" | "review"),
    mode_line(), transcript(), picker_matches(), picker_selected,
    picker_query, picker_loading.
    """

    def __init__(self, roku, send_mode="review"):
        self.roku = roku
        self.send_mode = send_mode
        self.mode = "remote"
        self.last_action = ""
        self.hint = ""          # transient message, cleared on next keypress
        self.pending = ""       # transcript awaiting review
        self.dictation = None
        self._prev_mode = "remote"  # mode to return to after dictation
        self.picker_entries = None
        self.picker_loading = False
        self.picker_query = ""
        self.picker_selected = 0
        self._picker_gen = 0

    # --- rendering helpers -------------------------------------------------

    def mode_line(self):
        if self.hint:
            return f" {self.hint}"
        if self.mode == "recording":
            return f" DICTATING — speak · enter stop · esc cancel · mode: {self.send_mode}"
        if self.mode == "review":
            return " REVIEW — enter send to TV · esc discard · ^d re-record"
        if self.mode == "type":
            return " TYPE MODE — keys go to the TV · ^d dictate · esc to exit"
        if self.mode == "picker":
            return f" Launch app — type to filter: {self.picker_query}_"
        return f" remote mode{('  ·  ' + self.last_action) if self.last_action else ''}"

    def transcript(self):
        """Text to show in recording/review modes (with placeholder/cursor)."""
        recording = self.mode == "recording"
        text = self.dictation.transcript if recording and self.dictation else self.pending
        shown = text if text else ("(listening…)" if recording else "(empty)")
        if recording and text:
            shown += " _"
        return shown

    def picker_matches(self):
        entries = self.picker_entries or []
        q = self.picker_query.lower()
        matches = [e for e in entries if q in e[0].lower()]
        self.picker_selected = max(0, min(self.picker_selected, len(matches) - 1))
        return matches

    # --- dictation ---------------------------------------------------------

    def _start_recording(self):
        reason = Dictation.unavailable_reason()
        if reason:
            self.hint = reason
            return
        self.dictation = Dictation()
        if self.dictation.start():
            self.mode = "recording"
        else:
            self.hint = self.dictation.error or "dictation failed"

    def _finish_recording(self):
        text = self.dictation.stop().strip()
        if not text:
            self.hint = self.dictation.error or "heard nothing"
            self.mode = self._prev_mode
        elif self.send_mode == "send":
            self.roku.type_text(text)
            self.hint = "sent to TV"
            self.mode = self._prev_mode
        else:
            self.pending = text
            self.mode = "review"

    def tick(self):
        if self.mode == "recording" and not self.dictation.running():
            self._finish_recording()  # hear exited on its own (error or timeout)

    def close(self):
        if self.dictation is not None:
            self.dictation.cancel()
        if self.mode == "recording":
            self.mode = self._prev_mode

    def reset(self):
        """Back to plain remote mode, cancelling any dictation/picker."""
        self.close()
        self._picker_gen += 1
        self.picker_loading = False
        self.mode, self._prev_mode = "remote", "remote"
        self.hint = self.pending = ""

    # --- app picker --------------------------------------------------------

    def _open_picker(self):
        self.mode = "picker"
        self.picker_query, self.picker_selected = "", 0
        self.picker_entries, self.picker_loading = None, True
        self._picker_gen += 1
        gen = self._picker_gen

        def load():
            try:
                entries = self.roku.apps()
            except OSError:
                entries = None
            if gen != self._picker_gen:
                return
            self.picker_loading = False
            if entries is None and self.mode == "picker":
                self.mode = "remote"
                self.hint = "couldn't load apps — TV unreachable?"
            self.picker_entries = entries
        threading.Thread(target=load, daemon=True).start()

    def _picker_key(self, key):
        if key in ("esc", "q") and not self.picker_query:
            self.mode = "remote"
        elif key == "esc":
            self.picker_query = ""
        elif key == "up":
            self.picker_selected -= 1
        elif key == "down":
            self.picker_selected += 1
        elif key == "enter":
            matches = self.picker_matches()
            if matches:
                self.roku.launch(matches[self.picker_selected][1])
                self.last_action = f"launch {matches[self.picker_selected][0]}"
                self.mode = "remote"
            elif not self.picker_loading:
                self.mode = "remote"
        elif key == "backspace":
            self.picker_query = self.picker_query[:-1]
        elif len(key) == 1:
            self.picker_query += key
            self.picker_selected = 0

    # --- main dispatch -----------------------------------------------------

    def handle(self, key):
        self.hint = ""

        if self.mode == "recording":
            if key in ("enter", "ctrl-d"):
                self._finish_recording()
            elif key == "esc":
                self.dictation.cancel()
                self.mode = self._prev_mode
            return None

        if self.mode == "review":
            if key == "enter":
                self.roku.type_text(self.pending)
                self.hint = "sent to TV"
                self.mode = self._prev_mode
            elif key == "esc":
                self.mode = self._prev_mode
            elif key == "ctrl-d":
                self.mode = self._prev_mode
                self._start_recording()
            return None

        if self.mode == "picker":
            self._picker_key(key)
            return None

        if self.mode == "type":
            if key == "esc":
                self.mode = "remote"
            elif key == "ctrl-d":
                self._prev_mode = "type"
                self._start_recording()
            elif key == "backspace":
                self.roku.keypress("Backspace")
            elif key == "enter":
                self.roku.keypress("Enter")
            elif len(key) == 1:
                self.roku.type_char(key)
            return None

        # remote mode
        if key == "q":
            return "quit"
        if key == "esc":
            return "esc"
        if key == "t":
            self.mode = "type"
        elif key == "a":
            self._open_picker()
        elif key in REMOTE_KEYS:
            self.roku.keypress(REMOTE_KEYS[key])
            self.last_action = REMOTE_KEYS[key]
        return None
