"""
CanSession — the single owner of the CAN bus for the web UI.

Only this object opens the serial/slcan port. A background can.Notifier thread
receives frames: it tracks per-ID stats + a short history (for the live monitor
and plots), decodes OBD2 responses into a live snapshot, optionally decodes every
frame through an imported DBC, and tees all frames to a webCAN CSV when recording.
A poller thread performs self-healing OBD2 PID discovery + polling. Web handlers
only read snapshots and post commands.
"""
from __future__ import annotations

import glob
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import can
import cantools
import python_can_cansub  # noqa: F401  registers webCAN ".csv" writer

from . import obd2

HISTORY_LEN = 1500   # per-ID rolling samples kept for plotting


def autodetect_port() -> Optional[str]:
    c = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/tty.usbmodem*")
               + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    return c[0] if c else None


class _RxListener(can.Listener):
    def __init__(self, session: "CanSession"):
        self.s = session

    def on_message_received(self, msg: can.Message) -> None:
        s = self.s
        aid = msg.arbitration_id
        data = bytes(msg.data)
        ts = msg.timestamp
        parsed = obd2.parse_response(msg)
        with s.lock:
            s.frame_count += 1
            m = s.id_meta.get(aid)
            if m is not None:
                m["count"] += 1; m["data"] = data; m["last"] = ts; m["dlc"] = msg.dlc
            else:
                s.id_meta[aid] = {"count": 1, "ext": bool(msg.is_extended_id),
                                  "data": data, "first": ts, "last": ts, "dlc": msg.dlc}
                s.history[aid] = deque(maxlen=HISTORY_LEN)
            s.history[aid].append((ts, data))
            if parsed is not None:
                if s.scheme is None:
                    sc = obd2.classify(msg)
                    if sc:
                        s.scheme = sc
                if parsed[0] == "bitmask":
                    s.supported = sorted(set(s.supported) | set(parsed[2]))
                elif parsed[0] == "value":
                    s.values[parsed[2]] = (parsed[3], ts)
        with s.log_lock:
            if s.logger is not None:
                msg.channel = 0
                s.logger.on_message_received(msg)


class _Poller(threading.Thread):
    def __init__(self, session: "CanSession", rate_hz: float = 25.0):
        super().__init__(daemon=True)
        self.s = session
        self.period = 1.0 / max(rate_hz, 1.0)
        self._stop = threading.Event()

    def run(self) -> None:
        i = 0
        while not self._stop.is_set():
            bus = self.s.bus
            if bus is None:
                break
            try:
                if self.s.scheme is None:
                    for name in ("29bit", "11bit"):
                        bus.send(obd2.make_request(0x00, name))
                    self._stop.wait(0.5)
                    continue
                sched = self.s.poll_schedule()
                if not sched:
                    bus.send(obd2.make_request(0x00, self.s.scheme))
                    self._stop.wait(0.3)
                    continue
                bus.send(obd2.make_request(sched[i % len(sched)], self.s.scheme))
                i += 1
            except can.CanError:
                pass
            self._stop.wait(self.period)

    def stop(self) -> None:
        self._stop.set()


