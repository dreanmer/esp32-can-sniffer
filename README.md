# canbus — XIAO ESP32-C6 CAN sniffer for `python-can` + reverse engineering

A DIY, laptop-powered CAN interface: a **Seeed XIAO ESP32-C6** + **SN65HVD230
("VP230")** transceiver that speaks the **SLCAN / LAWICEL** protocol over native
USB, so it drops straight into [`python-can`](https://python-can.readthedocs.io/).
Record a car's CAN bus to a **webCAN CSV** and decode proprietary signals with the
bundled `cansub-reverse-engineering` skill — no CANsub hardware needed.

Test vehicle: a Mexican **Fiat 500** exposing its 500 kbit/s drivetrain CAN on the
OBD2 connector.

```
 Fiat 500  ──OBD2 pin6/pin14──►  SN65HVD230  ──D2/D3──►  XIAO ESP32-C6
 (CAN-H / CAN-L)                (VP230)                   │  USB-C (SLCAN over USB-CDC)
                                                          ▼
                          Mac ──►  python-can (interface="slcan")  ──►  webCAN CSV
                                                                          │
                                          .claude/skills/cansub-reverse-engineering
                                          survey → correlate → bitsearch → build_dbc → verify
                                                                          ▼
                                                                        .dbc
```

## Repo layout

```
firmware/            PlatformIO project (Arduino, ESP32-C6)
  platformio.ini
  src/main.cpp       SLCAN firmware (TWAI ↔ USB-CDC)
tools/
  record.py          capture bus -> temp-output/trace_<label>.csv (webCAN CSV)
  check_device.py    quick "is it wired right / are frames flowing?" check
  obd2_test.py       probe OBD2 support (11-bit + 29-bit) and decode PIDs
webui/               Flask web UI — admin console
  app.py session.py obd2.py
  templates/         base + dashboard, diagnostics, frames
  static/            style.css, app.js
docs/hardware.md     wiring, OBD2 pinout, termination & safety notes
requirements.txt     host Python deps (venv at repo root)
.claude/skills/      cansub-reverse-engineering, combine-dbc, cansub-knowledge
temp-output/         (generated) traces + working files
decoding-output/     (generated) the DBCs you produce
```

## 1. Build & flash the firmware

Wire it up first — see **[docs/hardware.md](docs/hardware.md)** (⚠️ read the
*termination* section: do **not** add a 120 Ω resistor on a car bus).

```bash
cd firmware
pio run                 # compile (first run downloads the pioarduino toolchain)
pio run -t upload       # flash over USB-C
pio device monitor      # optional: type `V` + Enter -> version string
```

If upload won't start: **hold BOOT, tap RESET, release BOOT**, then re-upload.

## 2. Set up the host Python env

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Confirm frames are flowing

Ignition on, then:

```bash
.venv/bin/python tools/check_device.py --seconds 5
```

You should see a frame count and the busiest CAN IDs. If it says **NO FRAMES**, the
transceiver TXD/RXD are likely swapped (see the note in docs/hardware.md), or the
bitrate/pins/ground are wrong.

## 4. Record the bus

```bash
# passive/listen-only (safe) — records until Ctrl-C
.venv/bin/python tools/record.py --label drive

# fixed duration
.venv/bin/python tools/record.py --label baseline --duration 30
```

Output → `temp-output/trace_<label>.csv` in the exact **webCAN CSV** format the
reverse-engineering skill reads.

## Web console

A small self-contained admin console (no internet needed — all CSS/JS is local,
no CDN/build step) with a sidebar menu and a global connection bar. The device is
exclusive, so one page uses the bus at a time. Three tools:

- **Dashboard** (`/`) — connect, **Record** raw+OBD2, frame rate, and key live OBD2
  values (RPM, speed, coolant, fuel, throttle, load, module voltage) as stat tiles.
- **Diagnostics** (`/diagnostics`) — what OBD2 exposes by default: addressing
  (11/29-bit), OBD standard, supported-PID list, and all live decoded values.
- **Live frames** (`/frames`) — raw CAN monitor: per-ID table (count, cycle, data)
  with a filter, a **.dbc import** to decode identified frames, and a **plot** of any
  byte or decoded signal over time.

```bash
.venv/bin/python -m webui.app               # http://localhost:5000
```

In the top-right bar pick the **transport** and **Connect**:
- **USB** — auto-detects the plugged-in device.
- **Wi-Fi** — join the board's access point **`CANSNIFFER-xxxx`** (password
  `cansniffer`), then connect to host `192.168.4.1:3333` (the default in the field).

