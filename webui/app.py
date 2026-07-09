"""
Flask web UI for the ESP32-C6 CAN sniffer.

Serves a live car-style OBD2 dashboard and controls for connecting to the bus and
recording raw + OBD2 logs (webCAN CSV) that the reverse-engineering skill decodes.

Run:
    .venv/bin/python -m webui.app            # http://127.0.0.1:5000
    PORT=5001 HOST=0.0.0.0 .venv/bin/python -m webui.app
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .session import CanSession, autodetect_port

app = Flask(__name__)
session = CanSession()


def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _err(exc, code=400):
    return jsonify({"ok": False, "error": str(exc)}), code


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    st = session.status()
    st["ok"] = True
    st["ports"] = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*")
                         + glob.glob("/dev/ttyUSB*"))
    return jsonify(st)


@app.get("/api/live")
def api_live():
    return jsonify(session.live())


@app.post("/api/connect")
def api_connect():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "normal")
    bitrate = int(body.get("bitrate", 500000))
    port = body.get("port") or None
    try:
        return _ok(**session.connect(mode=mode, bitrate=bitrate, port=port))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@app.post("/api/disconnect")
def api_disconnect():
    try:
        return _ok(**session.disconnect())
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@app.post("/api/record/start")
def api_record_start():
    body = request.get_json(silent=True) or {}
    try:
        path = session.start_recording(body.get("label", "capture"))
        return _ok(recording=path)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@app.post("/api/record/stop")
def api_record_stop():
    try:
        return _ok(saved=session.stop_recording())
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@app.get("/api/logs")
def api_logs():
    out = []
    for p in sorted(glob.glob("temp-output/*.csv")):
        path = Path(p)
        try:
            with open(p, "rb") as f:
                frames = max(0, sum(1 for _ in f) - 1)  # minus header
        except OSError:
            frames = None
        out.append({"name": path.name, "path": p,
                    "size": path.stat().st_size, "frames": frames})
    return jsonify({"ok": True, "logs": out})


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"CAN web UI on http://{host}:{port}  (device: {autodetect_port() or 'none detected'})")
    # threaded=True so the live poll + control requests don't block each other
    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
