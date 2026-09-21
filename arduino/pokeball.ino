// Pokeball: button -> buzz -> photo -> POST to soufz.local:8080
// Button: IO13 to 3V3.  Audio: IO14 -> RC filter -> PAM8403.

#include <WiFi.h>
#include <ESPmDNS.h>
#include "esp_camera.h"

const char *WIFI_SSID = "Aashvik's iPhone";
const char *WIFI_PASS = "hangdynasty";

const char *LAPTOP_HOST = "soufz";
const uint16_t LAPTOP_PORT = 8080;
const char *UPLOAD_PATH = "/upload";

const bool USE_FIXED_IP = false;
const IPAddress FIXED_IP(192, 168, 1, 100);

const int BTN_PIN   = 13;
const int AUDIO_PIN = 14;
const int LED_PIN   = 33;

const uint32_t HOLD_MS = 30;
const uint32_t RELEASE_MS = 300;

#define PWDN_GPIO_NUM  32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM   0
#define SIOD_GPIO_NUM  26
#define SIOC_GPIO_NUM  27
#define Y9_GPIO_NUM    35
#define Y8_GPIO_NUM    34
#define Y7_GPIO_NUM    39
#define Y6_GPIO_NUM    36
#define Y5_GPIO_NUM    21
#define Y4_GPIO_NUM    19
#define Y3_GPIO_NUM    18
#define Y2_GPIO_NUM     5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  23
#define PCLK_GPIO_NUM  22

#define AUDIO_CH 2

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  #define AUDIO_TGT AUDIO_PIN
#else
  #define AUDIO_TGT AUDIO_CH
#endif

void setupAudio() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttachChannel(AUDIO_PIN, 200, 8, AUDIO_CH);
#else
  ledcSetup(AUDIO_CH, 200, 8);
  ledcAttachPin(AUDIO_PIN, AUDIO_CH);
#endif
  ledcWrite(AUDIO_TGT, 0);
}

void buzz(uint32_t hz, uint16_t ms) {
  ledcWriteTone(AUDIO_TGT, hz);
  delay(ms);
  ledcWrite(AUDIO_TGT, 0);
}

void buzzFail() { buzz(90, 120); delay(70); buzz(90, 120); }

bool startCamera() {
  camera_config_t c;
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;   c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;   c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;   c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;   c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    c.frame_size = FRAMESIZE_SVGA;
    c.jpeg_quality = 12;
    c.fb_count = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode = CAMERA_GRAB_LATEST;
  } else {
    c.frame_size = FRAMESIZE_VGA;
    c.jpeg_quality = 14;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("camera init failed: 0x%x\n", err);
    return false;
  }
  return true;
}

IPAddress laptopIP;
bool haveIP = false;

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("connecting to %s ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("connected, my IP is ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("wifi failed, will retry later");
  }
}

bool findLaptop() {
  if (USE_FIXED_IP) { laptopIP = FIXED_IP; haveIP = true; return true; }
  Serial.printf("looking up %s.local ... ", LAPTOP_HOST);
  IPAddress found = MDNS.queryHost(LAPTOP_HOST, 3000);
  if (found == IPAddress((uint32_t)0)) {
    Serial.println("not found");
    haveIP = false;
    return false;
  }
  laptopIP = found;
  haveIP = true;
  Serial.println(laptopIP);
  return true;
}

bool sendJpeg(uint8_t *buf, size_t len) {
  connectWifi();
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!haveIP && !findLaptop()) return false;

  WiFiClient client;
  if (!client.connect(laptopIP, LAPTOP_PORT, 5000)) {
    Serial.println("could not connect to laptop");
    haveIP = false;
    return false;
  }

  client.printf("POST %s HTTP/1.1\r\n", UPLOAD_PATH);
  client.printf("Host: %s.local:%u\r\n", LAPTOP_HOST, LAPTOP_PORT);
  client.print("Content-Type: image/jpeg\r\n");
  client.printf("Content-Length: %u\r\n", (unsigned)len);
  client.print("Connection: close\r\n\r\n");

  size_t sent = 0;
  while (sent < len) {
    size_t chunk = len - sent;
    if (chunk > 1024) chunk = 1024;
    size_t n = client.write(buf + sent, chunk);
    if (n == 0) break;
    sent += n;
  }

  client.setTimeout(6000);
  String status = client.readStringUntil('\n');
  client.stop();

  Serial.printf("sent %u/%u bytes, server said: %s\n",
                (unsigned)sent, (unsigned)len, status.c_str());
  return sent == len && status.indexOf("200") > 0;
}

void capture() {
  buzz(140, 200);
  digitalWrite(LED_PIN, LOW);

  for (int i = 0; i < 2; i++) {
    camera_fb_t *warm = esp_camera_fb_get();
    if (warm) esp_camera_fb_return(warm);
  }
  camera_fb_t *fb = esp_camera_fb_get();
  digitalWrite(LED_PIN, HIGH);

  if (!fb) {
    Serial.println("capture failed");
    buzzFail();
    return;
  }

  bool ok = sendJpeg(fb->buf, fb->len);
  esp_camera_fb_return(fb);

  if (!ok) buzzFail();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\npokeball starting");

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  pinMode(BTN_PIN, INPUT_PULLDOWN);

  setupAudio();

  if (!startCamera()) {
    while (true) { buzzFail(); delay(900); }
  }

  connectWifi();
  MDNS.begin("pokeball");
  findLaptop();

  Serial.println("ready, press the button");
  buzz(160, 150);
}

void loop() {
  static bool armed = true;
  static uint32_t downSince = 0;
  static uint32_t upSince = 0;

  uint32_t now = millis();
  bool pressed = digitalRead(BTN_PIN) == HIGH;

  if (pressed) {
    upSince = 0;
    if (downSince == 0) downSince = now;
    if (armed && now - downSince >= HOLD_MS) {
      armed = false;
      capture();
    }
  } else {
    downSince = 0;
    if (upSince == 0) upSince = now;
    if (!armed && now - upSince >= RELEASE_MS) armed = true;
  }

  delay(5);
}
