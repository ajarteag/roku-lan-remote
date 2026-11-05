#!/usr/bin/env python3
"""Roku terminal remote — a curses TUI that talks directly to the TV over ECP.

Usage: python3 roku_tui.py [--ip 192.168.4.217] [--dictate review|send]
Reads the default TV IP (and optional "dictation_send") from config.json
next to this script. Dictation (^d in type mode) uses the macOS `hear` CLI.
Key bindings and modes live in roku_core.Remote (shared with roku_gui.py).
"""
import argparse
import curses
import textwrap

from roku_core import DEFAULT_IP, KEY_LEGEND, Remote, Roku, StatusPoller, load_config

CURSES_KEYS = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down",
    curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
    10: "enter", 13: "enter", curses.KEY_ENTER: "enter",
    127: "backspace", 8: "backspace", curses.KEY_BACKSPACE: "backspace",
    27: "esc", 4: "ctrl-d",
}


def logical_key(ch):
    if ch in CURSES_KEYS:
        return CURSES_KEYS[ch]
    if 32 <= ch < 127:
        return chr(ch)
    return None


def draw_header(stdscr, poller, width):
    line, ok = poller.header()
    color = curses.color_pair(2 if ok else 3)
    stdscr.addnstr(0, 0, line.ljust(width - 1), width - 1, color | curses.A_BOLD)


def draw_legend(stdscr, top, width):
    row, col = top, 2
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


def draw_transcript(stdscr, top, width, shown):
    try:
        for offset, line in enumerate(textwrap.wrap(shown, max(10, width - 4))):
            stdscr.addnstr(top + offset, 2, line, width - 3,
                           curses.color_pair(2) | curses.A_BOLD)
    except curses.error:
        pass


def draw_picker(stdscr, remote, height, width):
    stdscr.addnstr(0, 0, remote.mode_line().ljust(width - 1), width - 1,
                   curses.color_pair(2) | curses.A_BOLD)
    if remote.picker_loading:
        stdscr.addnstr(2, 2, "loading apps…", width - 3)
        return
    for idx, (name, _app_id, kind) in enumerate(remote.picker_matches()[: height - 3]):
        label = f"  {'[input] ' if kind == 'tvin' else ''}{name}"
        attr = curses.color_pair(1) | curses.A_REVERSE if idx == remote.picker_selected else 0
        stdscr.addnstr(2 + idx, 0, label.ljust(width - 1), width - 1, attr)


def main(stdscr, roku, send_mode="review"):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_MAGENTA, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    stdscr.timeout(250)

    poller = StatusPoller(roku)
    poller.start()
    remote = Remote(roku, send_mode)

    try:
        while True:
            remote.tick()
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if remote.mode == "picker":
                draw_picker(stdscr, remote, height, width)
            else:
                draw_header(stdscr, poller, width)
                stdscr.addnstr(2, 0, remote.mode_line().ljust(width - 1), width - 1,
                               curses.color_pair(4))
                if remote.mode in ("recording", "review"):
                    draw_transcript(stdscr, 4, width, remote.transcript())
                else:
                    draw_legend(stdscr, 4, width)
            stdscr.refresh()

            ch = stdscr.getch()
            if ch == -1:
                continue
            key = logical_key(ch)
            if key and remote.handle(key) == "quit":
                return
    finally:
        remote.close()


if __name__ == "__main__":
    config = load_config()
    parser = argparse.ArgumentParser(description="Roku terminal remote")
    parser.add_argument("--ip", default=config.get("tv_ip", DEFAULT_IP),
                        help="Roku TV IP address")
    parser.add_argument("--dictate", choices=("review", "send"),
                        default=config.get("dictation_send", "review"),
                        help="dictated text: review before sending (default) or send immediately")
    args = parser.parse_args()
    curses.wrapper(main, Roku(args.ip), args.dictate)
