#!/usr/bin/env python3
"""
keyboard.py - random per-key RGB for the A4Tech Bloody B820R on macOS.

Talks to the keyboard's vendor HID feature report (report ID 7) through IOKit,
so there is nothing to install. Requires "Input Monitoring" permission for the
terminal app that runs it (System Settings > Privacy & Security > Input Monitoring).

Usage:
    python3 keyboard.py                  # twinkle forever
    python3 keyboard.py --duration 20    # ... for 20 seconds
    python3 keyboard.py --hold           # keys pile up until the board is full
    python3 keyboard.py --keys 20 --fps 15
    python3 keyboard.py --list           # show detected device and permission
    python3 keyboard.py --off            # blank the LEDs and exit
    python3 keyboard.py --restore        # hand the keyboard back to its firmware
"""

import argparse
import collections
import colorsys
import ctypes as C
import random
import signal
import sys
import threading
import time

# --- device / protocol constants -------------------------------------------

VID = 0x09DA
PID = 0xFA10

KEY_COUNT = 104
KEYS_PER_HALF = 58          # keys that fit in one 64 byte report
REPORT_ID = 0x07
PACKET_SIZE = 64            # report ID byte + 63 payload bytes
COLOR_OFFSET = 6            # first colour byte inside the packet

# Usage Page (0xFF52) from the vendor collection in the keyboard's report
# descriptor; this is what identifies the LED channel among the three HID
# interfaces the keyboard exposes.
VENDOR_MARKER = b"\x06\x52\xff"

# The 104 LED positions in protocol order (row-major over the 6x21 key matrix).
KEY_NAMES = [
    "Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    "PrtSc", "ScrLk", "Pause",
    "`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=", "Backspace",
    "Ins", "Home", "PgUp", "NumLk", "Num/", "Num*", "Num-",
    "Tab", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "[", "]", "\\",
    "Del", "End", "PgDn", "Num7", "Num8", "Num9", "Num+",
    "Caps", "A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "'", "Enter",
    "Num4", "Num5", "Num6",
    "LShift", "Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "RShift",
    "Up", "Num1", "Num2", "Num3", "NumEnter",
    "LCtrl", "LWin", "LAlt", "Space", "RAlt", "Fn", "Menu", "RCtrl",
    "Left", "Down", "Right", "Num0", "Num.",
]

assert len(KEY_NAMES) == KEY_COUNT

# HID usage IDs (Keyboard/Keypad page 0x07) to the names above. The board
# reports through the standard boot keyboard collection; Fn is handled in
# firmware and never appears here.
HID_USAGE_TO_KEY = {
    0x04: "A", 0x05: "B", 0x06: "C", 0x07: "D", 0x08: "E", 0x09: "F",
    0x0A: "G", 0x0B: "H", 0x0C: "I", 0x0D: "J", 0x0E: "K", 0x0F: "L",
    0x10: "M", 0x11: "N", 0x12: "O", 0x13: "P", 0x14: "Q", 0x15: "R",
    0x16: "S", 0x17: "T", 0x18: "U", 0x19: "V", 0x1A: "W", 0x1B: "X",
    0x1C: "Y", 0x1D: "Z",
    0x1E: "1", 0x1F: "2", 0x20: "3", 0x21: "4", 0x22: "5", 0x23: "6",
    0x24: "7", 0x25: "8", 0x26: "9", 0x27: "0",
    0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab",
    0x2C: "Space", 0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\",
    0x33: ";", 0x34: "'", 0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/",
    0x39: "Caps",
    0x46: "PrtSc", 0x47: "ScrLk", 0x48: "Pause", 0x49: "Ins", 0x4A: "Home",
    0x4B: "PgUp", 0x4C: "Del", 0x4D: "End", 0x4E: "PgDn",
    0x4F: "Right", 0x50: "Left", 0x51: "Down", 0x52: "Up",
    0x53: "NumLk", 0x54: "Num/", 0x55: "Num*", 0x56: "Num-", 0x57: "Num+",
    0x58: "NumEnter", 0x62: "Num0", 0x63: "Num.",
    0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LWin",
    0xE4: "RCtrl", 0xE5: "RShift", 0xE6: "RAlt", 0xE7: "RWin",
}
HID_USAGE_TO_KEY.update({0x3A + i: "F%d" % (i + 1) for i in range(12)})
HID_USAGE_TO_KEY.update({0x59 + i: "Num%d" % (i + 1) for i in range(9)})

