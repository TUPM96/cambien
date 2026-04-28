"""
Dashboard MQTT cho gateway LoRa: topic mặc định sensor/lora/data (JSON từ gateway).
Chạy qua entry: python run_all.py
Lịch sử: thư mục data/lora_history_YYYY-MM-DD.csv (UTF-8 BOM, mở được Excel).
"""

import csv
import json
import os
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

MQTT_HOST = os.getenv("MQTT_HOST", "103.146.22.13")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "user1")
MQTT_PASS = os.getenv("MQTT_PASS", "12345678")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "sensor/lora/data")
CAPTURE_EXPECTED_INTERVAL_SEC = float(os.getenv("CAPTURE_EXPECTED_INTERVAL_SEC", "13"))

DATA_DIR = BASE_DIR / "data"
CSV_LOG_ENABLED = os.getenv("CSV_LOG", "1").lower() not in ("0", "false", "no")
CSV_FIELDNAMES = (
    "timestamp",
    "sensor_id",
    "temperature",
    "humidity",
    "water_temperature",
    "tds",
    "rssi",
    "snr",
    "packet_id",
    "valid_mask",
    "air_temp_valid",
    "humidity_valid",
    "water_temp_valid",
    "tds_valid",
    "signal_quality",
    "rssi_min",
    "rssi_max",
    "packet_count",
    "gw_timestamp_ms",
)

MAX_POINTS = 120
_lock = threading.Lock()
_csv_lock = threading.Lock()
_history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=MAX_POINTS))
_latest: dict[str, dict[str, Any]] = {}
_capture_stats: dict[str, dict[str, Any]] = {}

_mqtt_thread_started = False
_mqtt_start_lock = threading.Lock()


