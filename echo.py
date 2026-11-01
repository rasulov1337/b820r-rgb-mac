#!/usr/bin/env python3
"""
echo.py - live keystroke echo with a heat trail on the Bloody B820R.

Every key press lights that physical key instantly: white-hot at the hit,
then fading through an accent colour over a fraction of a second. A slower
layer accumulates per-key heat, so the keys you use most stay warm and the
board slowly paints your session. With --ripple every press also sends an
expanding ring outward from the key that was hit; with --blast that ring
becomes a filled fireball that cools from amber to red, and the --ember load
meter behind it lets a burst of typing heat the whole board.

Usage:
    python3 echo.py                 # echo + heat trail
    python3 echo.py --hue 280       # purple accent
    python3 echo.py --no-heat       # pure echo, nothing accumulates
    python3 echo.py --heat-only     # slow heat map only
    python3 echo.py --ripple        # each press also ripples outward
    python3 echo.py --blast --ember 0.35   # critical overheat: black board, fireballs
    python3 echo.py --duration 60   # stop after a minute
"""

import argparse
import colorsys
import math
import signal
import sys
import time

import keyboard as kbmod

KEY_COUNT = kbmod.KEY_COUNT
ROWS = kbmod.ROWS
COLS = kbmod.COLS
BLACK = (0, 0, 0)

# (row, column, protocol index) for every key that physically exists; the
# matrix holes have no key behind them, so they stay dark.
CELLS = [
    (r, c, key)
    for r in range(ROWS)
    for c, key in enumerate(kbmod.MATRIX[r])
    if key >= 0
]
CELL_OF = {key: (r, c) for r, c, key in CELLS}

# Blue -> cyan -> yellow -> red, for the accumulated heat layer.
HEAT_STOPS = (0.62, 0.50, 0.14, 0.0)
HEAT_MAX = 6.0

# Rings alive at once; fast typing would otherwise make every frame quadratic.
RIPPLE_MAX = 10

# Critical overheat: the blasts cool from amber (31 degrees) to red, and the
# standing ember walks the same path as the overload meter fills.
AMBER_HUE = 0.085
RED_HUE = 0.0
BLAST_MAX = 12
LOAD_PER_PRESS = 0.25


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def hsv(hue, value, saturation=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation), clamp(value))
    return int(r * 255), int(g * 255), int(b * 255)


def heat_hue(level):
    position = clamp(level) * (len(HEAT_STOPS) - 1)
    index = min(int(position), len(HEAT_STOPS) - 2)
    fraction = position - index
    return HEAT_STOPS[index] + (HEAT_STOPS[index + 1] - HEAT_STOPS[index]) * fraction


