/*
  ============================================================================
  SMART MEDICINE BOX - DEMO FIRMWARE
  ============================================================================
  Board  : ESP32 Dev Module (ESP32-WROOM-32 DevKit V1)
  Library: U8g2 (Preferences is built into the ESP32 Arduino core)
  Files  : medbox_demo.ino + config.h in a folder named medbox_demo

  Hardware used : DHT11, 2 x HX711 + load cells, SSD1315 OLED, red + green LED.
  NOT used      : battery, LDR, reed, servo, keypad, Wi-Fi.

  What it does
    - connects to the local Python app directly over the USB cable
    - every 3 s sends temperature, humidity and shelf weights over USB serial
    - receives the PC time, dose schedule and tare/calibration commands over USB
    - OLED: clock, next dose, flashing "TAKE NOW" when a dose is due
    - green LED = connected and sensors OK, red blinking = dose due, red steady = problem

  Sensor sampling, USB serial communication and OLED updates run locally.
  ============================================================================
*/
#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include <U8g2lib.h>
#include <time.h>
#include <sys/time.h>
#include "config.h"

U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
Preferences prefs;

// ============================================================================
//  SHARED DATA (main loop), protected by a mutex
// ============================================================================
static SemaphoreHandle_t mtx;
#define LOCK()   xSemaphoreTake(mtx, portMAX_DELAY)
#define UNLOCK() xSemaphoreGive(mtx)

struct Snap {
  float t, h; int dht;            // dht: 0 ok, 1 stuck low, 2 no reply, 3 timing, 4 checksum, -1 not read
  float w[2], n[2]; bool hx[2];   // grams, noise (g), load cell present
  float cpg[2]; bool userCal[2];  // counts per gram used, user-calibrated?
};
static Snap snap = { NAN, NAN, -1, { NAN, NAN }, { 0, 0 }, { false, false }, { DEFAULT_COUNTS_PER_GRAM, DEFAULT_COUNTS_PER_GRAM }, { false, false } };

struct SchedItem { char time[6]; char name[24]; char dose[18]; bool taken; };
static SchedItem sched[MAX_SCHED];
static int schedCount = 0;
static int tzMin = DEFAULT_TZ_MIN;
static int dueWindowMin = DUE_WINDOW_MIN_DEFAULT;

static volatile bool pcConnected = false;
static volatile uint32_t lastPcSyncMs = 0;
static volatile int  cmdTare[2] = { 0, 0 };
static volatile float cmdCalGrams[2] = { 0, 0 };
static char ackMsg[48] = "";
static volatile uint32_t ackSeq = 0;

// ============================================================================
//  HX711 (bit-banged, non-blocking ring buffer)
// ============================================================================
struct HxPins { uint8_t dout, sck; };
static const HxPins HXP[2] = { { PIN_HX1_DOUT, PIN_HX1_SCK }, { PIN_HX2_DOUT, PIN_HX2_SCK } };
struct HxState { long ring[HX_RING]; int idx; int count; uint32_t lastMs; long tare; float cpg; bool userCal; };
static HxState hx[2];

