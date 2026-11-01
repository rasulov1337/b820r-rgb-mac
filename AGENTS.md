# AGENTS.md

## Project

`keyboard.py` — random per-key RGB lighting for an A4Tech **Bloody B820R**
keyboard on macOS. Single file, Python 3 stdlib only (talks to IOKit via
`ctypes`); no pip/npm installs.

## Hardware

- Enumerates as VID `0x09DA` / PID `0xFA10`, product string "USB Device".
- The LED channel is a vendor HID collection (`usage page 0xFF52`,
  `usage 0x0210`) with **feature report ID 7, 63 payload bytes**.
- Find it by checking that the device's `ReportDescriptor` property contains
  `06 52 FF` — the keyboard exposes several HID interfaces and two keyboard
  collections, so VID/PID plus usage page alone is ambiguous.
- 104 keys. The protocol index order is the row-major order of the 6x21 key
  matrix; `KEY_NAMES` in `keyboard.py` lists it (Esc, F1-F12, Print/Scroll/Pause,
  numrow, nav cluster, numpad, three letter rows, modifiers, arrows).

## Protocol

- Take control: `07 03 06 01 00 00 00 00` then
  `07 03 06 00 00 00 00 00 01`.
- Per-key frame: six reports, selectors `0x07..0x0C` =
  R1, R2, G1, G2, B1, B2. Colours start at packet byte 6, 58 keys per report,
  104 keys total.
- Release (restore firmware effects): same control pair with data byte `0x00`.

## Critical macOS gotchas

- `IOHIDDeviceSetReport` must receive the **whole 64-byte buffer with the
  report ID as byte 0** — IOKit strips it. Passing 63 bytes and the ID
  separately silently shifts every payload byte by one: calls return success,
  reads echo the data back, but the firmware ignores it. (This is how hidapi
  frames reports on macOS; Windows/Linux differ.)
- Opening the device needs **Input Monitoring** for the hosting terminal app;
  otherwise `IOHIDDeviceOpen` returns `0xE00002E2` (`kIOReturnNotPermitted`).
- Space feature reports ~3 ms apart; the firmware returns transient
  `kIOReturnNotReady` / `kIOReturnNotResponding` when hammered.
- While the script owns the board it stays in direct mode, so Fn+F12 does
  nothing — `--restore` (or a replug) hands it back.
- Verify framing with `IOHIDDeviceGetReport(feature, 7)`: a correct write
  echoes `07 03 06 <selector> 00 00 ...`.

## Music visualiser (`music.py` + `audiotap`)

- `audiotap.swift` captures the system audio mix through ScreenCaptureKit and
  writes raw 32-bit float mono PCM to stdout. Build once:
  `xcrun swiftc -O -o audiotap audiotap.swift -framework ScreenCaptureKit -framework CoreMedia -framework AVFoundation`
  (`music.py` rebuilds it automatically when the source is newer).
- `music.py` reads that PCM, runs FFT into 21 log-spaced bands (numpy),
  auto-gains and smooths them, and renders one of three styles: `bars`
  (spectrum columns over the real 6x21 key matrix), `pulse` (whole board
  breathing with the bass), `beat` (random keys ignite on detected beats).
- Sources: system audio by default, `--mic` (ffmpeg avfoundation) or `--test`
  (synthetic loop, no permissions).
- System audio needs **Screen & System Audio Recording** permission for the
  hosting app; after granting it the app usually has to be restarted. The mic
  path needs Microphone permission and only hears music played through
  speakers — check the device index with
  `ffmpeg -f avfoundation -list_devices true -i ""` (`--device N`).
- Two ScreenCaptureKit traps that produce a *silent* stream: SCStream does not
  retain its `SCStreamOutput` objects (keep strong references), and
  `excludesCurrentProcessAudio` must stay **false** — macOS treats every
  process launched from the same terminal as one app, so excluding "our"
  audio also drops the music players.
- Frame budget: a frame is 6 feature reports with 3 ms spacing, so keep
  `--fps` at or below ~25.
- The `MATRIX` / `ROWS` / `COLS` 6x21 board layout lives in `keyboard.py` and
  is imported by `music.py`.

## Ambient looks (`ambient.py`)

