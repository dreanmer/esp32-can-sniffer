#!/usr/bin/env python3
"""
record.py — capture the vehicle CAN bus from the XIAO ESP32-C6 SLCAN device into
a webCAN-format CSV that the `cansub-reverse-engineering` skill reads directly
(its OFFLINE / VISION workflows).

Passive & LISTEN-ONLY by default: the device transmits nothing (no ACK, no error
frames) and cannot disturb a live vehicle bus. Pass --obd2 to additionally poll
standard OBD2 PIDs (RPM / speed / coolant ...) so the same log carries a
machine-decodable reference for the offline workflow — this needs NORMAL mode
(the device then ACKs and transmits), enabled automatically with --obd2 or
explicitly with --normal.

Output: temp-output/trace_<label>.csv   (webCAN CSV, via python-can-cansub)

Examples:
    python tools/record.py --duration 30 --label baseline
    python tools/record.py --label drive                      # until Ctrl-C
    python tools/record.py --label drive --obd2 rpm,speed,coolant --obd2-hz 10
    python tools/record.py --normal --print                   # normal mode, echo frames
"""
from __future__ import annotations

import argparse
import glob
import sys
import threading
import time
from pathlib import Path

import can
import python_can_cansub  # noqa: F401  registers the webCAN ".csv" writer + "cansub" iface

# OBD2 (ISO 15765-4) mode-01 functional broadcast request IDs.
OBD2_REQ_11BIT = 0x7DF          # 11-bit functional broadcast (-> resp 0x7E8..0x7EF)
OBD2_REQ_29BIT = 0x18DB33F1     # 29-bit functional broadcast (-> resp 0x18DAF1xx); Fiat 500 uses this
OBD2_PIDS = {
    "rpm": 0x0C, "speed": 0x0D, "coolant": 0x05, "throttle": 0x11,
    "load": 0x04, "maf": 0x10, "intake_temp": 0x0F, "fuel_level": 0x2F,
}


def autodetect_port() -> str | None:
    cands = sorted(
        glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbmodem*")
        + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
    )
    return cands[0] if cands else None


def parse_pids(spec: str) -> list[int]:
    pids: list[int] = []
    for tok in (t.strip() for t in spec.split(",")):
        if not tok:
            continue
        if tok.lower() in OBD2_PIDS:
            pids.append(OBD2_PIDS[tok.lower()])
        else:
            pids.append(int(tok, 0))          # accept a raw PID like 0x0C or 12
    return pids


def build_obd2_requests(pids: list[int], addr: str) -> list[can.Message]:
    msgs: list[can.Message] = []
    for pid in pids:
        data = [0x02, 0x01, pid, 0, 0, 0, 0, 0]
        if addr in ("11bit", "both"):
            msgs.append(can.Message(arbitration_id=OBD2_REQ_11BIT, is_extended_id=False, data=data))
        if addr in ("29bit", "both"):
            msgs.append(can.Message(arbitration_id=OBD2_REQ_29BIT, is_extended_id=True, data=data))
    return msgs


class _ChannelStamp(can.Listener):
    """Stamp msg.channel so the webCAN CSV BusChannel column is populated, then forward."""

    def __init__(self, channel: int, target: can.Listener):
        self.channel = channel
        self.target = target

    def on_message_received(self, msg: can.Message) -> None:
        msg.channel = self.channel
        self.target.on_message_received(msg)

    def stop(self) -> None:
        self.target.stop()


class _Counter(can.Listener):
    def __init__(self):
        self.n = 0
        self.ids: set[int] = set()

    def on_message_received(self, msg: can.Message) -> None:
        self.n += 1
        self.ids.add(msg.arbitration_id)


