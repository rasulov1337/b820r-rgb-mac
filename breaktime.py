#!/usr/bin/env python3
"""
breaktime.py - a typing break reminder on the Bloody B820R.

Watches keystrokes (non-seized, so typing works normally) and accumulates
work time: any press within --idle seconds of the last one counts as still
typing. The function row fills from green to red as the --limit approaches,
and a dim board-wide glow rises with it. When the limit hits, the whole
board breathes red until you actually stop: --break minutes without a press
resets the timer with a green flash.

Usage:
    python3 breaktime.py                       # 45 min limit, 5 min rest
    python3 breaktime.py --limit 25 --break 5
    python3 breaktime.py --limit 0.2 --break 0.2   # quick demo
"""

import argparse
import colorsys
import math
import signal
import sys
import time

import keyboard as kbmod

KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS
BLACK = (0, 0, 0)

# Function-row keys in left-to-right order: the strain gauge.
ROW0_KEYS = [MATRIX[0][c] for c in range(COLS) if MATRIX[0][c] >= 0]

GREEN = 0.33
RESET_FLASH = 1.2  # seconds


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def hsv(hue, value, saturation=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation), clamp(value))
    return int(r * 255), int(g * 255), int(b * 255)


class BreakReminder:
    def __init__(self, args):
        self.limit = args.limit * 60.0
        self.rest = args.rest * 60.0
        self.idle = args.idle
        self.brightness = args.brightness
        self.work = 0.0
        self.last_press = None
        self.last_t = None
        self.due = False
        self.reset_at = None

    def press(self, key, t):
        self.last_press = t

    def render(self, t):
        dt = 0.0 if self.last_t is None else max(0.0, min(t - self.last_t, 0.5))
        self.last_t = t
        quiet = None if self.last_press is None else t - self.last_press
        if quiet is not None and quiet >= self.rest:
            # A real rest: reset, and celebrate with a green flash.
            if self.work > 0.0 or self.due:
                self.reset_at = t
            self.work = 0.0
            self.due = False
        elif quiet is not None and quiet < self.idle and not self.due:
            self.work += dt
            if self.work >= self.limit:
                self.work = self.limit
                self.due = True
        progress = clamp(self.work / self.limit)

        colors = [BLACK] * KEY_COUNT
        hue = GREEN * (1.0 - progress)  # green -> amber -> red
        if progress > 0.0:
            base = hsv(hue, (0.02 + 0.10 * progress) * self.brightness)
            for key in range(KEY_COUNT):
                colors[key] = base
        lit = int(round(progress * len(ROW0_KEYS)))
        gauge = hsv(hue, 0.85 * self.brightness)
        for key in ROW0_KEYS[:lit]:
            colors[key] = gauge

        if self.due:
            level = (0.22 + 0.16 * math.sin(t * 2.2)) * self.brightness
            breathing = hsv(0.0, level)
            for key in range(KEY_COUNT):
                colors[key] = breathing

        if self.reset_at is not None:
            age = t - self.reset_at
            if age >= RESET_FLASH:
                self.reset_at = None
            else:
                flash = hsv(GREEN, 0.7 * (1.0 - age / RESET_FLASH)
                            * self.brightness)
                for key in range(KEY_COUNT):
                    colors[key] = flash
        return colors


def run(args):
    led = kbmod.find_led_device()
    if led is None:
        print("Bloody B820R LED channel not found (is it plugged in?)",
              file=sys.stderr)
        return 1
    keys = kbmod.find_keyboard_device()
    if keys is None:
        print("Bloody B820R keyboard interface not found", file=sys.stderr)
        return 1

    keyboard = kbmod.BloodyB820R(led).open()
    listener = kbmod.KeyListener(keys)
    reminder = BreakReminder(args)
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    print("break reminder | %.0f min limit | %.0f min rest | %.0f fps | "
          "Ctrl-C to stop" % (args.limit, args.rest, args.fps))
    interval = 1.0 / args.fps
    started = time.monotonic()
    next_tick = started

    try:
        keyboard.take_control()
        listener.open()
        while not stop["requested"]:
            now = time.monotonic()
            elapsed = now - started
            if args.duration and elapsed >= args.duration:
                break
            for key in listener.poll():
                reminder.press(key, elapsed)
            keyboard.frame(reminder.render(elapsed))
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        listener.close()
        keyboard.release()
        keyboard.close()
        print("\nReleased, keyboard is back on its built-in effect.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="A typing break reminder on a Bloody B820R keyboard.")
    parser.add_argument("--limit", type=float, default=45.0,
                        help="minutes of typing before a break is due "
                             "(default 45)")
    parser.add_argument("--break", dest="rest", type=float, default=5.0,
                        help="quiet minutes that reset the timer (default 5)")
    parser.add_argument("--idle", type=float, default=60.0,
                        help="seconds without a press that pause the work "
                             "clock without resetting it (default 60)")
    parser.add_argument("--fps", type=float, default=5.0,
                        help="frames per second (default 5)")
    parser.add_argument("--brightness", type=float, default=1.0,
                        help="brightness 0..1 (default 1.0)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after N seconds")
    return parser


def prepare(parser, args):
    """Validate parsed args (also used by the control panel)."""
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if args.rest <= 0:
        parser.error("--break must be positive")
    if args.idle <= 0:
        parser.error("--idle must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if not 0 < args.brightness <= 1:
        parser.error("--brightness must be in (0, 1]")
    if args.duration < 0:
        parser.error("--duration must be >= 0")
    return args


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    prepare(parser, args)

    try:
        return run(args)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