class CanSession:
    def __init__(self):
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.bus: Optional[can.BusABC] = None
        self.notifier: Optional[can.Notifier] = None
        self.poller: Optional[_Poller] = None
        self.logger = None
        self.log_path: Optional[str] = None
        self.mode: Optional[str] = None
        self.scheme: Optional[str] = None
        self.supported: list[int] = []
        self.values: dict[str, tuple[float, float]] = {}
        self.id_meta: dict[int, dict] = {}
        self.history: dict[int, deque] = {}
        self.frame_count = 0
        self.port: Optional[str] = None
        self.transport: Optional[str] = None
        self.bitrate = 500000
        # imported DBC (persists across connections)
        self.db = None
        self.db_name: Optional[str] = None

    # ---- lifecycle ----
    def connect(self, mode="normal", bitrate=500000, port=None,
                transport="usb", host=None) -> dict:
        with self.lock:
            if self.bus is not None:
                raise RuntimeError("already connected")
        if transport == "wifi":
            host = host or "192.168.4.1:3333"
            channel = host if host.startswith("socket://") else f"socket://{host}"
        else:
            channel = port or autodetect_port()
            if not channel:
                raise RuntimeError("no USB serial port found (or connect via Wi-Fi)")
        bus = can.Bus(interface="slcan", channel=channel, bitrate=bitrate,
                      listen_only=(mode == "listen"))
        with self.lock:
            self.bus = bus; self.port = channel; self.transport = transport
            self.bitrate = bitrate; self.mode = mode
            self.scheme = None; self.supported = []; self.values = {}
            self.id_meta = {}; self.history = {}; self.frame_count = 0
        self.notifier = can.Notifier([bus], [_RxListener(self)])
        if mode == "normal":
            self.poller = _Poller(self); self.poller.start()
        return self.status()

    def disconnect(self) -> dict:
        self.stop_recording()
        if self.poller is not None:
            self.poller.stop(); self.poller.join(timeout=1.0); self.poller = None
        if self.notifier is not None:
            try: self.notifier.stop()
            except Exception: pass
            self.notifier = None
        with self.lock:
            bus = self.bus; self.bus = None
        if bus is not None:
            try: bus.shutdown()
            except Exception: pass
        with self.lock:
            self.mode = None; self.scheme = None
        return self.status()

    # ---- recording ----
    def start_recording(self, label="capture") -> str:
        if self.bus is None:
            raise RuntimeError("not connected")
        safe = "".join(c for c in label if c.isalnum() or c in "-_") or "capture"
        path = Path("temp-output") / f"trace_{safe}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_lock:
            if self.logger is not None:
                raise RuntimeError("already recording")
            self.logger = can.Logger(str(path)); self.log_path = str(path)
        return str(path)

    def stop_recording(self) -> Optional[str]:
        with self.log_lock:
            lg, path = self.logger, self.log_path
            self.logger = None; self.log_path = None
        if lg is not None:
            try: lg.stop()
            except Exception: pass
        return path

    # ---- DBC import ----
    def load_dbc(self, text: str, name: str) -> dict:
        db = cantools.database.load_string(text, database_format="dbc")
        with self.lock:
            self.db = db; self.db_name = name
        return self.dbc_info()

    def clear_dbc(self) -> None:
        with self.lock:
            self.db = None; self.db_name = None

    def dbc_info(self) -> dict:
        with self.lock:
            db, name = self.db, self.db_name
        if db is None:
            return {"loaded": False, "messages": []}
        msgs = []
        for m in db.messages:
            fid = m.frame_id
            msgs.append({
                "id": f"0x{fid:0{8 if m.is_extended_frame else 3}X}",
                "id_int": fid, "name": m.name,
                "signals": [s.name for s in m.signals],
            })
        return {"loaded": True, "name": name, "messages": msgs}

    def _decode(self, aid: int, data: bytes) -> dict:
        db = self.db
        if db is None:
            return {}
        try:
            m = db.get_message_by_frame_id(aid)
            dec = m.decode(data, decode_choices=False, allow_truncated=True)
            out = {}
            for k, v in dec.items():
                out[k] = round(float(v), 3) if isinstance(v, (int, float)) else str(v)
            return out
        except Exception:
            return {}

    # ---- polling schedule ----
    def poll_schedule(self) -> list[int]:
        with self.lock:
            sup = list(self.supported)
        disc = [0x00] + ([0x20] if 0x20 in sup else []) + ([0x40] if 0x40 in sup else [])
        data = [p for p in sup if p in obd2.PIDS]
        sched = list(data) + [p for p in data if p in obd2.FAST] * 2
        return sched + disc if sched else disc

    # ---- snapshots ----
    def _supported_names(self) -> list[str]:
        return sorted({obd2.PIDS[p].name for p in self.supported if p in obd2.PIDS})

    def status(self) -> dict:
        with self.lock:
            return {
                "connected": self.bus is not None, "port": self.port, "mode": self.mode,
                "transport": self.transport,
                "scheme": self.scheme, "bitrate": self.bitrate, "recording": self.log_path,
                "frame_count": self.frame_count, "unique_ids": len(self.id_meta),
                "supported": [f"0x{p:02X}" for p in self.supported],
                "supported_names": self._supported_names(),
                "dbc": self.db_name,
            }

    def live(self) -> dict:
        now = time.time()
        with self.lock:
            vals = {name: {"value": round(v, 3), "age": round(now - ts, 3)}
                    for name, (v, ts) in self.values.items()}
            return {"connected": self.bus is not None, "mode": self.mode,
                    "scheme": self.scheme, "recording": self.log_path,
                    "frame_count": self.frame_count, "unique_ids": len(self.id_meta),
                    "supported_names": self._supported_names(), "values": vals}

    def obd2_scan(self) -> dict:
        lv = self.live()
        code = lv["values"].get("obd_std", {}).get("value")
        with self.lock:
            supported = [f"0x{p:02X}" for p in self.supported]
        return {"connected": lv["connected"], "mode": lv["mode"], "scheme": lv["scheme"],
                "supported": supported, "supported_count": len(supported),
                "supported_names": lv["supported_names"],
                "obd_standard": obd2.obd_standard_name(code) if code is not None else None,
                "values": lv["values"]}

    def frames(self) -> dict:
        now = time.time()
        with self.lock:
            connected = self.bus is not None
            rows = []
            for aid, m in self.id_meta.items():
                cyc = (m["last"] - m["first"]) / (m["count"] - 1) * 1000 if m["count"] > 1 else 0.0
                row = {"id": f"0x{aid:0{8 if m['ext'] else 3}X}", "id_int": aid,
                       "ext": m["ext"], "count": m["count"], "cycle_ms": round(cyc, 1),
                       "dlc": m["dlc"], "data": m["data"].hex().upper(),
                       "age": round(now - m["last"], 2),
                       "signals": self._decode(aid, m["data"])}
                rows.append(row)
        rows.sort(key=lambda r: r["id_int"])
        return {"connected": connected, "dbc": self.db_name, "count": len(rows), "rows": rows}

    def history_series(self, aid: int, byte: Optional[int] = None, width: int = 1,
                       signal: Optional[str] = None) -> dict:
        with self.lock:
            samples = list(self.history.get(aid, []))
            db = self.db
        pts = []
        for ts, data in samples:
            v = None
            if signal and db is not None:
                try:
                    v = db.get_message_by_frame_id(aid).decode(
                        data, decode_choices=False, allow_truncated=True).get(signal)
                except Exception:
                    v = None
            elif byte is not None and len(data) >= byte + width:
                v = int.from_bytes(data[byte:byte + width], "big")
            if isinstance(v, (int, float)):
                pts.append([round(ts, 3), float(v)])
        field = signal if signal else (f"byte{byte}" + (f"..{byte + width - 1}" if width > 1 else ""))
        return {"id": f"0x{aid:X}", "field": field, "n": len(pts), "points": pts}
