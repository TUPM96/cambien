## Gateway Python cho Raspberry Pi 4

Thư mục này thay thế `gateway/` (ESP32) bằng gateway chạy trên **Raspberry Pi 4**:

- **Nhận LoRa (433MHz)** từ node (payload giống ESP32: `ID:temp:hum:waterTemp:tds:validMask`)
- **Publish MQTT** đúng schema JSON như ESP32 (`sensor/lora/data`)
- **Chạy web dashboard** (FastAPI) để xem dữ liệu và lưu CSV

### 1) Cài đặt (trên Raspberry Pi OS)

- **Bật SPI**:
  - `sudo raspi-config` → `Interface Options` → `SPI` → Enable
  - Reboot

- **Tạo môi trường Python + cài thư viện**:

```bash
cd ~/IUH_Lora/gateway_python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Cấu hình

- Copy file môi trường:

```bash
cp .env.example .env
```

- Sửa `.env` nếu cần:
  - **MQTT**: `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`, `MQTT_TOPIC`
  - **LoRa**:
    - `LORA_FREQUENCY_MHZ` (mặc định `433.0`)
    - `LORA_SPI_CS` = `CE0` hoặc `CE1`
    - `LORA_RESET_BCM` (GPIO reset của module LoRa)
  - **Fallback “0 = mất dữ liệu”** (khi node *không gửi* `validMask`):
    - `FALLBACK_ZERO_INVALID_FIELDS=water_temperature,tds`
  - **Web**: `WEB_PORT`

### 3) Chạy cả gateway + web

```bash
cd ~/IUH_Lora/gateway_python
source .venv/bin/activate
python run_all.py
```

Mở web: `http://<ip_pi>:8000` (nếu cổng 8000 bận sẽ tự nhảy sang cổng kế tiếp).

### 4) Cắm chân LoRa vào Raspberry Pi 4 (SPI0)

Mặc định code dùng **SPI0** và chọn **CS = CE0** (`LORA_SPI_CS=CE0`).

- **Nguồn**
  - **3V3** (Pi pin 1 hoặc 17) → **VCC** của module LoRa (**không dùng 5V**)
  - **GND** (Pi pin 6/9/14/20/25/30/34/39) → **GND**

- **SPI (SPI0)**
  - Pi **GPIO11 / SCLK** (pin 23) → **SCK**
  - Pi **GPIO10 / MOSI** (pin 19) → **MOSI**
  - Pi **GPIO9 / MISO** (pin 21) → **MISO**
  - Pi **GPIO8 / CE0** (pin 24) → **NSS/CS**
    - Nếu bạn muốn dùng CE1: Pi **GPIO7 / CE1** (pin 26) và set `LORA_SPI_CS=CE1`

- **RESET**
  - Pi **GPIO22** (pin 15) → **RST**
  - Nếu bạn dùng GPIO khác, sửa `LORA_RESET_BCM` theo BCM number.

### 5) Ghi chú phần cứng

- LoRa module phổ biến (SX1278/RFM98/RFM95) chạy mức **3.3V logic**. Không cấp 5V.
- Nếu bạn thấy **không nhận gói**:
  - kiểm tra tần số `LORA_FREQUENCY_MHZ=433.0`
  - kiểm tra dây SPI đúng pin, và SPI đã enable
  - kiểm tra chân `RST` đúng GPIO trong `.env`