- Ten modes, all using the shared `keyboard.BloodyB820R` loop at ~18 fps:
  `aurora` (drifting colour field), `weather` (rain/snow/fog/clouds/clear/
  night/storm), `bias` (screen-colour wash), `scanner` (Knight Rider bounce
  along one row), `radar` (rotating sweep + contacts), `stars` (parallax
  twinkle + shooting stars), `ripple` (expanding rings from random drops),
  `sunset` (magenta-to-orange gradient), `fire` (Doom fire), `plasma`
  (interference field or lava blobs).
- `weather` queries Open-Meteo (no API key) in a background thread, so a slow
  or offline network never stalls the LEDs; while it has no data it shows a
  neutral gradient. `--lat/--lon` default to Moscow, `--refresh` to 900 s.
  `--condition rain|snow|fog|clear|night|cloudy|storm --temp N` forces a look
  and works fully offline.
- `bias` grabs the screen with `screencapture -x -t bmp` and averages the
  pixels (BMP parsing handles uncompressed 24/32-bit and 32-bit bitfields),
  smoothing toward the colour so it breathes instead of strobing. Needs
  **Screen & System Audio Recording**. `--image PATH` samples any image
  (converted with `sips`) and `--color RRGGBB` is a fixed fallback.
- `scanner` takes `--row 0..5`, `--speed`, `--trail`; `radar` takes
  `--period`; `aurora` takes `--hue`/`--spread`; all accept `--brightness`.
- `ripple` drops keys at random cells every `--interval` seconds (default 1.6,
  jittered) and grows a ring at `--speed` cells/s (default 7). The ring is a
  Gaussian band of `--thickness` cells (default 1.1) around an exact Euclidean
  distance over the grid, fading with `--decay` (default 2.2 s); each drop gets
  a slightly different hue around `--hue` (default 200). Cells with no key
  never light up.
- `sunset` is one horizontal gradient across the columns: warm magenta
  `#D24A8C` on the left, golden orange `#F7B733` on the right, rows identical
  and holes untouched. The interpolation runs in HSV with the hue going
  `331° -> 400°`, straight through pure red, because an RGB lerp sags through
  a muddy mauve in the middle; value climbs left to right so the golden end is
  brighter. Endpoints are `ambient.SUNSET_LEFT`/`SUNSET_RIGHT`. `--drift 0..1`
  (default 0) makes the colours slide slowly along the columns, and a still
  gradient defaults to 2 fps because it repaints the same frame.
- `fire` is the classic Doom fire: the bottom row is stoked with heat and
  each row above keeps a randomly cooled share of what rises into it, so
  flames climb the 6x21 grid (holes included in the sim, never lit) from a
  white-hot base through orange to deep red tips. `--intensity` sets the
  bottom-row heat, `--cooling` the max heat lost per row step (default 0.12 —
  small, because six rows burn out fast), `--wind` biases the drift -1..1.
- `plasma` sums four sines over (row, col, t) into an interference field
  whose value drives brightness and a hue sweep around `--hue`
  (`--spread` degrees, `--scale` the spatial frequency, `--speed` rad/s,
  default 0.6). `--style lava` swaps the field for three metaballs on
  lissajous paths whose summed 1/d² field thresholds into blobs; the default
  hue then becomes 15° (lava red) instead of 280°.
- The screen is 6x21 with holes, so `radar` centres on the grid and the
  scanner skips columns without a key.

## Clock (`clock.py`)

- `digits` renders HH:MM in the shared 3x5 bitmap font (`font.py`) across
  rows 1-5 (17 of 21 columns), colon blinking at 1 Hz; `--seconds on`
  (default) adds a 60-second sweep on the function row. `--style binary`
  shows hours/minutes/seconds as three 6-bit rows (MSB left,
  green/amber/cyan), off bits faintly lit so the positions read.
- `--format 12` for 12-hour time, `--hue` for the digit colour
  (default 200).
- `--fps` defaults to 2: the display changes once a minute, and 2 fps covers
  the colon blink.

## System monitor (`sysmon.py`)

- Four gauge rows: number row = CPU (`os.getloadavg` per core), QWERTY row =
  memory (`vm_stat` active+wired+compressor over `hw.memsize`), A row =
  network rate (`netstat -ibn` byte deltas over physical interfaces, log
  scale, 10 MB/s full), Z row = battery (`pmset -g batt`; dark on desktops,
  red breathing under 20 %, green while charging).
