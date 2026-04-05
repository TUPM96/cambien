#include <Arduino.h>
#include <SPI.h>
#include <LoRa.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include "DHT.h"

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
  float temp = dht.readTemperature();
  float hum  = dht.readHumidity();

  if (isnan(temp) || isnan(hum)) {
    lcd.setCursor(0,0);
    lcd.print("DHT Fail");
    delay(2000);
    return;
  }

  // Tạo chuỗi kèm ID sensor
  String message = "S2:" + String(temp,1) + ":" + String(hum,1);

  // Gửi LoRa
  LoRa.beginPacket();
  LoRa.print(message);
  LoRa.endPacket();

  Serial.println("Sent: " + message);

  // Hiển thị LCD
  lcd.clear();
  lcd.setCursor(0,0);
  lcd.print("S2 T:" + String(temp,1) + "C");
  lcd.setCursor(0,1);
  lcd.print("H:" + String(hum,1) + "%");

  delay(13000); // 13 giây
}