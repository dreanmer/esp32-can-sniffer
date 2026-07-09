"""
CanSession — the single owner of the CAN bus for the web UI.

Only this object opens the serial/slcan port. A background can.Notifier thread
receives frames (decoding OBD2 responses into a live snapshot and, when enabled,
teeing every frame to a webCAN CSV logger). A poller thread continuously performs
self-healing OBD2 PID discovery and polls the supported PIDs. Web handlers only
read snapshots and post commands — they never touch the port directly.
"""
from __future__ import annotations

import glob
import threading
import time
from pathlib import Path
from typing import Optional

import can
import python_can_cansub  # noqa: F401  registers webCAN ".csv" writer

from . import obd2


def autodetect_port() -> Optional[str]:
    c = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbmodem*")
               + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    return c[0] if c else None


class _RxListener(can.Listener):
    """Decodes OBD2 responses into the session snapshot, counts frames, and tees
    every frame to the recording logger when active."""

    def __init__(self, session: "CanSession"):
        self.s = session

    def on_message_received(self, msg: can.Message) -> None:
        s = self.s
        parsed = obd2.parse_response(msg)
        with s.lock:
            s.frame_count += 1
            e = s.id_counts.get(msg.arbitration_id)
            if e is not None:
                e[0] += 1
            else:
                s.id_counts[msg.arbitration_id] = [1, bool(msg.is_extended_id)]
            if parsed is not None:
                kind = parsed[0]
                if s.scheme is None:
                    sc = obd2.classify(msg)
                    if sc:
                        s.scheme = sc
                if kind == "bitmask":
                    _, _, pids = parsed
                    s.supported = sorted(set(s.supported) | set(pids))
                elif kind == "value":
                    _, _, name, value = parsed
                    s.values[name] = (value, msg.timestamp)
        # recording is guarded by its own lock (file IO kept off the state lock)
        with s.log_lock:
            if s.logger is not None:
                msg.channel = 0
                s.logger.on_message_received(msg)


class _Poller(threading.Thread):
    """Sends OBD2 requests: continuous discovery + weighted polling of supported PIDs."""

    def __init__(self, session: "CanSession", rate_hz: float = 25.0):
        super().__init__(daemon=True)
        self.s = session
        self.period = 1.0 / max(rate_hz, 1.0)
        self._stop = threading.Event()

    def run(self) -> None:
        i = 0
        while not self._stop.is_set():
            s = self.s
            bus = s.bus
            if bus is None:
                break
            scheme = s.scheme
            try:
                if scheme is None:
                    # scheme unknown -> probe both until one answers
                    for name in ("29bit", "11bit"):
                        bus.send(obd2.make_request(0x00, name))
                    self._stop.wait(0.5)
                    continue
                sched = s.poll_schedule()
                if not sched:
                    bus.send(obd2.make_request(0x00, scheme))
                    self._stop.wait(0.3)
                    continue
                bus.send(obd2.make_request(sched[i % len(sched)], scheme))
                i += 1
            except can.CanError:
                pass
            self._stop.wait(self.period)

    def stop(self) -> None:
        self._stop.set()


