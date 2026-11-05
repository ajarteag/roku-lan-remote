#!/usr/bin/env python3
"""Roku remote as a small macOS window you can summon from anywhere.

Same keys and modes as roku_tui.py (both use roku_core.Remote). Stays
running in the background with no Dock icon; a global hotkey (default
cmd+shift+u, set "hotkey" in config.json) shows/hides it. Esc in remote
mode, q, or cmd+w hide it; cmd+q quits.

Usage:
  python3 roku_gui.py               start (or show the running instance)
  python3 roku_gui.py --background  start hidden (used by the login agent)
  python3 roku_gui.py --toggle      show/hide the running instance

The hotkey uses Carbon's RegisterEventHotKey via ctypes, which needs no
Accessibility / Input Monitoring permission. Standard library only.
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

from roku_core import DEFAULT_IP, KEY_LEGEND, Remote, Roku, StatusPoller, load_config

PID_PATH = Path(__file__).resolve().parent / "roku_gui.pid"
DEFAULT_HOTKEY = "cmd+shift+u"
COLS, ROWS = 72, 14
COLORS = {
    "bg": "#1c1c1e", "fg": "#d8d8d8", "key": "#d670d6", "ok": "#5fd35f",
    "bad": "#f25f5c", "mode": "#e5c07b", "sel_bg": "#d670d6", "sel_fg": "#1c1c1e",
}

# --- macOS glue (ctypes) ---------------------------------------------------

# kVK_* virtual key codes (ANSI layout).
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "space": 49, "`": 50,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
MODIFIERS = {
    "cmd": 0x100, "command": 0x100, "shift": 0x200,
    "opt": 0x800, "option": 0x800, "alt": 0x800,
    "ctrl": 0x1000, "control": 0x1000,
}


def parse_hotkey(spec):
    """'ctrl+opt+r' -> (keycode, modifier mask). Raises ValueError."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts or parts[-1] not in KEYCODES:
        raise ValueError(f"unknown key in hotkey {spec!r}")
    mods = 0
    for part in parts[:-1]:
        if part not in MODIFIERS:
            raise ValueError(f"unknown modifier {part!r} in hotkey {spec!r}")
        mods |= MODIFIERS[part]
    return KEYCODES[parts[-1]], mods


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _fourcc(text):
    return int.from_bytes(text.encode(), "big")


class GlobalHotkey:
    """System-wide hotkey via Carbon RegisterEventHotKey. The callback only
    sets a flag; poll `fired()` from the Tk loop to act on it safely."""

    def __init__(self, spec):
        self._fired = False
        self._ref = ctypes.c_void_p()
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
            ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p, ctypes.c_void_p]
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        keycode, mods = parse_hotkey(spec)

        def on_hotkey(_call_ref, _event, _data):
            self._fired = True
            return 0
        self._callback = _HANDLER(on_hotkey)  # keep a reference alive
        target = carbon.GetApplicationEventTarget()
        spec_struct = _EventTypeSpec(_fourcc("keyb"), 5)  # kEventHotKeyPressed
        err = carbon.InstallEventHandler(target, self._callback, 1,
                                         ctypes.byref(spec_struct), None, None)
        if err:
            raise OSError(f"InstallEventHandler failed ({err})")
        err = carbon.RegisterEventHotKey(keycode, mods, _EventHotKeyID(_fourcc("rokU"), 1),
                                         target, 0, ctypes.byref(self._ref))
        if err:
            raise OSError(f"RegisterEventHotKey failed ({err}) — {spec} may be taken")

    def fired(self):
        fired, self._fired = self._fired, False
        return fired