- Gauges fill left to right and walk green -> amber -> red as they fill.
  The subprocess samplers run in `ambient.Refresher` threads (`--refresh`,
  default 2 s; battery every 15+ s), so a slow command never stalls the
  LEDs. `netstat` needs `-n`: without it reverse DNS can block for seconds.
- `--fps` defaults to 4.

## Break reminder (`breaktime.py`)

- Watches keystrokes with `keyboard.KeyListener` like echo.py (non-seized,
  needs Input Monitoring, own CFRunLoop -> the panel runs it as a supervised
  child process with no live preview). A press within `--idle` seconds
  (default 60) of the last one counts as typing and advances the work clock
  toward `--limit` minutes (default 45); `--break` quiet minutes (default 5)
  reset it.
- The function row is the strain gauge, green -> amber -> red, over a dim
  board-wide glow that rises with strain; at the limit the whole board
  breathes red until the rest happens, which is answered with a green flash.
- Small `--limit`/`--break` values (0.2 minutes) make it demoable.

## Agent spinner (`agent.py`)

- A loading comet on the numpad ring: NumLk, Num/, Num*, Num-, Num+,
  NumEnter, Num., Num0, Num1, Num4, Num7. The matrix holes under the tall
  +/-/Enter keys and the wide 0 key are skipped, so the ring is 11 physical
  keys but still looks continuous.
- One revolution per `--period` (default 1 s), `--tail` (default 4). The
  colour is re-randomised on every revolution; `--hue` pins it instead.
- Finishing: the whole 17-key numpad flashes green (exit 0) or pulses red
  (failure) for `--flash` seconds, then the keyboard is released.
- Triggers (pick one): `-- <command>` (spins while the child runs, exits
  with its code), `--pid N` (spins while the process lives), or
  `--status-file PATH` polled for `busy` / `idle` / `done` / `error`.
  No trigger spins until Ctrl-C. `idle` shows a dim ring glow.
- An agent can drive it without spawning children: write `busy` when work
  starts, `done` or `error` when it ends.

## Keystroke echo (`echo.py`)

- Reads the keyboard's HID interface (usage page 1 / usage 6), **not** the LED
  channel: the board exposes three interfaces on the same VID/PID (LED,
  mouse, keyboard), and the LED channel also advertises keyboard usage, so
  `find_keyboard_device()` excludes anything whose report descriptor has the
  vendor marker `06 52 FF`.
- `keyboard.KeyListener` opens the device non-seized, so typing keeps working
  normally; it schedules a CFRunLoop on a daemon thread and queues key
  indices. `IOHIDValue` callbacks fire per element, so key-repeat is filtered
  with a down-set.
- `HID_USAGE_TO_KEY` maps usages `0x04`-`0x63` and `0xE0`-`0xE7` to
  `KEY_NAMES`; 102 of the 104 keys map. `Fn` is firmware-only and `Menu`
  is not reported on this board.
- Renderer: each press flashes white-hot then fades through `--hue` (decay to
  ~10% in `--decay` seconds); a slower layer accumulates per-key heat (blue ->
  red) that cools over `--cool`. `--no-heat` / `--heat-only` split the two.
- `--ripple` adds a third layer: every press also sends a ring outward from the
  key's cell in the 6x21 matrix, so a fast burst radiates from where the hands
  are. Geometry matches `ambient.py ripple` (gaussian band of
  `--ripple-thickness` cells, default 1.0, around `radius = age *
  --ripple-speed`, default 9 cells/s, fading to ~10% in `--ripple-decay`,
  default 1.4 s). Rings are capped at 10 (oldest dropped), pinned to the accent
  hue, and never light a matrix hole. Composition is "brightest layer wins", so
  a faint ring cannot blank out a warm heat trail and a flash is never dulled.
- `--blast` is the critical-overheat burst: instead of a thin ring, each press
  detonates a *filled* fireball from the key's cell — a gaussian core
  (`exp(-(dist/radius)² · 2.2)`, 65% at the centre) inside a brighter gaussian
  rim of `--blast-thickness` cells riding `radius = age * --blast-speed`
  (default 10 cells/s), fading to ~10% in `--blast-decay` (default 1.2 s).
  Colours walk `AMBER_HUE` (31°) to red by `max(age/life, dist/radius)`, so a
  fresh ball is amber all over and the growing leading edge cools to red. Up to
  12 balls live at once; holes stay dark. `--blast` also makes `--hue` default to
  31° instead of cyan, so the pressed key's white-hot flash cools through amber
  rather than the echo accent.
