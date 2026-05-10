import csv
import os
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "web" / "data"
CSV_LOG_ENABLED = os.getenv("CSV_LOG", "1").lower() not in ("0", "false", "no")
CAPTURE_EXPECTED_INTERVAL_SEC = float(os.getenv("CAPTURE_EXPECTED_INTERVAL_SEC", "13"))
MAX_POINTS = 120

CSV_FIELDNAMES = (
    "timestamp",
    "sensor_id",
    "temperature",
    "humidity",
    "water_temperature",
    "tds",
    "ph",
    "rssi",
    "snr",
    "packet_id",
    "valid_mask",
    "air_temp_valid",
    "humidity_valid",
    "water_temp_valid",
    "tds_valid",
    "ph_valid",
    "signal_quality",
    "rssi_min",
    "rssi_max",
    "packet_count",
    "gw_timestamp_ms",
)

_lock = threading.Lock()
_csv_lock = threading.Lock()
_history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=MAX_POINTS))
_latest: dict[str, dict[str, Any]] = {}
_capture_stats: dict[str, dict[str, Any]] = {}


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
        "ph": latest["ph"],
        "rssi": latest["rssi"],
        "snr": latest["snr"],
        "packet_id": latest.get("packet_id"),
        "valid_mask": latest.get("valid_mask"),
        "air_temp_valid": latest.get("air_temp_valid"),
        "humidity_valid": latest.get("humidity_valid"),
        "water_temp_valid": latest.get("water_temp_valid"),
        "tds_valid": latest.get("tds_valid"),
        "ph_valid": latest.get("ph_valid"),
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
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
    except OSError as e:
        print(f"CSV log loi: {e}")


def ingest_gateway_message(data: dict[str, Any]) -> None:
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
    ph_valid = bool(sensor_status.get("ph", (valid_mask & 0x10) != 0 if has_valid_info else default_valid))

    point = {
        "t": _now_iso(),
        "temperature": float(data.get("temperature", 0)),
        "humidity": float(data.get("humidity", 0)),
        "water_temperature": float(data.get("water_temperature", 0)),
        "tds": float(data.get("tds", 0)),
        "ph": float(data.get("ph", 0)),
        "valid_mask": valid_mask,
        "air_temp_valid": air_temp_valid,
        "humidity_valid": humidity_valid,
        "water_temp_valid": water_temp_valid,
        "tds_valid": tds_valid,
        "ph_valid": ph_valid,
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


def get_state() -> dict[str, Any]:
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
        "source": "Gateway noi bo",
        "capture_config": {
            "expected_interval_sec": CAPTURE_EXPECTED_INTERVAL_SEC,
            "interval_by_node": CAPTURE_INTERVAL_BY_NODE,
        },
    }
