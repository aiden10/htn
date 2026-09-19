/*
 * pokeball_cam.ino
 * Freenove ESP32-S3-WROOM CAM (the board with two USB-C ports)
 *
 * Takes a photo when a trigger pin is pulled to GND.
 * Saves the JPEG to the microSD card (if one is inserted) AND streams it
 * over the serial port so a script on your laptop can write it to disk.
 *
 * Board settings (Arduino IDE):
 *   Board:              "ESP32S3 Dev Module"
 *   PSRAM:              "OPI PSRAM"      <-- required
 *   Flash Size:         "8MB (64Mb)"
 *   Partition Scheme:   "Huge APP (3MB No OTA/1MB SPIFFS)"
 *   USB CDC On Boot:    "Disabled"       <-- we use the UART-labelled port
 *   Upload Speed:       921600
 */

#include "esp_camera.h"
#include "FS.h"
#include "SD_MMC.h"

// ---------------------------------------------------------------- config ---

// Pull this pin to GND to take a photo. Safe free pins on this board:
// 14, 21, 47, 1, 2, 41, 42. Do NOT use the camera pins, 38/39/40 (SD),
// 48 (LED), 19/20 (USB), 0/3/45/46 (strapping) or 35/36/37 (PSRAM).
#define TRIGGER_PIN      14

// GPIO 0 (the BOOT button) is deliberately NOT used as a trigger: it is wired
// into the USB auto-reset circuit, so opening the serial port pulls it low and
// fires a phantom capture. Use the 'c' command over serial to test instead.

#define RGB_LED_PIN      48     // onboard WS2812 status LED
#define SERIAL_BAUD      921600 // drop to 115200 if transfers are flaky

#define JPEG_QUALITY     12     // 4 (best) .. 63 (worst). Raise if you see FB-OVF.
#define FRAME_SIZE       FRAMESIZE_SVGA   // 800x600

#define BOOT_GRACE_MS    1500   // ignore all triggers for this long after boot

// ----------------------------------------------- Freenove ESP32-S3 pinout ---

#define PWDN_GPIO_NUM   -1
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM   15
#define SIOD_GPIO_NUM    4
#define SIOC_GPIO_NUM    5
#define Y9_GPIO_NUM     16
#define Y8_GPIO_NUM     17
#define Y7_GPIO_NUM     18
#define Y6_GPIO_NUM     12
#define Y5_GPIO_NUM     10
#define Y4_GPIO_NUM      8
#define Y3_GPIO_NUM      9
#define Y2_GPIO_NUM     11
#define VSYNC_GPIO_NUM   6
#define HREF_GPIO_NUM    7
#define PCLK_GPIO_NUM   13

#define SD_MMC_CLK 39
#define SD_MMC_CMD 38
#define SD_MMC_D0  40

// ----------------------------------------------------------------- state ---

static bool     sdReady     = false;
static uint32_t photoIndex  = 0;
static bool     prevTrigLow = false;
static bool     trigArmed   = false;   // must be seen idle-HIGH before it can fire
static uint32_t bootTime    = 0;

// ------------------------------------------------------------- utilities ---

// If this fails to compile on your core version, swap in
// rgbLedWrite(RGB_LED_PIN, r, g, b); or delete the body.
void led(uint8_t r, uint8_t g, uint8_t b) {
  neopixelWrite(RGB_LED_PIN, r, g, b);
}

void logln(const String &s) {
  Serial.print("LOG:");
  Serial.println(s);
}

// ------------------------------------------------------------------ init ---

