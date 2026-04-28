#include <Arduino.h>
#include <SPI.h>
#include <LoRa.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// LCD 16x2
LiquidCrystal_I2C lcd(0x27, 16, 2);

// LoRa pins
#define SS   5
#define RST  14
#define DIO0 26

// WiFi & MQTT config
const char* ssid = "Bamos Coffee 2G";
const char* password = "bamosxinchao";
const char* mqtt_server = "103.146.22.13";
const int mqtt_port = 1883;
const char* mqtt_user = "user1";
const char* mqtt_pass = "12345678";
const char* mqtt_topic = "sensor/lora/data";

WiFiClient espClient;
PubSubClient client(espClient);

String receivedData = "";
String lastMessage = "";

struct LoRaQuality {
  int lastRSSI = 0;
  float lastSNR = 0;
  int minRSSI = 0;
  int maxRSSI = -200;
  int packetCount = 0;
  unsigned long lastPacketTime = 0;
} loraStats;

// Biến lưu data cuối cùng
String lastSensorId = "---";
float lastTemp = 0;
float lastHum = 0;
float lastWaterTemp = 0;
float lastTds = 0;
bool lastAirValid = false;
bool lastHumValid = false;
bool lastWaterValid = false;
bool lastTdsValid = false;
uint8_t lastValidMask = 0;
int lastRssi = 0;
float lastSnr = 0;
bool hasData = false;

// Khai báo prototype
void updateLCDStatus();
void updateLCDData();
void reconnectMQTT();
void sendToMQTT(String sensorId, float temp, float hum, float waterTemp, float tds, uint8_t validMask, int rssi, float snr, int packetId);
void parseAndSend(String data, int rssi, float snr);
void checkPacketLoss();