class NSApp:
    """Just enough of NSApplication to drop the Dock icon and hand focus
    back to the previous app when hiding."""

    def __init__(self):
        self._objc = ctypes.CDLL("/usr/lib/libobjc.dylib")
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        cls = self._objc.objc_getClass(b"NSApplication")
        self.app = self._send(cls, "sharedApplication")

    def _send(self, obj, selector, *args, restype=ctypes.c_void_p, argtypes=()):
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
            ("objc_msgSend", self._objc))
        return fn(obj, self._objc.sel_registerName(selector.encode()), *args)

    def set_accessory(self):  # NSApplicationActivationPolicyAccessory: no Dock icon
        self._send(self.app, "setActivationPolicy:", 1,
                   restype=ctypes.c_bool, argtypes=(ctypes.c_long,))

    def _responds(self, obj, selector):
        return self._send(obj, "respondsToSelector:", self._objc.sel_registerName(selector.encode()),
                          restype=ctypes.c_bool, argtypes=(ctypes.c_void_p,))

    def activate(self):
        self._send(self.app, "unhide:", None, argtypes=(ctypes.c_void_p,))
        if self._responds(self.app, "activate"):  # macOS 14+
            self._send(self.app, "activate", restype=None)
        self._send(self.app, "activateIgnoringOtherApps:", True,
                   restype=None, argtypes=(ctypes.c_bool,))
        running = self._send(self._objc.objc_getClass(b"NSRunningApplication"),
                             "currentApplication")
        self._send(running, "activateWithOptions:", 1 << 1,  # IgnoringOtherApps
                   restype=ctypes.c_bool, argtypes=(ctypes.c_ulong,))

    def hide(self):
        self._send(self.app, "hide:", None, argtypes=(ctypes.c_void_p,))


# --- single instance / IPC ---------------------------------------------------

def running_pid():
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                         capture_output=True, text=True).stdout
    return pid if "roku_gui" in cmd else None


# --- UI ----------------------------------------------------------------------

TK_KEYS = {
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Return": "enter", "KP_Enter": "enter",
    "BackSpace": "backspace", "Delete": "backspace", "Escape": "esc",
}
MOD_CONTROL, MOD_COMMAND, MOD_OPTION = 0x4, 0x8, 0x10  # Tk aqua event.state bits


def logical_key(event):
    if event.state & (MOD_COMMAND | MOD_OPTION):
        return None
    if event.state & MOD_CONTROL:
        return "ctrl-d" if event.keysym.lower() == "d" else None
    if event.keysym in TK_KEYS:
        return TK_KEYS[event.keysym]
    if len(event.char) == 1 and 32 <= ord(event.char) < 127:
        return event.char
    return None


