"""
OBD2 (SAE J1979) mode-01 helpers: PID decode table, request framing, response
parsing and supported-PID bitmask decoding. Formulas cross-checked against the
bundled OBD-v4.4.dbc and the SAE/Wikipedia OBD-II PID table.

Supports both addressing schemes:
  * 11-bit: request 0x7DF          -> responses 0x7E8..0x7EF
  * 29-bit: request 0x18DB33F1     -> responses 0x18DAF1xx   (the Fiat 500 uses this)

Signal names match the web dashboard's gauge/tile keys.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import can


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    nbytes: int
    unit: str
    vmin: float
    vmax: float
    decode: Callable[[bytes], float]   # receives data bytes A,B,... (after the mode/PID echo)


def _u16(d: bytes) -> int:
    return 256 * d[0] + d[1]


# The dashboard-relevant standard PIDs (name == dashboard key where one exists).
PIDS: dict[int, Pid] = {
    0x04: Pid(0x04, "load",          1, "%",    0, 100,       lambda d: d[0] * 100 / 255),
    0x05: Pid(0x05, "coolant",       1, "degC", -40, 215,     lambda d: d[0] - 40),
    0x0A: Pid(0x0A, "fuel_pressure", 1, "kPa",  0, 765,       lambda d: d[0] * 3),
    0x0B: Pid(0x0B, "map",           1, "kPa",  0, 255,       lambda d: d[0]),
    0x0C: Pid(0x0C, "rpm",           2, "rpm",  0, 16383.75,  lambda d: _u16(d) / 4),
    0x0D: Pid(0x0D, "speed",         1, "km/h", 0, 255,       lambda d: d[0]),
    0x0E: Pid(0x0E, "timing",        1, "deg",  -64, 63.5,    lambda d: d[0] / 2 - 64),
    0x0F: Pid(0x0F, "intake",        1, "degC", -40, 215,     lambda d: d[0] - 40),
    0x10: Pid(0x10, "maf",           2, "g/s",  0, 655.35,    lambda d: _u16(d) / 100),
    0x11: Pid(0x11, "throttle",      1, "%",    0, 100,       lambda d: d[0] * 100 / 255),
    0x1F: Pid(0x1F, "runtime",       2, "s",    0, 65535,     lambda d: _u16(d)),
    0x2F: Pid(0x2F, "fuel",          1, "%",    0, 100,       lambda d: d[0] * 100 / 255),
    0x33: Pid(0x33, "baro",          1, "kPa",  0, 255,       lambda d: d[0]),
    0x42: Pid(0x42, "volt",          2, "V",    0, 65.535,    lambda d: _u16(d) / 1000),
    0x46: Pid(0x46, "ambient",       1, "degC", -40, 215,     lambda d: d[0] - 40),
    0x5C: Pid(0x5C, "oil_temp",      1, "degC", -40, 215,     lambda d: d[0] - 40),
    0x5E: Pid(0x5E, "fuel_rate",     2, "L/h",  0, 3276.75,   lambda d: _u16(d) / 20),
}

# Poll these more often (they drive the primary gauges / change fast).
FAST = {0x0C, 0x0D, 0x11, 0x04, 0x10}

# Supported-PID bitmask query PIDs (each returns a 32-bit mask for the next block).
BITMASK_PIDS = {0x00, 0x20, 0x40, 0x60, 0x80}

SCHEMES = {
    "29bit": {"req": 0x18DB33F1, "ext": True,
              "is_resp": lambda i: (i & 0xFFFFFF00) == 0x18DAF100},
    "11bit": {"req": 0x7DF, "ext": False,
              "is_resp": lambda i: 0x7E8 <= i <= 0x7EF},
}


def make_request(pid: int, scheme: str) -> can.Message:
    sc = SCHEMES[scheme]
    return can.Message(arbitration_id=sc["req"], is_extended_id=sc["ext"],
                       data=[0x02, 0x01, pid, 0, 0, 0, 0, 0])


def classify(msg: can.Message) -> Optional[str]:
    """Return the scheme name if msg is an OBD2 response, else None."""
    i = msg.arbitration_id
    if msg.is_extended_id and (i & 0xFFFFFF00) == 0x18DAF100:
        return "29bit"
    if not msg.is_extended_id and 0x7E8 <= i <= 0x7EF:
        return "11bit"
    return None


def parse_bitmask(base: int, data: bytes) -> list[int]:
    """Decode a supported-PID bitmask response (data = [PCI,0x41,base,A,B,C,D])."""
    if len(data) < 7:
        return []
    mask = int.from_bytes(data[3:7], "big")
    return [base + 1 + k for k in range(32) if mask & (1 << (31 - k))]


def parse_response(msg: can.Message):
    """Decode a mode-01 response frame.

    Returns one of:
      ("bitmask", base_pid, [supported pids])   for 0x00/0x20/0x40/...
      ("value", pid, name, value)               for a known data PID
      None                                       if not a decodable OBD2 response
    """
    if classify(msg) is None:
        return None
    d = bytes(msg.data)
    if len(d) < 3 or d[1] != 0x41:
        return None
    pid = d[2]
    if pid in BITMASK_PIDS:
        return ("bitmask", pid, parse_bitmask(pid, d))
    p = PIDS.get(pid)
    if p is None or len(d) < 3 + p.nbytes:
        return None
    try:
        return ("value", pid, p.name, float(p.decode(d[3:3 + p.nbytes])))
    except Exception:
        return None
