#!/usr/bin/env python3
"""
ambient.py - ambient lighting looks for the Bloody B820R.

Modes:
    aurora    slow drifting colour field
    weather   rain / snow / fog / clear, pulled from Open-Meteo
    bias      wash the board in the screen's average colour
    scanner   bouncing Knight Rider / Cylon sweep
    radar     rotating sweep with fading contacts
    stars     twinkling parallax starfield with shooting stars
    ripple    drops send expanding rings across the key matrix
    sunset    magenta-to-orange gradient along the columns
    fire      Doom fire rising off the bottom row
    plasma    sum-of-sines colour field, or drifting lava blobs

Usage:
    python3 ambient.py aurora
    python3 ambient.py aurora --hue 280 --spread 60
    python3 ambient.py weather --lat 55.75 --lon 37.62
    python3 ambient.py bias
    python3 ambient.py bias --image wallpaper.png
    python3 ambient.py scanner --row 2 --speed 14
    python3 ambient.py radar --period 6
    python3 ambient.py stars
    python3 ambient.py sunset
    python3 ambient.py sunset --drift 0.3 --fps 12
    python3 ambient.py fire
    python3 ambient.py fire --intensity 0.8 --wind 0.4
    python3 ambient.py plasma
    python3 ambient.py plasma --style lava --hue 15
"""

import argparse
import colorsys
import json
import math
import os
import random
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import keyboard as kbmod

KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BLACK = (0, 0, 0)

# (row, column, protocol index) for every key that physically exists.
CELLS = [
    (r, c, key)
    for r in range(ROWS)
    for c, key in enumerate(MATRIX[r])
    if key >= 0
]
assert len(CELLS) == KEY_COUNT


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def hsv(hue, value, saturation=1.0):
    """hue in turns (0..1), value/brightness 0..1."""
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, clamp(saturation), clamp(value))
    return int(r * 255), int(g * 255), int(b * 255)


def rgb_from_hex(value):
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError("colour must be RRGGBB")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


class Refresher:
    """Runs a blocking fetch in a daemon thread and keeps the latest result."""

    def __init__(self, fetch, interval):
        self.fetch = fetch
        self.interval = interval
        self.lock = threading.Lock()
        self.value = None
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                value = self.fetch()
                with self.lock:
                    self.value = value
                    self.error = None
            except Exception as exc:  # network, permission, format...
                with self.lock:
                    self.error = exc
            self.stop.wait(self.interval)

    def get(self):
        with self.lock:
            return self.value, self.error

    def close(self):
        self.stop.set()


# --- aurora -----------------------------------------------------------------


class Aurora:
    def __init__(self, args):
        self.hue = args.hue / 360.0
        self.spread = args.spread / 360.0
        self.brightness = args.brightness
        rng = random.Random(args.seed)
        self.phase = {(r, c): rng.uniform(0.0, 2.0 * math.pi) for r, c, _ in CELLS}

    def start(self):
        pass

    def close(self):
        pass

    def render(self, t):
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            p = self.phase[(r, c)]
            v = (0.50
                 + 0.30 * math.sin(c * 0.42 + t * 0.35 + p)
                 + 0.22 * math.sin(r * 0.90 - t * 0.23 + p * 1.7)
                 + 0.14 * math.sin((c + r * 1.5) * 0.27 + t * 0.51 + p * 0.6))
            v = clamp(v)
            level = 0.04 + 0.96 * v ** 1.8
            hue = self.hue + self.spread * (0.5 - v) + 0.02 * math.sin(t * 0.1)
            colors[key] = hsv(hue, level * self.brightness)
        return colors


