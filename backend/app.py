"""
Flask web application for the StereoCam Pi system.

Routes:
  GET  /                        Main UI
  GET  /stream/left             MJPEG preview stream (left camera)
  GET  /stream/right            MJPEG preview stream (right camera)
  POST /capture                 Trigger synchronized stereo capture
  GET  /status                  JSON camera/system status
  GET  /captures                JSON list of all sessions
  GET  /captures/<date>/<sess>  JSON session metadata
  GET  /images/<date>/<sess>/<kind>  Serve left|right|anaglyph JPEG
  POST /reprocess               Re-generate anaglyph for existing session
  GET  /focus                   Return stored focus_dioptre and live lens position
  POST /focus                   Set focus (dioptre float) or null for auto AF lock
"""

import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_start_time = time.monotonic()

import cv2
from flask import Flask, Response, jsonify, request, send_file

# Make backend/ importable when run directly
sys.path.insert(0, str(Path(__file__).parent))

from anaglyph import AnaglyphMethod, generate_anaglyph, reprocess_pair
from camera import CameraError, StereoCamera
from config import config, save_config
from storage import ImageStorage, StorageError

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

log_path = Path(config["storage"]["base_path"]).parent / "logs" / "app.log"
log_path.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_path)),
    ],
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------

_frontend = Path(__file__).parent.parent / "frontend"

app = Flask(
    __name__,
    template_folder=str(_frontend),
    static_folder=str(_frontend / "static"),
    static_url_path="/static",
)

# Global singletons
stereo_cam = StereoCamera(config)
storage = ImageStorage(config["storage"]["base_path"])
last_capture: dict = {}

# ------------------------------------------------------------------
# MJPEG helpers
# ------------------------------------------------------------------

def _mjpeg_generator(side: str):
    target_interval = 1.0 / 30  # 30 fps cap
    while True:
        t0 = time.monotonic()
        left, right = stereo_cam.get_preview_frames()
        frame = left if side == "left" else right
        if frame is None:
            time.sleep(0.05)
            continue
        jpeg = stereo_cam.encode_jpeg(frame, quality=70)
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        elapsed = time.monotonic() - t0
        sleep_for = max(0.0, target_interval - elapsed)
        if sleep_for:
            time.sleep(sleep_for)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    html_path = _frontend / "index.html"
    return html_path.read_text()


