#!/usr/bin/env python3
"""
music.py - drive the Bloody B820R LEDs from whatever audio is playing.

Three looks:
    bars    spectrum columns, height = energy (default)
    pulse   the whole board breathes with the bass
    beat    random keys ignite on detected beats

Audio sources:
    default        system audio via audiotap (ScreenCaptureKit)
    --mic          microphone via ffmpeg (works when playing through speakers)
    --test         built-in synthetic loop, no permissions needed

Usage:
    python3 music.py                  # spectrum bars from system audio
    python3 music.py --style pulse
    python3 music.py --style beat --keys 14
    python3 music.py --mic            # speakers -> microphone
    python3 music.py --test           # check the visuals without any audio
"""

import argparse
import colorsys
import os
import random
import signal
import subprocess
import sys
import time

import numpy as np

import keyboard as kbmod

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIOTAP_SRC = os.path.join(HERE, "audiotap.swift")
AUDIOTAP_BIN = os.path.join(HERE, "audiotap")

KEY_COUNT = kbmod.KEY_COUNT

# 6x21 physical key matrix; -1 marks a gap where there is no key.
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BLACK = (0, 0, 0)


def hsv(hue, value, saturation=1.0):
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, saturation, max(0.0, min(1.0, value)))
    return int(r * 255), int(g * 255), int(b * 255)


# --- audio sources ----------------------------------------------------------


class AudioUnavailable(RuntimeError):
    pass


class PipeSource:
    """Reads mono float32 PCM from a child process' stdout."""

    def __init__(self, command, name):
        self.name = name
        try:
            # stdin is kept open purely as a lifeline: audiotap exits when it
            # closes, so a killed visualiser cannot orphan the capture.
            self.proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
            )
        except FileNotFoundError as exc:
            raise AudioUnavailable("%s not found: %s" % (name, exc))

    def read(self, samples):
        need = samples * 4
        chunks = []
        got = 0
        while got < need:
            chunk = self.proc.stdout.read(need - got)
            if not chunk:
                raise AudioUnavailable(
                    "%s stopped producing audio (exit code %s)" % (self.name, self.proc.poll())
                )
            chunks.append(chunk)
            got += len(chunk)
        return np.frombuffer(b"".join(chunks), dtype="<f4").astype(np.float32, copy=True)

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class TestSource:
    """Synthetic loop: a kick every 500 ms plus a warbling lead line."""

    def __init__(self, rate):
        self.rate = rate
        self.t = 0.0

    def read(self, samples):
        t = self.t + np.arange(samples, dtype=np.float32) / self.rate
        self.t += samples / self.rate
        beat = t % 0.5
        kick = np.exp(-beat * 16.0) * np.sin(2 * np.pi * 55.0 * t)
        lead_freq = 220.0 * 2.0 ** (np.floor(t * 2.0) % 4) / 2.0
        lead = 0.25 * np.sin(2 * np.pi * lead_freq * t)
        hats = 0.08 * np.random.standard_normal(samples) * (beat > 0.25)
        return (kick + lead + hats).astype(np.float32)

    def close(self):
        pass


def ensure_audiotap():
    if os.path.exists(AUDIOTAP_BIN) and os.path.getmtime(AUDIOTAP_BIN) >= os.path.getmtime(AUDIOTAP_SRC):
        return AUDIOTAP_BIN
    print("compiling audiotap.swift ...")
    result = subprocess.run(
        [
            "xcrun", "swiftc", "-O", "-o", AUDIOTAP_BIN, AUDIOTAP_SRC,
            "-framework", "ScreenCaptureKit",
            "-framework", "CoreMedia",
            "-framework", "AVFoundation",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioUnavailable("swiftc failed:\n" + result.stderr.strip())
    return AUDIOTAP_BIN


def make_source(args):
    if args.test:
        return TestSource(args.rate), args.rate
    if args.mic:
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", ":%d" % args.device,
            "-f", "f32le", "-ac", "1", "-ar", str(args.rate), "-",
        ]
        return PipeSource(command, "microphone"), args.rate
    binary = ensure_audiotap()
    command = [binary, "--rate", str(args.rate), "--channels", "1"]
    return PipeSource(command, "system audio"), args.rate


# --- analysis ---------------------------------------------------------------


def next_power_of_two(value):
    result = 1
    while result < value:
        result *= 2
    return result


class Analyzer:
    """FFT into log-spaced bands, with auto-gain and smoothing."""

    def __init__(self, rate, hop, bands, decay=0.72):
        self.hop = hop
        self.size = next_power_of_two(hop * 4)
        self.window = np.hanning(self.size).astype(np.float32)
        self.buffer = np.zeros(self.size, dtype=np.float32)
        freqs = np.fft.rfftfreq(self.size, 1.0 / rate)
        top = min(16000.0, rate * 0.45)
        edges = np.geomspace(40.0, top, bands + 1)
        self.masks = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges[:-1], edges[1:])]
        self.decay = decay
        self.levels = np.zeros(bands, dtype=np.float32)
        self.bass = 0.0
        self.peak_db = -40.0
        self.range_db = 42.0

    def push(self, samples):
        count = len(samples)
        self.buffer = np.roll(self.buffer, -count)
        self.buffer[-count:] = samples

        spectrum = np.abs(np.fft.rfft(self.buffer * self.window)) * (2.0 / self.size)
        db = 20.0 * np.log10(spectrum + 1e-9)
        band_db = np.array(
            [float(db[mask].max()) if mask.any() else -100.0 for mask in self.masks],
            dtype=np.float32,
        )

        loudest = float(band_db.max())
        self.peak_db = max(loudest, self.peak_db - 0.15)
        floor = self.peak_db - self.range_db
        normalized = np.clip((band_db - floor) / self.range_db, 0.0, 1.0)

        self.levels = np.maximum(normalized, self.levels * self.decay)
        bass_band = band_db[:3].max()
        bass_level = float(np.clip((bass_band - (self.peak_db - 26.0)) / 26.0, 0.0, 1.0))
        self.bass = max(bass_level, self.bass * 0.86)
        return self.levels, self.bass


