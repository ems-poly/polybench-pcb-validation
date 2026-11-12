// Polybench demo firmware, ESP32-S3-DevKitC-1.
// Line protocol on UART0 (the UART/USB-bridge port of the DevKitC-1; ARDUINO_USB_CDC_ON_BOOT=0), 115200 8N1.
// One command per line, one JSON object per reply line:
//   selftest   -> {"type":"selftest", ...}   chip, MAC, flash and PSRAM size
//   wifi       -> {"type":"wifi","count":N}  number of access points seen in one scan (no connection is made)
//   led [gpio] -> {"type":"led","gpio":G}    blinks the RGB LED red, green, blue for about 4 s
// A command may end with " #<n>" (a request id). Every reply to it then carries "id":<n>, so a late reply can never be
// taken for the answer to the next command. The selftest line printed 1.5 s after boot has no id.
#include <Arduino.h>
#include <WiFi.h>
#include <esp_mac.h>

#ifndef LED_GPIO_DEFAULT
#define LED_GPIO_DEFAULT 38
#endif
static const char *FW = "polybench-esp32-demo 1.0.0";
static int ledGpio = LED_GPIO_DEFAULT;
static long reqId = -1;  // id of the command being served, -1 for none

static void idField() { if (reqId >= 0) Serial.printf(",\"id\":%ld", reqId); }

static void selftest() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char m[18];
  snprintf(m, sizeof m, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  Serial.printf("{\"type\":\"selftest\",\"fw\":\"%s\",\"chip\":\"%s\",\"revision\":%d,\"cores\":%d,"
                "\"mac\":\"%s\",\"flash_mb\":%u,\"psram_mb\":%u,\"free_heap_kb\":%u",
                FW, ESP.getChipModel(), (int)ESP.getChipRevision(), (int)ESP.getChipCores(), m,
                (unsigned)(ESP.getFlashChipSize() / (1024 * 1024)), (unsigned)(ESP.getPsramSize() / (1024 * 1024)),
                (unsigned)(ESP.getFreeHeap() / 1024));
  idField();
  Serial.println("}");
}

static void wifiScan() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  int n = WiFi.scanNetworks();
  Serial.printf("{\"type\":\"wifi\",\"count\":%d", n < 0 ? 0 : n);
  idField();
  Serial.println("}");
  WiFi.scanDelete();
}

static void ledBlink(int gpio) {
  if (gpio > 0) ledGpio = gpio;
  for (int i = 0; i < 4; i++) {
    neopixelWrite(ledGpio, 40, 0, 0);  delay(350);
    neopixelWrite(ledGpio, 0, 40, 0);  delay(350);
    neopixelWrite(ledGpio, 0, 0, 40);  delay(350);
    neopixelWrite(ledGpio, 0, 0, 0);   delay(150);
  }
  Serial.printf("{\"type\":\"led\",\"gpio\":%d", ledGpio);
  idField();
  Serial.println("}");
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  selftest();
}

void loop() {
  if (!Serial.available()) { delay(5); return; }
  String line = Serial.readStringUntil('\n');
  line.trim();
  reqId = -1;
  int h = line.lastIndexOf(" #");
  if (h >= 0) { reqId = line.substring(h + 2).toInt(); line = line.substring(0, h); line.trim(); }
  if (line == "selftest") selftest();
  else if (line == "wifi") wifiScan();
  else if (line.startsWith("led")) ledBlink(line.length() > 3 ? line.substring(3).toInt() : 0);
  else if (line.length()) { Serial.print("{\"type\":\"error\",\"text\":\"unknown command\""); idField(); Serial.println("}"); }
  reqId = -1;
}