static void hxInit() {
  for (int i = 0; i < 2; i++) {
    pinMode(HXP[i].dout, INPUT_PULLUP);
    pinMode(HXP[i].sck, OUTPUT);
    digitalWrite(HXP[i].sck, LOW);
    hx[i].idx = 0; hx[i].count = 0; hx[i].lastMs = 0;
  }
}
static bool hxReadRaw(int i, long &out) {
  uint32_t v = 0;
  noInterrupts();                                  // SCK must never stay high > 60 us
  for (int b = 0; b < 24; b++) {
    digitalWrite(HXP[i].sck, HIGH); delayMicroseconds(1);
    v = (v << 1) | (uint32_t)digitalRead(HXP[i].dout);
    digitalWrite(HXP[i].sck, LOW);  delayMicroseconds(1);
  }
  digitalWrite(HXP[i].sck, HIGH); delayMicroseconds(1);   // 25th pulse: channel A, gain 128
  digitalWrite(HXP[i].sck, LOW);  delayMicroseconds(1);
  interrupts();
  if (v & 0x800000UL) v |= 0xFF000000UL;
  out = (int32_t)v;
  return true;
}
static void hxPoll() {                              // call every loop: reads only when a sample is ready
  for (int i = 0; i < 2; i++) {
    if (digitalRead(HXP[i].dout) == LOW) {
      long v;
      hxReadRaw(i, v);
      hx[i].ring[hx[i].idx] = v;
      hx[i].idx = (hx[i].idx + 1) % HX_RING;
      if (hx[i].count < HX_RING) hx[i].count++;
      hx[i].lastMs = millis();
    }
  }
}
static bool hxPresent(int i) { return hx[i].count >= 5 && (millis() - hx[i].lastMs) < 1500; }
// trimmed mean (drops highest and lowest) + spread (max-min)
static bool hxStats(int i, long &filt, long &spread) {
  if (!hxPresent(i)) return false;
  long b[HX_RING]; int n = hx[i].count;
  for (int k = 0; k < n; k++) b[k] = hx[i].ring[k];
  for (int a = 1; a < n; a++) { long key = b[a]; int j = a - 1; while (j >= 0 && b[j] > key) { b[j + 1] = b[j]; j--; } b[j + 1] = key; }
  spread = b[n - 1] - b[0];
  long long sum = 0; int cnt = 0;
  for (int k = 1; k < n - 1; k++) { sum += b[k]; cnt++; }
  filt = (long)(sum / cnt);
  return true;
}
static void setAck(const char *msg) {
  LOCK(); strncpy(ackMsg, msg, sizeof(ackMsg) - 1); ackMsg[sizeof(ackMsg) - 1] = 0; ackSeq++; UNLOCK();
  Serial.printf("[CMD] %s\n", msg);
}
static void handleCommands() {                      // runs in the main loop (tare / calibrate from the website)
  for (int i = 0; i < 2; i++) {
    char key[12], msg[48];
    if (cmdTare[i]) {
      cmdTare[i] = 0;
      long f, s;
      if (hxStats(i, f, s)) {
        hx[i].tare = f; snprintf(key, sizeof(key), "tare%d", i + 1); prefs.putLong(key, f);
        snprintf(msg, sizeof(msg), "TARE%d OK", i + 1);
      } else snprintf(msg, sizeof(msg), "TARE%d FAIL no load cell data", i + 1);
      setAck(msg);
    }
    if (cmdCalGrams[i] > 0) {
      float g = cmdCalGrams[i]; cmdCalGrams[i] = 0;
      long f, s;
      if (!hxStats(i, f, s)) snprintf(msg, sizeof(msg), "CAL%d FAIL no load cell data", i + 1);
      else {
        float cpg = (float)(f - hx[i].tare) / g;
        if (fabsf(cpg) < 20.0f || fabsf(cpg) > 30000.0f) snprintf(msg, sizeof(msg), "CAL%d FAIL tare first, then add weight", i + 1);
        else {
          hx[i].cpg = cpg; hx[i].userCal = true;
          snprintf(key, sizeof(key), "cpg%d", i + 1); prefs.putFloat(key, cpg);
          snprintf(key, sizeof(key), "ucal%d", i + 1); prefs.putBool(key, true);
          snprintf(msg, sizeof(msg), "CAL%d OK %.1f counts/g", i + 1, cpg);
        }
      }
      setAck(msg);
    }
  }
}