class BeatDetector:
    def __init__(self):
        self.previous = 0.0
        self.average_flux = 0.0
        self.last_beat = 0.0

    def update(self, bass, now):
        flux = max(0.0, bass - self.previous)
        self.previous = bass
        self.average_flux = 0.9 * self.average_flux + 0.1 * flux
        threshold = max(0.06, self.average_flux * 2.2)
        if flux > threshold and now - self.last_beat > 0.16:
            self.last_beat = now
            return True
        return False


# --- renderers --------------------------------------------------------------


class Bars:
    """Spectrum columns: bottom-up bars across the key rows."""

    def render(self, levels, bass, now):
        colors = [BLACK] * KEY_COUNT
        for column, level in enumerate(levels):
            hue = 0.66 * column / max(1, COLS - 1)
            height = float(level) * ROWS
            for row in range(ROWS):
                from_bottom = ROWS - 1 - row
                lit = min(1.0, max(0.0, height - from_bottom))
                if lit <= 0.03:
                    continue
                key = MATRIX[row][column]
                if key < 0:
                    continue
                colors[key] = hsv(hue, 0.30 + 0.70 * lit)
        return colors


class Pulse:
    """The whole board breathes with the bass, hue drifts over time."""

    def render(self, levels, bass, now):
        hue = (now * 0.07) % 1.0
        value = 0.12 + 0.88 * bass
        return [hsv(hue, value, saturation=0.85)] * KEY_COUNT


class Beats:
    """Random keys ignite on beats and fade out."""

    def __init__(self, rng, per_beat):
        self.rng = rng
        self.per_beat = per_beat
        self.detector = BeatDetector()
        self.lit = {}  # key -> [hue, level]

    def render(self, levels, bass, now):
        for key in list(self.lit):
            self.lit[key][1] *= 0.80
            if self.lit[key][1] < 0.04:
                del self.lit[key]

        if self.detector.update(bass, now):
            free = [k for k in range(KEY_COUNT) if k not in self.lit]
            if free:
                for key in self.rng.sample(free, min(self.per_beat, len(free))):
                    self.lit[key] = [self.rng.random(), 1.0]

        colors = [BLACK] * KEY_COUNT
        for key, (hue, level) in self.lit.items():
            colors[key] = hsv(hue, level)
        return colors


# --- main -------------------------------------------------------------------


def run(args):
    device = kbmod.find_led_device()
    if device is None:
        print("Bloody B820R not found (is it plugged in?)", file=sys.stderr)
        return 1

    try:
        source, rate = make_source(args)
    except AudioUnavailable as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    hop = max(256, int(rate / args.fps))
    analyzer = Analyzer(rate, hop, COLS)
    rng = random.Random(args.seed)

    if args.style == "bars":
        renderer = Bars()
    elif args.style == "pulse":
        renderer = Pulse()
    else:
        renderer = Beats(rng, args.keys)

    keyboard = kbmod.BloodyB820R(device).open()
    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    print("%s | %s | %.0f Hz | %d bands | %.0f fps | Ctrl-C to stop"
          % (args.style, "synthetic" if args.test else ("microphone" if args.mic else "system audio"),
             rate, COLS, rate / hop))
    started = time.monotonic()
    try:
        keyboard.take_control()
        while not stop["requested"]:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            samples = source.read(hop)
            levels, bass = analyzer.push(samples)
            keyboard.frame(renderer.render(levels, bass, time.monotonic()))
    finally:
        keyboard.release()
        keyboard.close()
        source.close()
        print("\nReleased, keyboard is back on its built-in effect.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Music visualiser for a Bloody B820R keyboard.")
    parser.add_argument("--style", choices=["bars", "pulse", "beat"], default="bars")
    parser.add_argument("--mic", action="store_true", help="capture the microphone instead of system audio")
    parser.add_argument("--test", action="store_true", help="use a synthetic signal (no permissions)")
    parser.add_argument("--device", type=int, default=0, help="microphone index for --mic (default 0)")
    parser.add_argument("--fps", type=float, default=22.0, help="frames per second (default 22)")
    parser.add_argument("--rate", type=int, default=48000, help="capture sample rate (default 48000)")
    parser.add_argument("--keys", type=int, default=12, help="keys per beat for --style beat")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (default: until Ctrl-C)")
    parser.add_argument("--seed", type=int, default=None)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.keys <= 0:
        parser.error("--keys must be positive")
    if args.test and args.mic:
        parser.error("--test and --mic are mutually exclusive")

    try:
        return run(args)
    except AudioUnavailable as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