class _Poller:
    """Round-robin OBD2 request sender on a background thread.

    Used instead of bus.send_periodic(): the slcan generic cyclic task requires
    all messages to share one arbitration ID, but our requests mix 11-bit and
    29-bit IDs. Sending on a thread while the Notifier reads mirrors python-can's
    own periodic-send concurrency model.
    """

    def __init__(self, bus: can.BusABC, msgs: list[can.Message], period: float):
        self.bus = bus
        self.msgs = msgs
        self.period = period
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            try:
                self.bus.send(self.msgs[i % len(self.msgs)])
            except can.CanError:
                pass
            i += 1
            self._stop.wait(self.period)

    def stop(self) -> None:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=1.0)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: autodetect usbmodem/ttyACM)")
    ap.add_argument("--bitrate", type=int, default=500000, help="CAN bitrate (default 500000)")
    ap.add_argument("--duration", type=float, default=None, help="seconds (default: until Ctrl-C)")
    ap.add_argument("--label", default="capture", help="filename stem (default: capture)")
    ap.add_argument("--out", help="output CSV (default temp-output/trace_<label>.csv)")
    ap.add_argument("--channel", type=int, default=0, help="BusChannel value written to CSV")
    ap.add_argument("--print", dest="do_print", action="store_true", help="also echo frames to stdout")

    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--listen-only", dest="listen_only", action="store_true", default=True,
                     help="passive/silent — DEFAULT (safe on a live vehicle bus)")
    grp.add_argument("--normal", dest="listen_only", action="store_false",
                     help="NORMAL mode: the device ACKs and can transmit")

    ap.add_argument("--obd2", nargs="?", const="rpm,speed,coolant", default=None,
                    metavar="PIDS",
                    help="poll OBD2 PIDs while recording (implies --normal); "
                         f"comma list of {sorted(OBD2_PIDS)} or raw PIDs (default: rpm,speed,coolant)")
    ap.add_argument("--obd2-hz", type=float, default=5.0,
                    help="OBD2 request rate, frames/s across the PID set (default 5)")
    ap.add_argument("--obd2-addr", choices=["both", "11bit", "29bit"], default="both",
                    help="OBD2 addressing scheme (Fiat 500 = 29bit; default: both)")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        print("ERROR: no serial port found. Plug in the XIAO and/or pass --port.", file=sys.stderr)
        return 1

    listen_only = args.listen_only
    pids = None
    if args.obd2 is not None:
        pids = parse_pids(args.obd2)
        listen_only = False               # OBD2 polling needs to transmit
        if not pids:
            print("ERROR: --obd2 given but no valid PIDs parsed.", file=sys.stderr)
            return 1

    out = Path(args.out) if args.out else Path(f"temp-output/trace_{args.label}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    mode = "listen-only (silent)" if listen_only else "NORMAL (ACK/TX)"
    dur = f"{args.duration:.0f}s" if args.duration else "until Ctrl-C"
    print(f"[record] port={port} bitrate={args.bitrate} mode={mode} -> {out} ({dur})",
          file=sys.stderr)
    if not listen_only:
        print("[record] WARNING: NORMAL mode — the device will ACK and transmit on the bus.",
              file=sys.stderr)

    counter = _Counter()
    task = None
    try:
        with can.Bus(interface="slcan", channel=port,
                     bitrate=args.bitrate, listen_only=listen_only) as bus:
            logger = can.Logger(str(out))          # webCAN CSV via CanSubCSVWriter
            listeners: list[can.Listener] = [_ChannelStamp(args.channel, logger), counter]
            if args.do_print:
                listeners.append(can.Printer())
            with can.Notifier([bus], listeners):
                if pids:
                    reqs = build_obd2_requests(pids, args.obd2_addr)
                    period = 1.0 / max(args.obd2_hz, 0.1)
                    task = _Poller(bus, reqs, period)
                    task.start()
                    print(f"[record] polling OBD2 PIDs {['0x%02X' % p for p in pids]} "
                          f"({args.obd2_addr}) at {args.obd2_hz:g} req/s", file=sys.stderr)
                if args.duration:
                    time.sleep(args.duration)
                else:
                    print("[record] recording... press Ctrl-C to stop", file=sys.stderr)
                    while True:
                        time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[record] stopping...", file=sys.stderr)
    except can.CanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if task is not None:
            task.stop()

    print(f"[record] captured {counter.n} frames, {len(counter.ids)} unique IDs -> {out}")
    if counter.n == 0:
        print("[record] NO FRAMES — check: wiring (try swapping TX/RX), bitrate, "
              "OBD pins 6/14, common ground, and that the ignition is on.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