def _parse_node_intervals(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in (raw or "").split(","):
        part = item.strip()
        if not part or "=" not in part:
            continue
        sid, val = part.split("=", 1)
        sid = sid.strip()
        if not sid:
            continue
        try:
            sec = float(val.strip())
            if sec > 0:
                out[sid] = sec
        except ValueError:
            continue
    return out


CAPTURE_INTERVAL_BY_NODE = _parse_node_intervals(os.getenv("CAPTURE_INTERVAL_BY_NODE", ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _csv_path_today() -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    return DATA_DIR / f"lora_history_{day}.csv"


def _append_history_csv(data: dict[str, Any], latest: dict[str, Any], sid: str) -> None:
    if not CSV_LOG_ENABLED:
        return
    row = {
        "timestamp": latest["t"],
        "sensor_id": sid,
        "temperature": latest["temperature"],
        "humidity": latest["humidity"],
        "water_temperature": latest["water_temperature"],
        "tds": latest["tds"],
        "rssi": latest["rssi"],
        "snr": latest["snr"],
        "packet_id": latest.get("packet_id"),
        "valid_mask": latest.get("valid_mask"),
        "air_temp_valid": latest.get("air_temp_valid"),
        "humidity_valid": latest.get("humidity_valid"),
        "water_temp_valid": latest.get("water_temp_valid"),
        "tds_valid": latest.get("tds_valid"),
        "signal_quality": latest.get("signal_quality") or "",
        "rssi_min": latest.get("rssi_min"),
        "rssi_max": latest.get("rssi_max"),
        "packet_count": latest.get("packet_count"),
        "gw_timestamp_ms": data.get("timestamp"),
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = _csv_path_today()
        with _csv_lock:
            new_file = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
                if new_file:
                    w.writeheader()
                w.writerow(row)
    except OSError as e:
        print(f"CSV log lỗi: {e}")


def _handle_payload(raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    sid = str(data.get("sensor_id", "unknown"))
    lora = data.get("lora_signal") or {}
    sensor_status = data.get("sensor_status") or {}
    valid_mask = int(data.get("valid_mask", 0))
    has_valid_info = bool(sensor_status) or ("valid_mask" in data)
    default_valid = True

    air_temp_valid = bool(sensor_status.get("air_temp", (valid_mask & 0x01) != 0 if has_valid_info else default_valid))
    humidity_valid = bool(sensor_status.get("humidity", (valid_mask & 0x02) != 0 if has_valid_info else default_valid))
    water_temp_valid = bool(sensor_status.get("water_temp", (valid_mask & 0x04) != 0 if has_valid_info else default_valid))
    tds_valid = bool(sensor_status.get("tds", (valid_mask & 0x08) != 0 if has_valid_info else default_valid))

    point = {
        "t": _now_iso(),
        "temperature": float(data.get("temperature", 0)),
        "humidity": float(data.get("humidity", 0)),
        "water_temperature": float(data.get("water_temperature", 0)),
        "tds": float(data.get("tds", 0)),
        "valid_mask": valid_mask,
        "air_temp_valid": air_temp_valid,
        "humidity_valid": humidity_valid,
        "water_temp_valid": water_temp_valid,
        "tds_valid": tds_valid,
        "rssi": int(lora.get("rssi", 0)),
        "snr": float(lora.get("snr", 0)),
        "packet_id": data.get("packet_id"),
        "signal_quality": data.get("signal_quality", ""),
    }
    latest = {
        **point,
        "rssi_min": lora.get("rssi_min"),
        "rssi_max": lora.get("rssi_max"),
        "packet_count": lora.get("packet_count"),
    }

    with _lock:
        _history[sid].append(point)
        _latest[sid] = latest
        stats = _capture_stats.get(sid)
        if stats is None:
            _capture_stats[sid] = {
                "first_seen": point["t"],
                "last_seen": point["t"],
                "received_total": 1,
            }
        else:
            stats["last_seen"] = point["t"]
            stats["received_total"] = int(stats.get("received_total", 0)) + 1

    _append_history_csv(data, latest, sid)


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code.is_failure:
        print(f"MQTT connect issue: {reason_code}")
        return
    client.subscribe(MQTT_TOPIC, qos=0)
    cid = userdata if isinstance(userdata, str) else "?"
    print(f"MQTT đã kết nối, đăng ký 1 topic: {MQTT_TOPIC} (client_id={cid})")


def on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode("utf-8", errors="replace")
        _handle_payload(raw)
    except Exception as e:
        print("on_message error:", e)


def start_mqtt():
    global _mqtt_thread_started
    with _mqtt_start_lock:
        if _mqtt_thread_started:
            return
        _mqtt_thread_started = True

    cid = f"lora_web_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=cid,
        protocol=mqtt.MQTTv311,
        userdata=cid,
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS or "")
    client.on_connect = on_connect
    client.on_message = on_message

    def run():
        while True:
            try:
                client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                client.loop_forever()
            except Exception as e:
                print(f"MQTT reconnect in 5s: {e}")
                time.sleep(5)

    threading.Thread(target=run, daemon=True, name="mqtt").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_mqtt()
    yield


app = FastAPI(title="LoRa MQTT Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"mqtt_topic": MQTT_TOPIC},
    )


@app.get("/api/state")
def api_state():
    with _lock:
        nodes = {}
        for sid, dq in _history.items():
            nodes[sid] = {
                "latest": _latest.get(sid),
                "history": list(dq),
                "capture": dict(_capture_stats.get(sid, {})),
            }
        for sid, lat in _latest.items():
            if sid not in nodes:
                nodes[sid] = {
                    "latest": lat,
                    "history": list(_history[sid]),
                    "capture": dict(_capture_stats.get(sid, {})),
                }
    return {
        "nodes": nodes,
        "topic": MQTT_TOPIC,
        "capture_config": {
            "expected_interval_sec": CAPTURE_EXPECTED_INTERVAL_SEC,
            "interval_by_node": CAPTURE_INTERVAL_BY_NODE,
        },
    }


def _pick_listen_port(preferred: int, span: int = 40) -> int:
    last_err: OSError | None = None
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError as e:
                last_err = e
                continue
            return port
    msg = f"Không có cổng trống trong [{preferred}, {preferred + span - 1}]"
    if last_err:
        msg += f": {last_err}"
    raise OSError(msg)


if __name__ == "__main__":
    import uvicorn

    want = int(os.getenv("WEB_PORT", "8000"))
    port = _pick_listen_port(want)
    if port != want:
        print(f"Cổng {want} đang bận → dùng cổng {port}")
    print(f"Mở trình duyệt: http://127.0.0.1:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

