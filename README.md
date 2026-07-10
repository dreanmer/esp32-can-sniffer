# canbus — XIAO ESP32-C6 CAN sniffer for `python-can` + reverse engineering

A DIY, laptop-powered CAN interface: a **Seeed XIAO ESP32-C6** + **SN65HVD230
("VP230")** transceiver that speaks the **SLCAN / LAWICEL** protocol over **USB or
Wi-Fi**, so it drops straight into [`python-can`](https://python-can.readthedocs.io/).
Record a vehicle's CAN bus to a **webCAN CSV** and decode proprietary signals with the
`cansub-reverse-engineering` skill (obtained separately — see
[Reverse-engineering skills](#reverse-engineering-skills)). No CANsub hardware needed.

Nothing here is vehicle-specific; it was developed and tested on a Fiat 500
(500 kbit/s classical drivetrain CAN on the OBD2 connector).

```
 Vehicle  ──OBD2 pin6/pin14──►  SN65HVD230  ──D2/D3──►  XIAO ESP32-C6
 (CAN-H / CAN-L)               (VP230)                  │  USB-C or Wi-Fi (SLCAN)
                                                        ▼
                       Host ──►  python-can (interface="slcan")  ──►  webCAN CSV
                                                                       │
                                       cansub-reverse-engineering skill
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
.claude/skills/      RE skills — NOT in this repo (see "Reverse-engineering skills")
temp-output/         (generated) traces + working files
decoding-output/     (generated) the DBCs you produce
```

## 1. Build & flash the firmware (PlatformIO CLI)

Wire it up first — see **[docs/hardware.md](docs/hardware.md)** (⚠️ read the
*termination* section: do **not** add a 120 Ω resistor on a car bus).

```bash
cd firmware
pio run                                              # build (1st run downloads the toolchain)
pio run -t upload                                    # build + flash over USB (auto-detect port)
pio run -t upload --upload-port /dev/cu.usbmodemXXXX # ...or name the port explicitly
pio device monitor -b 115200                         # serial monitor (type `V`+Enter -> version)
pio run -t clean                                     # clean build artifacts
```

- **Port names:** macOS `/dev/cu.usbmodem*` · Linux `/dev/ttyACM*` · Windows `COMx`
  (`pio device list` to find it).
- If upload won't start (native-USB download mode can be flaky): **hold BOOT, tap
  RESET, release BOOT**, then re-run the upload.
- **Wi-Fi is enabled by default.** After flashing, the board hosts its own access
  point — see [Connect over Wi-Fi](#connect-over-wi-fi). To build without it:
  ```bash
  PLATFORMIO_BUILD_FLAGS="-DENABLE_WIFI=0" pio run -t upload
  ```

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
signal in the same log to anchor a proprietary one. If the vehicle answers OBD2 on the
exposed bus, poll it while recording — the OBD2 responses (11-bit `0x7E8…` or 29-bit
`0x18DAF1xx`) land in the same CSV and the skill decodes them with its OBD2 DBC:

```bash
.venv/bin/python tools/record.py --label drive --obd2 rpm,speed,coolant --obd2-hz 10
```

`--obd2` switches the device to **normal** mode (it ACKs + transmits the requests;
`--obd2-addr both` tries 11-bit and 29-bit). If no OBD2 responses appear, use the
**VISION** workflow instead (record a phone video of the dashboard alongside the log).

## 5. Reverse-engineer a signal

Use the **`cansub-reverse-engineering`** skill's **Offline workflow** — point it at
your recorded CSV and the OBD2/GPS reference. Example ask:

> "Reverse engineer vehicle speed from `temp-output/trace_drive.csv`, using the OBD2
> speed PID in the log as the reference."

It runs `survey → correlate → bitsearch → build_dbc → verify` and writes a
single-signal DBC under `decoding-output/<vehicle>/<signal>/`. Combine several with the
**`combine-dbc`** skill into `decoding-output/<vehicle>/<vehicle>.dbc`.

## Reverse-engineering skills

That pipeline is provided by the **CSS Electronics CANsub skills**
(`cansub-reverse-engineering`, `cansub-knowledge`, `combine-dbc`). **They are NOT
part of this repository** — `.claude/skills/` is git-ignored. Obtain them from CSS
Electronics and drop them under `.claude/skills/`:

- CSS Electronics — <https://csselectronics.com/> · <https://github.com/CSS-Electronics>
- python-can integration used here — <https://github.com/CSS-Electronics/python-can-cansub>

The skills consume the **webCAN CSV** this tool records, so once installed the OFFLINE
workflow runs unchanged. Everything else in this repo (firmware, host tools, web
console) works **without** them.

## Connect over Wi-Fi

After flashing, the board brings up its own access point (no router needed):

| | |
|---|---|
| SSID | `CANSNIFFER-<id>` |
| Password | `cansniffer` |
| Device IP | `192.168.4.1` |
| TCP SLCAN port | `3333` |

Join that Wi-Fi from your laptop, then connect either way:

- **Web console** — set transport to **Wi-Fi**, host `192.168.4.1:3333`, **Connect**.
- **python-can directly** (pyserial's `socket://` handler):
  ```python
  import can
  bus = can.Bus(interface="slcan", channel="socket://192.168.4.1:3333",
                bitrate=500000, listen_only=True)
  ```
- **CLI tools** — pass the socket URL as the port:
  ```bash
  .venv/bin/python tools/record.py --port socket://192.168.4.1:3333 --label drive
  ```

USB and Wi-Fi speak the same SLCAN; use **one host at a time**. For **full-fidelity
recording / reverse engineering prefer USB** — on a busy bus (~1000+ fps) Wi-Fi can
drop frames under TCP backpressure. Wi-Fi shines for an untethered live dashboard or
diagnostics. Flash with `-DENABLE_WIFI=0` to turn the AP off.

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

- **Classical CAN only** (no CAN FD) — an ESP32-C6 TWAI hardware limit; CAN FD frames
  are reported as bus errors.
- **One CAN channel, one host at a time** — connect over USB *or* Wi-Fi, not both.
- **Wi-Fi can drop frames under load.** On a busy bus (~1000+ fps) the TCP link may
  drop frames under backpressure — use **USB** for full-fidelity recording / reverse
  engineering; Wi-Fi is best for live viewing / diagnostics.
- **Reverse-engineering skills are not bundled** — see
  [Reverse-engineering skills](#reverse-engineering-skills).
- Recording throughput is bounded by the host reading continuously; the firmware RX
  queue is 64 frames.