class Echo:
    def __init__(self, args):
        self.decay = args.decay
        self.cool = args.cool
        # exp decay: reach ~10% of the level at the configured time
        self.decay_rate = 2.3 / args.decay
        self.cool_rate = 2.3 / args.cool
        # A fireball look wants the pressed key to cool through amber too, so
        # --blast defaults the flash hue to AMBER_HUE unless --hue is given.
        flash_hue = args.hue
        if flash_hue is None:
            flash_hue = AMBER_HUE * 360.0 if args.blast else 190.0
        self.hue = flash_hue / 360.0
        self.brightness = args.brightness
        self.heat_enabled = not args.no_heat
        self.echo_enabled = not args.heat_only
        self.ripple_enabled = args.ripple
        self.ripple_speed = args.ripple_speed
        self.ripple_thickness = args.ripple_thickness
        self.ripple_rate = 2.3 / args.ripple_decay
        self.blast_enabled = args.blast
        self.blast_speed = args.blast_speed
        self.blast_thickness = args.blast_thickness
        self.blast_rate = 2.3 / args.blast_decay
        self.blast_life = args.blast_decay
        self.ember_max = args.ember
        self.ember_rate = 2.3 / args.ember_cool
        self.reach = math.hypot(ROWS, COLS)
        self.echo = {}
        self.heat = {}
        self.rings = []
        self.blasts = []
        self.load = 0.0
        self.last_t = None
        self.presses = 0

    def press(self, key, t):
        self.presses += 1
        if self.echo_enabled:
            self.echo[key] = 1.0
        if self.heat_enabled:
            self.heat[key] = min(HEAT_MAX, self.heat.get(key, 0.0) + 1.0)
        if self.ripple_enabled and key in CELL_OF:
            row, col = CELL_OF[key]
            self.rings.append({"row": row, "col": col, "born": t})
            if len(self.rings) > RIPPLE_MAX:
                del self.rings[0]
        if self.blast_enabled:
            self.load = clamp(self.load + LOAD_PER_PRESS)
            if key in CELL_OF:
                row, col = CELL_OF[key]
                self.blasts.append({"row": row, "col": col, "born": t})
                if len(self.blasts) > BLAST_MAX:
                    del self.blasts[0]

    def render(self, t):
        dt = 0.0 if self.last_t is None else max(0.0, min(t - self.last_t, 0.25))
        self.last_t = t

        if dt > 0.0:
            fade = math.exp(-dt * self.decay_rate)
            for key in list(self.echo):
                level = self.echo[key] * fade
                if level < 0.01:
                    del self.echo[key]
                else:
                    self.echo[key] = level
            if self.heat_enabled:
                cool = math.exp(-dt * self.cool_rate)
                for key in list(self.heat):
                    value = self.heat[key] * cool
                    if value < 0.02:
                        del self.heat[key]
                    else:
                        self.heat[key] = value
            if self.blast_enabled or self.ember_max > 0.0:
                self.load *= math.exp(-dt * self.ember_rate)
                if self.load < 0.005:
                    self.load = 0.0

        # The brightest layer wins per key: a faint ring must not blank out a
        # warm heat trail, and a fresh flash must not be dulled by one.
        level = [0.0] * KEY_COUNT
        hue = [0.0] * KEY_COUNT
        saturation = [0.0] * KEY_COUNT

        def put(key, value, tone, sat):
            if value > level[key]:
                level[key] = value
                hue[key] = tone
                saturation[key] = sat

        # Standing ember: black at rest, warming toward a dim red-orange glow
        # as the overload meter fills. Written first, so it only ever shows on
        # keys nothing brighter is using.
        if self.ember_max > 0.0 and self.load > 0.0:
            glow = self.ember_max * (self.load ** 0.7) * self.brightness
            tone = AMBER_HUE + (RED_HUE - AMBER_HUE) * self.load
            for key in range(KEY_COUNT):
                put(key, glow, tone, 0.95)

        if self.heat_enabled:
            for key, value in self.heat.items():
                heat_level = clamp(value / HEAT_MAX)
                put(key, (0.10 + 0.55 * heat_level) * self.brightness,
                    heat_hue(heat_level), 0.95)
        for key, echo_level in self.echo.items():
            put(key, echo_level * self.brightness, self.hue,
                clamp(1.0 - 1.4 * echo_level))

        if self.ripple_enabled:
            alive = []
            for ring in self.rings:
                age = t - ring["born"]
                radius = age * self.ripple_speed
                fade = math.exp(-age * self.ripple_rate)
                if radius > self.reach + 3.0 * self.ripple_thickness or fade < 0.01:
                    continue
                alive.append(ring)
                for r, c, key in CELLS:
                    dist = math.hypot(c - ring["col"], r - ring["row"])
                    delta = (dist - radius) / self.ripple_thickness
                    ring_level = math.exp(-delta * delta) * fade
                    put(key, ring_level * self.brightness, self.hue,
                        clamp(1.0 - 1.4 * ring_level))
            self.rings = alive

        if self.blast_enabled:
            alive = []
            surge = 0.8 + 0.2 * self.load
            for blast in self.blasts:
                age = t - blast["born"]
                radius = age * self.blast_speed
                fade = math.exp(-age * self.blast_rate)
                if radius > self.reach + 3.0 * self.blast_thickness or fade < 0.01:
                    continue
                alive.append(blast)
                for r, c, key in CELLS:
                    dist = math.hypot(c - blast["col"], r - blast["row"])
                    # A filled core with a brighter leading rim: the core makes
                    # it a fireball rather than the thin ripple ring, the rim
                    # keeps the shockwave edge visible as the ball grows.
                    if radius > 0.15:
                        core = math.exp(-(dist / radius) ** 2 * 2.2)
                    else:
                        core = 1.0
                    rim = math.exp(-((dist - radius) / self.blast_thickness) ** 2)
                    blast_level = max(0.65 * core, rim) * fade * surge
                    # Fresh fire is amber all over; as it grows, the leading
                    # edge and the older rings cool through red.
                    position = max(age / self.blast_life, dist / max(radius, 0.15))
                    tone = AMBER_HUE + (RED_HUE - AMBER_HUE) * clamp(position)
                    put(key, blast_level * self.brightness, tone, 0.95)
            self.blasts = alive

        colors = [BLACK] * KEY_COUNT
        for key in range(KEY_COUNT):
            if level[key] > 0.01:
                colors[key] = hsv(hue[key], level[key], saturation=saturation[key])
        return colors