// ============================================================================
//  DHT11 (bit-banged)
// ============================================================================
static int waitFor(int pin, int level, int timeoutUs) {
  uint32_t t = micros();
  while (digitalRead(pin) != level) { if ((int)(micros() - t) > timeoutUs) return -1; }
  return (int)(micros() - t);
}
static int dhtRead(float &t, float &h) {            // 0 ok, 1 stuck low, 2 no reply, 3 bad timing, 4 checksum
  uint8_t d[5] = { 0, 0, 0, 0, 0 };
  pinMode(PIN_DHT, INPUT_PULLUP);
  delay(2);
  if (digitalRead(PIN_DHT) == LOW) return 1;
  pinMode(PIN_DHT, OUTPUT); digitalWrite(PIN_DHT, LOW); delay(20);
  noInterrupts();
  pinMode(PIN_DHT, INPUT_PULLUP);
  int code = 0;
  if (waitFor(PIN_DHT, HIGH, 100) < 0) code = 1;
  else if (waitFor(PIN_DHT, LOW, 150) < 0) code = 2;
  else if (waitFor(PIN_DHT, HIGH, 120) < 0) code = 3;
  else if (waitFor(PIN_DHT, LOW, 120) < 0) code = 3;
  for (int b = 0; code == 0 && b < 40; b++) {
    if (waitFor(PIN_DHT, HIGH, 100) < 0) { code = 3; break; }
    int hi = waitFor(PIN_DHT, LOW, 120);
    if (hi < 0) { code = 3; break; }
    d[b / 8] = (d[b / 8] << 1) | (hi > 45 ? 1 : 0);
  }
  interrupts();
  if (code) return code;
  if ((uint8_t)(d[0] + d[1] + d[2] + d[3]) != d[4]) return 4;
  h = d[0] + d[1] * 0.1f;
  t = d[2] + (d[3] & 0x7F) * 0.1f;
  if (d[3] & 0x80) t = -t;
  return 0;
}

// ============================================================================
//  SENSOR UPDATE (main loop)
// ============================================================================
static void updateSensors() {
  static uint32_t lastDht = 0, lastSnap = 0;
  uint32_t now = millis();

  float t = NAN, h = NAN; int dc = -1; bool dhtNew = false;
  if (now - lastDht >= DHT_PERIOD_MS) { lastDht = now; dc = dhtRead(t, h); dhtNew = true; }

  if (now - lastSnap >= 500) {                      // refresh the snapshot used for telemetry
    lastSnap = now;
    Snap s;
    LOCK(); s = snap; UNLOCK();
    for (int i = 0; i < 2; i++) {
      long f, sp;
      s.hx[i] = hxStats(i, f, sp);
      s.cpg[i] = hx[i].cpg; s.userCal[i] = hx[i].userCal;
      if (s.hx[i]) {
        if (hx[i].userCal && fabsf(hx[i].cpg) > 1.0f) {
          float w = (float)(f - hx[i].tare) / hx[i].cpg;
          s.w[i] = fabsf(w) < 0.05f ? 0.0f : w;
          s.n[i] = fabsf((float)sp / hx[i].cpg);
        } else {
          s.w[i] = NAN;                     // never report fake grams before calibration
          s.n[i] = 0;
        }
      } else { s.w[i] = NAN; s.n[i] = NAN; }
    }
    LOCK(); snap.hx[0] = s.hx[0]; snap.hx[1] = s.hx[1]; snap.w[0] = s.w[0]; snap.w[1] = s.w[1]; snap.n[0] = s.n[0]; snap.n[1] = s.n[1];
    snap.cpg[0] = s.cpg[0]; snap.cpg[1] = s.cpg[1]; snap.userCal[0] = s.userCal[0]; snap.userCal[1] = s.userCal[1]; UNLOCK();
  }
  if (dhtNew) {
    LOCK();
    snap.dht = dc;
    if (dc == 0) { snap.t = t; snap.h = h; }
    else { snap.t = NAN; snap.h = NAN; }
    UNLOCK();
  }
}

// ============================================================================
//  USB SERIAL LINK: ESP32 -> local Python app, and Python app -> ESP32
// ============================================================================
static SchedItem pendingSched[MAX_SCHED];
static int pendingSchedCount = 0;
static char serialLine[192];
static size_t serialLineLen = 0;

