#!/usr/bin/env python3
"""
check_device.py — quick sanity check for the XIAO ESP32-C6 SLCAN sniffer.

Lists candidate serial ports, opens the bus LISTEN-ONLY, and reports how many
frames / unique IDs arrive in a short window. Use this to confirm wiring and
bitrate BEFORE a real capture.

Examples:
    python tools/check_device.py
    python tools/check_device.py --seconds 5 --bitrate 500000
"""
from __future__ import annotations

import argparse
import glob
import sys
import time
from collections import Counter

import can
import python_can_cansub  # noqa: F401


def list_ports() -> list[str]:
    return sorted(
        glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbmodem*")
        + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: first autodetected)")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--top", type=int, default=15, help="how many busiest IDs to show")
    args = ap.parse_args()

    ports = list_ports()
    print(f"serial ports: {ports or '(none found)'}", file=sys.stderr)
    port = args.port or (ports[0] if ports else None)
    if not port:
        print("ERROR: no serial port. Plug in the XIAO or pass --port.", file=sys.stderr)
        return 1

    print(f"opening {port} @ {args.bitrate} bit/s (listen-only) for {args.seconds:.0f}s...",
          file=sys.stderr)
    ids: Counter[int] = Counter()
    ext_ids: set[int] = set()
    n = 0
    try:
        with can.Bus(interface="slcan", channel=port,
                     bitrate=args.bitrate, listen_only=True) as bus:
            deadline = time.time() + args.seconds
            while time.time() < deadline:
                msg = bus.recv(timeout=deadline - time.time())
                if msg is None:
                    continue
                n += 1
                ids[msg.arbitration_id] += 1
                if msg.is_extended_id:
                    ext_ids.add(msg.arbitration_id)
    except can.CanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rate = n / args.seconds if args.seconds else 0
    print(f"\n{n} frames  |  {len(ids)} unique IDs  |  ~{rate:.0f} frames/s")
    if n == 0:
        print("NO FRAMES. Check: TX/RX swap, bitrate, OBD pins 6/14, common ground, ignition on.")
        return 2
    width = 8 if ext_ids else 3
    print(f"\ntop {args.top} IDs by count:")
    for cid, c in ids.most_common(args.top):
        tag = " (ext)" if cid in ext_ids else ""
        print(f"  0x{cid:0{width}X}{tag:6}  {c:6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
