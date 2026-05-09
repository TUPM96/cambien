import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

_REG_MODEM_CONFIG1 = 0x1D
_REG_MODEM_CONFIG2 = 0x1E
_REG_MODEM_CONFIG3 = 0x26
_REG_SYNC_WORD = 0x39
_SENSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SX127xSpidev:
    REG_FIFO = 0x00
    REG_OP_MODE = 0x01
    REG_FRF_MSB = 0x06
    REG_FRF_MID = 0x07
    REG_FRF_LSB = 0x08
    REG_LNA = 0x0C
    REG_FIFO_ADDR_PTR = 0x0D
    REG_FIFO_TX_BASE_ADDR = 0x0E
    REG_FIFO_RX_BASE_ADDR = 0x0F
    REG_FIFO_RX_CURRENT_ADDR = 0x10
    REG_IRQ_FLAGS = 0x12
    REG_RX_NB_BYTES = 0x13
    REG_PKT_SNR_VALUE = 0x19
    REG_PKT_RSSI_VALUE = 0x1A
    REG_PREAMBLE_MSB = 0x20
    REG_PREAMBLE_LSB = 0x21
    REG_DETECTION_OPTIMIZE = 0x31
    REG_DETECTION_THRESHOLD = 0x37
    REG_VERSION = 0x42

    MODE_LONG_RANGE = 0x80
    MODE_LOW_FREQUENCY = 0x08
    MODE_SLEEP = 0x00
    MODE_STDBY = 0x01
    MODE_RX_CONTINUOUS = 0x05

    IRQ_RX_DONE = 0x40
    IRQ_PAYLOAD_CRC_ERROR = 0x20

    BW_BINS = (7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000)

    def __init__(
        self,
        *,
        bus: int,
        device: int,
        frequency_mhz: float,
        baudrate: int,
        reset_bcm: int,
        spreading_factor: int,
        signal_bandwidth: int,
        coding_rate: int,
        preamble_length: int,
        sync_word: int,
        crc: bool,
        agc: bool,
    ) -> None:
        import spidev  # type: ignore

        self.frequency_mhz = frequency_mhz
        self.last_rssi = 0
        self.last_snr = 0.0

        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = baudrate
        self.spi.mode = 0

        self._pulse_reset(reset_bcm)
        if self.read_u8(self.REG_VERSION) != 0x12:
            version = self.read_u8(self.REG_VERSION)
            raise RuntimeError(f"SX127x not found on SPI{bus}.{device}: version=0x{version:02X}, expected 0x12")

        self._configure(
            spreading_factor=spreading_factor,
            signal_bandwidth=signal_bandwidth,
            coding_rate=coding_rate,
            preamble_length=preamble_length,
            sync_word=sync_word,
            crc=crc,
            agc=agc,
        )

    def _pulse_reset(self, reset_bcm: int) -> None:
        try:
            import RPi.GPIO as GPIO  # type: ignore

            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(reset_bcm, GPIO.OUT, initial=GPIO.HIGH)
            time.sleep(0.01)
            GPIO.output(reset_bcm, GPIO.LOW)
            time.sleep(0.001)
            GPIO.output(reset_bcm, GPIO.HIGH)
            time.sleep(0.01)
        except Exception:
            # RST can be tied high; SPI configuration below is enough after power-up.
            time.sleep(0.01)

    def read_u8(self, address: int) -> int:
        return int(self.spi.xfer2([address & 0x7F, 0x00])[1]) & 0xFF

    def write_u8(self, address: int, value: int) -> None:
        self.spi.xfer2([(address | 0x80) & 0xFF, value & 0xFF])

    def read_fifo(self, length: int) -> bytes:
        if length <= 0:
            return b""
        return bytes(self.spi.xfer2([self.REG_FIFO & 0x7F] + [0x00] * length)[1:])

    def _set_mode(self, mode: int) -> None:
        lf = self.MODE_LOW_FREQUENCY if self.frequency_mhz < 525 else 0x00
        self.write_u8(self.REG_OP_MODE, self.MODE_LONG_RANGE | lf | mode)

    def _set_frequency(self, mhz: float) -> None:
        frf = int((mhz * 1_000_000.0) / 61.03515625) & 0xFFFFFF
        self.write_u8(self.REG_FRF_MSB, (frf >> 16) & 0xFF)
        self.write_u8(self.REG_FRF_MID, (frf >> 8) & 0xFF)
        self.write_u8(self.REG_FRF_LSB, frf & 0xFF)

    def _bandwidth_id(self, bandwidth: int) -> int:
        for idx, cutoff in enumerate(self.BW_BINS):
            if bandwidth <= cutoff:
                return idx
        return 9

    def _configure(
        self,
        *,
        spreading_factor: int,
        signal_bandwidth: int,
        coding_rate: int,
        preamble_length: int,
        sync_word: int,
        crc: bool,
        agc: bool,
    ) -> None:
        spreading_factor = min(max(spreading_factor, 6), 12)
        coding_rate = min(max(coding_rate, 5), 8)

        self._set_mode(self.MODE_SLEEP)
        time.sleep(0.01)
        self._set_frequency(self.frequency_mhz)
        self.write_u8(self.REG_FIFO_TX_BASE_ADDR, 0x00)
        self.write_u8(self.REG_FIFO_RX_BASE_ADDR, 0x00)
        self.write_u8(self.REG_LNA, self.read_u8(self.REG_LNA) | 0x03)

        bw = self._bandwidth_id(signal_bandwidth)
        cr = coding_rate - 4
        self.write_u8(_REG_MODEM_CONFIG1, (bw << 4) | (cr << 1))  # explicit header
        self.write_u8(_REG_MODEM_CONFIG2, (spreading_factor << 4) | (0x04 if crc else 0x00))

        self.write_u8(self.REG_DETECTION_OPTIMIZE, 0x05 if spreading_factor == 6 else 0x03)
        self.write_u8(self.REG_DETECTION_THRESHOLD, 0x0C if spreading_factor == 6 else 0x0A)
        self.write_u8(self.REG_PREAMBLE_MSB, (preamble_length >> 8) & 0xFF)
        self.write_u8(self.REG_PREAMBLE_LSB, preamble_length & 0xFF)
        self.write_u8(_REG_SYNC_WORD, sync_word & 0xFF)

        symbol_duration_ms = 1000.0 / (signal_bandwidth / float(1 << spreading_factor))
        ldo = 0x08 if symbol_duration_ms > 16 else 0x00
        self.write_u8(_REG_MODEM_CONFIG3, ldo | (0x04 if agc else 0x00))

        self.write_u8(self.REG_IRQ_FLAGS, 0xFF)
        self.write_u8(self.REG_FIFO_ADDR_PTR, 0x00)
        self._set_mode(self.MODE_RX_CONTINUOUS)

    def receive(self, *, timeout: float, with_header: bool = True) -> Optional[bytes]:
        del with_header
        deadline = time.monotonic() + timeout
        self._set_mode(self.MODE_RX_CONTINUOUS)

        while time.monotonic() < deadline:
            if self.read_u8(self.REG_IRQ_FLAGS) & self.IRQ_RX_DONE:
                break
            time.sleep(0.005)
        else:
            return None

        flags = self.read_u8(self.REG_IRQ_FLAGS)
        if flags & self.IRQ_PAYLOAD_CRC_ERROR:
            self.write_u8(self.REG_IRQ_FLAGS, 0xFF)
            return None

        snr_raw = self.read_u8(self.REG_PKT_SNR_VALUE)
        if snr_raw & 0x80:
            snr_raw -= 256
        self.last_snr = snr_raw / 4.0

        rssi_raw = self.read_u8(self.REG_PKT_RSSI_VALUE)
        self.last_rssi = rssi_raw - (164 if self.frequency_mhz < 525 else 157)

        length = self.read_u8(self.REG_RX_NB_BYTES)
        current_addr = self.read_u8(self.REG_FIFO_RX_CURRENT_ADDR)
        self.write_u8(self.REG_FIFO_ADDR_PTR, current_addr)
        packet = self.read_fifo(length)
        self.write_u8(self.REG_IRQ_FLAGS, 0xFF)
        return packet


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip(), 0)


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def _parse_csv_set(raw: str) -> set[str]:
    return {p.strip().lower() for p in (raw or "").split(",") if p.strip()}


