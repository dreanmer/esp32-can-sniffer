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

Because it's standard SLCAN it also works with **SavvyCAN**, **cangaroo**, and
`can-utils` `slcand`.

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