class CanSession:
    def __init__(self):
        self.lock = threading.Lock()          # guards bus/state/values/supported/frame_count
        self.log_lock = threading.Lock()      # guards the recording logger
        self.bus: Optional[can.BusABC] = None
        self.notifier: Optional[can.Notifier] = None
        self.poller: Optional[_Poller] = None
        self.logger = None
        self.log_path: Optional[str] = None
        self.mode: Optional[str] = None        # 'listen' | 'normal'
        self.scheme: Optional[str] = None      # '29bit' | '11bit'
        self.supported: list[int] = []
        self.values: dict[str, tuple[float, float]] = {}   # name -> (value, epoch)
        self.id_counts: dict[int, list] = {}               # arb_id -> [count, ext]
        self.frame_count = 0
        self.port: Optional[str] = None
        self.bitrate = 500000

    # ---- lifecycle ----
    def connect(self, mode: str = "normal", bitrate: int = 500000,
                port: Optional[str] = None) -> dict:
        with self.lock:
            if self.bus is not None:
                raise RuntimeError("already connected")
        port = port or autodetect_port()
        if not port:
            raise RuntimeError("no serial port found (plug in the device or pass port)")
        listen_only = (mode == "listen")
        bus = can.Bus(interface="slcan", channel=port, bitrate=bitrate,
                      listen_only=listen_only)
        with self.lock:
            self.bus = bus
            self.port = port
            self.bitrate = bitrate
            self.mode = mode
            self.scheme = None
            self.supported = []
            self.values = {}
            self.id_counts = {}
            self.frame_count = 0
        self.notifier = can.Notifier([bus], [_RxListener(self)])
        if mode == "normal":
            self.poller = _Poller(self)
            self.poller.start()
        return self.status()

    def disconnect(self) -> dict:
        self.stop_recording()
        if self.poller is not None:
            self.poller.stop()
            self.poller.join(timeout=1.0)
            self.poller = None
        if self.notifier is not None:
            try:
                self.notifier.stop()
            except Exception:
                pass
            self.notifier = None
        with self.lock:
            bus = self.bus
            self.bus = None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass
        with self.lock:
            self.mode = None
            self.scheme = None
        return self.status()

    # ---- recording ----
    def start_recording(self, label: str = "capture") -> str:
        if self.bus is None:
            raise RuntimeError("not connected")
        safe = "".join(c for c in label if c.isalnum() or c in "-_") or "capture"
        path = Path("temp-output") / f"trace_{safe}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_lock:
            if self.logger is not None:
                raise RuntimeError("already recording")
            self.logger = can.Logger(str(path))
            self.log_path = str(path)
        return str(path)

    def stop_recording(self) -> Optional[str]:
        with self.log_lock:
            lg, path = self.logger, self.log_path
            self.logger = None
            self.log_path = None
        if lg is not None:
            try:
                lg.stop()
            except Exception:
                pass
        return path

    # ---- polling schedule ----
    def poll_schedule(self) -> list[int]:
        with self.lock:
            sup = list(self.supported)
        disc = [0x00]
        if 0x20 in sup:
            disc.append(0x20)
        if 0x40 in sup:
            disc.append(0x40)
        data = [p for p in sup if p in obd2.PIDS]
        sched: list[int] = []
        for p in data:
            sched.append(p)
        for p in data:
            if p in obd2.FAST:
                sched += [p, p]        # extra weight for fast gauges
        return sched + disc if sched else disc

    # ---- snapshots ----
    def _supported_names(self) -> list[str]:
        return sorted({obd2.PIDS[p].name for p in self.supported if p in obd2.PIDS})

    def status(self) -> dict:
        with self.lock:
            return {
                "connected": self.bus is not None,
                "port": self.port,
                "mode": self.mode,
                "scheme": self.scheme,
                "bitrate": self.bitrate,
                "recording": self.log_path,
                "frame_count": self.frame_count,
                "supported": [f"0x{p:02X}" for p in self.supported],
                "supported_names": self._supported_names(),
            }

    def bus_stats(self, top: int = 80) -> dict:
        """Per-ID frame stats for the hardware-test view."""
        with self.lock:
            connected = self.bus is not None
            fc = self.frame_count
            rows = [{"id": f"0x{i:0{8 if ext else 3}X}", "count": c, "ext": ext}
                    for i, (c, ext) in self.id_counts.items()]
        rows.sort(key=lambda r: r["count"], reverse=True)
        return {"connected": connected, "mode": self.mode, "frame_count": fc,
                "unique_ids": len(rows), "ids": rows[:top]}

    def live(self) -> dict:
        now = time.time()
        with self.lock:
            vals = {name: {"value": round(v, 3), "age": round(now - ts, 3)}
                    for name, (v, ts) in self.values.items()}
            return {
                "connected": self.bus is not None,
                "mode": self.mode,
                "scheme": self.scheme,
                "recording": self.log_path,
                "frame_count": self.frame_count,
                "supported_names": self._supported_names(),
                "values": vals,
            }