- `--ember N` (default 0, i.e. black between presses) adds the overload meter:
  every blast press adds 0.25 to a `load` value capped at 1 and decaying to ~10%
  in `--ember-cool` (default 3 s). The standing glow is
  `N · load^0.7 · brightness`, hue 31°→0° by load, written first so anything
  brighter wins. Fast typing therefore heats the whole board from black to a dim
  red-orange glow and it cools back after the hands stop; blasts also get 20%
  brighter at full load. `--no-heat` keeps the blue heat trail from muddying the
  fire palette.
- Same Input Monitoring permission as the LED channel; no new TCC prompts.

## Control panel (`ui.py` + `ui.html`)

- `python3 ui.py` serves `http://127.0.0.1:8777` and opens a browser
  (`--port`, `--host`, `--no-browser`). Stdlib only: `http.server` + `json`,
  no build step, no external assets; `ui.html` is read from disk next to
  `ui.py`.
- The page is an app shell, not a document: `body` is a grid of four regions
  at `100dvh` — header (brand + Off/Restore), mode library (filter + 19 modes
  in 7 groups), workspace, status bar. The library and the inspector scroll
  independently, so the board preview never leaves the screen while you turn a
  knob. Below 900 px it relaxes back to a normal scrolling page with a chip
  row instead of the rail.
- Chrome is deliberately monochrome: neutral greys, hairlines, one pale accent
  for interactive state. Every saturated pixel on screen belongs to the 104-key
  preview, so attention lands on the hardware, not the UI.
- One process owns the LED channel. `Engine` opens the device lazily, calls
  `take_control()`, runs exactly one renderer thread, and publishes the last
  frame so the page can draw a live 6x21 board preview (matrix holes blank).
  The canvas measures its container and re-derives the cell size on resize.
- A mode is label + grouping + parameter schema + builder (`define_modes()`);
  the page picks the control from the schema, so a new look costs ~6 lines in
  `ui.py` and none in the page. Defaults come from each script's
  `build_parser()` (extracted from `main`), not duplicated numbers.
- Schema → control: `num` with a range is a slider (drag tracks 1:1, the
  readout is an editable field), `nullable` gets an **Auto** switch, `integer`
  with ≤8 values is a segmented control, `choice` with ≤5 short labels is a
  segmented control and otherwise a select, `text` is a field (`color` gets a
  swatch, paths go monospace). Two optional spec keys change the presentation
  only: `advanced=True` moves a param under **Advanced** (every `fps`, plus
  lat/lon, mic index, status file), and `percent()` sets `scale=100` so a
  0.05–1 brightness reads as 5–100.
- Transport is explicit and lives on the board, not in a form: **Play/Pause**
  for the selected mode. Browsing the rail never touches the hardware, because
  switching a renderer can cost seconds. `Off` and `Restore` are escape
  hatches: if a start is still in flight they queue and run the moment it
  settles rather than being dropped.
- Changing a parameter while a mode runs restarts only that renderer
  (`POST /api/params`). The page keeps the slider local, commits on a ~220 ms
  floor, and allows at most one request in flight with latest-wins coalescing,
  so a two-second drag is a handful of restarts instead of one per frame. The
  device stays taken, so there is no flicker and no second permission prompt.
  Incoming snapshots never overwrite a control that is focused or mid-drag.
- Per-mode parameters persist in `localStorage` (`b820r.params.<mode>`), are
  sanitised against the schema on load, and the last mode is remembered;
  `#mode` in the URL deep-links to one.
- Keyboard: `/` filters the rail, arrows (and Home/End) walk the modes,
  Space plays/pauses, `o` is Off, `r` is Restore, Esc closes the log drawer.
- `atexit`, SIGINT/SIGTERM and the worker's `finally` all release, so Ctrl-C
  or a crashed renderer cannot leave the board stuck.
- Mode switches are generation-counted: the previous thread is asked to stop
  and joined for up to 6 s, and an abandoned slow renderer (weather fetch,
  screen grab) is fenced off by generation so it can never write a frame over
  the new mode. Leaving a music mode can therefore sit on "Starting…" for the
  full join timeout — the panel shows the phase honestly instead of pretending.
