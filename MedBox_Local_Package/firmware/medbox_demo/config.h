// ============================================================================
// config.h - USB-serial MedBox demo (ESP32-WROOM-32 DevKit V1)
// No hotspot or network configuration is used in this local version.
// ==========================================================================
#pragma once
#include <Arduino.h>

// ------------------------------ PINS ---------------------------------------
#define PIN_DHT        4     // DHT11 DATA
#define PIN_LED_RED    13    // GPIO13 -> 220R -> LED -> GND
#define PIN_HX1_DOUT   16    // HX711 #1 (shelf 1)
#define PIN_HX1_SCK    17
#define PIN_HX2_DOUT   18    // HX711 #2 (shelf 2)
#define PIN_HX2_SCK    19
#define PIN_SDA        21    // SSD1315 OLED SDA
#define PIN_SCL        22    // SSD1315 OLED SCL
#define PIN_LED_GREEN  27    // GPIO27 -> 220R -> LED -> GND

// ------------------------------ SETTINGS -----------------------------------
#define OLED_ADDR_A    0x3C
#define OLED_ADDR_B    0x3D
#define DEFAULT_COUNTS_PER_GRAM 215.0f  // placeholder only; calibrate both shelves
#define HX_RING        10
#define DHT_PERIOD_MS  3000
#define POST_INTERVAL_MS 3000
#define DUE_WINDOW_MIN_DEFAULT 30
#define DEFAULT_TZ_MIN 330  // India Standard Time; PC sync also supplies this offset
#define MAX_SCHED      16
#define FW_VERSION     "local1.0"
#define SERIAL_BAUD    115200
