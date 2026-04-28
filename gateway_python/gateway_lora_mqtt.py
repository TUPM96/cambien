import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import paho.mqtt.client as mqtt
from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def _parse_csv_set(raw: str) -> set[str]:
    return {p.strip().lower() for p in (raw or "").split(",") if p.strip()}


def _parse_payload(payload: str, zero_invalid_fields: set[str]) -> Optional[dict]:
    """
    ESP32 format:
      - new: ID:airTemp:hum:waterTemp:tds:validMask
      - old: ID:airTemp:hum
    """
    parts = payload.strip().split(":")
    if len(parts) < 3:
        return None

    sensor_id = parts[0].strip()
    if not sensor_id:
        return None

    def _to_float(x: str) -> float:
        try:
            return float(x)
        except ValueError:
            return 0.0

    air_temp = _to_float(parts[1])
    hum = _to_float(parts[2])

    water_temp = _to_float(parts[3]) if len(parts) >= 4 and parts[3] != "" else 0.0
    tds = _to_float(parts[4]) if len(parts) >= 5 and parts[4] != "" else 0.0

    explicit_mask = False
    if len(parts) >= 6 and parts[5] != "":
        try:
            valid_mask = int(float(parts[5]))
            explicit_mask = True
        except ValueError:
            valid_mask = 0
    else:
        # Fallback: nếu node KHÔNG gửi validMask, ta suy luận hợp lệ dựa trên dữ liệu.
        # Mặc định giống ESP32: có đủ field -> coi hợp lệ, nhưng có thể cấu hình "0 = mất dữ liệu".
        valid_mask = 0
        if not (("temperature" in zero_invalid_fields) and air_temp == 0.0):
            valid_mask |= 0x01
        if not (("humidity" in zero_invalid_fields) and hum == 0.0):
            valid_mask |= 0x02

        if len(parts) >= 4:
            if ("water_temperature" in zero_invalid_fields) and water_temp == 0.0:
                pass
            else:
                valid_mask |= 0x04
        if len(parts) >= 5:
            if ("tds" in zero_invalid_fields) and tds == 0.0:
                pass
            else:
                valid_mask |= 0x08

    return {
        "sensor_id": sensor_id,
        "temperature": air_temp,
        "humidity": hum,
        "water_temperature": water_temp,
        "tds": tds,
        "valid_mask": valid_mask & 0xFF,
        "explicit_mask": explicit_mask,
    }


@dataclass
class LoRaStats:
    last_rssi: int = 0
    last_snr: float = 0.0
    min_rssi: int = 0
    max_rssi: int = -200
    packet_count: int = 0
    last_packet_ms: int = 0


