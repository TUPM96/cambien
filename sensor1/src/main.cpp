#include <Arduino.h>
#include <SPI.h>
#include <LoRa.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include "DHT.h"
#include <OneWire.h>
#include <DallasTemperature.h>

// LCD 16x2
LiquidCrystal_I2C lcd(0x27, 16, 2);

// LoRa pins
#define SS   5
#define RST  14
#define DIO0 26

// DHT11
#define DHTPIN 4
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// DS18B20 - nhiet do nuoc
#define WATER_TEMP_PIN 15
OneWire oneWire(WATER_TEMP_PIN);
DallasTemperature waterSensor(&oneWire);

// TDS analog
#define TDS_PIN 34

// pH analog
#define PH_PIN 35
const float ADC_VREF = 3.3f;
const int ADC_RESOLUTION = 4095;
const float PH_NEUTRAL_VOLTAGE = 2.50f; // Voltage at pH 7.00, calibrate with buffer solution.
const float PH_VOLTAGE_PER_PH = 0.18f;  // Typical pH probe module slope near 25C.

const char* SENSOR_ID = "S1";
const unsigned long SEND_INTERVAL_MS = 10000; // Sensor 1: 10 giay

const long LORA_FREQUENCY_HZ = 433000000L;
const int LORA_SPREADING_FACTOR = 7;
const long LORA_SIGNAL_BANDWIDTH = 125000L;
const int LORA_CODING_RATE_DENOMINATOR = 5; // CR 4/5
const int LORA_PREAMBLE_LENGTH = 8;
const int LORA_SYNC_WORD = 0x12;
const bool LORA_CRC_ENABLED = false;

// 1 = gửi kèm validMask để gateway phân biệt giá trị 0 thật và 0 do lỗi cảm biến.
#define SEND_VALID_MASK 1

void configureLoRaRadio() {
  LoRa.setSpreadingFactor(LORA_SPREADING_FACTOR);
  LoRa.setSignalBandwidth(LORA_SIGNAL_BANDWIDTH);
  LoRa.setCodingRate4(LORA_CODING_RATE_DENOMINATOR);
  LoRa.setPreambleLength(LORA_PREAMBLE_LENGTH);
  LoRa.setSyncWord(LORA_SYNC_WORD);
  if (LORA_CRC_ENABLED) {
    LoRa.enableCrc();
  } else {
    LoRa.disableCrc();
  }
}

float readWaterTemperatureC() {
  waterSensor.requestTemperatures();
  float t = waterSensor.getTempCByIndex(0);
  if (t == DEVICE_DISCONNECTED_C) {
    return NAN;
  }
  return t;
}

float readTdsPpm(float waterTempC, bool* isValid) {
  const int samples = 30;
  uint32_t adcSum = 0;
  for (int i = 0; i < samples; i++) {
    adcSum += analogRead(TDS_PIN);
    delay(10);
  }
  float adcAvg = (float)adcSum / samples;
  float voltage = adcAvg * ADC_VREF / ADC_RESOLUTION;
  if (isValid != nullptr) {
    // Neu module mat ket noi thuong dien ap rat thap, quy uoc <20mV la invalid.
    *isValid = voltage >= 0.02f;
  }

  // Bu nhiet theo cong thuc tham khao DFRobot
  float temp = isnan(waterTempC) ? 25.0f : waterTempC;
  float compensationCoefficient = 1.0f + 0.02f * (temp - 25.0f);
  float compensationVoltage = voltage / compensationCoefficient;

  float tds = (133.42f * compensationVoltage * compensationVoltage * compensationVoltage
              - 255.86f * compensationVoltage * compensationVoltage
              + 857.39f * compensationVoltage) * 0.5f;
  if (tds < 0) tds = 0;
  return tds;
}