KEY_INDEX = {name: index for index, name in enumerate(KEY_NAMES)}
HID_KEY_INDEX = {
    usage: KEY_INDEX[name]
    for usage, name in HID_USAGE_TO_KEY.items()
    if name in KEY_INDEX
}

# Physical layout of the board: 6 rows x 21 columns of key indices, with -1
# where the matrix has a hole (no key behind that cell).
MATRIX = [
    [0,  -1, 1,  2,  3,  4,  5,  6,  7,  8,  9,  10, 11, 12, -1, -1, -1, -1, 13, 14, 15],
    [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36],
    [37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57],
    [58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, -1, -1, -1, -1, 71, 72, 73, -1],
    [74, -1, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, -1, -1, 86, -1, 87, 88, 89, 90],
    [91, 92, 93, 94, -1, -1, -1, -1, -1, -1, 95, 96, 97, 98, 99, 100, 101, 102, -1, 103, -1],
]
ROWS = len(MATRIX)
COLS = len(MATRIX[0])

# --- IOKit / CoreFoundation bindings ---------------------------------------

_iokit = C.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _bind(lib, name, restype, argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_CFStringCreateWithCString = _bind(
    _cf, "CFStringCreateWithCString", C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint32]
)
_CFStringEncodingUTF8 = 0x08000100

_CFSetGetCount = _bind(_cf, "CFSetGetCount", C.c_long, [C.c_void_p])
_CFSetGetValues = _bind(_cf, "CFSetGetValues", None, [C.c_void_p, C.POINTER(C.c_void_p)])
_CFGetTypeID = _bind(_cf, "CFGetTypeID", C.c_ulong, [C.c_void_p])
_CFNumberGetTypeID = _bind(_cf, "CFNumberGetTypeID", C.c_ulong, [])
_CFNumberGetValue = _bind(_cf, "CFNumberGetValue", C.c_bool, [C.c_void_p, C.c_long, C.c_void_p])
_CFStringGetTypeID = _bind(_cf, "CFStringGetTypeID", C.c_ulong, [])
_CFStringGetCString = _bind(_cf, "CFStringGetCString", C.c_bool, [C.c_void_p, C.c_char_p, C.c_long, C.c_uint32])
_CFDataGetTypeID = _bind(_cf, "CFDataGetTypeID", C.c_ulong, [])
_CFDataGetLength = _bind(_cf, "CFDataGetLength", C.c_long, [C.c_void_p])
_CFDataGetBytePtr = _bind(_cf, "CFDataGetBytePtr", C.POINTER(C.c_ubyte), [C.c_void_p])

_IOHIDManagerCreate = _bind(_iokit, "IOHIDManagerCreate", C.c_void_p, [C.c_void_p, C.c_uint32])
_IOHIDManagerSetDeviceMatching = _bind(_iokit, "IOHIDManagerSetDeviceMatching", None, [C.c_void_p, C.c_void_p])
_IOHIDManagerCopyDevices = _bind(_iokit, "IOHIDManagerCopyDevices", C.c_void_p, [C.c_void_p])
_IOHIDDeviceGetProperty = _bind(_iokit, "IOHIDDeviceGetProperty", C.c_void_p, [C.c_void_p, C.c_void_p])
_IOHIDDeviceOpen = _bind(_iokit, "IOHIDDeviceOpen", C.c_int32, [C.c_void_p, C.c_uint32])
_IOHIDDeviceClose = _bind(_iokit, "IOHIDDeviceClose", C.c_int32, [C.c_void_p, C.c_uint32])
_IOHIDDeviceSetReport = _bind(
    _iokit, "IOHIDDeviceSetReport", C.c_int32,
    [C.c_void_p, C.c_uint32, C.c_long, C.POINTER(C.c_ubyte), C.c_long],
)
_IOHIDCheckAccess = _bind(_iokit, "IOHIDCheckAccess", C.c_uint32, [C.c_uint32])

