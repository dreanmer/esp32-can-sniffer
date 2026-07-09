# Hardware & wiring

## Bill of materials

| Part | Notes |
|------|-------|
| Seeed **XIAO ESP32-C6** | RISC-V, native USB-C (USB-Serial/JTAG), 2× TWAI controllers |
| **SN65HVD230** CAN transceiver ("VP230" blue board) | **3.3 V** transceiver — do **not** substitute a 5 V TJA1050/MCP2551 |
| OBD2 connector / cable | only 3 wires used: CAN-H (pin 6), CAN-L (pin 14), GND (pin 4 or 5) |
| Dupont wires | XIAO ↔ transceiver ↔ OBD |

The XIAO is powered from the **laptop USB-C** (that same cable carries the CAN data
to `python-can`). We do **not** take +12 V from OBD pin 16.

## Pin map (Seeed XIAO ESP32-C6)

Silkscreen → GPIO (from the Seeed wiki):

| Silkscreen | GPIO | Used for |
|-----------|------|----------|
| **D3** | **GPIO21** | TWAI **TX** → transceiver TXD |
| **D2** | **GPIO2** | TWAI **RX** ← transceiver RXD |
| 3V3 | — | power to transceiver VCC |
| GND | — | common ground |
| (on-board LED) | GPIO15 | activity, active-LOW |

This is the **firmware default** and the wiring verified on the test unit (reads the
Fiat 500 bus). Either XIAO pin can be TX or RX (the TWAI signals route through the
GPIO matrix). If you wire a different unit and get **no frames**, your transceiver's
TXD/RXD are probably swapped relative to this — swap the two Dupont wires, or build
with the pins swapped:
`PLATFORMIO_BUILD_FLAGS="-DPIN_CAN_TX=GPIO_NUM_2 -DPIN_CAN_RX=GPIO_NUM_21" pio run -t upload`

## Wiring

```
   XIAO ESP32-C6                 SN65HVD230 (VP230)              OBD2 (female)
  ┌──────────────┐             ┌──────────────────┐           ┌───────────────┐
  │ 3V3 ─────────┼────────────►│ VCC              │           │               │
  │ GND ─────────┼───────┬────►│ GND              │           │               │
  │ D3  (GPIO21)─┼───────┼────►│ TXD (D)   CANH ──┼──────────►│ pin 6  CAN-H  │
  │ D2  (GPIO2) ─┼───────┼───◄─┤ RXD (R)   CANL ──┼──────────►│ pin 14 CAN-L  │
  │              │       │     │ RS  ── GND       │           │ pin 4/5 GND ──┼──┐
  │  USB-C ──────┼─►laptop     └──────────────────┘           └───────────────┘  │
  └──────────────┘       └─────────────────────────────────────────────────────┘
                                        (single common ground)
```

- **RS (slope) pin** → tie to **GND** for high-speed mode. Many VP230 blue boards already
  do this through a resistor; if yours breaks RS out, connect it to GND.
- **Common ground is important.** Connect OBD pin 4 (chassis) or 5 (signal) GND to the
  transceiver GND / XIAO GND so the differential pair has a shared reference with the car.

## ⚠️ Termination — do NOT add 120 Ω

A vehicle CAN bus is already terminated (two 120 Ω resistors ≈ 60 Ω total). You are a
**passive tap**, so you must **not** add another terminator.

Many SN65HVD230 "blue boards" ship with an on-board **120 Ω resistor between CANH and
CANL**. If yours has one, **remove it** (desolder R120, or cut the trace). Leaving it
loads the bus (60 ∥ 120 = 40 Ω) and can cause errors. Check with a multimeter across
CANH–CANL on the *unpowered* board: ~120 Ω means it's populated.

## Safety on a live vehicle bus

- The firmware defaults to **listen-only** (SLCAN `L` / `TWAI_MODE_LISTEN_ONLY`): it
  transmits **nothing** — no data, no ACK, no error frames — so it cannot disturb the bus.
- Only `tools/record.py --normal` / `--obd2` (or SLCAN `O`) put the device into **normal**
  mode, where it ACKs and can transmit. Use that deliberately (e.g. OBD2 polling), and
  prefer doing it while parked first.
- The **ESP32-C6 TWAI is classical CAN only**. If a bus carries CAN **FD** frames, the
  controller reports them as bus errors. The Fiat 500 drivetrain bus (500 kbit/s classical)
  is fine.

## Fiat 500 (test vehicle)

- Drivetrain CAN is exposed on the OBD connector at **500 kbit/s** — CAN-H = pin 6,
  CAN-L = pin 14 (default in all tools).
- Turn the **ignition on** (accessory/run) so the bus is active before capturing.
