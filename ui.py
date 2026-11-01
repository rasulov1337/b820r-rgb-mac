#!/usr/bin/env python3
"""
ui.py - a small local web control panel for the B820R scripts.

    python3 ui.py                  # http://127.0.0.1:8777, opens a browser
    python3 ui.py --port 9000 --no-browser

One process owns the LED channel: the panel builds a renderer in a worker
thread, keeps the keyboard in direct mode while it runs, and hands the board
back to firmware on Stop, Restore, Ctrl-C or a renderer crash. Starting a mode
while another one runs stops the old one first, so two effects never fight for
the HID channel.

Echo is the exception: it needs the keyboard interface as well as the LEDs and
runs its own CFRunLoop, so it is started as a child process and supervised.

External scripts can drive the board through POST /api/frame (accepts 104 RGB
triples and an optional ttl in seconds, after which the board is handed back
to the firmware); push.py is a ready-made client for colours, images and
scrolling text.

Stdlib only, no build step. ui.html is served from this directory.
"""

import argparse
import atexit
import collections
import fcntl
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import keyboard as kbmod

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(HERE, ".b820r.lock")

BLACK = (0, 0, 0)
KEY_COUNT = kbmod.KEY_COUNT
MATRIX = kbmod.MATRIX
ROWS = kbmod.ROWS
COLS = kbmod.COLS

BOARD = {
    "rows": ROWS,
    "cols": COLS,
    "matrix": [[int(key) for key in row] for row in MATRIX],
    "names": list(kbmod.KEY_NAMES),
}


# --- parameter descriptions -------------------------------------------------


def num(key, label, default, low=None, high=None, step=1.0,
        advanced=False, scale=None):
    spec = {"kind": "num", "key": key, "label": label, "default": default,
            "min": low, "max": high, "step": step}
    if advanced:
        spec["advanced"] = True
    if scale:
        spec["scale"] = scale
    return spec


def percent(key, label, default, low=0.05, high=1.0, step=0.05, advanced=False):
    """A 0..1 fraction the panel shows as a whole percentage."""
    return num(key, label, default, low, high, step,
               advanced=advanced, scale=100)


def integer(key, label, default, low=None, high=None, advanced=False):
    spec = num(key, label, default, low, high, step=1, advanced=advanced)
    spec["integer"] = True
    return spec


def nullable(key, label, default, low=None, high=None, step=1.0, advanced=False):
    spec = num(key, label, default, low, high, step, advanced=advanced)
    spec["nullable"] = True
    return spec


def choice(key, label, default, options, advanced=False):
    spec = {"kind": "choice", "key": key, "label": label, "default": default,
            "options": [{"value": value, "label": text} for value, text in options]}
    if advanced:
        spec["advanced"] = True
    return spec


def text(key, label, default="", advanced=False):
    spec = {"kind": "text", "key": key, "label": label, "default": default}
    if advanced:
        spec["advanced"] = True
    return spec


def fps(key="fps", label="Frames/s", default=18, low=1, high=30):
    """Every mode has one; it belongs under Advanced, not in the main grid."""
    return num(key, label, default, low, high, step=1, advanced=True)


def _coerce(spec, raw):
    kind = spec["kind"]
    if kind == "num":
        if raw is None or raw == "":
            return None if spec.get("nullable") else spec["default"]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return spec["default"]
        if spec.get("integer"):
            value = int(round(value))
        if spec.get("min") is not None:
            value = max(spec["min"], value)
        if spec.get("max") is not None:
            value = min(spec["max"], value)
        return value
    if kind == "choice":
        allowed = [option["value"] for option in spec["options"]]
        return raw if raw in allowed else spec["default"]
    return "" if raw is None else str(raw)


class Mode:
    """One entry in the panel: a label, a parameter schema and a builder."""

    def __init__(self, key, group, label, hint, params, build=None, kind="frame"):
        self.key = key
        self.group = group
        self.label = label
        self.hint = hint
        self.params = params
        self.kind = kind
        self._build = build

    def defaults(self):
        return {spec["key"]: spec["default"] for spec in self.params}

    def clean(self, values):
        values = values if isinstance(values, dict) else {}
        return {spec["key"]: _coerce(spec, values.get(spec["key"], spec["default"]))
                for spec in self.params}

    def build(self, values):
        return self._build(values)

    def describe(self):
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "hint": self.hint,
            "kind": self.kind,
            "params": self.params,
        }