@app.route("/stream/left")
def stream_left():
    return Response(
        _mjpeg_generator("left"),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/stream/right")
def stream_right():
    return Response(
        _mjpeg_generator("right"),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/status")
def status():
    left, right = stereo_cam.get_preview_frames()
    return jsonify(
        {
            "cameras_ready": left is not None and right is not None,
            "last_capture": last_capture or None,
        }
    )


@app.route("/capture", methods=["POST"])
def capture():
    global last_capture
    try:
        logger.info("Capture request received")
        ts = datetime.now()

        stereo_cam.apply_focus(config["cameras"].get("focus_dioptre"))
        left_bgr, right_bgr = stereo_cam.capture_synchronized()

        method_key = config.get("anaglyph", {}).get("method", "color").upper()
        try:
            method = AnaglyphMethod[method_key]
        except KeyError:
            method = AnaglyphMethod.COLOR

        anaglyph = generate_anaglyph(left_bgr, right_bgr, method)
        result = storage.save_capture(left_bgr, right_bgr, {"anaglyph": anaglyph}, ts)
        last_capture = result

        logger.info("Capture saved: %s/%s", result["date"], result["session"])
        return jsonify({"status": "ok", "capture": result})

    except (CameraError, StorageError) as exc:
        logger.error("Capture failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    except Exception as exc:
        logger.exception("Unexpected capture error")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/health")
def health():
    left, right = stereo_cam.get_preview_frames()
    cameras_ready = left is not None and right is not None
    return jsonify(
        {
            "status": "ok" if cameras_ready else "degraded",
            "cameras_ready": cameras_ready,
            "uptime_seconds": round(time.monotonic() - _start_time),
        }
    ), 200 if cameras_ready else 503


_RECONNECT = {
    "ap":     {"url": "http://192.168.4.1:8080",   "ssid": "StereoCamPi"},
    "client": {"url": "http://pi5.fritz.box:8080", "ssid": "dungy24"},
}


@app.route("/wifi/status")
def wifi_status():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "con", "show", "--active"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            name, _, device = line.rpartition(":")
            if device == "wlan0":
                if name == "dungy24":
                    return jsonify({"mode": "client", "ssid": "dungy24"})
                if name == "StereoCamPi-AP":
                    return jsonify({"mode": "ap", "ssid": "StereoCamPi"})
        return jsonify({"mode": "unknown", "ssid": None})
    except Exception as exc:
        logger.error("wifi_status error: %s", exc)
        return jsonify({"mode": "unknown", "ssid": None})


@app.route("/wifi/switch", methods=["POST"])
def wifi_switch():
    data = request.get_json(force=True)
    target = data.get("target")
    if target not in ("ap", "client"):
        return jsonify({"status": "error", "message": "target must be 'ap' or 'client'"}), 400

    def _do_switch():
        time.sleep(2)
        try:
            if target == "ap":
                subprocess.run(["sudo", "nmcli", "con", "down", "dungy24"],        timeout=10)
                subprocess.run(["sudo", "nmcli", "con", "up",   "StereoCamPi-AP"], timeout=15)
            else:
                subprocess.run(["sudo", "nmcli", "con", "down", "StereoCamPi-AP"], timeout=10)
                subprocess.run(["sudo", "nmcli", "con", "up",   "dungy24"],        timeout=15)
            logger.info("WiFi switched to %s", target)
        except Exception as exc:
            logger.error("WiFi switch failed: %s", exc)

    threading.Thread(target=_do_switch, daemon=True).start()
    return jsonify({"status": "ok", "target": target, "reconnect": _RECONNECT[target]})


@app.route("/captures")
def list_captures():
    return jsonify(storage.list_captures())


@app.route("/captures/<date>/<session>")
def get_session(date, session):
    meta = storage.get_session(date, session)
    if not meta:
        return jsonify({"error": "not found"}), 404
    return jsonify(meta)


@app.route("/images/<date>/<session>/<kind>")
def serve_image(date, session, kind):
    if kind not in ("left", "right", "anaglyph"):
        return "Invalid image kind", 400
    path = storage.get_image_path(date, session, kind)
    if not path:
        return "Not found", 404
    return send_file(str(path), mimetype="image/jpeg")


@app.route("/focus", methods=["GET"])
def get_focus():
    return jsonify({
        "focus_dioptre": config["cameras"].get("focus_dioptre"),
        "current_lens_position": stereo_cam.get_lens_position(),
    })


@app.route("/focus", methods=["POST"])
def set_focus():
    data = request.get_json(force=True)
    raw = data.get("focus_dioptre")

    if raw is None:
        dioptre = None
    else:
        try:
            dioptre = float(raw)
            if dioptre < 0:
                return jsonify({"status": "error", "message": "focus_dioptre must be >= 0"}), 400
        except (ValueError, TypeError):
            return jsonify({"status": "error", "message": "focus_dioptre must be a number"}), 400

    config["cameras"]["focus_dioptre"] = dioptre
    save_config(config)
    stereo_cam.apply_focus(dioptre)

    logger.info("Focus updated: %s", f"{dioptre} dioptre" if dioptre is not None else "auto AF lock")
    return jsonify({"status": "ok", "focus_dioptre": dioptre})


@app.route("/reprocess", methods=["POST"])
def reprocess():
    data = request.get_json(force=True)
    date = data.get("date")
    session = data.get("session")
    method_key = data.get("method", "color").upper()

    if not date or not session:
        return jsonify({"status": "error", "message": "date and session required"}), 400

    left_path = storage.get_image_path(date, session, "left")
    right_path = storage.get_image_path(date, session, "right")
    if not left_path or not right_path:
        return jsonify({"status": "error", "message": "Source images not found"}), 404

    try:
        method = AnaglyphMethod[method_key]
    except KeyError:
        method = AnaglyphMethod.COLOR

    anaglyph = reprocess_pair(str(left_path), str(right_path), method)
    out_path = storage.get_image_path(date, session, "anaglyph") or (
        left_path.parent / left_path.name.replace("_left.jpg", "_anaglyph.jpg")
    )
    cv2.imwrite(str(out_path), anaglyph, [cv2.IMWRITE_JPEG_QUALITY, 95])

    logger.info("Reprocessed %s/%s with method %s", date, session, method_key)
    return jsonify({"status": "ok"})


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Initialising cameras…")
    stereo_cam.initialize()
    stereo_cam.start_streaming()
    logger.info("Starting Flask on %s:%s", config["server"]["host"], config["server"]["port"])
    app.run(
        host=config["server"]["host"],
        port=config["server"]["port"],
        debug=config["server"]["debug"],
        threaded=True,
        use_reloader=False,
    )
