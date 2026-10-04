// Garage door sensor node: TFS20-L LiDAR on an ESP32, door state sent over ESP-NOW.
//
// Wiring (UART mode): sensor pins 1+2 -> 3V3, pin 3 (TX) -> GPIO16, pin 4 (RX) -> GPIO17,
// pin 5 -> GND (selects UART at power-on), pin 6 -> GND.
//
// The sensor points at the door. Closed = the door panel at a fixed distance.
// Open = the beam goes past where the door was (much farther, or no return at all).
//
// Calibrate once, from the Serial Monitor at 115200 baud, with the door CLOSED:
//   c  save the current distance as "closed" (kept across reboots)
//   x  forget the calibration
// Until calibrated it reports UNCALIBRATED and the LED board shows purple.

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

// Must match the LED board.
static const uint8_t ESPNOW_CHANNEL = 1;
static const uint32_t PACKET_MAGIC = 0x47444F52;  // "GDOR"

enum DoorState : uint8_t { UNCALIBRATED = 0, CLOSED = 1, OPEN = 2, SENSOR_FAULT = 3 };

struct __attribute__((packed)) DoorPacket {
  uint32_t magic;
  uint8_t state;
  uint16_t distCm;
  uint16_t strength;
  uint16_t closedCm;
};

static const int LIDAR_RX = 16;
static const int LIDAR_TX = 17;

// Below this the sensor's distance isn't trustworthy (datasheet guidance for Benewake units).
static const uint16_t MIN_STRENGTH = 100;
// A new state must hold this long before we switch, so a person or car passing doesn't flicker it.
static const uint32_t DEBOUNCE_MS = 1000;
// No valid frame for this long = sensor unplugged or dead.
static const uint32_t SENSOR_TIMEOUT_MS = 1000;
static const uint32_t SEND_INTERVAL_MS = 500;

static const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

Preferences prefs;
static uint16_t closedCm = 0;  // 0 = not calibrated

static uint8_t frame[9];
static uint8_t idx = 0;
static uint16_t lastDist = 0, lastStrength = 0;
static uint32_t lastFrameMs = 0;

// Recent valid distances, for calibration averaging.
static const int HIST = 50;
static uint16_t hist[HIST];
static int histCount = 0, histPos = 0;

static DoorState state = UNCALIBRATED;
static DoorState pending = UNCALIBRATED;
static uint32_t pendingSince = 0;
static uint32_t lastSend = 0, lastPrint = 0;

static uint16_t openThresholdCm() {
  // Anything clearly past the closed door counts as open: 20 cm or 20%, whichever is larger.
  uint16_t margin = max<uint16_t>(20, closedCm / 5);
  return closedCm + margin;
}

static bool readFrame() {
  while (Serial2.available()) {
    uint8_t b = Serial2.read();
    if (idx == 0 && b != 0x59) continue;
    if (idx == 1 && b != 0x59) { idx = 0; continue; }
    frame[idx++] = b;
    if (idx < 9) continue;
    idx = 0;
    uint8_t sum = 0;
    for (int i = 0; i < 8; i++) sum += frame[i];
    if (sum != frame[8]) continue;
    lastDist = frame[2] | (frame[3] << 8);
    lastStrength = frame[4] | (frame[5] << 8);
    lastFrameMs = millis();
    return true;
  }
  return false;
}

static DoorState classify() {
  if (millis() - lastFrameMs > SENSOR_TIMEOUT_MS) return SENSOR_FAULT;
  if (closedCm == 0) return UNCALIBRATED;
  // A weak or zero return means the beam found nothing nearby: the door isn't there.
  if (lastStrength < MIN_STRENGTH || lastDist == 0) return OPEN;
  return lastDist > openThresholdCm() ? OPEN : CLOSED;
}

static const char *stateName(DoorState s) {
  switch (s) {
    case CLOSED: return "CLOSED";
    case OPEN: return "OPEN";
    case SENSOR_FAULT: return "SENSOR_FAULT";
    default: return "UNCALIBRATED";
  }
}

static void sendState() {
  DoorPacket p{PACKET_MAGIC, state, lastDist, lastStrength, closedCm};
  esp_now_send(BROADCAST, (const uint8_t *)&p, sizeof(p));
  lastSend = millis();
}

static void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'c') {
      if (histCount < 10) {
        Serial.println("Not enough strong readings to calibrate - is the sensor aimed at the door?");
        continue;
      }
      uint32_t sum = 0;
      for (int i = 0; i < histCount; i++) sum += hist[i];
      closedCm = sum / histCount;
      prefs.putUShort("closedCm", closedCm);
      Serial.printf("Calibrated: closed = %u cm, open above %u cm\n", closedCm, openThresholdCm());
    } else if (c == 'x') {
      closedCm = 0;
      prefs.remove("closedCm");
      Serial.println("Calibration cleared");
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nGarage door sensor (TFS20-L + ESP-NOW)");

  prefs.begin("garage", false);
  closedCm = prefs.getUShort("closedCm", 0);
  if (closedCm) Serial.printf("Calibration: closed = %u cm, open above %u cm\n", closedCm, openThresholdCm());
  else Serial.println("Not calibrated: close the door and type c in the Serial Monitor");

  Serial2.begin(115200, SERIAL_8N1, LIDAR_RX, LIDAR_TX);

  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed, restarting");
    delay(1000);
    ESP.restart();
  }
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

void loop() {
  handleSerial();

  if (readFrame() && lastStrength >= MIN_STRENGTH && lastDist > 0) {
    hist[histPos] = lastDist;
    histPos = (histPos + 1) % HIST;
    if (histCount < HIST) histCount++;
  }

  uint32_t now = millis();
  DoorState raw = classify();
  if (raw != pending) {
    pending = raw;
    pendingSince = now;
  }
  // Calibration and sensor faults show up immediately; open/closed waits out the debounce.
  bool immediate = raw == UNCALIBRATED || raw == SENSOR_FAULT || state == UNCALIBRATED || state == SENSOR_FAULT;
  if (pending != state && (immediate || now - pendingSince >= DEBOUNCE_MS)) {
    state = pending;
    Serial.printf(">>> Door %s\n", stateName(state));
    sendState();
  }

  if (now - lastSend >= SEND_INTERVAL_MS) sendState();

  if (now - lastPrint >= 1000) {
    lastPrint = now;
    Serial.printf("%s  dist=%u cm  strength=%u  closed=%u cm\n",
                  stateName(state), lastDist, lastStrength, closedCm);
  }
}