class RokuWindow:
    def __init__(self, roku, send_mode, hotkey_spec, start_hidden):
        self.root = tk.Tk()
        self.root.title("Roku")
        self.root.configure(bg=COLORS["bg"])
        self.root.resizable(False, False)
        self.nsapp = NSApp()
        self.nsapp.set_accessory()

        self.text = tk.Text(
            self.root, width=COLS, height=ROWS, wrap="word", bd=0,
            highlightthickness=0, padx=14, pady=12, cursor="arrow",
            font=("Menlo", 14), bg=COLORS["bg"], fg=COLORS["fg"])
        self.text.pack()
        tags = {"key": {"foreground": COLORS["key"], "font": ("Menlo", 14, "bold")},
                "ok": {"foreground": COLORS["ok"], "font": ("Menlo", 14, "bold")},
                "bad": {"foreground": COLORS["bad"], "font": ("Menlo", 14, "bold")},
                "mode": {"foreground": COLORS["mode"]},
                "sel": {"background": COLORS["sel_bg"], "foreground": COLORS["sel_fg"]}}
        for name, opts in tags.items():
            self.text.tag_configure(name, **opts)
        self.text.configure(state="disabled")

        self.poller = StatusPoller(roku)
        self.poller.start()
        self.remote = Remote(roku, send_mode)
        self._last_render = None
        self.visible = False
        self.signal_request = None

        self.hotkey = None
        try:
            self.hotkey = GlobalHotkey(hotkey_spec)
            print(f"roku_gui: hotkey {hotkey_spec} registered", flush=True)
        except (OSError, ValueError) as exc:
            print(f"roku_gui: hotkey disabled: {exc}", file=sys.stderr, flush=True)

        def probe():  # surfaces Local Network permission problems in the log
            try:
                roku.status()
                print(f"roku_gui: TV at {roku.ip} reachable", flush=True)
            except OSError as exc:
                print(f"roku_gui: TV at {roku.ip} unreachable: {exc}", flush=True)
        threading.Thread(target=probe, daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Command-w>", lambda _e: self.hide())
        self.root.bind("<Command-q>", lambda _e: self.quit())
        self.root.createcommand("tk::mac::Quit", self.quit)
        self.root.createcommand("tk::mac::ReopenApplication", self.show)
        self.root.bind("<KeyPress>", self.on_key)

        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        self.root.geometry(f"+{x}+{int(self.root.winfo_screenheight() * 0.18)}")

        if start_hidden:
            self.root.withdraw()
            self.poller.active = False
        else:
            self.show()
        self.root.after(30, self.loop)

    # -- visibility --

    def show(self):
        self.visible = True
        self.poller.active = True
        self.poller.refresh_now()
        self.root.deiconify()
        self.nsapp.activate()
        self.root.lift()
        self.root.focus_force()
        self.text.focus_set()
        self.render()

    def hide(self):
        self.visible = False
        self.poller.active = False
        self.remote.reset()
        self.root.withdraw()
        self.nsapp.hide()

    def toggle(self):
        self.hide() if self.visible else self.show()

    def quit(self):
        self.remote.close()
        self.root.destroy()

    # -- events --

    def on_key(self, event):
        key = logical_key(event)
        if key is None:
            return None
        result = self.remote.handle(key)
        if result in ("quit", "esc"):
            self.hide()
        else:
            self.render()
        return "break"

    def loop(self):
        if self.hotkey is not None and self.hotkey.fired():
            self.toggle()
        if self.signal_request:
            request, self.signal_request = self.signal_request, None
            self.toggle() if request == "toggle" else self.show()
        if self.visible:
            self.remote.tick()
            self.render()
        self.root.after(30, self.loop)

    # -- drawing --

    def segments(self):
        r = self.remote
        out = []
        if r.mode == "picker":
            out.append((r.mode_line() + "\n\n", "ok"))
            if r.picker_loading:
                return out + [("  loading apps…", None)]
            matches = r.picker_matches()
            visible_rows = ROWS - 2
            start = max(0, r.picker_selected - visible_rows + 1)
            for idx, (name, _id, kind) in enumerate(matches[start:start + visible_rows], start):
                label = f"  {'[input] ' if kind == 'tvin' else ''}{name}".ljust(COLS)
                out.append((label + "\n", "sel" if idx == r.picker_selected else None))
            if not matches:
                out.append(("  no matches", None))
            return out

        header, ok = self.poller.header()
        out.append((header + "\n\n", "ok" if ok else "bad"))
        out.append((r.mode_line() + "\n\n", "mode"))
        if r.mode in ("recording", "review"):
            out.append(("  " + r.transcript(), "ok"))
            return out
        col = 2
        out.append(("  ", None))
        for key, label in KEY_LEGEND:
            chunk = f"{key} {label}   "
            if col + len(chunk) >= COLS:
                out.append(("\n  ", None))
                col = 2
            out.append((key, "key"))
            out.append((f" {label}   ", None))
            col += len(chunk)
        return out

    def render(self):
        segs = self.segments()
        if segs == self._last_render:
            return
        self._last_render = segs
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for text, tag in segs:
            self.text.insert("end", text, tag or ())
        self.text.configure(state="disabled")

    def run(self):
        self.root.mainloop()


def main():
    config = load_config()
    parser = argparse.ArgumentParser(description="Roku remote window (macOS)")
    parser.add_argument("--ip", default=config.get("tv_ip", DEFAULT_IP), help="Roku TV IP address")
    parser.add_argument("--dictate", choices=("review", "send"),
                        default=config.get("dictation_send", "review"))
    parser.add_argument("--hotkey", default=config.get("hotkey", DEFAULT_HOTKEY),
                        help=f"global hotkey, e.g. {DEFAULT_HOTKEY} or cmd+shift+f5")
    parser.add_argument("--background", action="store_true", help="start hidden")
    parser.add_argument("--toggle", action="store_true", help="show/hide the running instance")
    args = parser.parse_args()

    pid = running_pid()
    if pid:
        if not args.background:
            os.kill(pid, signal.SIGUSR1 if args.toggle else signal.SIGUSR2)
        return
    if args.toggle and not pid:
        args.background = False  # nothing running: --toggle just opens it

    window = RokuWindow(Roku(args.ip), args.dictate, args.hotkey, args.background)

    def on_signal(signum, _frame):
        window.signal_request = "toggle" if signum == signal.SIGUSR1 else "show"
    signal.signal(signal.SIGUSR1, on_signal)
    signal.signal(signal.SIGUSR2, on_signal)

    PID_PATH.write_text(str(os.getpid()))
    try:
        window.run()
    finally:
        if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
            PID_PATH.unlink()


if __name__ == "__main__":
    main()