# --- weather ----------------------------------------------------------------

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather(lat, lon):
    url = ("%s?latitude=%s&longitude=%s"
           "&current=temperature_2m,precipitation,weather_code,is_day"
           "&timezone=auto" % (WEATHER_URL, lat, lon))
    request = urllib.request.Request(url, headers={"User-Agent": "b820r-ambient/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    current = data.get("current") or {}
    return {
        "temp": float(current.get("temperature_2m") or 0.0),
        "precip": float(current.get("precipitation") or 0.0),
        "code": int(current.get("weather_code") or 0),
        "day": bool(int(current.get("is_day") or 1)),
    }


def condition_of(state):
    code = state["code"]
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if code in (45, 48):
        return "fog"
    if code in (0, 1):
        return "clear" if state["day"] else "night"
    if code in (2, 3):
        return "cloudy"
    return "rain" if state["precip"] > 0 else "cloudy"


def temperature_hue(temp):
    """Cold -> blue (0.60), hot -> red (0.0)."""
    return 0.62 - 0.62 * clamp((temp + 10.0) / 45.0)


class Weather:
    def __init__(self, args):
        self.brightness = args.brightness
        self.static = None
        self.refresher = None
        if args.condition or args.temp is not None:
            codes = {"storm": 95, "snow": 71, "rain": 61, "fog": 45,
                     "clear": 0, "night": 0, "cloudy": 2}
            self.static = {
                "temp": args.temp if args.temp is not None else 18.0,
                "precip": 2.0 if args.condition in ("rain", "storm") else 0.0,
                "code": codes.get(args.condition, 2),
                "day": args.condition != "night",
            }
        else:
            self.refresher = Refresher(lambda: fetch_weather(args.lat, args.lon), args.refresh)
        rng = random.Random(args.seed if args.seed is not None else 7)
        self.column = []
        for c in range(COLS):
            self.column.append({
                "phase": rng.uniform(0.0, 10.0),
                "speed": rng.uniform(0.8, 1.3),
                "active": rng.random(),
                "twinkle": rng.uniform(0.0, 2.0 * math.pi),
            })
        self.announced = None
        self.warned = False

    def start(self):
        if self.refresher:
            self.refresher.start()

    def close(self):
        if self.refresher:
            self.refresher.close()

    def render(self, t):
        if self.static is not None:
            state, error = self.static, None
        else:
            state, error = self.refresher.get()

        if error is not None and not self.warned:
            self.warned = True
            print("weather: fetch failed (%s); showing a neutral gradient" % error,
                  file=sys.stderr)
        if state is None:
            return self._gradient(t, 0.55, 0.18)

        condition = condition_of(state)
        if (condition, round(state["temp"])) != self.announced:
            self.announced = (condition, round(state["temp"]))
            print("weather: %s, %.1f C, precipitation %.1f mm"
                  % (condition, state["temp"], state["precip"]), flush=True)

        if condition == "rain":
            return self._falling(t, state, hue=0.58, speed=4.5, density=0.35 + 0.55 * clamp(state["precip"] / 4.0))
        if condition == "snow":
            return self._falling(t, state, hue=0.55, speed=1.3, density=0.7, saturation=0.18)
        if condition == "storm":
            colors = self._falling(t, state, hue=0.58, speed=6.0, density=0.9)
            if (t % 7.0) < 0.12:
                return [(255, 255, 255)] * KEY_COUNT
            return colors
        if condition == "fog":
            return self._fog(t, 0.58)
        if condition == "cloudy":
            return self._clouds(t, 0.58)
        if condition == "clear":
            return self._sun(t, temperature_hue(state["temp"]))
        return self._night(t)

    # -- individual looks ----------------------------------------------------

    def _gradient(self, t, hue, level):
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            v = level * (0.6 + 0.4 * math.sin(c * 0.3 + r * 0.9 + t * 0.4))
            colors[key] = hsv(hue, v * self.brightness)
        return colors

    def _falling(self, t, state, hue, speed, density, saturation=1.0):
        colors = [BLACK] * KEY_COUNT
        for c in range(COLS):
            info = self.column[c]
            if info["active"] > density:
                continue
            span = ROWS + 2.5
            pos = (t * speed * info["speed"] + info["phase"] * 2.0) % span - 1.5
            row = int(math.floor(pos))
            frac = pos - row
            for offset, level in ((0, 0.95 - 0.35 * frac), (1, 0.45 - 0.25 * frac)):
                y = row - offset
                if 0 <= y < ROWS:
                    key = MATRIX[y][c]
                    if key >= 0:
                        colors[key] = hsv(hue, level * self.brightness,
                                          saturation=saturation)
        return colors

    def _fog(self, t, hue):
        colors = [BLACK] * KEY_COUNT
        band = 2.5 + 1.8 * math.sin(t * 0.15)
        for r, c, key in CELLS:
            v = math.exp(-((r - band) ** 2) / (2 * 1.4 ** 2)) * 0.28
            v *= 0.8 + 0.2 * math.sin(c * 0.2 + t * 0.3)
            colors[key] = hsv(hue, v * self.brightness, saturation=0.25)
        return colors

    def _clouds(self, t, hue):
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            v = 0.16 + 0.07 * math.sin(c * 0.25 + t * 0.18)
            v += 0.04 * math.sin(r * 0.9 - t * 0.24)
            colors[key] = hsv(hue, clamp(v) * self.brightness, saturation=0.35)
        return colors

    def _sun(self, t, hue):
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            v = 0.45 + 0.25 * math.sin(c * 0.16 - t * 0.22 + r * 0.4)
            v = clamp(v) ** 1.3
            colors[key] = hsv(hue, v * self.brightness, saturation=0.75)
        return colors

    def _night(self, t):
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            v = 0.5 + 0.5 * math.sin(t * 0.7 + c * 1.7 + r * 2.3)
            level = 0.04 + 0.20 * v ** 6
            colors[key] = hsv(0.58, level * self.brightness, saturation=0.2)
        return colors


# --- bias lighting ----------------------------------------------------------


def average_bmp(path):
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] != b"BM":
        raise ValueError("not a BMP file")
    offset = struct.unpack_from("<I", data, 10)[0]
    width = abs(struct.unpack_from("<i", data, 18)[0])
    height = abs(struct.unpack_from("<i", data, 22)[0])
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if bpp not in (24, 32) or compression not in (0, 3):
        raise ValueError("unsupported BMP (needs 24/32-bit, uncompressed or bitfields)")
    masks = None
    if compression == 3 and bpp == 32:
        masks = struct.unpack_from("<III", data, 54)

    def extract(pixel, mask):
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        return ((pixel & mask) >> shift) * 255 // (mask >> shift)

    step_x = max(1, width // 64)
    step_y = max(1, height // 64)
    row_size = ((width * bpp + 31) // 32) * 4
    bytes_per_pixel = bpp // 8
    total_r = total_g = total_b = 0
    count = 0
    for y in range(0, height, step_y):
        base = offset + y * row_size
        for x in range(0, width, step_x):
            i = base + x * bytes_per_pixel
            if i + bytes_per_pixel > len(data):
                continue
            if masks:
                pixel = struct.unpack_from("<I", data, i)[0]
                total_r += extract(pixel, masks[0])
                total_g += extract(pixel, masks[1])
                total_b += extract(pixel, masks[2])
            else:
                total_b += data[i]
                total_g += data[i + 1]
                total_r += data[i + 2]
            count += 1
    if count == 0:
        raise ValueError("empty image")
    return (total_r // count, total_g // count, total_b // count)


def average_image(path):
    if path.lower().endswith(".bmp"):
        return average_bmp(path)
    handle = tempfile.NamedTemporaryFile(suffix=".bmp", delete=False)
    handle.close()
    try:
        subprocess.run(
            ["sips", "-s", "format", "bmp", path, "--out", handle.name],
            check=True, capture_output=True, timeout=20,
        )
        return average_bmp(handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def capture_screen():
    handle = tempfile.NamedTemporaryFile(suffix=".bmp", delete=False)
    handle.close()
    try:
        subprocess.run(
            ["screencapture", "-x", "-t", "bmp", handle.name],
            check=True, capture_output=True, timeout=20,
        )
        return average_bmp(handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


class Bias:
    def __init__(self, args):
        self.brightness = args.brightness
        self.current = None
        self.last_t = None
        self.warned = False
        self.refresher = None
        if args.image:
            self.target = average_image(args.image)
        elif args.color:
            self.target = rgb_from_hex(args.color)
        else:
            self.target = None
            self.refresher = Refresher(capture_screen, args.refresh)

    def start(self):
        if self.refresher:
            self.refresher.start()

    def close(self):
        if self.refresher:
            self.refresher.close()

    def render(self, t):
        target = self.target
        if self.refresher is not None:
            value, error = self.refresher.get()
            if error is not None and not self.warned:
                self.warned = True
                print("bias: screen capture failed (%s)\n"
                      "      grant Screen & System Audio Recording, or use --image/--color"
                      % error, file=sys.stderr)
            if value is not None:
                target = value

        if target is None:
            return [BLACK] * KEY_COUNT

        if self.current is None:
            self.current = target
        else:
            dt = 0.0 if self.last_t is None else max(0.0, t - self.last_t)
            blend = 1.0 - math.exp(-dt / 0.8)
            self.current = tuple(
                self.current[i] + (target[i] - self.current[i]) * blend
                for i in range(3)
            )
        self.last_t = t

        base = tuple(int(clamp(component, 0, 255) * self.brightness)
                     for component in self.current)
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            wobble = 0.93 + 0.07 * math.sin(c * 0.7 + r * 1.3 + t * 0.5)
            colors[key] = tuple(int(component * wobble) for component in base)
        return colors


# --- scanner ----------------------------------------------------------------


class Scanner:
    def __init__(self, args):
        self.row = args.row
        self.speed = args.speed
        self.trail = max(1.0, args.trail)
        self.hue = args.hue / 360.0
        self.brightness = args.brightness

    def start(self):
        pass

    def close(self):
        pass

    def render(self, t):
        colors = [BLACK] * KEY_COUNT
        span = COLS - 1
        cycle = 2.0 * span / self.speed
        p = (t % cycle) * self.speed
        head = p if p <= span else 2.0 * span - p
        for c in range(COLS):
            key = MATRIX[self.row][c]
            if key < 0:
                continue
            level = max(0.0, 1.0 - abs(c - head) / self.trail) ** 1.4
            if level <= 0.01:
                continue
            saturation = max(0.0, 1.0 - 1.6 * level)
            colors[key] = hsv(self.hue, level * self.brightness, saturation)
        return colors


# --- radar ------------------------------------------------------------------


class Radar:
    def __init__(self, args):
        self.period = args.period
        self.hue = args.hue / 360.0
        self.brightness = args.brightness
        self.rng = random.Random(args.seed)
        self.cx = (COLS - 1) / 2.0
        self.cy = (ROWS - 1) / 2.0
        self.max_dist = math.hypot(self.cx, self.cy)
        self.blips = {}

    def start(self):
        pass

    def close(self):
        pass

    def render(self, t):
        colors = [BLACK] * KEY_COUNT
        beam = (t / self.period) * 2.0 * math.pi

        if self.rng.random() < 0.06:
            _, _, key = self.rng.choice(CELLS)
            self.blips[key] = t + self.rng.uniform(1.5, 3.0)
        for key in [k for k, expiry in self.blips.items() if t > expiry]:
            del self.blips[key]

        for r, c, key in CELLS:
            dx = c - self.cx
            dy = r - self.cy
            distance = math.hypot(dx, dy)
            angle = math.atan2(dy, dx) if distance else 0.0
            delta = abs((angle - beam + math.pi) % (2.0 * math.pi) - math.pi)
            level = max(0.0, 1.0 - delta / 1.1) * (1.0 - 0.55 * distance / self.max_dist)
            if level > 0.02:
                colors[key] = hsv(self.hue, level * self.brightness * 0.9, 0.85)

        for key, expiry in self.blips.items():
            level = clamp((expiry - t) / 1.0) * 0.9
            colors[key] = hsv(self.hue, level * self.brightness, saturation=0.15)
        return colors


# --- stars ------------------------------------------------------------------


class Stars:
    def __init__(self, args):
        self.hue = args.hue / 360.0
        self.brightness = args.brightness
        self.rng = random.Random(args.seed)
        self.layers = []
        plan = ((0.55, 0.55, 0.85), (0.32, 0.35, 1.0), (0.18, 0.22, 1.15))
        for density, speed, gain in plan:
            stars = {}
            for r, c, key in CELLS:
                if self.rng.random() < density:
                    stars[key] = (self.rng.uniform(0.0, 2.0 * math.pi),
                                  self.rng.uniform(0.6, 1.4))
            self.layers.append({"stars": stars, "speed": speed, "gain": gain})
        self.next_shot = self.rng.uniform(3.0, 6.0)
        self.shot = None

    def start(self):
        pass

    def close(self):
        pass

    def render(self, t):
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            v = 0.5 + 0.5 * math.sin(t * 0.35 + c * 0.9 + r * 1.7)
            colors[key] = hsv(0.58, 0.03 * self.brightness, saturation=0.15)

        for layer in self.layers:
            for key, (phase, rate) in layer["stars"].items():
                v = 0.5 + 0.5 * math.sin(t * layer["speed"] * rate + phase)
                level = v ** 8 * layer["gain"]
                if level > 0.03:
                    colors[key] = hsv(self.hue, level * self.brightness, saturation=0.12)

        if self.shot is None and t >= self.next_shot:
            row = self.rng.choice([r for r in range(ROWS)
                                   if any(MATRIX[r][c] >= 0 for c in range(COLS))])
            direction = self.rng.choice((-1, 1))
            start = -2.0 if direction > 0 else COLS + 1.0
            self.shot = {"t0": t, "row": row, "dir": direction,
                         "start": start, "speed": 26.0}

        if self.shot:
            shot = self.shot
            head = shot["start"] + shot["dir"] * shot["speed"] * (t - shot["t0"])
            if head < -6 or head > COLS + 6:
                self.shot = None
                self.next_shot = t + self.rng.uniform(4.0, 9.0)
            else:
                for offset in range(5):
                    x = int(round(head - shot["dir"] * offset))
                    if 0 <= x < COLS:
                        key = MATRIX[shot["row"]][x]
                        if key >= 0:
                            level = 0.95 * (1.0 - offset / 5.0)
                            colors[key] = hsv(self.hue, level * self.brightness,
                                              saturation=0.05)
        return colors


# --- ripple -----------------------------------------------------------------


class Ripple:
    """Rain on a pond: drops send expanding rings across the key matrix."""

    def __init__(self, args):
        self.rng = random.Random(args.seed)
        self.hue = args.hue / 360.0
        self.speed = args.speed
        self.thickness = args.thickness
        self.decay = args.decay
        self.interval = args.interval
        self.brightness = args.brightness
        self.drops = []
        self.next_drop = 0.0
        self.reach = math.hypot(ROWS, COLS)

    def start(self):
        pass

    def close(self):
        pass

    def _spawn(self, t):
        r, c, _key = self.rng.choice(CELLS)
        self.drops.append({"row": r, "col": c, "born": t,
                           "hue": self.hue + self.rng.uniform(-0.03, 0.03)})

    def render(self, t):
        while t >= self.next_drop and len(self.drops) < 12:
            self._spawn(self.next_drop)
            self.next_drop += self.interval * self.rng.uniform(0.6, 1.4)

        best = [0.0] * KEY_COUNT
        hues = [0.0] * KEY_COUNT
        alive = []
        for drop in self.drops:
            age = t - drop["born"]
            radius = age * self.speed
            fade = math.exp(-age / self.decay)
            if radius > self.reach + 3.0 * self.thickness or fade < 0.01:
                continue
            alive.append(drop)
            for r, c, key in CELLS:
                dist = math.hypot(c - drop["col"], r - drop["row"])
                delta = (dist - radius) / self.thickness
                level = math.exp(-delta * delta) * fade
                if level > best[key]:
                    best[key] = level
                    hues[key] = drop["hue"]
        self.drops = alive

        colors = [BLACK] * KEY_COUNT
        for key, level in enumerate(best):
            if level > 0.01:
                colors[key] = hsv(hues[key], level * self.brightness, saturation=0.9)
        return colors


# --- sunset -----------------------------------------------------------------


# Endpoints in HSV: hue in turns, saturation, value. The hue deliberately runs
# past 1.0 so the interpolation passes through pure red, i.e. magenta to
# orange the long way round the colour wheel; a straight RGB lerp would sag
# through a muddy mauve in the middle. Value climbs left to right so the
# golden end reads brighter, like sun near the horizon.
SUNSET_LEFT = (331 / 360, 0.65, 0.82)    # #D24A8C warm magenta
SUNSET_RIGHT = (400 / 360, 0.79, 0.97)   # #F7B733 golden orange


class Sunset:
    """A magenta-to-orange gradient across the columns, holes untouched."""

    def __init__(self, args):
        self.brightness = args.brightness
        self.drift = args.drift

    def start(self):
        pass

    def close(self):
        pass

    def render(self, t):
        phase = self.drift * 0.3 * math.sin(t * 0.35) if self.drift else 0.0
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            x = clamp(c / (COLS - 1) + phase)
            hue = SUNSET_LEFT[0] + (SUNSET_RIGHT[0] - SUNSET_LEFT[0]) * x
            sat = SUNSET_LEFT[1] + (SUNSET_RIGHT[1] - SUNSET_LEFT[1]) * x
            val = SUNSET_LEFT[2] + (SUNSET_RIGHT[2] - SUNSET_LEFT[2]) * x
            colors[key] = hsv(hue, val * self.brightness, saturation=sat)
        return colors


# --- fire -------------------------------------------------------------------


class Fire:
    """The classic Doom fire: heat rises off the bottom row and cools as it
    climbs. The sim runs on the full 6x21 grid (holes included) so the flames
    flow without breaks; only real keys are lit."""

    def __init__(self, args):
        self.brightness = args.brightness
        self.intensity = args.intensity
        self.cooling = args.cooling
        self.wind = args.wind
        self.rng = random.Random(args.seed)
        self.heat = [[0.0] * COLS for _ in range(ROWS)]

    def start(self):
        pass

    def close(self):
        pass

    def _step(self):
        rng = self.rng
        heat = self.heat
        # Stoke the fire line, then let each row cool what rises from below.
        # Reading rows already updated this frame (bottom-up, in place) makes
        # the flames climb fast enough to fill a six-row board.
        bottom = heat[ROWS - 1]
        for c in range(COLS):
            bottom[c] = self.intensity * (0.75 + 0.25 * rng.random())
        for r in range(ROWS - 2, -1, -1):
            below = heat[r + 1]
            row = heat[r]
            for c in range(COLS):
                shift = rng.randint(-1, 1)
                if self.wind and rng.random() < abs(self.wind):
                    shift += 1 if self.wind > 0 else -1
                src = below[min(COLS - 1, max(0, c + shift))]
                row[c] = max(0.0, src - self.cooling * rng.random())

    def render(self, t):
        self._step()
        colors = [BLACK] * KEY_COUNT
        for r, c, key in CELLS:
            h = self.heat[r][c]
            if h <= 0.03:
                continue
            # Deep red embers -> orange -> yellow as the heat climbs; the
            # hottest cells desaturate toward white, like the top of the
            # Doom palette.
            hue = 0.16 * clamp((h - 0.25) / 0.75)
            saturation = 1.0 - clamp((h - 0.85) / 0.15)
            value = clamp(h * 1.5) ** 1.1
            colors[key] = hsv(hue, value * self.brightness, saturation)
        return colors


# --- plasma -----------------------------------------------------------------


class Plasma:
    """A sum-of-sines interference field, or drifting lava blobs."""

    def __init__(self, args):
        self.style = args.style
        self.hue = args.hue / 360.0
        self.spread = args.spread / 360.0
        self.scale = args.scale
        self.speed = args.speed
        self.brightness = args.brightness
        self.cx = (COLS - 1) / 2.0
        self.cy = (ROWS - 1) / 2.0
        rng = random.Random(args.seed)
        self.blobs = []
        for _i in range(3):
            self.blobs.append({
                "ax": rng.uniform(5.0, 9.0),
                "ay": rng.uniform(1.2, 2.2),
                "wx": rng.uniform(0.11, 0.23),
                "wy": rng.uniform(0.13, 0.29),
                "px": rng.uniform(0.0, 2.0 * math.pi),
                "py": rng.uniform(0.0, 2.0 * math.pi),
                "w": rng.uniform(0.8, 1.3),
            })

    def start(self):
        pass

    def close(self):
        pass

    def render(self, t):
        if self.style == "lava":
            return self._lava(t)
        return self._plasma(t)

    def _plasma(self, t):
        colors = [BLACK] * KEY_COUNT
        tt = t * self.speed
        for r, c, key in CELLS:
            v = (math.sin(c * 0.50 * self.scale + tt)
                 + math.sin(r * 1.10 * self.scale - tt * 0.8)
                 + math.sin((c * 0.30 + r * 0.60) * self.scale + tt * 0.6)
                 + math.sin(math.hypot(c - self.cx, r - self.cy) * 0.55
                            * self.scale - tt * 1.1))
            v = (v + 4.0) / 8.0
            hue = self.hue + (v - 0.5) * self.spread
            value = 0.12 + 0.88 * v ** 1.6
            colors[key] = hsv(hue, value * self.brightness)
        return colors

    def _lava(self, t):
        colors = [BLACK] * KEY_COUNT
        blobs = []
        for b in self.blobs:
            x = self.cx + b["ax"] * math.sin(t * b["wx"] + b["px"])
            y = self.cy + b["ay"] * math.cos(t * b["wy"] + b["py"])
            blobs.append((x, y, b["w"]))
        for r, c, key in CELLS:
            field = 0.0
            for x, y, w in blobs:
                dist2 = (c - x) ** 2 + (r - y) ** 2
                field += w / (1.0 + dist2 * 0.2)
            level = clamp((field - 0.30) / 0.70)
            if level <= 0.01:
                continue
            value = 0.10 + 0.90 * level ** 1.4
            colors[key] = hsv(self.hue + self.spread * level,
                              value * self.brightness)
        return colors


# --- runner -----------------------------------------------------------------


RENDERERS = {
    "aurora": Aurora,
    "weather": Weather,
    "bias": Bias,
    "scanner": Scanner,
    "radar": Radar,
    "stars": Stars,
    "ripple": Ripple,
    "sunset": Sunset,
    "fire": Fire,
    "plasma": Plasma,
}


def run(args):
    device = kbmod.find_led_device()
    if device is None:
        print("Bloody B820R not found (is it plugged in?)", file=sys.stderr)
        return 1

    renderer = RENDERERS[args.mode](args)
    keyboard = kbmod.BloodyB820R(device).open()
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    print("%s | %.0f fps | Ctrl-C to stop" % (args.mode, args.fps))
    interval = 1.0 / args.fps
    started = time.monotonic()
    next_tick = started

    try:
        keyboard.take_control()
        renderer.start()
        while not stop["requested"]:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            colors = renderer.render(time.monotonic() - started)
            keyboard.frame(colors)
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        renderer.close()
        keyboard.release()
        keyboard.close()
        print("\nReleased, keyboard is back on its built-in effect.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Ambient lighting for a Bloody B820R keyboard.")
    parser.add_argument("mode", choices=sorted(RENDERERS), help="which look to run")
    parser.add_argument("--fps", type=float, default=None,
                        help="frames per second (default 18; 2 for a still look)")
    parser.add_argument("--brightness", type=float, default=1.0, help="brightness 0..1 (default 1.0)")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds")
    parser.add_argument("--seed", type=int, default=None)

    parser.add_argument("--hue", type=float, default=None, help="base hue in degrees")
    parser.add_argument("--spread", type=float, default=80.0, help="aurora hue spread in degrees")

    parser.add_argument("--lat", type=float, default=55.7558, help="weather latitude (default Moscow)")
    parser.add_argument("--lon", type=float, default=37.6173, help="weather longitude (default Moscow)")
    parser.add_argument("--condition", choices=["rain", "snow", "fog", "clear", "night",
                                               "cloudy", "storm"], default=None,
                        help="weather: force a look instead of fetching (works offline)")
    parser.add_argument("--temp", type=float, default=None,
                        help="weather: temperature in C when forcing a condition")
    parser.add_argument("--refresh", type=float, default=None,
                        help="weather: seconds between fetches (default 900); "
                             "bias: seconds between screen samples (default 2)")

    parser.add_argument("--image", default=None, help="bias: sample this image instead of the screen")
    parser.add_argument("--color", default=None, help="bias: fixed RRGGBB colour instead of the screen")

    parser.add_argument("--row", type=int, default=2, help="scanner: board row (default 2)")
    parser.add_argument("--speed", type=float, default=None,
                        help="scanner: columns per second (default 14); "
                             "ripple: ring speed in cells per second (default 7); "
                             "plasma: field speed in radians/s (default 0.6)")
    parser.add_argument("--trail", type=float, default=6.0, help="scanner: trail length in columns")
    parser.add_argument("--period", type=float, default=6.0, help="radar: seconds per revolution")
    parser.add_argument("--interval", type=float, default=1.6,
                        help="ripple: seconds between drops (default 1.6)")
    parser.add_argument("--thickness", type=float, default=1.1,
                        help="ripple: ring thickness in cells (default 1.1)")
    parser.add_argument("--decay", type=float, default=2.2,
                        help="ripple: seconds for a ring to fade to ~10%% (default 2.2)")
    parser.add_argument("--drift", type=float, default=0.0,
                        help="sunset: how far the colours slide 0..1 (default 0, a still gradient)")
    parser.add_argument("--intensity", type=float, default=1.0,
                        help="fire: bottom-row heat 0..1 (default 1.0)")
    parser.add_argument("--cooling", type=float, default=0.12,
                        help="fire: max heat lost per row step (default 0.12)")
    parser.add_argument("--wind", type=float, default=0.0,
                        help="fire: horizontal flame drift bias -1..1 (default 0)")
    parser.add_argument("--style", choices=["plasma", "lava"], default="plasma",
                        help="plasma: interference field or lava blobs (default plasma)")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="plasma: spatial frequency multiplier (default 1.0)")
    return parser


def prepare(parser, args):
    """Validate parsed args and fill in the mode-dependent defaults."""
    if args.fps is None:
        # A still look does not need a per-frame repaint: six HID reports per
        # tick is wasteful when every tick paints the same picture.
        still = args.mode == "sunset" and args.drift == 0
        args.fps = 2.0 if still else 18.0
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if not 0 < args.brightness <= 1:
        parser.error("--brightness must be in (0, 1]")
    if args.duration < 0:
        parser.error("--duration must be >= 0")
    if args.refresh is None:
        args.refresh = 900.0 if args.mode == "weather" else 2.0
    if args.refresh <= 0:
        parser.error("--refresh must be positive")
    if args.mode == "aurora" and args.hue is None:
        args.hue = 190.0
    elif args.mode == "scanner" and args.hue is None:
        args.hue = 0.0
    elif args.mode == "radar" and args.hue is None:
        args.hue = 120.0
    elif args.mode == "stars" and args.hue is None:
        args.hue = 210.0
    elif args.mode == "ripple" and args.hue is None:
        args.hue = 200.0
    elif args.mode == "plasma" and args.hue is None:
        args.hue = 15.0 if args.style == "lava" else 280.0
    elif args.hue is None:
        args.hue = 190.0
    if args.speed is None:
        args.speed = {"scanner": 14.0, "ripple": 7.0,
                      "plasma": 0.6}.get(args.mode, 7.0)
    if not 0 <= args.row < ROWS:
        parser.error("--row must be 0..%d" % (ROWS - 1))
    if args.speed <= 0:
        parser.error("--speed must be positive")
    if args.trail <= 0:
        parser.error("--trail must be positive")
    if args.period <= 0:
        parser.error("--period must be positive")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.thickness <= 0:
        parser.error("--thickness must be positive")
    if args.decay <= 0:
        parser.error("--decay must be positive")
    if not 0 <= args.drift <= 1:
        parser.error("--drift must be in [0, 1]")
    if not 0 < args.intensity <= 1:
        parser.error("--intensity must be in (0, 1]")
    if not 0 < args.cooling <= 1:
        parser.error("--cooling must be in (0, 1]")
    if not -1 <= args.wind <= 1:
        parser.error("--wind must be in [-1, 1]")
    if args.scale <= 0:
        parser.error("--scale must be positive")
    if args.color is not None:
        try:
            rgb_from_hex(args.color)
        except ValueError as exc:
            parser.error(str(exc))
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