static void applyPcTime(long epoch, int offsetMin, int dueMin) {
  struct timeval tv;
  tv.tv_sec = (time_t)epoch;
  tv.tv_usec = 0;
  settimeofday(&tv, nullptr);
  tzMin = offsetMin;
  dueWindowMin = (dueMin >= 5 && dueMin <= 240) ? dueMin : DUE_WINDOW_MIN_DEFAULT;
  char tz[24];
  int off = abs(offsetMin);
  // POSIX TZ signs are opposite to UTC offset signs.
  snprintf(tz, sizeof(tz), "UTC%c%02d:%02d", offsetMin >= 0 ? '-' : '+', off / 60, off % 60);
  setenv("TZ", tz, 1);
  tzset();
  lastPcSyncMs = millis();
  pcConnected = true;
}

static void parseSerialLine(char *line) {
  if (!line || !line[0]) return;
  if (strncmp(line, "SYNC:", 5) == 0) {
    long epoch = 0; int offset = DEFAULT_TZ_MIN, due = DUE_WINDOW_MIN_DEFAULT;
    if (sscanf(line + 5, "%ld:%d:%d", &epoch, &offset, &due) == 3 && epoch > 1700000000L) {
      applyPcTime(epoch, offset, due);
      pendingSchedCount = 0;
    }
    return;
  }
  if (strncmp(line, "CMD:TARE:", 9) == 0) {
    int shelf = atoi(line + 9);
    if (shelf == 1 || shelf == 2) cmdTare[shelf - 1] = 1;
    return;
  }
  if (strncmp(line, "CMD:CAL:", 8) == 0) {
    int shelf = 0; float grams = 0;
    if (sscanf(line + 8, "%d:%f", &shelf, &grams) == 2 && (shelf == 1 || shelf == 2) && grams >= 5.0f)
      cmdCalGrams[shelf - 1] = grams;
    return;
  }
  if (strncmp(line, "SCHED:", 6) == 0 && pendingSchedCount < MAX_SCHED) {
    char *saveptr = nullptr;
    char *timePart = strtok_r(line + 6, "|", &saveptr);
    char *namePart = strtok_r(nullptr, "|", &saveptr);
    char *dosePart = strtok_r(nullptr, "|", &saveptr);
    char *takenPart = strtok_r(nullptr, "|", &saveptr);
    if (timePart && namePart && dosePart && takenPart) {
      SchedItem &it = pendingSched[pendingSchedCount++];
      strncpy(it.time, timePart, sizeof(it.time) - 1); it.time[sizeof(it.time) - 1] = 0;
      strncpy(it.name, namePart, sizeof(it.name) - 1); it.name[sizeof(it.name) - 1] = 0;
      strncpy(it.dose, dosePart, sizeof(it.dose) - 1); it.dose[sizeof(it.dose) - 1] = 0;
      it.taken = atoi(takenPart) == 1;
    }
    return;
  }
  if (strcmp(line, "END") == 0) {
    LOCK();
    for (int i = 0; i < pendingSchedCount; i++) sched[i] = pendingSched[i];
    schedCount = pendingSchedCount;
    UNLOCK();
    return;
  }
}

static void readPcSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      serialLine[serialLineLen] = 0;
      parseSerialLine(serialLine);
      serialLineLen = 0;
    } else if (serialLineLen < sizeof(serialLine) - 1) {
      serialLine[serialLineLen++] = c;
    } else {
      serialLineLen = 0;  // discard an overlong/corrupt line
    }
  }
  if (millis() - lastPcSyncMs > 10000) pcConnected = false;
}

static void jsonNumber(char *out, size_t size, float value, int decimals) {
  if (!isfinite(value)) snprintf(out, size, "null");
  else snprintf(out, size, decimals == 0 ? "%.0f" : (decimals == 1 ? "%.1f" : "%.2f"), value);
}

