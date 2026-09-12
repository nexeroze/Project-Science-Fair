/*
  Orthostat wearable firmware for ESP32 (Arduino).
  Advertises as "Orthostat" and talks to BrainFlow Guard on a nearby Windows PC.

  Arduino IDE:
    - Board: ESP32 Dev Module (or your ESP32 board)
    - Library: none extra (uses built-in BLE)

  Pins you can change:
    HAPTIC_PIN  vibration motor / MOSFET gate
    Replace readSensors() with MAX30102 / IMU code when those boards are wired.
*/

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#define DEVICE_NAME "Orthostat"
#define SERVICE_UUID        "8f0a0001-7d3c-4e2a-9b1f-0c2d4e6f8001"
#define TELEMETRY_UUID      "8f0a0002-7d3c-4e2a-9b1f-0c2d4e6f8001"
#define COMMAND_UUID        "8f0a0003-7d3c-4e2a-9b1f-0c2d4e6f8001"
#define HAPTIC_PIN 26
#define HAPTIC_CHANNEL 0

BLECharacteristic *telemetryChar;
bool deviceConnected = false;
uint8_t hapticLevel = 0;
unsigned long lastNotify = 0;

enum Posture : uint8_t { LYING = 0, SITTING = 1, STANDING = 2 };

struct Telemetry {
  float earBvp = 96.0;
  uint16_t bpm = 72;
  uint8_t systolic = 120;
  uint8_t diastolic = 80;
  float accelG = 1.0;
  uint8_t posture = SITTING;
  uint8_t battEar = 94;
  uint8_t battWrist = 98;
  uint8_t battSleeve = 89;
} telemetry;

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override { deviceConnected = true; }
  void onDisconnect(BLEServer *server) override {
    deviceConnected = false;
    server->startAdvertising();
  }
};

class CommandCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    String value = characteristic->getValue();
    if (value.length() == 0) return;
    hapticLevel = (uint8_t)value[0];
    if (hapticLevel > 3) hapticLevel = 3;
  }
};

void applyHaptic() {
  const int duty[] = {0, 80, 160, 255};
  ledcWrite(HAPTIC_CHANNEL, duty[hapticLevel]);
}

void readSensors() {
  // Simulated standing-drop pattern so you can test the PC app before sensors are soldered.
  // Swap this block for real PPG (ear_bvp 0-100), HR, IMU posture, and battery ADC reads.
  static unsigned long start = millis();
  float t = (millis() - start) / 1000.0f;
  bool standing = digitalRead(0) == LOW;  // hold BOOT to simulate STANDING on many ESP32 boards
  telemetry.posture = standing ? STANDING : SITTING;
  if (standing) {
    telemetry.earBvp = 12.0f + 2.0f * sinf(t);
    telemetry.bpm = 110;
    telemetry.systolic = 85;
    telemetry.diastolic = 55;
    telemetry.accelG = 2.4f;
  } else {
    telemetry.earBvp = 96.0f + 3.0f * sinf(t);
    telemetry.bpm = 72;
    telemetry.systolic = 120;
    telemetry.diastolic = 80;
    telemetry.accelG = 1.0f;
  }
}

void notifyTelemetry() {
  uint8_t pkt[13];
  uint16_t bvp10 = (uint16_t)constrain(telemetry.earBvp * 10.0f, 0, 65535);
  uint16_t accel100 = (uint16_t)constrain(telemetry.accelG * 100.0f, 0, 65535);
  pkt[0] = 1;
  memcpy(pkt + 1, &bvp10, 2);
  memcpy(pkt + 3, &telemetry.bpm, 2);
  pkt[5] = telemetry.systolic;
  pkt[6] = telemetry.diastolic;
  memcpy(pkt + 7, &accel100, 2);
  pkt[9] = telemetry.posture;
  pkt[10] = telemetry.battEar;
  pkt[11] = telemetry.battWrist;
  pkt[12] = telemetry.battSleeve;
  telemetryChar->setValue(pkt, sizeof(pkt));
  telemetryChar->notify();
}

void setup() {
  pinMode(HAPTIC_PIN, OUTPUT);
  pinMode(0, INPUT_PULLUP);
  ledcSetup(HAPTIC_CHANNEL, 5000, 8);
  ledcAttachPin(HAPTIC_PIN, HAPTIC_CHANNEL);
  ledcWrite(HAPTIC_CHANNEL, 0);

  BLEDevice::init(DEVICE_NAME);
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService *service = server->createService(SERVICE_UUID);

  telemetryChar = service->createCharacteristic(
      TELEMETRY_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  telemetryChar->addDescriptor(new BLE2902());

  BLECharacteristic *commandChar = service->createCharacteristic(
      COMMAND_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  commandChar->setCallbacks(new CommandCallbacks());

  service->start();
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->start();
}

void loop() {
  readSensors();
  applyHaptic();
  if (deviceConnected && millis() - lastNotify >= 250) {
    lastNotify = millis();
    notifyTelemetry();
  }
}