_INPUT_VALUE_CALLBACK = C.CFUNCTYPE(None, C.c_void_p, C.c_int32, C.c_void_p, C.c_void_p)
_IOHIDDeviceRegisterInputValueCallback = _bind(
    _iokit, "IOHIDDeviceRegisterInputValueCallback", None,
    [C.c_void_p, _INPUT_VALUE_CALLBACK, C.c_void_p],
)
_IOHIDDeviceScheduleWithRunLoop = _bind(
    _iokit, "IOHIDDeviceScheduleWithRunLoop", None,
    [C.c_void_p, C.c_void_p, C.c_void_p],
)
_IOHIDValueGetElement = _bind(_iokit, "IOHIDValueGetElement", C.c_void_p, [C.c_void_p])
_IOHIDElementGetUsagePage = _bind(_iokit, "IOHIDElementGetUsagePage", C.c_uint32, [C.c_void_p])
_IOHIDElementGetUsage = _bind(_iokit, "IOHIDElementGetUsage", C.c_uint32, [C.c_void_p])
_IOHIDValueGetIntegerValue = _bind(_iokit, "IOHIDValueGetIntegerValue", C.c_long, [C.c_void_p])
_CFRunLoopGetCurrent = _bind(_cf, "CFRunLoopGetCurrent", C.c_void_p, [])
_CFRunLoopRunInMode = _bind(
    _cf, "CFRunLoopRunInMode", C.c_int32, [C.c_void_p, C.c_double, C.c_bool]
)

_KIO_HID_REPORT_TYPE_FEATURE = 2
_KIO_HID_REQUEST_TYPE_LISTEN_EVENT = 1
_KIO_RETURN_NOT_PERMITTED = 0xE00002E2
# transient failures the keyboard reports when reports arrive faster than it
# can digest them; worth a retry instead of aborting the effect
_TRANSIENT_ERRORS = {0xE00002D6, 0xE00002D8, 0xE00002ED}


def _cfstr(text):
    return _CFStringCreateWithCString(None, text.encode("utf-8"), _CFStringEncodingUTF8)


_DEFAULT_RUN_LOOP_MODE = _cfstr("kCFRunLoopDefaultMode")


def _int_prop(dev, key):
    value = _IOHIDDeviceGetProperty(dev, _cfstr(key))
    if not value:
        return None
    if _CFGetTypeID(value) != _CFNumberGetTypeID():
        return None
    out = C.c_int32()
    _CFNumberGetValue(value, 3, C.byref(out))  # 3 = kCFNumberSInt32Type
    return out.value


def _str_prop(dev, key):
    value = _IOHIDDeviceGetProperty(dev, _cfstr(key))
    if not value or _CFGetTypeID(value) != _CFStringGetTypeID():
        return None
    buf = C.create_string_buffer(256)
    if not _CFStringGetCString(value, buf, 256, _CFStringEncodingUTF8):
        return None
    return buf.value.decode("utf-8", "replace")


def _data_prop(dev, key):
    value = _IOHIDDeviceGetProperty(dev, _cfstr(key))
    if not value or _CFGetTypeID(value) != _CFDataGetTypeID():
        return b""
    size = _CFDataGetLength(value)
    ptr = _CFDataGetBytePtr(value)
    return bytes(bytearray(ptr[i] for i in range(size)))


def enumerate_hid_devices():
    manager = _IOHIDManagerCreate(None, 0)
    _IOHIDManagerSetDeviceMatching(manager, None)
    device_set = _IOHIDManagerCopyDevices(manager)
    if not device_set:
        return []
    count = _CFSetGetCount(device_set)
    array = (C.c_void_p * count)()
    _CFSetGetValues(device_set, array)
    return [array[i] for i in range(count)]