static void sendTelemetry() {
  Snap s; char ack[48]; uint32_t aseq;
  LOCK(); s = snap; strncpy(ack, ackMsg, sizeof(ack)); ack[sizeof(ack) - 1] = 0; aseq = ackSeq; UNLOCK();
  char t[20], h[20], w1[20], w2[20], n1[20], n2[20], c1[20], c2[20];
  jsonNumber(t, sizeof(t), s.dht == 0 ? s.t : NAN, 1);
  jsonNumber(h, sizeof(h), s.dht == 0 ? s.h : NAN, 0);
  jsonNumber(w1, sizeof(w1), s.w[0], 1); jsonNumber(w2, sizeof(w2), s.w[1], 1);
  jsonNumber(n1, sizeof(n1), s.n[0], 2); jsonNumber(n2, sizeof(n2), s.n[1], 2);
  jsonNumber(c1, sizeof(c1), s.cpg[0], 1); jsonNumber(c2, sizeof(c2), s.cpg[1], 1);
  char js[620];
  snprintf(js, sizeof(js),
    "{\"type\":\"telemetry\",\"t\":%s,\"h\":%s,\"dht\":%d,\"w1\":%s,\"w2\":%s,\"n1\":%s,\"n2\":%s,\"hx1\":%s,\"hx2\":%s,\"cal1\":%s,\"cal2\":%s,\"c1\":%s,\"c2\":%s,\"up\":%lu,\"fw\":\"%s\",\"ack\":\"%s\",\"aseq\":%lu}",
    t, h, s.dht, w1, w2, n1, n2, s.hx[0] ? "true" : "false", s.hx[1] ? "true" : "false",
    c1, c2, s.userCal[0] ? "true" : "false", s.userCal[1] ? "true" : "false",
    (unsigned long)(millis() / 1000), FW_VERSION, ack, (unsigned long)aseq);
  Serial.print("MBX:");
  Serial.println(js);
}

// ============================================================================
//  OLED USER INTERFACE
// ============================================================================
static bool oledOk = false;
static uint8_t oledAddr = OLED_ADDR_A;
static bool i2cPing(uint8_t a) { Wire.beginTransmission(a); return Wire.endTransmission() == 0; }
static bool oledTryInit() {
  uint8_t a = 0;
  if (i2cPing(OLED_ADDR_A)) a = OLED_ADDR_A; else if (i2cPing(OLED_ADDR_B)) a = OLED_ADDR_B; else return false;
  oled.setI2CAddress(a << 1);
  oled.begin();
  Wire.setClock(400000);
  oledAddr = a;
  return true;
}

// Avoid a custom return type here. The Arduino IDE can generate a function
// prototype before a local struct declaration, which causes the
// "DueInfo does not name a type" compilation error.
static void computeDue(int nowMin,
                       bool &isDue, char *dueTime, size_t dueTimeSize,
                       char *dueName, size_t dueNameSize,
                       char *dueDose, size_t dueDoseSize,
                       bool &haveNext, char *nextTime, size_t nextTimeSize,
                       char *nextName, size_t nextNameSize) {
  isDue = false;
  haveNext = false;
  if (dueTime && dueTimeSize) dueTime[0] = '\0';
  if (dueName && dueNameSize) dueName[0] = '\0';
  if (dueDose && dueDoseSize) dueDose[0] = '\0';
  if (nextTime && nextTimeSize) nextTime[0] = '\0';
  if (nextName && nextNameSize) nextName[0] = '\0';
  if (nowMin < 0) return;

  SchedItem local[MAX_SCHED];
  int n = 0;
  int win = DUE_WINDOW_MIN_DEFAULT;
  LOCK();
  n = schedCount;
  win = dueWindowMin;
  for (int i = 0; i < n; i++) local[i] = sched[i];
  UNLOCK();

  int bestNext = 24 * 60 + 1;
  for (int i = 0; i < n; i++) {
    int hh = 0, mm = 0;
    if (sscanf(local[i].time, "%d:%d", &hh, &mm) != 2) continue;
    if (hh < 0 || hh > 23 || mm < 0 || mm > 59) continue;
    int t = hh * 60 + mm;

    if (!local[i].taken && nowMin >= t && nowMin < t + win && !isDue) {
      isDue = true;
      if (dueTime && dueTimeSize) snprintf(dueTime, dueTimeSize, "%s", local[i].time);
      if (dueName && dueNameSize) snprintf(dueName, dueNameSize, "%s", local[i].name);
      if (dueDose && dueDoseSize) snprintf(dueDose, dueDoseSize, "%s", local[i].dose);
    }
    if (!local[i].taken && t > nowMin && t < bestNext) {
      bestNext = t;
      haveNext = true;
      if (nextTime && nextTimeSize) snprintf(nextTime, nextTimeSize, "%s", local[i].time);
      if (nextName && nextNameSize) snprintf(nextName, nextNameSize, "%s", local[i].name);
    }
  }
}

