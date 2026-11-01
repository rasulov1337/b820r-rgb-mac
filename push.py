#!/usr/bin/env python3
"""
push.py - drive the keyboard through a running ui.py control panel.

The panel owns the LED channel; pushing frames to it (POST /api/frame) is how
other scripts light the board without touching IOKit or needing Input
Monitoring themselves.

Usage:
    python3 push.py --color FF5500        # solid fill, holds until replaced
    python3 push.py --image photo.jpg     # the image across the 6x21 matrix
    python3 push.py --text "HELLO"        # scrolling marquee
    python3 push.py --text "B820R" --once --speed 8
    python3 push.py --off                 # LEDs off
    python3 push.py --restore             # hand back to firmware effects
"""

import argparse
import colorsys
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import font
import keyboard as kbmod

KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BLACK = (0, 0, 0)

CELLS = [
    (r, c, key)
    for r in range(ROWS)
    for c, key in enumerate(MATRIX[r])
    if key >= 0
]


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


def post(path, payload, args):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "http://%s:%d%s" % (args.host, args.port, path), data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def push_frame(colors, args, ttl):
    flat = [channel for rgb in colors for channel in rgb]
    return post("/api/frame", {"colors": flat, "ttl": ttl}, args)


def load_bmp(path):
    """Return (width, height, pixel(x, y)) with (0, 0) at the top left."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] != b"BM":
        raise ValueError("not a BMP file")
    offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if bpp not in (24, 32) or compression != 0:
        raise ValueError("unsupported BMP (need 24/32-bit uncompressed)")
    top_down = height < 0
    height = abs(height)
    row_size = ((width * bpp + 31) // 32) * 4
    step = bpp // 8

    def pixel(x, y):
        row = y if top_down else height - 1 - y
        i = offset + row * row_size + x * step
        return data[i + 2], data[i + 1], data[i]

    return width, height, pixel


def image_colors(path):
    """Resize an image onto the 6x21 matrix and sample one pixel per key."""
    handle = tempfile.NamedTemporaryFile(suffix=".bmp", delete=False)
    handle.close()
    try:
        subprocess.run(
            ["sips", "-z", str(ROWS), str(COLS), "-s", "format", "bmp",
             path, "--out", handle.name],
            check=True, capture_output=True, timeout=20,
        )
        width, height, pixel = load_bmp(handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    colors = [BLACK] * KEY_COUNT
    for r, c, key in CELLS:
        x = min(width - 1, c * width // COLS)
        y = min(height - 1, r * height // ROWS)
        colors[key] = pixel(x, y)
    return colors


def text_frame(columns, offset, rgb, brightness):
    """One marquee frame: the glyph columns at a horizontal pixel offset."""
    color = tuple(int(channel * brightness) for channel in rgb)
    colors = [BLACK] * KEY_COUNT
    base = int(math.floor(offset))
    for r, c, key in CELLS:
        x = c - base
        if 0 <= x < len(columns) and 1 <= r <= font.HEIGHT:
            if columns[x][r - 1]:
                colors[key] = color
    return colors


def run_text(args):
    columns = font.columns_of(args.text)
    if not columns:
        raise ValueError("nothing to scroll")
    rgb = hsv(args.hue / 360.0, 0.9)
    interval = 1.0 / args.fps
    step = args.speed * interval
    offset = float(COLS)  # start just off the right edge
    end = -len(columns)
    # Default ttl keeps a crashed or Ctrl-C'd client from freezing the board.
    ttl = args.ttl if args.ttl is not None else 3.0 * interval
    try:
        while True:
            push_frame(text_frame(columns, offset, rgb, args.brightness),
                       args, ttl)
            offset -= step
            if offset < end:
                if args.once:
                    break
                offset = float(COLS)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Push frames to a running ui.py control panel.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--ttl", type=float, default=None,
                        help="seconds the panel waits for the next frame before "
                             "handing the board back (default: hold forever; "
                             "three frame intervals for --text)")
    parser.add_argument("--color", default=None, help="solid fill, RRGGBB")
    parser.add_argument("--image", default=None,
                        help="sample this image onto the key matrix")
    parser.add_argument("--text", default=None, help="scroll this text")
    parser.add_argument("--off", action="store_true", help="LEDs off")
    parser.add_argument("--restore", action="store_true",
                        help="hand the keyboard back to its firmware effects")
    parser.add_argument("--hue", type=float, default=190.0,
                        help="--text hue in degrees (default 190)")
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument("--speed", type=float, default=6.0,
                        help="--text scroll speed in columns/s (default 6)")
    parser.add_argument("--fps", type=float, default=12.0,
                        help="--text frames per second (default 12)")
    parser.add_argument("--once", action="store_true",
                        help="--text: scroll once instead of looping")
    args = parser.parse_args(argv)

    if not 0 < args.brightness <= 1:
        parser.error("--brightness must be in (0, 1]")
    if args.speed <= 0:
        parser.error("--speed must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")

    try:
        if args.off:
            post("/api/off", {}, args)
            print("LEDs off.")
        elif args.restore:
            post("/api/restore", {}, args)
            print("Keyboard handed back to its firmware effects.")
        elif args.color is not None:
            rgb = rgb_from_hex(args.color)
            colors = [tuple(int(c * args.brightness) for c in rgb)] * KEY_COUNT
            push_frame(colors, args, args.ttl or 0)
            print("Filled with #%s." % args.color.lstrip("#").upper())
        elif args.image is not None:
            push_frame(image_colors(args.image), args, args.ttl or 0)
            print("Showing %s." % args.image)
        elif args.text is not None:
            run_text(args)
        else:
            parser.error("pick one of --color/--image/--text/--off/--restore")
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001 - any odd body still reports sanely
            detail = None
        print("error: the panel refused the request (%s)" % (detail or exc),
              file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print("error: cannot reach the panel at http://%s:%d (%s)\n"
              "      is ui.py running?" % (args.host, args.port, exc.reason),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
