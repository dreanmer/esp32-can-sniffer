"""
Flask web UI for the ESP32-C6 CAN sniffer — a small admin-style console.

Pages:  /            Dashboard  (connect, record raw+OBD2, key live values)
        /diagnostics OBD2 diagnostics (addressing, standard, supported PIDs)
        /frames      Live raw CAN monitor (filter, plot, import a .dbc to decode)

Run (default localhost:80 needs root on macOS/Linux):
    sudo .venv/bin/python -m webui.app
    PORT=8080 .venv/bin/python -m webui.app        # unprivileged
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from . import obd2  # noqa: F401
from .session import CanSession, autodetect_port

app = Flask(__name__)
session = CanSession()


def _ok(**kw):
    return jsonify({"ok": True, **kw})


def _err(exc, code=400):
    return jsonify({"ok": False, "error": str(exc)}), code


# ---- pages ----
@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/diagnostics")
def diagnostics():
    return render_template("diagnostics.html")


@app.get("/frames")
def frames():
    return render_template("frames.html")


# ---- status / live ----
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


@app.get("/api/obd2")
def api_obd2():
    return jsonify(session.obd2_scan())


@app.get("/api/frames")
def api_frames():
    return jsonify(session.frames())


@app.get("/api/history")
def api_history():
    try:
        aid = int(request.args["id"], 0)
    except (KeyError, ValueError):
        return _err("bad or missing id")
    byte = request.args.get("byte")
    signal = request.args.get("signal") or None
    return jsonify(session.history_series(
        aid,
        byte=int(byte) if byte is not None and byte != "" else None,
        width=int(request.args.get("width", 1)),
        signal=signal,
    ))


# ---- connection / recording ----
@app.post("/api/connect")
def api_connect():
    b = request.get_json(silent=True) or {}
    try:
        return _ok(**session.connect(mode=b.get("mode", "normal"),
                                     bitrate=int(b.get("bitrate", 500000)),
                                     port=b.get("port") or None))
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
    b = request.get_json(silent=True) or {}
    try:
        return _ok(recording=session.start_recording(b.get("label", "capture")))
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
                frames_n = max(0, sum(1 for _ in f) - 1)
        except OSError:
            frames_n = None
        out.append({"name": path.name, "size": path.stat().st_size, "frames": frames_n})
    return jsonify({"ok": True, "logs": out})


# ---- DBC import ----
@app.get("/api/dbc")
def api_dbc_get():
    return jsonify(session.dbc_info())


@app.post("/api/dbc")
def api_dbc_load():
    f = request.files.get("dbc")
    if f is None:
        return _err("no file uploaded (field 'dbc')")
    try:
        text = f.read().decode("utf-8", errors="replace")
        return _ok(**session.load_dbc(text, f.filename or "imported.dbc"))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@app.post("/api/dbc/clear")
def api_dbc_clear():
    session.clear_dbc()
    return _ok()


def main():
    host = os.environ.get("HOST", "localhost")
    port = int(os.environ.get("PORT", "80"))
    print(f"CAN web UI on http://{host}:{port}  (device: {autodetect_port() or 'none detected'})")
    try:
        app.run(host=host, port=port, threaded=True, debug=False)
    except PermissionError:
        print(f"\nPermission denied binding port {port}. Ports < 1024 need root:\n"
              f"  sudo .venv/bin/python -m webui.app\n"
              f"or run unprivileged on another port:\n"
              f"  PORT=8080 .venv/bin/python -m webui.app")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