static void drawUI() {
  char b[64];
  time_t nowT = time(nullptr);
  bool synced = nowT > 1700000000;
  struct tm tmv; memset(&tmv, 0, sizeof(tmv));
  if (synced) localtime_r(&nowT, &tmv);
  bool doseDue = false;
  bool haveNextDose = false;
  char dueTime[6] = "";
  char dueName[24] = "";
  char dueDose[18] = "";
  char nextDoseTime[6] = "";
  char nextDoseName[24] = "";
  computeDue(synced ? tmv.tm_hour * 60 + tmv.tm_min : -1,
             doseDue, dueTime, sizeof(dueTime),
             dueName, sizeof(dueName), dueDose, sizeof(dueDose),
             haveNextDose, nextDoseTime, sizeof(nextDoseTime),
             nextDoseName, sizeof(nextDoseName));
  bool flash = (millis() / 500) % 2;

  Snap s; LOCK(); s = snap; UNLOCK();

  oled.clearBuffer();
  // ---- header: temperature / humidity, connection ----
  oled.setFont(u8g2_font_5x8_tf);
  if (s.dht == 0) snprintf(b, sizeof(b), "T%.1fC H%.0f%%", s.t, s.h); else snprintf(b, sizeof(b), "T--  H--");
  oled.drawStr(0, 7, b);
  const char *conn = pcConnected ? "USB linked" : "no PC";
  oled.drawStr(128 - oled.getStrWidth(conn), 7, conn);

  // ---- big clock ----
  oled.setFont(u8g2_font_logisoso24_tn);
  if (synced) {
    char hh[3], mm[3];
    snprintf(hh, sizeof(hh), "%02d", tmv.tm_hour); snprintf(mm, sizeof(mm), "%02d", tmv.tm_min);
    int wd = oled.getStrWidth(hh), cw = 8, total = wd * 2 + cw;
    int x = (128 - total) / 2;
    oled.drawStr(x, 36, hh);
    oled.drawBox(x + wd + 2, 18, 3, 3); oled.drawBox(x + wd + 2, 28, 3, 3);   // colon
    oled.drawStr(x + wd + cw, 36, mm);
    oled.setFont(u8g2_font_5x8_tf);
    snprintf(b, sizeof(b), "%02d", tmv.tm_sec);
    oled.drawStr(x + total + 3, 36, b);
  } else {
    oled.setFont(u8g2_font_6x10_tf);
    oled.drawStr(18, 30, pcConnected ? "Syncing clock..." : "Connect USB to PC...");
  }

  // ---- bottom: dose reminder ----
  if (doseDue) {
    if (flash) { oled.drawBox(0, 40, 128, 24); oled.setDrawColor(0); }
    oled.setFont(u8g2_font_6x10_tf);
    snprintf(b, sizeof(b), "TAKE NOW  %s", dueTime); oled.drawStr(2, 51, b);
    snprintf(b, sizeof(b), "%s %s", dueName, dueDose); b[21] = 0; oled.drawStr(2, 62, b);
    oled.setDrawColor(1);
  } else {
    oled.setFont(u8g2_font_6x10_tf);
    if (haveNextDose) {
      snprintf(b, sizeof(b), "Next dose %s", nextDoseTime); oled.drawStr(2, 51, b);
      snprintf(b, sizeof(b), "%s", nextDoseName); b[21] = 0; oled.drawStr(2, 62, b);
    } else {
      oled.drawStr(2, 51, synced ? "No more doses today" : "");
      oled.drawStr(2, 62, "USB powered");
    }
  }
  oled.sendBuffer();

  // ---- LEDs ----
  bool problem = !pcConnected || s.dht != 0 || !s.hx[0] || !s.hx[1];
  if (doseDue) { digitalWrite(PIN_LED_RED, flash); digitalWrite(PIN_LED_GREEN, LOW); }
  else if (problem) { digitalWrite(PIN_LED_RED, HIGH); digitalWrite(PIN_LED_GREEN, LOW); }
  else { digitalWrite(PIN_LED_RED, LOW); digitalWrite(PIN_LED_GREEN, HIGH); }
}