class Runner:
    """A renderer bound to a frame rate, plus whatever cleanup it needs."""

    def __init__(self, step, close=None, fps=18.0):
        self.step = step
        self.fps = max(1.0, float(fps))
        self._close = close

    def close(self):
        if self._close is not None:
            self._close()
            self._close = None


# --- builders ---------------------------------------------------------------


def build_screensaver(values, accumulate):
    args = kbmod.build_parser().parse_args([])
    args.keys = int(values["keys"])
    rng = random.Random(args.seed)
    effect = kbmod.Accumulate(rng, args.keys) if accumulate else kbmod.Twinkle(rng, args.keys)

    def step(_t):
        colors, _lit = effect.update()
        return colors

    return Runner(step, None, values["fps"])


def build_music(values):
    import music as musicmod

    args = musicmod.build_parser().parse_args([])
    args.style = values["style"]
    args.fps = float(values["fps"])
    args.keys = int(values["keys"])
    args.device = int(values["device"])
    args.mic = values["source"] == "mic"
    args.test = values["source"] == "test"

    source, rate = musicmod.make_source(args)
    hop = max(256, int(rate / args.fps))
    analyzer = musicmod.Analyzer(rate, hop, COLS)
    rng = random.Random(args.seed)

    if args.style == "bars":
        renderer = musicmod.Bars()
    elif args.style == "pulse":
        renderer = musicmod.Pulse()
    else:
        renderer = musicmod.Beats(rng, args.keys)

    def step(_t):
        samples = source.read(hop)
        levels, bass = analyzer.push(samples)
        return renderer.render(levels, bass, time.monotonic())

    return Runner(step, source.close, args.fps)


def build_ambient(mode_key, values):
    import ambient as ambientmod

    parser = ambientmod.build_parser()
    args = parser.parse_args([mode_key])
    args.fps = float(values["fps"])
    args.brightness = float(values["brightness"])
    for key in ("hue", "spread", "lat", "lon", "condition", "temp", "refresh",
                "image", "color", "row", "speed", "trail", "period",
                "interval", "thickness", "decay", "drift",
                "intensity", "cooling", "wind", "style", "scale"):
        if key in values:
            value = values[key]
            if key in ("image", "color") and value == "":
                value = None
            if key == "condition" and value == "auto":
                value = None
            setattr(args, key, value)
    ambientmod.prepare(parser, args)

    renderer = ambientmod.RENDERERS[mode_key](args)
    renderer.start()

    def step(t):
        return renderer.render(t)

    return Runner(step, renderer.close, args.fps)


def build_agent(values):
    import agent as agentmod

    args = agentmod.build_parser().parse_args([])
    args.period = float(values["period"])
    args.tail = int(values["tail"])
    args.hue = values["hue"]
    args.brightness = float(values["brightness"])

    spinner = agentmod.Spinner(args, random.Random(args.seed))
    status_file = (values.get("status_file") or "").strip()
    manual = values.get("state") or "busy"

    def step(t):
        state = agentmod.read_status(status_file) if status_file else manual
        return spinner.render(t, state)

    return Runner(step, None, values["fps"])


def echo_base(values):
    return [
        sys.executable, os.path.join(HERE, "echo.py"),
        "--decay", str(values["decay"]),
        "--fps", str(values["fps"]),
        "--brightness", str(values["brightness"]),
    ]


def echo_argv(values):
    argv = echo_base(values) + ["--cool", str(values["cool"]),
                                "--hue", str(values["hue"])]
    style = values["style"]
    if style == "flash":
        argv.append("--no-heat")
    elif style == "heat":
        argv.append("--heat-only")
    elif style == "ripple":
        argv.extend(["--no-heat", "--ripple"])
    elif style == "ripple_heat":
        argv.append("--ripple")
    if style in ("ripple", "ripple_heat"):
        argv.extend([
            "--ripple-speed", str(values["ripple_speed"]),
            "--ripple-thickness", str(values["ripple_thickness"]),
            "--ripple-decay", str(values["ripple_decay"]),
        ])
    return argv