def _looks_like_sensor_payload(text: str) -> bool:
    parts = text.strip().split(":")
    sensor_id = parts[0].strip() if parts else ""
    if len(parts) < 3 or not _SENSOR_ID_RE.fullmatch(sensor_id):
        return False
    try:
        float(parts[1])
        float(parts[2])
    except ValueError:
        return False
    return True


def _decode_lora_packet(packet: bytes | bytearray) -> str:
    raw = bytes(packet)
    candidates = [raw]
    if len(raw) > 4:
        # Adafruit RFM9x is RadioHead-oriented. When reading with_header=True,
        # raw ESP32 LoRa.print() packets are returned intact; RadioHead packets
        # have a 4-byte header before the text payload.
        candidates.append(raw[4:])

    for candidate in candidates:
        text = candidate.decode("utf-8", errors="replace").strip(" \t\r\n\x00")
        if _looks_like_sensor_payload(text):
            return text

    return raw.decode("utf-8", errors="replace").strip(" \t\r\n\x00")


def _parse_payload(payload: str, zero_invalid_fields: set[str]) -> Optional[dict]:
    """
    ESP32 format:
      - new: ID:airTemp:hum:waterTemp:tds:ph:validMask
      - previous: ID:airTemp:hum:waterTemp:tds:validMask
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
    ph = 0.0

    mask_index = 5
    if len(parts) >= 7:
        ph = _to_float(parts[5]) if parts[5] != "" else 0.0
        mask_index = 6

    explicit_mask = False
    if len(parts) > mask_index and parts[mask_index] != "":
        try:
            valid_mask = int(float(parts[mask_index]))
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
        if len(parts) >= 6:
            if ("ph" in zero_invalid_fields) and ph == 0.0:
                pass
            else:
                valid_mask |= 0x10

    return {
        "sensor_id": sensor_id,
        "temperature": air_temp,
        "humidity": hum,
        "water_temperature": water_temp,
        "tds": tds,
        "ph": ph,
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
        self.lora_spi_baudrate = int(float(os.getenv("LORA_SPI_BAUDRATE", "1000000")))

        # Match sandeepmistry/LoRa defaults used by sensor1/sensor2:
        # LoRa.begin(433E6) + explicit header + SF7/BW125/CR4/5/preamble 8/sync 0x12/no CRC.
        self.lora_spreading_factor = int(os.getenv("LORA_SPREADING_FACTOR", "7"))
        self.lora_signal_bandwidth = int(float(os.getenv("LORA_SIGNAL_BANDWIDTH", "125000")))
        self.lora_coding_rate = int(os.getenv("LORA_CODING_RATE", "5"))
        self.lora_preamble_length = int(os.getenv("LORA_PREAMBLE_LENGTH", "8"))
        self.lora_sync_word = _env_int("LORA_SYNC_WORD", 0x12) & 0xFF
        self.lora_crc = _env_bool("LORA_CRC", False)
        self.lora_agc = _env_bool("LORA_AGC", True)

        self.print_packets = _env_bool("LORA_PRINT_PACKETS", True)
        # Nếu node không gửi validMask, coi giá trị 0 của các field này là "mất dữ liệu".
        # Ví dụ: "water_temperature,tds,ph"
        self.zero_invalid_fields = _parse_csv_set(os.getenv("FALLBACK_ZERO_INVALID_FIELDS", "water_temperature,tds,ph"))

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
        device = 1 if self.lora_spi_cs == "CE1" else 0
        return SX127xSpidev(
            bus=0,
            device=device,
            frequency_mhz=self.lora_frequency_mhz,
            baudrate=self.lora_spi_baudrate,
            reset_bcm=self.lora_reset_bcm,
            spreading_factor=self.lora_spreading_factor,
            signal_bandwidth=self.lora_signal_bandwidth,
            coding_rate=self.lora_coding_rate,
            preamble_length=self.lora_preamble_length,
            sync_word=self.lora_sync_word,
            crc=self.lora_crc,
            agc=self.lora_agc,
        )

    def _read_packet(self, rfm9x) -> Tuple[Optional[str], int, float]:
        pkt = rfm9x.receive(timeout=self.lora_rx_timeout_sec, with_header=True)
        if pkt is None:
            return None, 0, 0.0
        text = _decode_lora_packet(pkt)

        rssi = int(getattr(rfm9x, "last_rssi", getattr(rfm9x, "rssi", 0)) or 0)
        snr = float(getattr(rfm9x, "last_snr", getattr(rfm9x, "snr", 0.0)) or 0.0)
        return text, rssi, snr

    def _signal_quality(self, rssi: int, snr: float) -> str:
        # Simple heuristic for UI; web can display this string.
        if rssi >= -70 and snr >= 7:
            return "Rất tốt"
        if rssi >= -85 and snr >= 5:
            return "Tốt"
        if rssi >= -100 and snr >= 2:
            return "Trung bình"
        if rssi >= -115:
            return "Yếu"
        return "Rất yếu"

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
        print(
            "LoRa gateway ready on Pi "
            f"(freq={self.lora_frequency_mhz}MHz, CS={self.lora_spi_cs}, "
            f"SF{self.lora_spreading_factor}, BW={self.lora_signal_bandwidth}Hz, "
            f"CR=4/{self.lora_coding_rate}, preamble={self.lora_preamble_length}, "
            f"sync=0x{self.lora_sync_word:02X}, crc={'on' if self.lora_crc else 'off'})"
        )

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
                    "ph": parsed["ph"],
                    "timestamp": now_ms,  # giống ESP32 (millis), nhưng ở Pi dùng monotonic ms
                    "packet_id": self.stats.packet_count,
                    "valid_mask": valid_mask,
                    "sensor_status": {
                        "air_temp": (valid_mask & 0x01) != 0,
                        "humidity": (valid_mask & 0x02) != 0,
                        "water_temp": (valid_mask & 0x04) != 0,
                        "tds": (valid_mask & 0x08) != 0,
                        "ph": (valid_mask & 0x10) != 0,
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