def find_led_device():
    """Return the HID device that carries the B820R LED feature report."""
    for dev in enumerate_hid_devices():
        if _int_prop(dev, "VendorID") != VID or _int_prop(dev, "ProductID") != PID:
            continue
        if VENDOR_MARKER in _data_prop(dev, "ReportDescriptor"):
            return dev
    return None


def find_keyboard_device():
    """Return the HID device that reports key presses (boot keyboard usage).

    The board exposes three interfaces with the same VID/PID: the LED channel
    (vendor usage page, 64-byte feature reports), a mouse collection, and the
    actual keyboard. The LED channel also advertises usage page 1/usage 6, so
    the vendor marker is what distinguishes it.
    """
    for dev in enumerate_hid_devices():
        if _int_prop(dev, "VendorID") != VID or _int_prop(dev, "ProductID") != PID:
            continue
        if _int_prop(dev, "PrimaryUsagePage") != 0x01:
            continue
        if _int_prop(dev, "PrimaryUsage") != 0x06:
            continue
        if VENDOR_MARKER in _data_prop(dev, "ReportDescriptor"):
            continue
        return dev
    return None


class KeyListener:
    """Reads key presses from the keyboard's HID interface.

    The device is opened non-seized, so the OS still receives every keystroke
    and the board keeps typing normally; we only observe. The run loop lives on
    a daemon thread and pushes protocol key indices into a queue.
    """

    def __init__(self, dev):
        self.dev = dev
        self.events = collections.deque()
        self.down = set()
        self.opened = False
        self._stopping = False
        self._ready = threading.Event()
        self._thread = None
        self._callback = None

    def open(self):
        rc = _IOHIDDeviceOpen(self.dev, 0)
        if rc != 0:
            if (rc & 0xFFFFFFFF) == _KIO_RETURN_NOT_PERMITTED:
                raise PermissionError(
                    "macOS refused key reading (kIOReturnNotPermitted).\n"
                    "Open System Settings > Privacy & Security > Input Monitoring and enable\n"
                    "the terminal app you are running this from, then restart that app."
                )
            raise OSError("IOHIDDeviceOpen failed: 0x%08X" % (rc & 0xFFFFFFFF))
        self.opened = True
        self._callback = _INPUT_VALUE_CALLBACK(self._handle)
        _IOHIDDeviceRegisterInputValueCallback(self.dev, self._callback, None)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self

    def _handle(self, _context, _result, _sender, value):
        if not value:
            return
        element = _IOHIDValueGetElement(value)
        if not element or _IOHIDElementGetUsagePage(element) != 0x07:
            return
        index = HID_KEY_INDEX.get(_IOHIDElementGetUsage(element))
        if index is None:
            return
        if _IOHIDValueGetIntegerValue(value):
            if index in self.down:
                return  # auto-repeat: the key never went up
            self.down.add(index)
            self.events.append(index)
        else:
            self.down.discard(index)

    def _run(self):
        loop = _CFRunLoopGetCurrent()
        _IOHIDDeviceScheduleWithRunLoop(self.dev, loop, _DEFAULT_RUN_LOOP_MODE)
        self._ready.set()
        while not self._stopping:
            _CFRunLoopRunInMode(_DEFAULT_RUN_LOOP_MODE, 0.1, False)

    def poll(self):
        """Return the key indices pressed since the last call."""
        out = []
        while self.events:
            out.append(self.events.popleft())
        return out

    def close(self):
        self._stopping = True
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        if self.opened:
            _IOHIDDeviceClose(self.dev, 0)
            self.opened = False


# --- protocol ---------------------------------------------------------------