def overheat_argv(values):
    return echo_base(values) + [
        "--no-heat", "--blast",
        "--blast-speed", str(values["blast_speed"]),
        "--blast-thickness", str(values["blast_thickness"]),
        "--blast-decay", str(values["blast_decay"]),
        "--ember", str(values["ember"]),
        "--ember-cool", str(values["ember_cool"]),
    ]


def build_clock(values):
    import clock as clockmod

    parser = clockmod.build_parser()
    args = parser.parse_args([])
    args.style = values["style"]
    args.format = values["format"]
    args.seconds = values["seconds"]
    args.hue = float(values["hue"])
    args.brightness = float(values["brightness"])
    args.fps = float(values["fps"])
    clockmod.prepare(parser, args)

    clock = clockmod.Clock(args)

    def step(t):
        return clock.render(t)

    return Runner(step, None, args.fps)


def build_sysmon(values):
    import sysmon as sysmonmod

    parser = sysmonmod.build_parser()
    args = parser.parse_args([])
    args.brightness = float(values["brightness"])
    args.refresh = float(values["refresh"])
    args.fps = float(values["fps"])
    sysmonmod.prepare(parser, args)

    monitor = sysmonmod.SysMon(args)
    monitor.start()

    def step(t):
        return monitor.render(t)

    return Runner(step, monitor.close, args.fps)


def break_argv(values):
    return [
        sys.executable, os.path.join(HERE, "breaktime.py"),
        "--limit", str(values["limit"]),
        "--break", str(values["rest"]),
        "--idle", str(values["idle"]),
        "--brightness", str(values["brightness"]),
        "--fps", str(values["fps"]),
    ]


# --- mode registry ----------------------------------------------------------


