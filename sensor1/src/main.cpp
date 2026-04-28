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
const float ADC_VREF = 3.3f;
const int ADC_RESOLUTION = 4095;

const char* SENSOR_ID = "S1";
const unsigned long SEND_INTERVAL_MS = 10000; // Sensor 1: 10 giay

// 1 = gửi kèm validMask; 0 = không gửi validMask (gateway sẽ fallback "0 = mất dữ liệu")
#define SEND_VALID_MASK 0

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

  // LoRa init
  LoRa.setPins(SS, RST, DIO0);
  if (!LoRa.begin(433E6)) {
    Serial.println("LoRa FAIL");
    lcd.clear();
    lcd.print("LoRa FAIL");
    while (1);
  }

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

  bool airValid = !isnan(airTempRaw);
  bool humValid = !isnan(humRaw);
  bool waterValid = !isnan(waterTempRaw);

  // Neu doc loi thi gui 0 de ben nhan xu ly duoc, kem validMask de phan biet 0 that/0 loi.
  float temp = airValid ? airTempRaw : 0.0f;
  float hum = humValid ? humRaw : 0.0f;
  float waterTemp = waterValid ? waterTempRaw : 0.0f;
  float tds = tdsValid ? tdsRaw : 0.0f;

  uint8_t validMask = 0;
  if (airValid) validMask |= 0x01;
  if (humValid) validMask |= 0x02;
  if (waterValid) validMask |= 0x04;
  if (tdsValid) validMask |= 0x08;

  String message;
#if SEND_VALID_MASK
  // Dinh dang moi: ID:airTemp:hum:waterTemp:tds:validMask
  message = String(SENSOR_ID) + ":" + String(temp, 1) + ":" + String(hum, 1) +
            ":" + String(waterTemp, 1) + ":" + String(tds, 1) + ":" + String((int)validMask);
#else
  // Dinh dang fallback: ID:airTemp:hum:waterTemp:tds  (0 = mất dữ liệu)
  message = String(SENSOR_ID) + ":" + String(temp, 1) + ":" + String(hum, 1) +
            ":" + String(waterTemp, 1) + ":" + String(tds, 1);
#endif

  // Gửi LoRa
  LoRa.beginPacket();
  LoRa.print(message);
  LoRa.endPacket();

  Serial.println("Sent: " + message);

  // Hiển thị LCD
  lcd.clear();
  lcd.setCursor(0,0);
  lcd.print(String(SENSOR_ID) + " A:" + String(temp,1));
  lcd.setCursor(0,1);
  lcd.print("W:" + String(waterTemp,1) + " T:" + String(tds,0));

  unsigned long elapsed = millis() - loopStart;
  if (elapsed < SEND_INTERVAL_MS) {
    delay(SEND_INTERVAL_MS - elapsed);
  }
}