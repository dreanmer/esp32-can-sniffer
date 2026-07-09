#!/usr/bin/env python3
"""
obd2_test.py — probe whether the vehicle answers OBD2 on this bus, trying BOTH
addressing schemes (11-bit ISO 15765-4 and 29-bit), and decode a few PIDs.

Requires NORMAL mode (the device transmits the requests + ACKs), so this is an
ACTIVE test — it is what every OBD2 scan tool does, but it is not passive.

Examples:
    python tools/obd2_test.py
    python tools/obd2_test.py --seconds 1.0 --pids rpm,speed,coolant,throttle
"""
from __future__ import annotations

import argparse
import glob
import sys
import time

import can
import python_can_cansub  # noqa: F401

# name -> (pid, decoder(A,B)->(value, unit))
PID_DEFS = {
    "rpm":     (0x0C, lambda a, b: ((256 * a + b) / 4.0, "rpm")),
    "speed":   (0x0D, lambda a, b: (float(a), "km/h")),
    "coolant": (0x05, lambda a, b: (a - 40.0, "degC")),
    "throttle":(0x11, lambda a, b: (100.0 * a / 255.0, "%")),
    "load":    (0x04, lambda a, b: (100.0 * a / 255.0, "%")),
    "intake_temp": (0x0F, lambda a, b: (a - 40.0, "degC")),
}

# (label, request arbitration_id, is_extended, response-id predicate)
SCHEMES = [
    ("11-bit (0x7DF -> 0x7E8..EF)", 0x7DF, False, lambda i: 0x7E8 <= i <= 0x7EF),
    ("29-bit (0x18DB33F1 -> 0x18DAF1xx)", 0x18DB33F1, True,
     lambda i: (i & 0xFFFFFF00) == 0x18DAF100),
]


def autodetect_port() -> str | None:
    c = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbmodem*")
               + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    return c[0] if c else None


def request(pid: int) -> list[int]:
    return [0x02, 0x01, pid, 0x00, 0x00, 0x00, 0x00, 0x00]  # single frame, mode 01


def poll(bus: can.BusABC, req_id: int, ext: bool, resp_ok, pid: int,
         window: float) -> can.Message | None:
    """Send one request, return the first positive mode-01 response frame, if any."""
    bus.send(can.Message(arbitration_id=req_id, is_extended_id=ext, data=request(pid)))
    deadline = time.time() + window
    while time.time() < deadline:
        m = bus.recv(timeout=deadline - time.time())
        if m is None:
            continue
        d = bytes(m.data)
        if resp_ok(m.arbitration_id) and len(d) >= 4 and d[1] == 0x41 and d[2] == pid:
            return m
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--seconds", type=float, default=0.8, help="response window per request")
    ap.add_argument("--pids", default="rpm,speed,coolant,throttle,coolant")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("ERROR: no serial port.", file=sys.stderr)
        return 1

    names = [p.strip() for p in args.pids.split(",") if p.strip() in PID_DEFS]
    names = list(dict.fromkeys(names))  # de-dup, keep order
    print(f"[obd2] port={port} bitrate={args.bitrate}  ACTIVE / NORMAL mode "
          f"(the device WILL transmit)\n", file=sys.stderr)

    any_response = False
    with can.Bus(interface="slcan", channel=port, bitrate=args.bitrate,
                 listen_only=False) as bus:      # NORMAL mode
        # short passive sample of the ambient ID set, to flag NEW ids on request
        baseline: set[int] = set()
        t = time.time() + 0.7
        while time.time() < t:
            m = bus.recv(timeout=t - time.time())
            if m:
                baseline.add(m.arbitration_id)
        print(f"ambient IDs before requests: {len(baseline)}\n")

        for label, req_id, ext, resp_ok in SCHEMES:
            print(f"=== {label} ===")
            hits = 0
            for name in names:
                pid, dec = PID_DEFS[name]
                m = poll(bus, req_id, ext, resp_ok, pid, args.seconds)
                if m is None:
                    print(f"  {name:12s} pid 0x{pid:02X}  -> no response")
                    continue
                hits += 1
                any_response = True
                d = bytes(m.data)
                a = d[3] if len(d) > 3 else 0
                b = d[4] if len(d) > 4 else 0
                val, unit = dec(a, b)
                idw = 8 if m.is_extended_id else 3
                print(f"  {name:12s} pid 0x{pid:02X}  <- 0x{m.arbitration_id:0{idw}X} "
                      f"{d.hex().upper():18s} = {val:.1f} {unit}")
            print(f"  ({hits}/{len(names)} answered)\n")

    if not any_response:
        print("No OBD2 responses on either scheme. The exposed bus may not carry OBD2\n"
              "diagnostics (raw drivetrain only). For reverse engineering, use a\n"
              "dashboard-video reference instead of an on-bus OBD2 reference.")
        return 2
    print("OBD2 works on this bus — you can use --obd2 to log a reference while recording.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