def define_modes():
    modes = [
        Mode("twinkle", "Screensaver", "Twinkle",
             "A fresh random handful of keys every tick.",
             [integer("keys", "Keys per tick", 12, 1, 60),
              fps(default=8)],
             lambda values: build_screensaver(values, False)),
        Mode("accumulate", "Screensaver", "Accumulate",
             "Keys light up and stay lit until the board fills, then it starts over.",
             [integer("keys", "Keys per tick", 12, 1, 30),
              fps(default=8)],
             lambda values: build_screensaver(values, True)),
        Mode("music", "Music", "Spectrum",
             "Bars, pulse or beat from system audio, the microphone or a synthetic loop.",
             [choice("style", "Look", "bars",
                     [("bars", "Bars"), ("pulse", "Pulse"), ("beat", "Beat")]),
              choice("source", "Source", "system",
                     [("system", "System audio"), ("mic", "Microphone"),
                      ("test", "Test tone")]),
              integer("keys", "Keys per beat", 12, 1, 60),
              integer("device", "Mic index", 0, 0, 16, advanced=True),
              fps(default=22)],
             build_music),
        Mode("aurora", "Ambient", "Aurora",
             "Slow drifting colour field across the whole board.",
             [nullable("hue", "Base hue", None, 0, 360),
              num("spread", "Hue spread", 80, 0, 180),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("aurora", values)),
        Mode("weather", "Ambient", "Weather",
             "Rain, snow, fog, clouds or storm; live from Open-Meteo unless forced.",
             [choice("condition", "Condition", "auto",
                     [("auto", "Live weather"), ("clear", "Clear"), ("cloudy", "Cloudy"),
                      ("rain", "Rain"), ("snow", "Snow"), ("fog", "Fog"),
                      ("storm", "Storm"), ("night", "Night")]),
              nullable("temp", "Temperature C", None, -40, 50),
              percent("brightness", "Brightness", 1),
              num("lat", "Latitude", 55.7558, -90, 90, step=0.01, advanced=True),
              num("lon", "Longitude", 37.6173, -180, 180, step=0.01, advanced=True),
              num("refresh", "Refresh s", 900, 60, 3600, step=30, advanced=True),
              fps()],
             lambda values: build_ambient("weather", values)),
        Mode("bias", "Ambient", "Screen bias",
             "Washes the board in the average colour of the screen or an image.",
             [text("image", "Image path", ""),
              text("color", "Fixed RRGGBB", ""),
              percent("brightness", "Brightness", 1),
              num("refresh", "Sample every s", 2, 0.5, 60, step=0.5,
                  advanced=True),
              fps()],
             lambda values: build_ambient("bias", values)),
        Mode("scanner", "Ambient", "Scanner",
             "A Knight Rider bounce with a fading trail along one row.",
             [integer("row", "Board row", 2, 0, ROWS - 1),
              num("speed", "Columns/s", 14, 1, 40),
              num("trail", "Trail length", 6, 1, 20),
              nullable("hue", "Hue", None, 0, 360),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("scanner", values)),
        Mode("radar", "Ambient", "Radar",
             "A rotating sweep with contacts that fade behind it.",
             [num("period", "Seconds/revolution", 6, 1, 30),
              nullable("hue", "Hue", None, 0, 360),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("radar", values)),
        Mode("stars", "Ambient", "Stars",
             "Parallax twinkle with occasional shooting stars.",
             [nullable("hue", "Hue", None, 0, 360),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("stars", values)),
        Mode("ripple", "Ambient", "Ripple",
             "Drops send expanding rings outward across the key matrix.",
             [num("interval", "Drop interval s", 1.6, 0.3, 5, step=0.1),
              num("speed", "Ring speed", 7, 1, 20, step=0.5),
              num("thickness", "Ring thickness", 1.1, 0.3, 3, step=0.1),
              num("decay", "Fade s", 2.2, 0.5, 6, step=0.1),
              nullable("hue", "Hue", None, 0, 360),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("ripple", values)),
        Mode("sunset", "Ambient", "Sunset horizon",
             "A still magenta-to-orange gradient across the columns; raise Drift "
             "and the colours slide slowly left to right.",
             [num("drift", "Drift", 0, 0, 1, step=0.05),
              percent("brightness", "Brightness", 1),
              fps(default=2)],
             lambda values: build_ambient("sunset", values)),
        Mode("fire", "Ambient", "Doom fire",
             "Flames rise off the bottom row and cool from white to red as "
             "they climb.",
             [percent("intensity", "Intensity", 1, 0.1, 1, step=0.05),
              num("cooling", "Cooling", 0.12, 0.02, 0.5, step=0.01),
              num("wind", "Wind", 0, -1, 1, step=0.1),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("fire", values)),
        Mode("plasma", "Ambient", "Plasma",
             "A sum-of-sines colour field, or drifting lava blobs.",
             [choice("style", "Style", "plasma",
                     [("plasma", "Plasma"), ("lava", "Lava")]),
              nullable("hue", "Base hue", None, 0, 360),
              num("spread", "Hue spread", 80, 0, 180),
              num("scale", "Scale", 1, 0.2, 3, step=0.1),
              num("speed", "Speed", 0.6, 0.05, 3, step=0.05),
              percent("brightness", "Brightness", 1),
              fps()],
             lambda values: build_ambient("plasma", values)),
        Mode("agent", "Agent", "Numpad spinner",
             "Comet on the numpad ring; green when done, red on failure.",
             [num("period", "Seconds/revolution", 1, 0.2, 5, step=0.1),
              integer("tail", "Tail length", 4, 0, 10),
              nullable("hue", "Pin hue", None, 0, 360),
              choice("state", "State", "busy",
                     [("busy", "Busy"), ("idle", "Idle"), ("done", "Done"),
                      ("error", "Error")]),
              percent("brightness", "Brightness", 1),
              text("status_file", "Status file", "", advanced=True),
              fps()],
             build_agent),
        Mode("echo", "Echo", "Keystroke echo",
             "Flashes the pressed key and grows a slow heat trail, optionally ringing "
             "outward from the hit. Needs Input Monitoring. Runs as a child process, "
             "so the board preview is unavailable.",
             [num("decay", "Fade s", 0.7, 0.1, 5, step=0.1),
              num("cool", "Heat cooldown s", 25, 2, 120, step=1),
              num("hue", "Accent hue", 190, 0, 360),
              choice("style", "Layers", "both",
                     [("both", "Flash + heat"), ("flash", "Flash only"),
                      ("heat", "Heat only"), ("ripple", "Flash + ripple"),
                      ("ripple_heat", "Flash + ripple + heat")]),
              num("ripple_speed", "Ring speed", 9, 1, 20, step=0.5),
              num("ripple_thickness", "Ring thickness", 1, 0.3, 3, step=0.1),
              num("ripple_decay", "Ring fade s", 1.4, 0.3, 6, step=0.1),
              percent("brightness", "Brightness", 1),
              fps(default=20)],
             build=echo_argv, kind="echo"),
        Mode("overheat", "Echo", "Critical overheat",
             "Black board; every press detonates an amber-to-red fireball, and fast "
             "typing heats the whole board into a red-orange overload glow. Needs Input "
             "Monitoring. Runs as a child process, so the board preview is unavailable.",
             [num("decay", "Fade s", 0.7, 0.1, 5, step=0.1),
              num("blast_speed", "Blast speed", 10, 2, 30, step=0.5),
              num("blast_thickness", "Blast thickness", 1, 0.3, 3, step=0.1),
              num("blast_decay", "Blast fade s", 1.2, 0.3, 6, step=0.1),
              num("ember", "Max ember glow", 0.35, 0, 1, step=0.05),
              num("ember_cool", "Overload cooldown s", 3, 0.5, 20, step=0.5),
              percent("brightness", "Brightness", 1),
              fps(default=20)],
             build=overheat_argv, kind="echo"),
        Mode("clock", "Info", "Clock",
             "HH:MM in a 3x5 font with a blinking colon and a seconds sweep "
             "on the function row, or binary hours/minutes/seconds rows.",
             [choice("style", "Style", "digits",
                     [("digits", "Digits"), ("binary", "Binary")]),
              choice("format", "Format", "24",
                     [("24", "24-hour"), ("12", "12-hour")]),
              choice("seconds", "Seconds", "on",
                     [("on", "Sweep"), ("off", "Off")]),
              num("hue", "Digit hue", 200, 0, 360),
              percent("brightness", "Brightness", 1),
              fps(default=2)],
             build_clock),
        Mode("sysmon", "Info", "System monitor",
             "Gauge rows: CPU load, memory, network rate and battery charge, "
             "green to red as they fill.",
             [percent("brightness", "Brightness", 1),
              num("refresh", "Sample every s", 2, 0.5, 30, step=0.5,
                  advanced=True),
              fps(default=4)],
             build_sysmon),
        Mode("break", "Health", "Break reminder",
             "Watches typing and fills the function row green -> red over the "
             "work limit; a quiet rest resets it with a green flash. Needs "
             "Input Monitoring. Runs as a child process, so the board preview "
             "is unavailable.",
             [num("limit", "Work minutes", 45, 0.2, 120, step=1),
              num("rest", "Rest minutes", 5, 0.2, 30, step=1),
              num("idle", "Pause gap s", 60, 5, 300, step=5, advanced=True),
              percent("brightness", "Brightness", 1),
              fps(default=5)],
             build=break_argv, kind="echo"),
    ]
    return {mode.key: mode for mode in modes}


MODES = define_modes()


# --- echo child process -----------------------------------------------------


class EchoProcess:
    def __init__(self):
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, values, mode):
        self.stop()
        self.proc = subprocess.Popen(mode.build(values), cwd=HERE)

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


# --- engine -----------------------------------------------------------------


class Engine:
    """Owns the LED channel and one renderer thread at a time."""

    def __init__(self):
        self.guard = threading.RLock()
        self.device_guard = threading.Lock()
        self.stop_event = threading.Event()
        self.generation = 0
        self.thread = None
        self.device = None
        self.colors = [BLACK] * KEY_COUNT
        self.frame = 0
        self.mode = None
        self.params = {}
        self.error = None
        self.started = None
        self.log = collections.deque(maxlen=40)
        self.echo = EchoProcess()
        self.push_deadline = None
        self._watchdog = None
        self._present = kbmod.find_led_device() is not None
        self._checked = time.monotonic()

    # -- logging --
    def say(self, message):
        print(message, flush=True)
        with self.guard:
            self.log.append(time.strftime("%H:%M:%S") + "  " + message)

    # -- device --
    def _ensure_device(self):
        with self.device_guard:
            if self.device is not None:
                return self.device
            device = kbmod.find_led_device()
            if device is None:
                raise RuntimeError("Bloody B820R not found (is it plugged in?)")
            keyboard = kbmod.BloodyB820R(device).open()
            keyboard.take_control()
            self.device = keyboard
            return keyboard

    def _release_device(self):
        with self.device_guard:
            keyboard, self.device = self.device, None
            if keyboard is None:
                return
            try:
                keyboard.release()
            except Exception as exc:  # noqa: BLE001 - the board matters more than the log
                self.say("release failed: %s" % exc)
            finally:
                try:
                    keyboard.close()
                except Exception:
                    pass

    # -- worker --
    def _loop(self, generation, runner, label):
        interval = 1.0 / runner.fps
        started = time.monotonic()
        next_tick = started
        reason = "stopped"
        try:
            while not self.stop_event.is_set():
                if generation != self.generation:
                    reason = "superseded"
                    break
                colors = runner.step(time.monotonic() - started)
                if generation != self.generation or self.stop_event.is_set():
                    break
                device = self.device
                if device is not None:
                    with self.device_guard:
                        device.frame(colors)
                with self.guard:
                    self.colors = colors
                    self.frame += 1
                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.monotonic()
        except Exception:
            with self.guard:
                self.error = traceback.format_exc()
            reason = "failed"
        finally:
            try:
                runner.close()
            except Exception as exc:  # noqa: BLE001
                self.say("cleanup failed: %s" % exc)
            current = generation == self.generation
            with self.guard:
                if self.thread is threading.current_thread():
                    self.thread = None
            if current:
                # Nobody else took over, so this exit owns the board: hand it back.
                self._release_device()
            self.say("%s thread %s" % (label, reason))

    # -- lifecycle --
    def stop_current(self):
        with self.guard:
            self.generation += 1
            self.stop_event.set()
            self.push_deadline = None
            thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=6.0)
            if thread.is_alive():
                self.say("previous mode did not stop in time; abandoning it")
        self._release_device()
        self.echo.stop()
        with self.guard:
            self.thread = None
            self.mode = None
            self.params = {}
            self.started = None

    def start(self, mode_key, values):
        mode = MODES.get(mode_key)
        if mode is None:
            raise ValueError("unknown mode: %s" % mode_key)
        values = mode.clean(values)

        runner = mode.build(values) if mode.kind == "frame" else None
        self.stop_current()

        if mode.kind == "echo":
            self.echo.start(values, mode)
            with self.guard:
                self.mode = mode.key
                self.params = values
                self.error = None
                self.started = time.time()
            self.say("echo started (child process)")
            return

        try:
            self._ensure_device()
        except Exception:
            runner.close()
            raise

        with self.guard:
            self.generation += 1
            generation = self.generation
            self.stop_event = threading.Event()
            self.mode = mode.key
            self.params = values
            self.error = None
            self.started = time.time()
            self.colors = [BLACK] * KEY_COUNT
            self.thread = threading.Thread(
                target=self._loop, args=(generation, runner, mode.label), daemon=True)
            thread = self.thread
        self.say("%s started at %.0f fps" % (mode.label, runner.fps))
        thread.start()

    def stop(self):
        self.stop_current()
        self.say("stopped, keyboard handed back")

    def off(self):
        self.stop_current()
        keyboard = self._ensure_device()
        with self.device_guard:
            keyboard.frame([BLACK] * KEY_COUNT)
        with self.guard:
            self.mode = "off"
            self.params = {}
            self.colors = [BLACK] * KEY_COUNT
            self.frame += 1
        self.say("LEDs off (Restore hands the board back)")

    def restore(self):
        self.stop_current()
        self._release_device()
        with self.guard:
            self.mode = None
            self.error = None

    # -- frame push --
    def push(self, colors, ttl):
        """Write one externally-supplied frame, replacing whatever runs.

        With ttl > 0 a watchdog hands the board back to the firmware if no
        new frame arrives in time, so a dead producer cannot freeze the LEDs.
        """
        with self.guard:
            first = self.mode != "push"
        if first:
            self.stop_current()
        keyboard = self._ensure_device()
        with self.device_guard:
            keyboard.frame(colors)
        deadline = time.monotonic() + ttl if ttl and ttl > 0 else None
        with self.guard:
            self.mode = "push"
            self.params = {}
            self.error = None
            self.started = time.time()
            self.colors = colors
            self.frame += 1
            self.push_deadline = deadline
        if deadline is not None:
            self._start_watchdog()
        if first:
            # A streaming producer pushes many times a second; log only the
            # frame that took over the board.
            self.say("push frame%s" % (" (ttl %.1fs)" % ttl if deadline else ""))

    def _start_watchdog(self):
        with self.guard:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog = threading.Thread(target=self._watch_push,
                                              daemon=True)
            self._watchdog.start()

    def _watch_push(self):
        while True:
            with self.guard:
                deadline = self.push_deadline if self.mode == "push" else None
            if deadline is None:
                return
            if time.monotonic() >= deadline:
                self.restore()
                self.say("push ttl expired; keyboard handed back")
                return
            time.sleep(0.25)

    def reconfigure(self, mode_key, values):
        with self.guard:
            current = self.mode
            running = self.thread is not None or self.echo.running()
        if running and mode_key == current:
            self.start(mode_key, values)

    # -- reporting --
    def snapshot(self):
        now = time.monotonic()
        with self.guard:
            running = self.thread is not None and self.thread.is_alive()
            echo = self.echo.running()
            if now - self._checked > 3.0 or not self._present:
                self._present = kbmod.find_led_device() is not None
                self._checked = now
            return {
                "device": self._present,
                "permission": kbmod.permission_state(),
                "mode": self.mode,
                "params": self.params,
                "running": running or echo,
                "echo": echo,
                "error": self.error,
                "frame": self.frame,
                "colors": [channel for rgb in self.colors for channel in rgb],
                "log": list(self.log),
                "uptime": round(time.time() - self.started, 1) if running and self.started else 0,
            }


# --- http -------------------------------------------------------------------


def parse_colors(raw):
    """Accept 104 [r, g, b] triples or a flat list of 312 channel values."""
    if isinstance(raw, list) and len(raw) == KEY_COUNT and all(
            isinstance(item, (list, tuple)) and len(item) == 3 for item in raw):
        flat = [channel for item in raw for channel in item]
    else:
        flat = raw
    if not isinstance(flat, list) or len(flat) != KEY_COUNT * 3:
        raise ValueError("colors must be 104 RGB triples (312 channel values)")
    colors = []
    for i in range(0, len(flat), 3):
        try:
            colors.append(tuple(max(0, min(255, int(channel)))
                                for channel in flat[i:i + 3]))
        except (TypeError, ValueError):
            raise ValueError("color channels must be numbers")
    return colors


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    engine = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "ui.html"), "rb") as handle:
                    body = handle.read()
            except OSError:
                self._send(500, "ui.html is missing next to ui.py", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._json(200, {
                "state": self.engine.snapshot(),
                "modes": [mode.describe() for mode in MODES.values()],
                "board": BOARD,
            })
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._json(400, {"error": "bad json"})
            return
        mode = payload.get("mode") or ""
        params = payload.get("params") or {}
        try:
            if path == "/api/start":
                self.engine.start(mode, params)
            elif path == "/api/stop":
                self.engine.stop()
            elif path == "/api/off":
                self.engine.off()
            elif path == "/api/restore":
                self.engine.restore()
            elif path == "/api/params":
                self.engine.reconfigure(mode, params)
            elif path == "/api/frame":
                colors = parse_colors(payload.get("colors"))
                ttl = float(payload.get("ttl") or 0)
                self.engine.push(colors, ttl)
            else:
                self._json(404, {"error": "not found"})
                return
        except SystemExit:
            self._json(400, {"error": "those parameters were rejected by the renderer"})
            return
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"state": self.engine.snapshot()})


# --- cli --------------------------------------------------------------------


def acquire_lock():
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("another ui.py already owns the keyboard (%s)" % LOCK_PATH)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Local web control panel for the B820R scripts.")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the panel in a browser")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        lock = acquire_lock()  # noqa: F841 - held for the lifetime of the process
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    engine = Engine()

    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    atexit.register(engine.restore)

    Handler.engine = engine
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    url = "http://%s:%d/" % (args.host, args.port)
    device = "found" if kbmod.find_led_device() else "not found"
    print("B820R control panel: %s  (Ctrl-C to stop)" % url)
    print("keyboard: %s | Input Monitoring: %s" % (device, kbmod.permission_state()))
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    finally:
        engine.restore()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
