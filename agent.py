#!/usr/bin/env python3
"""
agent.py - loading spinner on the numpad while an AI agent (or anything
else) is working.

The comet chases the numpad ring (NumLk -> Num/ -> Num* -> Num- -> Num+ ->
NumEnter -> Num. -> Num0 -> Num1 -> Num4 -> Num7) and picks a new random
colour on every revolution. When the watched work finishes the numpad
flashes green (success) or pulses red (failure).

Usage:
    python3 agent.py                          # spin until Ctrl-C
    python3 agent.py -- <command>             # spin while the command runs
    python3 agent.py --pid 1234               # spin while that process lives
    python3 agent.py --status-file /tmp/agent.state   # poll busy/idle/done/error

Status file values: busy (spin), idle (dim glow), done (green),
error (red). "command" and "pid" take precedence over the file.
"""

import argparse
import colorsys
import math
import os
import random
import signal
import subprocess
import sys
import time

import keyboard as kbmod

KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BLACK = (0, 0, 0)

# The numpad occupies rows 1-5, columns 17-20. The matrix has holes where the
# tall +/Enter keys and the wide 0 key have no separate cell, so the ring is
# built from the perimeter and the holes are simply skipped: 11 physical keys.
NUMPAD_RING = []
NUMPAD_KEYS = []
for _r in range(1, 6):
    for _c in range(17, 21):
        _key = MATRIX[_r][_c]
        if _key >= 0:
            NUMPAD_KEYS.append(_key)
for _cell in ((1, 17), (1, 18), (1, 19), (1, 20), (2, 20), (3, 20), (4, 20),
              (5, 20), (5, 19), (5, 18), (5, 17), (4, 17), (3, 17), (2, 17)):
    _key = MATRIX[_cell[0]][_cell[1]]
    if _key >= 0:
        NUMPAD_RING.append(_key)

assert len(NUMPAD_RING) == 11
assert len(NUMPAD_KEYS) == 17


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def hsv(hue, value, saturation=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation), clamp(value))
    return int(r * 255), int(g * 255), int(b * 255)


def rgb_from_hex(value):
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError("colour must be RRGGBB")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


class Spinner:
    def __init__(self, args, rng):
        self.period = args.period
        self.tail = args.tail
        self.brightness = args.brightness
        self.fixed_hue = args.hue / 360.0 if args.hue is not None else None
        self.rng = rng
        self.hue = self.fixed_hue if self.fixed_hue is not None else rng.random()
        self.revolution = -1

    def render(self, t, state):
        colors = [BLACK] * KEY_COUNT
        if state == "busy":
            step = self.period / len(NUMPAD_RING)
            revolution = int(t / self.period)
            if revolution != self.revolution:
                self.revolution = revolution
                if self.fixed_hue is None:
                    self.hue = self.rng.random()
            head = int(t / step) % len(NUMPAD_RING)
            for offset in range(self.tail + 1):
                index = (head - offset) % len(NUMPAD_RING)
                level = max(0.0, 1.0 - offset / (self.tail + 1.0)) ** 1.3
                saturation = clamp(1.0 - 1.6 * level)
                colors[NUMPAD_RING[index]] = hsv(
                    self.hue, level * self.brightness, saturation)
        elif state == "idle":
            level = 0.07 + 0.04 * math.sin(t * 1.5)
            for key in NUMPAD_RING:
                colors[key] = hsv(self.hue, level * self.brightness, 0.85)
        elif state == "done":
            level = 0.55 + 0.45 * math.sin(t * 10.0)
            for key in NUMPAD_KEYS:
                colors[key] = hsv(1.0 / 3.0, level * self.brightness, 0.8)
        elif state == "error":
            level = 0.55 + 0.45 * math.sin(t * 6.0)
            for key in NUMPAD_KEYS:
                colors[key] = hsv(0.0, level * self.brightness, 0.9)
        return colors


def read_status(path):
    try:
        with open(path, "r") as handle:
            text = handle.read().strip().lower()
    except OSError:
        return "busy"
    if text in ("done", "ok", "success", "finished"):
        return "done"
    if text in ("error", "fail", "failed"):
        return "error"
    if text in ("idle", "waiting", "paused"):
        return "idle"
    return "busy"


def split_command(argv):
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def run(args, command):
    device = kbmod.find_led_device()
    if device is None:
        print("Bloody B820R not found (is it plugged in?)", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    spinner = Spinner(args, rng)
    keyboard = kbmod.BloodyB820R(device).open()
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    child = None
    if command:
        try:
            child = subprocess.Popen(command)
        except FileNotFoundError:
            print("error: command not found: %s" % command[0], file=sys.stderr)
            return 1
        source = "command: %s" % " ".join(command)
    elif args.pid is not None:
        source = "pid %d" % args.pid
    elif args.status_file:
        source = "status file: %s" % args.status_file
    else:
        source = "until Ctrl-C"

    print("agent | spinner on the numpad | %s | Ctrl-C to stop" % source)
    interval = 1.0 / args.fps
    started = time.monotonic()
    next_tick = started
    state = "busy"
    finished_at = None
    exit_code = 0

    try:
        keyboard.take_control()
        while not stop["requested"]:
            now = time.monotonic()
            elapsed = now - started
            if args.duration and elapsed >= args.duration:
                break
            if finished_at is None:
                if child is not None:
                    code = child.poll()
                    if code is not None:
                        state = "done" if code == 0 else "error"
                        exit_code = code
                        finished_at = now
                elif args.pid is not None:
                    if not process_alive(args.pid):
                        state = "done"
                        finished_at = now
                elif args.status_file:
                    state = read_status(args.status_file)
                    if state in ("done", "error"):
                        exit_code = 0 if state == "done" else 1
                        finished_at = now
            elif now - finished_at >= args.flash:
                break

            keyboard.frame(spinner.render(elapsed, state))
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
        keyboard.release()
        keyboard.close()
        print("\nReleased, keyboard is back on its built-in effect.")
    return exit_code


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def build_parser():
    parser = argparse.ArgumentParser(
        description="Loading spinner on a Bloody B820R numpad for agent work.")
    parser.add_argument("--period", type=float, default=1.0,
                        help="seconds per spinner revolution (default 1.0)")
    parser.add_argument("--tail", type=int, default=4, help="comet tail length (default 4)")
    parser.add_argument("--hue", type=float, default=None,
                        help="pin the colour (degrees); default picks a random one per revolution")
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument("--fps", type=float, default=18.0)
    parser.add_argument("--flash", type=float, default=2.0,
                        help="seconds of green/red feedback before exiting (default 2)")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--pid", type=int, default=None, help="spin while this process is alive")
    parser.add_argument("--status-file", default=None,
                        help="poll this file: busy / idle / done / error")
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    argv, command = split_command(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    if command and (args.pid is not None or args.status_file):
        parser.error("use only one of: -- <command>, --pid, --status-file")
    if args.pid is not None and args.status_file:
        parser.error("use only one of: -- <command>, --pid, --status-file")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.period <= 0:
        parser.error("--period must be positive")
    if args.tail < 0:
        parser.error("--tail must be >= 0")
    if not 0 < args.brightness <= 1:
        parser.error("--brightness must be in (0, 1]")
    if args.flash < 0:
        parser.error("--flash must be >= 0")

    try:
        return run(args, command)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