// ============================================================================
//  SETUP / LOOP
// ============================================================================
void setup() {
  Serial.begin(115200);
  delay(400);
  Serial.println("\nSMART MEDICINE BOX demo firmware " FW_VERSION);
  mtx = xSemaphoreCreateMutex();

  pinMode(PIN_LED_RED, OUTPUT); pinMode(PIN_LED_GREEN, OUTPUT);
  digitalWrite(PIN_LED_RED, HIGH); digitalWrite(PIN_LED_GREEN, HIGH);
  pinMode(PIN_DHT, INPUT_PULLUP);
  hxInit();

  prefs.begin("medbox", false);
  for (int i = 0; i < 2; i++) {
    char key[12];
    snprintf(key, sizeof(key), "tare%d", i + 1); hx[i].tare = prefs.getLong(key, 0);
    snprintf(key, sizeof(key), "cpg%d", i + 1);  hx[i].cpg = prefs.getFloat(key, DEFAULT_COUNTS_PER_GRAM);
    snprintf(key, sizeof(key), "ucal%d", i + 1); hx[i].userCal = prefs.getBool(key, false);
  }

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);
  oledOk = oledTryInit();
  if (oledOk) {
    oled.clearBuffer(); oled.setFont(u8g2_font_6x10_tf);
    oled.drawStr(0, 14, "SMART MED BOX"); oled.drawStr(0, 30, "Starting..."); oled.drawStr(0, 46, "Waiting for PC USB");
    oled.sendBuffer();
    Serial.printf("OLED found at 0x%02X\n", oledAddr);
  } else Serial.println("OLED not found (check 3V3, GND, SDA=21, SCL=22). Continuing without display.");

  Serial.println("USB serial link ready. Start the local Python MedBox app on the PC.");
  delay(600);
  digitalWrite(PIN_LED_RED, LOW); digitalWrite(PIN_LED_GREEN, LOW);
}

void loop() {
  static uint32_t lastUi = 0, lastLog = 0;
  hxPoll();
  readPcSerial();
  handleCommands();
  updateSensors();

  uint32_t now = millis();
  static uint32_t lastTelemetry = 0;
  if (now - lastTelemetry >= POST_INTERVAL_MS) { lastTelemetry = now; sendTelemetry(); }
  if (now - lastUi >= 250) {
    lastUi = now;
    if (oledOk && !i2cPing(oledAddr)) oledOk = false;
    if (!oledOk && (now / 250) % 20 == 0) oledOk = oledTryInit();
    if (oledOk) drawUI();
    else {                                          // no OLED: still drive the LEDs
      Snap s; LOCK(); s = snap; UNLOCK();
      bool problem = !pcConnected || s.dht != 0 || !s.hx[0] || !s.hx[1];
      digitalWrite(PIN_LED_RED, problem); digitalWrite(PIN_LED_GREEN, !problem);
    }
  }
  if (now - lastLog >= 5000) {
    lastLog = now;
    Snap s; LOCK(); s = snap; UNLOCK();
    Serial.printf("[%lus] T=%.1f H=%.0f (dht %d) | S1 %.1f g%s | S2 %.1f g%s | USB %d\n",
                  (unsigned long)(now / 1000), s.t, s.h, s.dht, s.w[0], s.hx[0] ? "" : " (no data)", s.w[1], s.hx[1] ? "" : " (no data)", (int)pcConnected);
  }
  delay(2);
}