def run(args):
    led = kbmod.find_led_device()
    if led is None:
        print("Bloody B820R LED channel not found (is it plugged in?)", file=sys.stderr)
        return 1
    keys = kbmod.find_keyboard_device()
    if keys is None:
        print("Bloody B820R keyboard interface not found", file=sys.stderr)
        return 1

    keyboard = kbmod.BloodyB820R(led).open()
    listener = kbmod.KeyListener(keys)
    echo = Echo(args)
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    heat = "off" if args.no_heat else "%.0fs" % args.cool
    extra = ""
    if args.ripple:
        extra += " | ripple %.0f c/s, %.2fs" % (args.ripple_speed, args.ripple_decay)
    if args.blast:
        extra += " | blast %.0f c/s, %.2fs" % (args.blast_speed, args.blast_decay)
    if args.ember > 0.0:
        extra += " | ember %.2f / %.1fs" % (args.ember, args.ember_cool)
    print("echo | fade %.2fs | heat %s%s | %.0f fps | Ctrl-C to stop"
          % (args.decay, heat, extra, args.fps))
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
                echo.press(key, elapsed)
                if args.verbose:
                    print("  %s" % kbmod.KEY_NAMES[key], flush=True)
            keyboard.frame(echo.render(elapsed))
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
        print("\n%d keys | Released, keyboard is back on its built-in effect."
              % echo.presses)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Live keystroke echo with a heat trail on a Bloody B820R.")
    parser.add_argument("--decay", type=float, default=0.7,
                        help="seconds for a press to fade to ~10%% (default 0.7)")
    parser.add_argument("--cool", type=float, default=25.0,
                        help="seconds for accumulated heat to fade to ~10%% (default 25)")
    parser.add_argument("--hue", type=float, default=None,
                        help="accent hue in degrees (default 190, cyan; amber "
                             "with --blast)")
    parser.add_argument("--no-heat", action="store_true",
                        help="pure echo; do not accumulate heat")
    parser.add_argument("--heat-only", action="store_true",
                        help="only the slow heat map, no per-press flash")
    parser.add_argument("--ripple", action="store_true",
                        help="every press also sends an expanding ring across the board")
    parser.add_argument("--ripple-speed", type=float, default=9.0,
                        help="ripple: ring speed in cells per second (default 9)")
    parser.add_argument("--ripple-thickness", type=float, default=1.0,
                        help="ripple: ring thickness in cells (default 1)")
    parser.add_argument("--ripple-decay", type=float, default=1.4,
                        help="ripple: seconds for a ring to fade to ~10%% (default 1.4)")
    parser.add_argument("--blast", action="store_true",
                        help="every press detonates a filled amber-to-red fireball")
    parser.add_argument("--blast-speed", type=float, default=10.0,
                        help="blast: expansion in cells per second (default 10)")
    parser.add_argument("--blast-thickness", type=float, default=1.0,
                        help="blast: rim thickness in cells (default 1)")
    parser.add_argument("--blast-decay", type=float, default=1.2,
                        help="blast: seconds to fade to ~10%% (default 1.2)")
    parser.add_argument("--ember", type=float, default=0.0,
                        help="standing glow at full overload; 0 = black at rest")
    parser.add_argument("--ember-cool", type=float, default=3.0,
                        help="seconds for the overload meter to fall to ~10%% "
                             "(default 3)")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--verbose", action="store_true", help="print every key name")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.decay <= 0:
        parser.error("--decay must be positive")
    if args.cool <= 0:
        parser.error("--cool must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if not 0 < args.brightness <= 1:
        parser.error("--brightness must be in (0, 1]")
    if args.no_heat and args.heat_only:
        parser.error("--no-heat and --heat-only are mutually exclusive")
    if args.ripple_speed <= 0:
        parser.error("--ripple-speed must be positive")
    if args.ripple_thickness <= 0:
        parser.error("--ripple-thickness must be positive")
    if args.ripple_decay <= 0:
        parser.error("--ripple-decay must be positive")
    if args.blast_speed <= 0:
        parser.error("--blast-speed must be positive")
    if args.blast_thickness <= 0:
        parser.error("--blast-thickness must be positive")
    if args.blast_decay <= 0:
        parser.error("--blast-decay must be positive")
    if not 0.0 <= args.ember <= 1.0:
        parser.error("--ember must be in [0, 1]")
    if args.ember_cool <= 0:
        parser.error("--ember-cool must be positive")
    if args.duration < 0:
        parser.error("--duration must be >= 0")

    try:
        return run(args)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