class BloodyB820R:
    """Take-control / per-key-frame / release on the B820R LED channel."""

    def __init__(self, dev):
        self.dev = dev
        self.opened = False
        self.controlling = False

    def open(self):
        rc = _IOHIDDeviceOpen(self.dev, 0)
        if rc != 0:
            if (rc & 0xFFFFFFFF) == _KIO_RETURN_NOT_PERMITTED:
                raise PermissionError(
                    "macOS refused access to the keyboard (kIOReturnNotPermitted).\n"
                    "Open System Settings > Privacy & Security > Input Monitoring and enable\n"
                    "the terminal app you are running this from, then restart that app."
                )
            raise OSError("IOHIDDeviceOpen failed: 0x%08X" % (rc & 0xFFFFFFFF))
        self.opened = True
        return self

    def close(self):
        if self.opened:
            _IOHIDDeviceClose(self.dev, 0)
            self.opened = False

    def _send(self, packet):
        """Send one 64 byte feature report.

        The packet starts with the report ID and is passed to IOKit whole:
        that is exactly how hidapi frames reports on macOS, and passing the
        ID separately shifts every payload byte by one.
        """
        if len(packet) != PACKET_SIZE:
            raise ValueError("packet must be %d bytes, got %d" % (PACKET_SIZE, len(packet)))
        buf = (C.c_ubyte * PACKET_SIZE)(*packet)
        last = 0
        for attempt in range(4):
            last = _IOHIDDeviceSetReport(
                self.dev, _KIO_HID_REPORT_TYPE_FEATURE, REPORT_ID, buf, PACKET_SIZE
            ) & 0xFFFFFFFF
            if last == 0:
                return
            if last in _TRANSIENT_ERRORS:
                time.sleep(0.01 * (attempt + 1))
                continue
            break
        raise OSError("IOHIDDeviceSetReport failed: 0x%08X" % last)

    def control(self, data_byte):
        """Magic pair that switches the keyboard in/out of software LED mode."""
        packet = [REPORT_ID, 0x03, 0x06, 0x01, 0x00, 0x00, 0x00, 0x00] + [0] * 56
        self._send(packet)
        packet[3] = 0x00
        packet[8] = data_byte
        self._send(packet)

    def take_control(self):
        self.control(0x01)
        self.controlling = True
        time.sleep(0.05)  # let the firmware settle into direct mode

    def release(self):
        if not self.opened:
            return
        if self.controlling:
            self.control(0x00)
            self.controlling = False

    def frame(self, colors):
        """Push a full 104 key frame; colors is a list of (r, g, b) tuples."""
        for report in range(6):
            selector = REPORT_ID + report
            channel = report // 2          # 0 = red, 1 = green, 2 = blue
            half = report % 2              # 0 = keys 0..57, 1 = keys 58..103
            packet = [REPORT_ID, 0x03, 0x06, selector, 0x00, 0x00] + [0] * KEYS_PER_HALF
            for slot in range(KEYS_PER_HALF):
                key = half * KEYS_PER_HALF + slot
                if key >= KEY_COUNT:
                    break
                packet[COLOR_OFFSET + slot] = colors[key][channel]
            self._send(packet)
            time.sleep(0.003)  # the firmware NAKs if reports arrive back to back


# --- effects ----------------------------------------------------------------


def random_color(rng):
    hue = rng.random()
    value = rng.uniform(0.55, 1.0)
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, value)
    return int(r * 255), int(g * 255), int(b * 255)


class Twinkle:
    """A fresh random handful of keys every tick, everything else dark."""

    def __init__(self, rng, count):
        self.rng = rng
        self.count = max(1, min(count, KEY_COUNT))

    def update(self):
        colors = [(0, 0, 0)] * KEY_COUNT
        lit = self.rng.sample(range(KEY_COUNT), self.count)
        for key in lit:
            colors[key] = random_color(self.rng)
        return colors, lit


class Accumulate:
    """Keys light up randomly and stay lit until the whole board is covered."""

    def __init__(self, rng, count):
        self.rng = rng
        self.count = max(1, min(count, KEY_COUNT))
        self.lit = {}

    def update(self):
        if len(self.lit) >= KEY_COUNT:
            time.sleep(0.7)
            self.lit.clear()
        free = [k for k in range(KEY_COUNT) if k not in self.lit]
        newly = self.rng.sample(free, min(self.count, len(free)))
        for key in newly:
            self.lit[key] = random_color(self.rng)
        colors = [(0, 0, 0)] * KEY_COUNT
        for key, color in self.lit.items():
            colors[key] = color
        return colors, newly