float readPhValue(bool* isValid) {
  const int samples = 30;
  uint32_t adcSum = 0;
  for (int i = 0; i < samples; i++) {
    adcSum += analogRead(PH_PIN);
    delay(10);
  }

  float adcAvg = (float)adcSum / samples;
  float voltage = adcAvg * ADC_VREF / ADC_RESOLUTION;
  if (isValid != nullptr) {
    *isValid = voltage >= 0.05f && voltage <= (ADC_VREF - 0.05f);
  }

  float ph = 7.0f + ((PH_NEUTRAL_VOLTAGE - voltage) / PH_VOLTAGE_PER_PH);
  if (ph < 0.0f) ph = 0.0f;
  if (ph > 14.0f) ph = 14.0f;
  return ph;
}

void setup() {
  Serial.begin(115200);

  // LCD init
  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0,0);
  lcd.print("LoRa Sender");

  // DHT init
  dht.begin();
  waterSensor.begin();
  analogReadResolution(12);
  pinMode(TDS_PIN, INPUT);
  pinMode(PH_PIN, INPUT);

  // LoRa init
  LoRa.setPins(SS, RST, DIO0);
  if (!LoRa.begin(LORA_FREQUENCY_HZ)) {
    Serial.println("LoRa FAIL");
    lcd.clear();
    lcd.print("LoRa FAIL");
    while (1);
  }
  configureLoRaRadio();

  delay(2000);
  lcd.clear();
}

void loop() {
  unsigned long loopStart = millis();
  float airTempRaw = dht.readTemperature();
  float humRaw  = dht.readHumidity();
  float waterTempRaw = readWaterTemperatureC();
  bool tdsValid = false;
  float tdsRaw = readTdsPpm(waterTempRaw, &tdsValid);
  bool phValid = false;
  float phRaw = readPhValue(&phValid);

  bool airValid = !isnan(airTempRaw);
  bool humValid = !isnan(humRaw);
  bool waterValid = !isnan(waterTempRaw);

  // Neu doc loi thi gui 0 de ben nhan xu ly duoc, kem validMask de phan biet 0 that/0 loi.
  float temp = airValid ? airTempRaw : 0.0f;
  float hum = humValid ? humRaw : 0.0f;
  float waterTemp = waterValid ? waterTempRaw : 0.0f;
  float tds = tdsValid ? tdsRaw : 0.0f;
  float ph = phValid ? phRaw : 0.0f;

  uint8_t validMask = 0;
  if (airValid) validMask |= 0x01;
  if (humValid) validMask |= 0x02;
  if (waterValid) validMask |= 0x04;
  if (tdsValid) validMask |= 0x08;
  if (phValid) validMask |= 0x10;

  String message;
#if SEND_VALID_MASK
  // Dinh dang moi: ID:airTemp:hum:waterTemp:tds:ph:validMask
  message = String(SENSOR_ID) + ":" + String(temp, 1) + ":" + String(hum, 1) +
            ":" + String(waterTemp, 1) + ":" + String(tds, 1) +
            ":" + String(ph, 2) + ":" + String((int)validMask);
#else
  // Dinh dang fallback: ID:airTemp:hum:waterTemp:tds:ph  (0 = mất dữ liệu)
  message = String(SENSOR_ID) + ":" + String(temp, 1) + ":" + String(hum, 1) +
            ":" + String(waterTemp, 1) + ":" + String(tds, 1) + ":" + String(ph, 2);
#endif

  // Gửi LoRa
  LoRa.beginPacket();
  LoRa.print(message);
  LoRa.endPacket();

  Serial.println("Sent: " + message);

  // Hiển thị LCD
  lcd.clear();
  lcd.setCursor(0,0);
  lcd.print(String(SENSOR_ID) + " W:" + (waterValid ? String(waterTemp,1) : "ERR"));
  lcd.setCursor(0,1);
  lcd.print("T:" + (tdsValid ? String(tds,0) : "ERR") + " pH:" + (phValid ? String(ph,1) : "ERR"));

  unsigned long elapsed = millis() - loopStart;
  if (elapsed < SEND_INTERVAL_MS) {
    delay(SEND_INTERVAL_MS - elapsed);
  }
}
