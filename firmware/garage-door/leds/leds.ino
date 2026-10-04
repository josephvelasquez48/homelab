// Garage door LED node: receives door state from the sensor ESP32 over ESP-NOW
// and shows it on WS2812B LEDs.
//
// Wiring: 16x16 WS2812B matrix powered from its own 5V supply (not the ESP32's pins).
// Supply 5V -> LED 5V, supply GND -> LED GND and ESP32 GND (shared ground),
// ESP32 GPIO13 -> ~330 ohm resistor -> LED DIN, 1000 uF capacitor across LED 5V/GND.
//
// Colors:
//   green           closed
//   red             open
//   purple          sensor not calibrated yet
//   yellow, blink   LiDAR not responding
//   blue, blink     no messages from the sensor board (out of range or off)

#include <Arduino.h>
#include <Adafruit_NeoPixel.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

static const int LED_PIN = 13;
static const int NUM_LEDS = 256;  // BTF-LIGHTING 16x16 matrix
// 0-255. At 60, the 256-LED matrix draws about 1.2-2 A; full white at 255 would be about 15 A.
static const uint8_t BRIGHTNESS = 60;

// Must match the sensor board.
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

// The sensor sends every 500 ms; a few missed packets means the link is gone.
static const uint32_t LINK_TIMEOUT_MS = 3000;

Adafruit_NeoPixel strip(NUM_LEDS, LED_PIN, NEO_GRB + NEO_KHZ800);

static volatile uint8_t rxState = UNCALIBRATED;
static volatile uint16_t rxDist = 0;
static volatile uint32_t lastRxMs = 0;
static volatile bool gotAny = false;

static void onRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != sizeof(DoorPacket)) return;
  DoorPacket p;
  memcpy(&p, data, sizeof(p));
  if (p.magic != PACKET_MAGIC) return;
  rxState = p.state;
  rxDist = p.distCm;
  lastRxMs = millis();
  gotAny = true;
}

static void fill(uint32_t color) {
  for (int i = 0; i < NUM_LEDS; i++) strip.setPixelColor(i, color);
  strip.show();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nGarage door LEDs (ESP-NOW receiver)");

  strip.begin();
  strip.setBrightness(BRIGHTNESS);
  fill(strip.Color(0, 0, 255));

  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed, restarting");
    delay(1000);
    ESP.restart();
  }
  esp_now_register_recv_cb(onRecv);
}

void loop() {
  static uint32_t lastPrint = 0;
  uint32_t now = millis();
  bool blinkOn = (now / 400) % 2 == 0;
  bool linkUp = gotAny && now - lastRxMs < LINK_TIMEOUT_MS;

  uint32_t color;
  const char *label;
  if (!linkUp) {
    color = blinkOn ? strip.Color(0, 0, 255) : 0;
    label = "NO LINK";
  } else {
    switch (rxState) {
      case CLOSED: color = strip.Color(0, 255, 0); label = "CLOSED"; break;
      case OPEN: color = strip.Color(255, 0, 0); label = "OPEN"; break;
      case SENSOR_FAULT: color = blinkOn ? strip.Color(255, 180, 0) : 0; label = "SENSOR_FAULT"; break;
      default: color = strip.Color(160, 0, 255); label = "UNCALIBRATED"; break;
    }
  }
  fill(color);

  if (now - lastPrint >= 1000) {
    lastPrint = now;
    Serial.printf("%s  dist=%u cm\n", label, rxDist);
  }
  delay(20);
}