Then pick **Normal** mode; **Record** writes the raw proprietary bus **plus** the OBD2
responses to `temp-output/trace_<label>.csv` — the exact log the reverse-engineering
skill decodes.

- One process owns the device: a background thread decodes OBD2 responses into a
  snapshot the pages poll (`/api/live`, `/api/frames`), tees all frames to a
  `can.Logger` when recording, and a poller does self-healing PID discovery (tries
  11-bit and 29-bit, auto-detecting the vehicle's addressing and recovering when the
  bus wakes).
- **Listen-only** mode records passively (no OBD2 values — OBD2 needs requests).

### Getting a decodable reference (recommended)

The OFFLINE reverse-engineering workflow needs a **separately decodable reference**
signal in the same log to anchor a proprietary one. If the Fiat answers OBD2 on the
exposed bus, poll it while recording — the OBD2 responses (on `0x7E8…`) land in the
same CSV and the skill decodes them with its bundled OBD2 DBC:

```bash
.venv/bin/python tools/record.py --label drive --obd2 rpm,speed,coolant --obd2-hz 10
```

`--obd2` switches the device to **normal** mode (it ACKs + transmits the requests).
If no `0x7E8` responses appear, use the **VISION** workflow instead (record a phone
video of the dashboard alongside the passive log).

## 5. Reverse-engineer a signal

Use the **`cansub-reverse-engineering`** skill's **Offline workflow** — point it at
your recorded CSV and the OBD2/GPS reference. Example ask:

> "Reverse engineer vehicle speed from `temp-output/trace_drive.csv`, using the OBD2
> speed PID in the log as the reference."

It runs `survey → correlate → bitsearch → build_dbc → verify` and writes a
single-signal DBC under `decoding-output/fiat500/<signal>/`. Combine several with the
**`combine-dbc`** skill into `decoding-output/fiat500/fiat500.dbc`.

## How the device talks to python-can (SLCAN)

The firmware implements the SLCAN commands python-can's `slcan` backend uses:

| From host | Meaning | Firmware action |
|-----------|---------|-----------------|
| `S6` | 500 kbit/s | set TWAI timing (also S0..S8) |
| `O` / `L` | open normal / listen-only | `twai_start()` in NORMAL / LISTEN_ONLY |
| `C` | close | `twai_stop()` |
| `t/T/r/R` | transmit std/ext (+remote) | `twai_transmit()` (normal mode only) |
| `V` / `N` | version / serial | reply string |

Received frames are streamed back as `tIIILDD…\r` (std) / `TIIIIIIIILDD…\r` (ext).
python-can stamps each with the host clock, which becomes the CSV `TimestampEpoch`.

The firmware speaks this SLCAN over **two transports at once**:
- **USB-CDC** — `channel="/dev/cu.usbmodemXXXX"`
- **Wi-Fi SoftAP + TCP** — the board hosts AP `CANSNIFFER-xxxx` (pw `cansniffer`,
  IP `192.168.4.1`) and a TCP SLCAN server on port 3333, so
  `channel="socket://192.168.4.1:3333"` works (pyserial's `socket://` handler).
  Set `-DENABLE_WIFI=0` to disable the AP. One host at a time.

Because it's standard SLCAN it also works with **SavvyCAN**, **cangaroo**, and
`can-utils` `slcand` (over USB or the TCP socket).

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| No frames | Swap TX/RX; wrong bitrate; OBD pins 6/14; no common ground; ignition off |
| Frames but garbage / errors | Bus is CAN **FD** (C6 TWAI is classical-only), or wrong bitrate |
| Device resets on connect | Native-USB DTR toggle — the 2 s `sleep_after_open` in python-can covers it |
| Upload fails | Manual download mode: hold BOOT, tap RESET, release BOOT |
| Bus load / dropped frames | firmware `rx_queue_len` is 64; keep the host reading continuously |

## Limitations

- **Classical CAN only** (no CAN FD) — an ESP32-C6 TWAI hardware limit.
- One CAN channel per device.
- Wired (USB). The C6 also has Wi-Fi, so a wireless SLCAN-over-TCP variant is a
  possible future addition.