- `echo.py` and `breaktime.py` run as supervised child processes, because they
  need the keyboard HID interface and their own CFRunLoop; their preview is
  deliberately not live, and the plinth says so rather than showing a stale
  frame. Each echo mode owns its argv (`Mode.build`): the Layers picker maps
  to `--no-heat` / `--heat-only`, its two ripple entries add `--ripple` (plus
  the ring sliders), and **Critical overheat** passes `--no-heat --blast
  --ember ...` with the blast and overload sliders.
- External scripts drive the board over HTTP instead of IOKit:
  `POST /api/frame` with `{"colors": [r,g,b, ...]}` (flat 312 values or 104
  triples) and an optional `"ttl"` in seconds. A push stops the current mode,
  writes the frame directly, and shows as mode `push` (the board preview
  follows it). With ttl > 0 a watchdog hands the board back to the firmware
  when no new frame arrives in time, so a dead producer cannot freeze the
  LEDs; only the first frame of a push session is logged. `push.py` is the
  ready-made client: `--color RRGGBB` solid fill, `--image PATH` (sips-resized
  onto the matrix, one pixel per key), `--text "..."` marquee in the
  `font.py` 3x5 font (`--speed`, `--hue`, `--once`; ttl defaults to three
  frame intervals), plus `--off` / `--restore`. push.py itself needs no
  permissions — the panel process already holds them.
- `.b820r.lock` (flock) keeps a second `ui.py` from fighting for the board.
  The standalone CLIs do not take the lock — don't run them alongside it.
- Launch the panel from the terminal that already has **Input Monitoring**
  (and Screen Recording for `bias`/system audio); a bundled `.app` would get
  its own grants and re-prompt.

## Commands

- `python3 ui.py` — control panel for every mode (`--port`, `--no-browser`)
- panel: Play/Pause on the board, `#radar` deep-links a mode, `/` filters,
  Space plays, `o` off, `r` restore, Esc closes the activity drawer
- `python3 keyboard.py` — twinkle until Ctrl-C
- `--hold` accumulate, `--keys`, `--fps`, `--duration`, `--seed`, `--verbose`
- `--list` device + permission check
- `--off` blank LEDs, `--restore` return to firmware effects
- `python3 music.py` — spectrum bars from system audio
- `--style pulse|beat`, `--mic`, `--test`, `--fps`, `--keys`, `--device`
- `python3 ambient.py aurora|weather|bias|scanner|radar|stars|ripple|sunset`
- `ambient.py sunset` — still magenta-to-orange gradient, 2 fps
- `ambient.py sunset --drift 0.3 --fps 12` — the colours slowly slide
- `ambient.py ripple --speed 10`, `ambient.py ripple --hue 330 --interval 0.8`
- `ambient.py weather --condition rain --temp 8` (offline weather preview)
- `ambient.py bias --image wallpaper.png`, `ambient.py scanner --row 2`
- `python3 ambient.py fire` — Doom fire; `--intensity 0.8 --wind 0.4`
- `python3 ambient.py plasma --style lava` — metaballs instead of sines
- `python3 clock.py` — HH:MM in 3x5 digits; `--style binary`, `--format 12`
- `python3 sysmon.py` — CPU/RAM/network/battery gauges on rows 1-4
- `python3 breaktime.py` — typing break reminder; `--limit 25 --break 5`
- `python3 push.py --color FF5500` / `--image x.png` / `--text "HI" --once`
- `python3 push.py --off` / `--restore` — panel transport from the shell
- `python3 agent.py -- <command>` — numpad spinner while the command runs
- `agent.py --pid N` / `--status-file PATH` (busy/idle/done/error)
- `python3 echo.py` — live keystroke echo + heat trail
- `echo.py --hue 280`, `--no-heat`, `--heat-only`, `--decay`, `--cool`
- `echo.py --ripple` — every press also ripples outward
- `echo.py --ripple --ripple-speed 12 --ripple-decay 2` — wider, longer rings
- `echo.py --blast --ember 0.35` — critical overheat: black board, fireballs,
  and a red-orange glow that builds while you type

## Code layout

IOKit bindings → device discovery → `BloodyB820R` protocol class →
effects (`Twinkle`, `Accumulate`) → per-look renderers (`music.py`,
`ambient.py`, `agent.py`, `echo.py`, `clock.py`, `sysmon.py`,
`breaktime.py`) → shared 3x5 font (`font.py`) → `ui.py` engine + HTTP →
`push.py` frame client → `ui.html` page.
