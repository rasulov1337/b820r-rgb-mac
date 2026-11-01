#!/usr/bin/env python3
"""
sysmon.py - system load gauges on the Bloody B820R key matrix.

Each gauge row fills left to right and walks green -> amber -> red as it
fills:
    row 1 (number row)  CPU load average, per core
    row 2 (QWERTY row)  memory in use
    row 3 (A row)       network rate, log scale
    row 4 (Z row)       battery charge (dark on a Mac without a battery)

Everything is sampled with stdlib tools (getloadavg, vm_stat, netstat,
pmset) in background threads, so a slow subprocess never stalls the LEDs.

Usage:
    python3 sysmon.py
    python3 sysmon.py --fps 6 --refresh 1
"""

import argparse
import colorsys
import math
import os
import re
import signal
import subprocess
import sys
import time

import keyboard as kbmod
from ambient import Refresher

KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BLACK = (0, 0, 0)

GAUGE_ROWS = {"cpu": 1, "mem": 2, "net": 3, "batt": 4}
ROW_KEYS = {
    row: [MATRIX[row][c] for c in range(COLS) if MATRIX[row][c] >= 0]
    for row in GAUGE_ROWS.values()
}

# log10(bytes/s) that fills the network gauge: 10^7 = 10 MB/s.
NET_FULL_SCALE = 7.0


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def hsv(hue, value, saturation=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation), clamp(value))
    return int(r * 255), int(g * 255), int(b * 255)


def sample_memory():
    """Used memory fraction: (active + wired + compressed) / total."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                         timeout=5).stdout
    match = re.search(r"page size of (\d+) bytes", out)
    page_size = int(match.group(1)) if match else 4096
    pages = {}
    for line in out.splitlines():
        match = re.match(r"Pages ([^:]+):\s+(\d+)\.", line)
        if match:
            pages[match.group(1).strip()] = int(match.group(2))
    used = (pages.get("active", 0) + pages.get("wired down", 0)
            + pages.get("occupied by compressor", 0))
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True,
                               timeout=5).stdout.strip())
    return clamp(used * page_size / float(total))


def sample_battery():
    """(charge fraction, charging) or None on a Mac without a battery."""
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                         text=True, timeout=5).stdout
    if "InternalBattery" not in out:
        return None
    match = re.search(r"(\d+)%", out)
    if not match:
        return None
    # pmset says "charging", "not charging", "discharging" or "charged".
    charging = ("discharging" not in out and "not charging" not in out
                and ("charging" in out or "charged" in out))
    return int(match.group(1)) / 100.0, charging


class NetworkRate:
    """netstat -ib byte deltas, summed over the physical interfaces."""

    def __init__(self):
        self.last = None

    def __call__(self):
        # -n matters: without it netstat reverse-resolves every address and
        # can block for seconds.
        out = subprocess.run(["netstat", "-ibn"], capture_output=True,
                             text=True, timeout=5).stdout
        total = 0
        for line in out.splitlines()[1:]:
            parts = line.split()
            # Only the <Link> rows carry byte counters; counting the
            # per-address rows too would double-count every packet.
            if len(parts) < 10 or not parts[2].startswith("<Link"):
                continue
            if parts[0].startswith("lo"):
                continue
            try:
                total += int(parts[-5]) + int(parts[-2])  # Ibytes + Obytes
            except ValueError:
                continue
        now = time.monotonic()
        last, self.last = self.last, (now, total)
        if last is None or total < last[1]:
            return 0.0
        return (total - last[1]) / max(0.1, now - last[0])


class SysMon:
    def __init__(self, args):
        self.brightness = args.brightness
        self.cores = max(1, os.cpu_count())
        self.mem = Refresher(sample_memory, args.refresh)
        self.net = Refresher(NetworkRate(), args.refresh)
        # Battery charge moves slowly; polling pmset every refresh is waste.
        self.batt = Refresher(sample_battery, max(15.0, args.refresh * 10))

    def start(self):
        self.mem.start()
        self.net.start()
        self.batt.start()

    def close(self):
        self.mem.close()
        self.net.close()
        self.batt.close()

    def _gauge(self, colors, row, fill, hue=None, pulse=1.0):
        if hue is None:
            hue = 0.33 * (1.0 - clamp(fill))  # green -> amber -> red
        keys = ROW_KEYS[row]
        lit = int(round(clamp(fill) * len(keys)))
        color = hsv(hue, 0.85 * self.brightness * pulse)
        for key in keys[:lit]:
            colors[key] = color

    def render(self, t):
        colors = [BLACK] * KEY_COUNT

        cpu = clamp(os.getloadavg()[0] / self.cores)
        self._gauge(colors, GAUGE_ROWS["cpu"], cpu)

        mem, _error = self.mem.get()
        if mem is not None:
            self._gauge(colors, GAUGE_ROWS["mem"], mem)

        rate, _error = self.net.get()
        if rate is not None:
            fill = clamp(math.log10(1.0 + rate) / NET_FULL_SCALE)
            self._gauge(colors, GAUGE_ROWS["net"], fill, hue=0.55)

        batt, _error = self.batt.get()
        if batt:
            frac, charging = batt
            if charging:
                self._gauge(colors, GAUGE_ROWS["batt"], frac, hue=0.33)
            elif frac < 0.2:
                # Low battery: red, breathing.
                pulse = 0.6 + 0.4 * math.sin(t * 4.0)
                self._gauge(colors, GAUGE_ROWS["batt"], frac, hue=0.0,
                            pulse=pulse)
            else:
                self._gauge(colors, GAUGE_ROWS["batt"], frac,
                            hue=0.33 * frac)
        return colors


def run(args):
    device = kbmod.find_led_device()
    if device is None:
        print("Bloody B820R not found (is it plugged in?)", file=sys.stderr)
        return 1

    monitor = SysMon(args)
    keyboard = kbmod.BloodyB820R(device).open()
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    print("sysmon | %.0f fps | Ctrl-C to stop" % args.fps)
    interval = 1.0 / args.fps
    started = time.monotonic()
    next_tick = started

    try:
        keyboard.take_control()
        monitor.start()
        while not stop["requested"]:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            colors = monitor.render(time.monotonic() - started)
            keyboard.frame(colors)
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        monitor.close()
        keyboard.release()
        keyboard.close()
        print("\nReleased, keyboard is back on its built-in effect.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="System load gauges on a Bloody B820R keyboard.")
    parser.add_argument("--refresh", type=float, default=2.0,
                        help="seconds between vm_stat/netstat samples (default 2)")
    parser.add_argument("--fps", type=float, default=4.0,
                        help="frames per second (default 4; gauges move slowly)")
    parser.add_argument("--brightness", type=float, default=1.0,
                        help="brightness 0..1 (default 1.0)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after N seconds")
    return parser


def prepare(parser, args):
    """Validate parsed args (also used by the control panel)."""
    if args.refresh <= 0:
        parser.error("--refresh must be positive")
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