bool initCamera() {
  // Zero-initialise: camera_config_t has fields this function doesn't set,
  // and garbage in them causes "frame buffer malloc failed" at init.
  camera_config_t config = {};

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
#else
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
#endif
  config.sccb_i2c_port = -1;

  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_LATEST;

  if (psramFound()) {
    config.frame_size   = FRAME_SIZE;
    config.jpeg_quality = JPEG_QUALITY;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
  } else {
    logln("WARNING: no PSRAM found. Check Tools > PSRAM = OPI PSRAM.");
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 14;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    logln("Camera init failed, error 0x" + String(err, HEX));
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    s->set_brightness(s, 1);
    s->set_saturation(s, 1);
    s->set_whitebal(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_exposure_ctrl(s, 1);
  }
  return true;
}

void initSD() {
  SD_MMC.setPins(SD_MMC_CLK, SD_MMC_CMD, SD_MMC_D0);
  if (!SD_MMC.begin("/sdcard", true)) {   // true = 1-bit mode
    logln("No microSD card (that's fine, photos still go over serial).");
    sdReady = false;
    return;
  }
  sdReady = true;

  char path[32];
  while (photoIndex < 9999) {
    snprintf(path, sizeof(path), "/photo_%04u.jpg", photoIndex);
    if (!SD_MMC.exists(path)) break;
    photoIndex++;
  }
  logln("microSD mounted. Next photo index: " + String(photoIndex));
}

// --------------------------------------------------------------- capture ---

void capturePhoto() {
  led(60, 60, 60);   // white: working

  for (int i = 0; i < 2; i++) {          // discard stale / badly exposed frames
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale) esp_camera_fb_return(stale);
  }

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb || fb->len == 0) {
    if (fb) esp_camera_fb_return(fb);
    logln("Capture failed.");
    led(60, 0, 0);
    delay(400);
    led(0, 0, 0);
    return;
  }

  char name[32];
  snprintf(name, sizeof(name), "photo_%04u.jpg", photoIndex);

  if (sdReady) {
    char path[40];
    snprintf(path, sizeof(path), "/%s", name);
    File f = SD_MMC.open(path, FILE_WRITE);
    if (f) {
      f.write(fb->buf, fb->len);
      f.close();
      logln("Saved to SD: " + String(path) + " (" + String(fb->len) + " bytes)");
    } else {
      logln("SD write failed for " + String(path));
    }
  }

  Serial.printf("IMG:%u:%s\n", (unsigned)fb->len, name);
  Serial.write(fb->buf, fb->len);
  Serial.flush();

  esp_camera_fb_return(fb);
  photoIndex++;

  led(0, 60, 0);     // green: done
  delay(250);
  led(0, 0, 0);
}

// -------------------------------------------------------- trigger polling ---

void handleTrigger() {
  if (millis() - bootTime < BOOT_GRACE_MS) return;

  bool low = (digitalRead(TRIGGER_PIN) == LOW);

  // The pin must be observed idle-HIGH once before it is allowed to fire.
  // Stops a pin that is already low at boot from producing a phantom capture.
  if (!trigArmed) {
    if (!low) trigArmed = true;
    prevTrigLow = low;
    return;
  }

  if (low && !prevTrigLow) {
    delay(30);                                     // debounce
    if (digitalRead(TRIGGER_PIN) == LOW) {
      capturePhoto();
      while (digitalRead(TRIGGER_PIN) == LOW) delay(10);   // wait for release
      delay(50);
      low = false;
    }
  }
  prevTrigLow = low;
}

// ------------------------------------------------------------ setup/loop ---

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);

  pinMode(TRIGGER_PIN, INPUT_PULLUP);

  led(0, 0, 60);     // blue: booting

  logln("pokeball_cam booting...");
  if (!initCamera()) {
    while (true) { led(60, 0, 0); delay(300); led(0, 0, 0); delay(300); }
  }
  initSD();

  // Throw away anything the host wrote while we were booting, so line noise
  // from the port opening can't be mistaken for a capture command.
  while (Serial.available()) Serial.read();

  bootTime    = millis();
  prevTrigLow = (digitalRead(TRIGGER_PIN) == LOW);
  trigArmed   = false;

  logln("Ready. Touch GPIO " + String(TRIGGER_PIN) +
        " to GND, or send 'c' to capture.");

  led(0, 0, 0);
}

void loop() {
  handleTrigger();

  if (Serial.available()) {
    int c = Serial.read();
    if ((c == 'c' || c == 'C') && millis() - bootTime > BOOT_GRACE_MS) {
      capturePhoto();
    }
  }

  delay(10);
}
