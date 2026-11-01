#!/usr/bin/env python3
"""
clock.py - the time on the Bloody B820R key matrix.

Styles:
    digits    HH:MM in the shared 3x5 bitmap font across rows 1-5, with a
              blinking colon and an optional seconds sweep on the function row
    binary    hours / minutes / seconds as three 6-bit rows, MSB on the left

Usage:
    python3 clock.py                      # 24-hour digits
    python3 clock.py --style binary
    python3 clock.py --format 12 --seconds off
    python3 clock.py --hue 280 --fps 4
"""

import argparse
import colorsys
import signal
import sys
import time

import font
import keyboard as kbmod

KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BLACK = (0, 0, 0)

# Function-row keys in left-to-right order, for the seconds sweep.
ROW0_KEYS = [MATRIX[0][c] for c in range(COLS) if MATRIX[0][c] >= 0]

# Binary rows (hours, minutes, seconds) and their hues.
BINARY_ROWS = (1, 2, 3)
BINARY_HUES = (0.33, 0.08, 0.55)
BINARY_BITS = 6


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def hsv(hue, value, saturation=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation), clamp(value))
    return int(r * 255), int(g * 255), int(b * 255)


class Clock:
    def __init__(self, args):
        self.style = args.style
        self.format = args.format
        self.seconds = args.seconds
        self.hue = args.hue / 360.0
        self.brightness = args.brightness

    def render(self, t):
        now = time.localtime()
        if self.style == "binary":
            return self._binary(now)
        return self._digits(now, t)

    def _digits(self, now, t):
        colors = [BLACK] * KEY_COUNT
        text = time.strftime("%H:%M" if self.format == "24" else "%I:%M", now)
        columns = font.columns_of(text)
        left = (COLS - len(columns)) // 2
        lit = hsv(self.hue, 0.85 * self.brightness)
        colon_on = (t % 1.0) < 0.55
        for x, column in enumerate(columns):
            # The colon of "HH:MM" always lands on column 8; blink it.
            if x == 8 and not colon_on:
                continue
            for y in range(font.HEIGHT):
                if not column[y]:
                    continue
                r, c = 1 + y, left + x
                if 0 <= c < COLS:
                    key = MATRIX[r][c]
                    if key >= 0:
                        colors[key] = lit
        if self.seconds == "on":
            filled = int(round(now.tm_sec / 60.0 * len(ROW0_KEYS)))
            dim = hsv(self.hue, 0.35 * self.brightness)
            for key in ROW0_KEYS[:filled]:
                colors[key] = dim
        return colors

    def _binary(self, now):
        colors = [BLACK] * KEY_COUNT
        hour = now.tm_hour if self.format == "24" else (now.tm_hour % 12 or 12)
        values = (hour, now.tm_min, now.tm_sec)
        start = (COLS - BINARY_BITS) // 2
        for row, value, hue in zip(BINARY_ROWS, values, BINARY_HUES):
            for bit in range(BINARY_BITS):
                key = MATRIX[row][start + (BINARY_BITS - 1 - bit)]
                if key < 0:
                    continue
                on = (value >> bit) & 1
                # Off bits stay faintly lit so the positions are readable.
                colors[key] = hsv(hue, (0.85 if on else 0.04) * self.brightness)
        return colors


def run(args):
    device = kbmod.find_led_device()
    if device is None:
        print("Bloody B820R not found (is it plugged in?)", file=sys.stderr)
        return 1

    clock = Clock(args)
    keyboard = kbmod.BloodyB820R(device).open()
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    print("%s clock | %.0f fps | Ctrl-C to stop" % (args.style, args.fps))
    interval = 1.0 / args.fps
    started = time.monotonic()
    next_tick = started

    try:
        keyboard.take_control()
        while not stop["requested"]:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            colors = clock.render(time.monotonic() - started)
            keyboard.frame(colors)
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        keyboard.release()
        keyboard.close()
        print("\nReleased, keyboard is back on its built-in effect.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="A clock on the Bloody B820R key matrix.")
    parser.add_argument("--style", choices=["digits", "binary"], default="digits",
                        help="digits: HH:MM in a 3x5 font; binary: 6-bit rows "
                             "(default digits)")
    parser.add_argument("--format", choices=["24", "12"], default="24",
                        help="24- or 12-hour time (default 24)")
    parser.add_argument("--seconds", choices=["on", "off"], default="on",
                        help="digits: seconds sweep on the function row (default on)")
    parser.add_argument("--hue", type=float, default=200.0,
                        help="digit hue in degrees (default 200)")
    parser.add_argument("--fps", type=float, default=2.0,
                        help="frames per second (default 2; the time changes slowly)")
    parser.add_argument("--brightness", type=float, default=1.0,
                        help="brightness 0..1 (default 1.0)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after N seconds")
    return parser


def prepare(parser, args):
    """Validate parsed args (also used by the control panel)."""
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