# --- commands ---------------------------------------------------------------


def permission_state():
    return {
        0: "granted",
        1: "denied",
        2: "unknown",
    }.get(_IOHIDCheckAccess(_KIO_HID_REQUEST_TYPE_LISTEN_EVENT), "unknown")


def cmd_list():
    dev = find_led_device()
    print("Input Monitoring for this process: %s" % permission_state())
    if dev is None:
        print("Bloody B820R (VID 0x%04X / PID 0x%04X) not found." % (VID, PID))
        return 1
    print("Device:    %s" % (_str_prop(dev, "Product") or "?").strip())
    print("VID/PID:   0x%04X / 0x%04X" % (_int_prop(dev, "VendorID"), _int_prop(dev, "ProductID")))
    print("Transport: %s" % (_str_prop(dev, "Transport") or "?"))
    print("LED channel: feature report %d, %d byte packets (ID + 63 payload)" % (REPORT_ID, PACKET_SIZE))
    try:
        BloodyB820R(dev).open().close()
    except OSError as exc:
        print("Open failed: %s" % exc)
        return 1
    print("Open:      ok")
    return 0


def cmd_off():
    dev = find_led_device()
    if dev is None:
        print("Keyboard not found.")
        return 1
    kb = BloodyB820R(dev).open()
    try:
        kb.take_control()
        kb.frame([(0, 0, 0)] * KEY_COUNT)
        kb.controlling = False  # leave it in direct mode, LEDs stay off
        print("LEDs off. Run with --restore to bring the built-in effects back.")
    finally:
        kb.close()
    return 0


def cmd_restore():
    dev = find_led_device()
    if dev is None:
        print("Keyboard not found.")
        return 1
    kb = BloodyB820R(dev).open()
    try:
        kb.control(0x00)
        print("Keyboard handed back to its firmware effects.")
    finally:
        kb.close()
    return 0


def cmd_effect(args):
    dev = find_led_device()
    if dev is None:
        print("Bloody B820R (VID 0x%04X / PID 0x%04X) not found." % (VID, PID))
        return 1

    kb = BloodyB820R(dev).open()
    rng = random.Random(args.seed)
    effect = Accumulate(rng, args.keys) if args.hold else Twinkle(rng, args.keys)

    stop = {"requested": False}

    def handler(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    interval = 1.0 / args.fps
    started = time.monotonic()
    next_tick = started

    print("Random keys on. Ctrl-C to stop and restore the built-in lighting.")
    try:
        kb.take_control()
        while not stop["requested"]:
            colors, lit = effect.update()
            kb.frame(colors)
            if args.verbose:
                print("lit: " + ", ".join(KEY_NAMES[k] for k in lit))
            if args.duration and time.monotonic() - started >= args.duration:
                break
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    finally:
        kb.release()
        time.sleep(0.05)
        kb.close()
        print("Released, keyboard is back on its built-in effect.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Random per-key colours on a Bloody B820R keyboard (macOS)."
    )
    parser.add_argument("--list", action="store_true", help="show detected device and permission state")
    parser.add_argument("--off", action="store_true", help="blank all LEDs and exit")
    parser.add_argument("--restore", action="store_true", help="hand the keyboard back to its firmware")
    parser.add_argument("--fps", type=float, default=8.0, help="frames per second (default 8)")
    parser.add_argument("--keys", type=int, default=12, help="keys lit per tick (default 12)")
    parser.add_argument("--duration", type=float, default=0.0, help="run for N seconds (default: until Ctrl-C)")
    parser.add_argument("--hold", action="store_true", help="accumulate lit keys until the board is full")
    parser.add_argument("--seed", type=int, default=None, help="random seed, for a repeatable show")
    parser.add_argument("--verbose", action="store_true", help="print which keys are lit")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.keys <= 0:
        parser.error("--keys must be positive")

    try:
        if args.list:
            return cmd_list()
        if args.off:
            return cmd_off()
        if args.restore:
            return cmd_restore()
        return cmd_effect(args)
    except OSError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
