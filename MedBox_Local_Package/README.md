# MedBox — Local USB Demo (Single-page website)

This is the simplified local version of the Smart Medicine Box. It keeps the ESP32 USB-serial connection and replaces the old customer/admin pages with one clean storage screen.

## What the website does

- One home page; no customer/admin login or separate tabs.
- Shows the live weight on Shelf 1 and Shelf 2, plus temperature, humidity and MKT.
- **Add item** asks for a name, shelf and the **full medicine pack/container weight** in grams. You can add multiple ingestion times while creating or editing an item.
- The **＋ Add ingestion** button in the reminder panel adds another time to an existing item. Reminders are also sent to the OLED.
- **Reload** reads both calibrated load cells. It records the initial storage weight on the first run, then compares later readings with the previous baseline.
- When a weight change matches one known pack or a unique combination, the item list is updated automatically. If multiple combinations match, the website asks you to choose instead of guessing.
- A compact **Scale setup** panel on the same screen provides tare and calibration for both shelves.

## Hardware used

- ESP32 DevKit connected to laptop by USB data cable
- DHT11 temperature/humidity sensor
- Two HX711 modules and two load cells
- SSD1315 128×64 OLED
- Red and green LEDs

No battery, hotspot/Wi-Fi, LDR, reed switch, servo, keypad or LCD is used.

## Start the local website (Windows)

1. Install Python 3.10 or newer. During setup, enable **Add Python to PATH**.
2. Connect the ESP32 to the laptop using a USB data cable.
3. Close Arduino IDE Serial Monitor / Serial Plotter (only one program can own the ESP32 COM port).
4. Double-click **`start_medbox.bat`**.
5. The launcher creates its own `.venv`, installs Flask and pyserial automatically, starts the local app and opens `http://127.0.0.1:5000`.

The first dependency installation needs an internet connection. After installation, the website and ESP32 data connection run locally on the laptop; no website hosting, phone hotspot or cloud service is used.

## Flash the ESP32

1. In Arduino IDE, select **ESP32 Dev Module**.
2. Install the **U8g2** library from Library Manager.
3. Open `firmware/medbox_demo/medbox_demo.ino` and upload it.
4. Keep the ESP32 connected by USB and start `start_medbox.bat`.

The firmware sends telemetry lines beginning with `MBX:` over USB. The Python app reads them and sends the PC clock, reminder schedule and tare/calibration commands back over the same cable.

## First-time setup and use

1. Open the website. There is no login.
2. Expand **Scale setup**. For each shelf, remove everything and press **Tare empty shelf**. Put a known weight on that shelf (for example 500 g), enter `500`, and press **Calibrate**. Wait for the firmware's next telemetry update before checking the live reading.
3. Press **Add item**. Enter the item name, shelf and the weight of the **complete pack/container**, not the weight of one tablet. Use **＋ Add ingestion time** to add one or more reminder times, or leave it blank.
4. Place all registered items on their selected shelves.
5. Press **Reload** once. This first reload records the starting storage weight and treats the registered items currently on the shelves as present.
6. Later, when a complete pack/container is removed or added, press **Reload** again. The page compares the new shelf weight with the saved baseline and tries to identify the item(s).
7. Use the item's clock icon or the **＋ Add ingestion** button to add or edit multiple times later. Each reminder is sent to the OLED.

## Important limits

- Weight matching works best when a whole known pack/container is added or removed. It cannot reliably identify partial consumption from a bottle or a partially used container.
- The entered pack weight should closely match the weight change seen by the load cell. Similar pack weights can create ambiguous matches; the page will ask you to confirm.
- Calibrate both shelves before using Reload. The readings are demonstration measurements, not a certified medicine-storage system.
- DHT11 readings are approximate. Follow the storage conditions printed on each medicine's label.
- Data is saved locally in `server/data.json`. Keep the extracted project folder intact.

## COM port troubleshooting

The app auto-detects common ESP32 serial adapters. If more than one serial device is connected, set `MEDBOX_SERIAL_PORT` before starting, for example:

```bat
set MEDBOX_SERIAL_PORT=COM6
start_medbox.bat
```

Find the port in Windows Device Manager → Ports (COM & LPT). If no ESP32 COM port appears, install the relevant USB-to-UART driver (often CP210x or CH340), use a USB data cable and reconnect.

## Wiring pin map

| Module signal | ESP32 GPIO |
|---|---:|
| DHT11 DATA | 4 |
| Red LED | 13 |
| HX711 Shelf 1 DOUT / SCK | 16 / 17 |
| HX711 Shelf 2 DOUT / SCK | 18 / 19 |
| OLED SDA / SCL | 21 / 22 |
| Green LED | 27 |

Power the DHT11 module, both HX711 modules and OLED from ESP32 3V3 and GND. All grounds must be common. Follow your load-cell manufacturer's wire labels. The HX711 SCK pins are driven by the ESP32 and held low between reads; do not add an SCK pull-up.

## Files

- `server/app.py` — local Python web app, USB serial reader, storage matching and reminder API
- `server/index.html` — single-page website
- `server/requirements.txt` — automatically installed Python packages
- `start_medbox.bat` — Windows launcher
- `firmware/medbox_demo/` — ESP32 sketch and pin configuration

The web page JavaScript and Python backend pass syntax checks. Calibration number fields are kept in place during 2.5-second live refreshes, and multiple ingestion times per item are supported (up to 16 total reminders, matching the firmware limit). The ESP32 firmware and physical load-cell accuracy still need to be verified on your actual board and wiring.