class LoRaMQTTGateway:
    def __init__(self) -> None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        load_dotenv(os.path.join(base_dir, ".env"))

        self.mqtt_host = os.getenv("MQTT_HOST", "103.146.22.13")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER", "user1")
        self.mqtt_pass = os.getenv("MQTT_PASS", "12345678")
        self.mqtt_topic = os.getenv("MQTT_TOPIC", "sensor/lora/data")

        self.lora_frequency_mhz = float(os.getenv("LORA_FREQUENCY_MHZ", "433.0"))
        self.lora_spi_cs = os.getenv("LORA_SPI_CS", "CE0").strip().upper()
        self.lora_reset_bcm = int(os.getenv("LORA_RESET_BCM", "22"))
        self.lora_rx_timeout_sec = float(os.getenv("LORA_RX_TIMEOUT_SEC", "0.2"))
        self.lora_lost_signal_sec = float(os.getenv("LORA_LOST_SIGNAL_SEC", "60"))

        self.print_packets = _env_bool("LORA_PRINT_PACKETS", True)
        # Nếu node không gửi validMask, coi giá trị 0 của các field này là "mất dữ liệu".
        # Ví dụ: "water_temperature,tds"
        self.zero_invalid_fields = _parse_csv_set(os.getenv("FALLBACK_ZERO_INVALID_FIELDS", "water_temperature,tds"))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.stats = LoRaStats()

        self._mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"lora_gw_pi_{os.getpid()}",
            protocol=mqtt.MQTTv311,
        )
        if self.mqtt_user:
            self._mqtt.username_pw_set(self.mqtt_user, self.mqtt_pass or "")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="lora-gateway")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._mqtt.disconnect()
        except Exception:
            pass

    def _mqtt_connect(self) -> bool:
        for _ in range(5):
            try:
                self._mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
                return True
            except Exception:
                time.sleep(2)
        return False

    def _publish(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        self._mqtt.publish(self.mqtt_topic, payload, qos=0, retain=False)

    def _init_lora(self):
        # Import lazily so dev machines without GPIO libs can still import this module.
        import board  # type: ignore
        import busio  # type: ignore
        import digitalio  # type: ignore
        import adafruit_rfm9x  # type: ignore

        spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)

        if self.lora_spi_cs == "CE1":
            cs_pin = board.CE1
        else:
            cs_pin = board.CE0
        cs = digitalio.DigitalInOut(cs_pin)

        reset_pin = getattr(board, f"D{self.lora_reset_bcm}", None)
        if reset_pin is None:
            # Fallback: some Blinka builds expose BCM pins as GPIOxx
            reset_pin = getattr(board, f"GPIO{self.lora_reset_bcm}")
        reset = digitalio.DigitalInOut(reset_pin)

        rfm9x = adafruit_rfm9x.RFM9x(spi, cs, reset, self.lora_frequency_mhz)
        return rfm9x

    def _read_packet(self, rfm9x) -> Tuple[Optional[str], int, float]:
        pkt = rfm9x.receive(timeout=self.lora_rx_timeout_sec)
        if pkt is None:
            return None, 0, 0.0
        try:
            text = pkt.decode("utf-8", errors="replace").strip()
        except Exception:
            text = str(pkt)

        rssi = int(getattr(rfm9x, "rssi", 0) or 0)
        snr = float(getattr(rfm9x, "snr", getattr(rfm9x, "last_snr", 0.0)) or 0.0)
        return text, rssi, snr

    def _signal_quality(self, rssi: int, snr: float) -> str:
        # Simple heuristic for UI; web can display this string.
        if rssi >= -70 and snr >= 7:
            return "excellent"
        if rssi >= -85 and snr >= 5:
            return "good"
        if rssi >= -100 and snr >= 2:
            return "fair"
        if rssi >= -115:
            return "poor"
        return "very weak"

    def _run(self) -> None:
        if not self._mqtt_connect():
            print("MQTT connect failed (gateway).")
            return
        self._mqtt.loop_start()

        try:
            rfm9x = self._init_lora()
        except Exception as e:
            print(f"LoRa init failed on Pi: {e}")
            return

        self.stats.last_packet_ms = _monotonic_ms()
        print(f"LoRa gateway ready on Pi (freq={self.lora_frequency_mhz}MHz, CS={self.lora_spi_cs})")

        while not self._stop.is_set():
            msg, rssi, snr = self._read_packet(rfm9x)
            now_ms = _monotonic_ms()

            if msg:
                parsed = _parse_payload(msg, self.zero_invalid_fields)
                if not parsed:
                    if self.print_packets:
                        print(f"Invalid payload: {msg}")
                    continue

                self.stats.packet_count += 1
                self.stats.last_rssi = rssi
                self.stats.last_snr = snr
                self.stats.last_packet_ms = now_ms
                if self.stats.packet_count == 1:
                    self.stats.min_rssi = rssi
                    self.stats.max_rssi = rssi
                else:
                    self.stats.min_rssi = min(self.stats.min_rssi, rssi)
                    self.stats.max_rssi = max(self.stats.max_rssi, rssi)

                valid_mask = int(parsed["valid_mask"])
                out = {
                    "sensor_id": parsed["sensor_id"],
                    "temperature": parsed["temperature"],
                    "humidity": parsed["humidity"],
                    "water_temperature": parsed["water_temperature"],
                    "tds": parsed["tds"],
                    "timestamp": now_ms,  # giống ESP32 (millis), nhưng ở Pi dùng monotonic ms
                    "packet_id": self.stats.packet_count,
                    "valid_mask": valid_mask,
                    "sensor_status": {
                        "air_temp": (valid_mask & 0x01) != 0,
                        "humidity": (valid_mask & 0x02) != 0,
                        "water_temp": (valid_mask & 0x04) != 0,
                        "tds": (valid_mask & 0x08) != 0,
                    },
                    "lora_signal": {
                        "rssi": rssi,
                        "snr": snr,
                        "rssi_min": self.stats.min_rssi,
                        "rssi_max": self.stats.max_rssi,
                        "packet_count": self.stats.packet_count,
                    },
                    "signal_quality": self._signal_quality(rssi, snr),
                }

                self._publish(out)
                if self.print_packets:
                    print(f"LoRa rx: {msg} | rssi={rssi} snr={snr:.2f} -> MQTT {self.mqtt_topic}")

            if self.lora_lost_signal_sec > 0:
                idle_sec = (now_ms - self.stats.last_packet_ms) / 1000.0
                if idle_sec > self.lora_lost_signal_sec:
                    print(f"WARNING: No LoRa packet for {self.lora_lost_signal_sec:.0f}s")
                    self.stats.last_packet_ms = now_ms

            time.sleep(0.05)