void setup() {
  Serial.begin(115200);

  // LCD init
  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  
  lcd.setCursor(0, 0);
  lcd.print("LoRa Gateway");
  lcd.setCursor(0, 1);
  lcd.print("Init...");
  delay(1000);
  
  // LoRa init - DÙNG THAM SỐ MẶC ĐỊNH để nhận từ sensor
  LoRa.setPins(SS, RST, DIO0);
  if (!LoRa.begin(433E6)) {
    Serial.println("LoRa init failed!");
    lcd.clear();
    lcd.print("LoRa FAIL");
    while (1);
  }
  
  // KHÔNG setSpreadingFactor, KHÔNG setSignalBandwidth, KHÔNG setCodingRate4
  // Để LoRa dùng tham số mặc định (SF7, BW125kHz, CR4/5) - giống với sensor
  
  Serial.println("LoRa Gateway ready - Default params (SF7, BW125, CR4/5)");
  
  // Kết nối WiFi
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("WiFi Conn...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());

  // MQTT setup
  client.setServer(mqtt_server, mqtt_port);
  
  // Hiển thị trạng thái
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Ready");
  delay(1000);
  updateLCDStatus();
}

void updateLCDStatus() {
  lcd.clear();
  
  // Dòng 1: Trạng thái
  lcd.setCursor(0, 0);
  if (WiFi.status() == WL_CONNECTED) {
    lcd.print("W:OK ");
  } else {
    lcd.print("W:FAIL ");
  }
  
  if (client.connected()) {
    lcd.print("MQTT:OK");
  } else {
    lcd.print("MQTT:FAIL");
  }
  
  // Dòng 2: Trạng thái LoRa
  lcd.setCursor(0, 1);
  if (hasData) {
    lcd.printf("Pkt:%d R:%d", loraStats.packetCount, lastRssi);
  } else {
    lcd.print("Waiting LoRa...");
  }
}

void updateLCDData() {
  lcd.clear();
  
  // Dòng 1: Cảm biến + nhiet do khong khi
  lcd.setCursor(0, 0);
  lcd.print(lastSensorId + " ");
  if (lastAirValid) {
    lcd.print("A:" + String(lastTemp, 1));
  } else {
    lcd.print("A:ERR");
  }
  
  // Dòng 2: TDS + RSSI
  lcd.setCursor(0, 1);
  // Quy ước mới: node gửi 0 khi không đo được -> LCD vẫn hiển thị 0, không hiển thị ERR.
  lcd.print("TDS:" + String(lastTds, 0));
  
  lcd.setCursor(12, 1);
  lcd.printf("R:%d", lastRssi);
}

void reconnectMQTT() {
  int retryCount = 0;
  while (!client.connected() && retryCount < 5) {
    Serial.print("Connecting MQTT...");
    lcd.setCursor(0, 1);
    lcd.print("MQTT Conn...");
    
    if (client.connect("LoRaGateway", mqtt_user, mqtt_pass)) {
      Serial.println("connected");
      updateLCDStatus();
      return;
    } else {
      Serial.print("failed, rc=");
      Serial.print(client.state());
      Serial.println(" retry in 5s");
      lcd.setCursor(0, 1);
      lcd.print("MQTT Fail ");
      delay(5000);
      retryCount++;
    }
  }
}

void sendToMQTT(String sensorId, float temp, float hum, float waterTemp, float tds, uint8_t validMask, int rssi, float snr, int packetId) {
  if (!client.connected()) {
    reconnectMQTT();
  }
  
  if (!client.connected()) {
    Serial.println("MQTT not connected, skip sending");
    return;
  }
  
  StaticJsonDocument<384> doc;
  doc["sensor_id"] = sensorId;
  doc["temperature"] = temp;
  doc["humidity"] = hum;
  doc["water_temperature"] = waterTemp;
  doc["tds"] = tds;
  doc["timestamp"] = millis();
  doc["packet_id"] = packetId;
  doc["valid_mask"] = validMask;

  JsonObject sensorStatus = doc.createNestedObject("sensor_status");
  sensorStatus["air_temp"] = (validMask & 0x01) != 0;
  sensorStatus["humidity"] = (validMask & 0x02) != 0;
  sensorStatus["water_temp"] = (validMask & 0x04) != 0;
  sensorStatus["tds"] = (validMask & 0x08) != 0;
  
  JsonObject lora = doc.createNestedObject("lora_signal");
  lora["rssi"] = rssi;
  lora["snr"] = snr;
  lora["rssi_min"] = loraStats.minRSSI;
  lora["rssi_max"] = loraStats.maxRSSI;
  lora["packet_count"] = loraStats.packetCount;
  
  char jsonBuffer[384];
  serializeJson(doc, jsonBuffer);
  
  if (client.publish(mqtt_topic, jsonBuffer)) {
    Serial.println("MQTT sent: " + String(jsonBuffer));
  } else {
    Serial.println("MQTT send failed");
  }
}

void parseAndSend(String data, int rssi, float snr) {
  // Payload moi: ID:airTemp:hum:waterTemp:tds:validMask
  // Van tuong thich payload cu: ID:airTemp:hum
  int firstColon = data.indexOf(':');
  int secondColon = data.indexOf(':', firstColon + 1);
  int thirdColon = data.indexOf(':', secondColon + 1);
  int fourthColon = data.indexOf(':', thirdColon + 1);
  int fifthColon = data.indexOf(':', fourthColon + 1);

  if (firstColon <= 0 || secondColon <= 0) {
    Serial.println("Invalid format: " + data);
    return;
  }

  lastSensorId = data.substring(0, firstColon);
  String tempStr = data.substring(firstColon + 1, secondColon);
  String humStr = (thirdColon > 0) ? data.substring(secondColon + 1, thirdColon) : data.substring(secondColon + 1);

  String waterTempStr = "";
  String tdsStr = "";
  String validMaskStr = "";
  if (thirdColon > 0 && fourthColon > 0) {
    if (fifthColon > 0) {
      waterTempStr = data.substring(thirdColon + 1, fourthColon);
      tdsStr = data.substring(fourthColon + 1, fifthColon);
      validMaskStr = data.substring(fifthColon + 1);
    } else {
      waterTempStr = data.substring(thirdColon + 1, fourthColon);
      tdsStr = data.substring(fourthColon + 1);
    }
  }

  lastTemp = tempStr.toFloat();
  lastHum = humStr.toFloat();
  lastWaterTemp = waterTempStr.length() > 0 ? waterTempStr.toFloat() : 0.0f;
  lastTds = tdsStr.length() > 0 ? tdsStr.toFloat() : 0.0f;

  if (validMaskStr.length() > 0) {
    lastValidMask = (uint8_t)validMaskStr.toInt();
  } else {
    // Fallback khi payload không có mask:
    // - Payload cũ (ID:temp:hum) hoặc payload thiếu field -> chỉ set bit cho field có giá trị != 0
    // Quy ước: 0 = mất dữ liệu
    lastValidMask = 0;
    if (lastTemp != 0.0f) lastValidMask |= 0x01;
    if (lastHum != 0.0f) lastValidMask |= 0x02;
    if (thirdColon > 0 && fourthColon > 0) {
      if (lastWaterTemp != 0.0f) lastValidMask |= 0x04;
      if (lastTds != 0.0f) lastValidMask |= 0x08;
    }
  }
  lastAirValid = (lastValidMask & 0x01) != 0;
  lastHumValid = (lastValidMask & 0x02) != 0;
  lastWaterValid = (lastValidMask & 0x04) != 0;
  lastTdsValid = (lastValidMask & 0x08) != 0;
  lastRssi = rssi;
  lastSnr = snr;
  hasData = true;
  
  loraStats.lastRSSI = rssi;
  loraStats.lastSNR = snr;
  loraStats.packetCount++;
  loraStats.lastPacketTime = millis();
  
  if (rssi < loraStats.minRSSI) loraStats.minRSSI = rssi;
  if (rssi > loraStats.maxRSSI) loraStats.maxRSSI = rssi;
  
  Serial.println("=========================");
  Serial.println("Received: " + data);
  Serial.printf("Parsed: ID=%s, Air=%.1fC(%d), Hum=%.1f%%(%d), Water=%.1fC(%d), TDS=%.1fppm(%d), mask=%u\n",
                lastSensorId.c_str(),
                lastTemp, lastAirValid,
                lastHum, lastHumValid,
                lastWaterTemp, lastWaterValid,
                lastTds, lastTdsValid,
                lastValidMask);
  Serial.printf("RSSI: %d dBm, SNR: %.2f dB\n", rssi, snr);
  Serial.printf("Packet count: %d\n", loraStats.packetCount);
  Serial.println("=========================");
  
  // Hiển thị lên LCD
  updateLCDData();
  
  // Gửi MQTT
  sendToMQTT(lastSensorId, lastTemp, lastHum, lastWaterTemp, lastTds, lastValidMask, rssi, snr, loraStats.packetCount);
}

void checkPacketLoss() {
  if (hasData && (millis() - loraStats.lastPacketTime) > 60000) { // 60 giây
    Serial.println("WARNING: No LoRa packet for 60 seconds!");
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("LOST SIGNAL!");
    lcd.setCursor(0, 1);
    lcd.print("Check Sensor");
    hasData = false;
    delay(2000);
    updateLCDStatus();
  }
}

void loop() {
  // Kết nối MQTT nếu cần
  if (!client.connected()) {
    reconnectMQTT();
  }
  if (client.connected()) {
    client.loop();
  }
  
  // Nhận dữ liệu LoRa
  int packetSize = LoRa.parsePacket();
  if (packetSize) {
    receivedData = "";
    while (LoRa.available()) {
      receivedData += (char)LoRa.read();
    }
    
    int rssi = LoRa.packetRssi();
    float snr = LoRa.packetSnr();
    
    if (receivedData.length() > 0) {
      parseAndSend(receivedData, rssi, snr);
      lastMessage = receivedData;
    }
  }
  
  // Cập nhật trạng thái định kỳ nếu chưa có data
  static unsigned long lastStatusUpdate = 0;
  if (!hasData && millis() - lastStatusUpdate > 5000) {
    lastStatusUpdate = millis();
    updateLCDStatus();
  }
  
  checkPacketLoss();
  
  delay(100);
}