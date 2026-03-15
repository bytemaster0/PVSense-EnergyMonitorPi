# Solar Energy Monitor

A Raspberry Pi display that shows real-time and daily solar production statistics on a Waveshare 7.5" HD 3-color e-ink screen.

Data is pulled from two sources: **PVOutput** (inverter generation history) and **Sense Energy** (real-time home power monitoring). The display refreshes every 2 minutes with live readings, and every 5 minutes with a full daily data update.

---

![description](https://github.com/bytemaster0/PVSense-EnergyMonitorPi/blob/main/sampleImage.jpg)

## Display layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SOLAR ENERGY MONITOR                          Sunday, March 15 2026        │
│  Updated 2:34 PM                                                            │
├──────────────┬──────────────┬──────────────┬──────────────┬─────────────── │
│ Peak Power   │ Total Energy │ Net Energy   │ Solar Now    │ Using Now       │
│ 10,944 W     │ 74.3 kWh     │ 30.1 kWh     │ 3,800 W      │ 1,200 W        │
│  (black)     │  (black)     │  (black)     │  (red)       │  (red)         │
├──────────────┴──────────────┴──────────────┴──────────────┴─────────────── │
│                                                                             │
│              [red filled-area graph — 5-minute PVOutput data]              │
│                                                                             │
│  6a  7a  8a  9a  10a  11a  12p  1p  2p  3p  4p  5p  6p  7p  8p  9p       │
└─────────────────────────────────────────────────────────────────────────────┘
```

| Column | Source | Colour | Description |
|---|---|---|---|
| Peak Power | PVOutput / Sense | Black | Highest single-watt reading today |
| Total Energy | PVOutput | Black | Gross solar generation today (kWh) |
| Net Energy | Sense | Black | To-grid minus from-grid (net metering balance; negative = net importer) |
| Solar Now | Sense (live) | Red | Instantaneous solar production (W) |
| Using Now | Sense (live) | Red | Instantaneous home consumption (W) |

---

## Hardware

| Component | Details |
|---|---|
| **Computer** | Raspberry Pi Zero W (or any Pi) |
| **Display** | [Waveshare 7.5" HD 3-colour e-Paper HAT (B)](https://www.waveshare.com/wiki/7.5inch_HD_e-Paper_HAT_(B)) |
| **Resolution** | 880 × 528 px — black, red, white |
| **Connection** | SPI (GPIO header, HAT format — no wiring needed) |

---

## Data sources

### PVOutput
[PVOutput](https://pvoutput.org) is a free service that logs inverter output. Your inverter (or a monitoring gateway) uploads readings every 5 minutes via the PVOutput API.

This monitor uses two PVOutput API v2 endpoints per refresh cycle:

- `getstatus.jsp` — 5-minute interval history (used for the graph)
- `getoutput.jsp` — authoritative daily total and recorded peak power

**Requirements:** A free PVOutput account with at least one registered system. The API key and system ID are found under *Settings → API Settings* on the PVOutput website.

### Sense Energy
[Sense](https://sense.com) is a home energy monitor with a current-transformer clamp on the main panel. It detects individual appliances via machine learning and measures real-time solar production (requires the Sense Solar add-on CT clamps).

This monitor uses Sense's **unofficial** REST + WebSocket API directly — no third-party Sense library is required:

- REST `trends` endpoint — daily cumulative `to_grid` / `from_grid` energy (for Net Energy)
- WebSocket `realtimefeed` — instantaneous solar W and home consumption W

**Note:** Sense does not publish or support this API. It works as of early 2026 but could change without notice.

---

## Prerequisites

### On the Raspberry Pi

**Raspberry Pi OS** (Bullseye or later recommended; works on Buster with Python 3.7+)

**SPI enabled** — run `sudo raspi-config` → *Interface Options → SPI → Enable*

**Waveshare e-Paper Python library:**
```bash
git clone https://github.com/waveshare/e-Paper.git /home/pi/e-Paper
```
The library path `/home/pi/e-Paper/RaspberryPi_JetsonNano/python/lib` is added to `sys.path` automatically by `config.py`. If you clone it elsewhere, update that path in `config.py`.

**System packages:**
```bash
sudo apt-get update
sudo apt-get install -y python3-pip python3-pil libopenjp2-7 fonts-dejavu-core
```

**Python packages:**
```bash
pip3 install -r requirements.txt
```

---

## Installation

```bash
git clone https://github.com/yourusername/solar-energy-monitor.git
cd solar-energy-monitor
pip3 install -r requirements.txt
cp config.py.example config.py   # then edit with your credentials
```

---

## Configuration

All settings live in `config.py`. Edit the following before running:

```python
# ── PVOutput ──────────────────────────────────────────────────────────────────
PVOUTPUT_API_KEY   = "your_api_key_here"
PVOUTPUT_SYSTEM_ID = "12345"          # numeric string from PVOutput Settings

# ── Sense Energy ──────────────────────────────────────────────────────────────
SENSE_EMAIL    = "you@example.com"
SENSE_PASSWORD = "your_sense_password"

# ── Display ───────────────────────────────────────────────────────────────────
DISPLAY_WIDTH  = 880     # Waveshare 7.5" HD — do not change unless using a different model
DISPLAY_HEIGHT = 528

# ── Behaviour ─────────────────────────────────────────────────────────────────
UPDATE_INTERVAL_SECONDS   = 300   # Full refresh (PVOutput + Sense daily) — matches PVOutput cadence
REALTIME_INTERVAL_SECONDS = 120   # Realtime-only refresh (Sense live readings)

GRAPH_START_HOUR = 6    # 6 AM
GRAPH_END_HOUR   = 21   # 9 PM
```

The Waveshare library path is also set at the top of `config.py`. Update it if you cloned the library to a different location:

```python
sys.path.insert(0, "/home/pi/e-Paper/RaspberryPi_JetsonNano/python/lib")
```

---

## Running

**Single refresh (useful for testing):**
```bash
python3 main.py --once
```

**Dry run (renders `preview.png` without touching the display hardware):**
```bash
python3 main.py --dry-run
```

**Normal operation:**
```bash
python3 main.py
```

---

## Running as a systemd service

To have the monitor start automatically on boot:

1. Create the service file:

```bash
sudo nano /etc/systemd/system/solarmonitor.service
```

```ini
[Unit]
Description=Solar Energy Monitor
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/solar-energy-monitor/main.py
WorkingDirectory=/home/pi/solar-energy-monitor
User=pi
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

2. Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable solarmonitor
sudo systemctl start solarmonitor
```

3. Check status and logs:

```bash
sudo systemctl status solarmonitor
journalctl -u solarmonitor -f
```

---

## File structure

```
solar-energy-monitor/
├── main.py               # Entry point — dual-cadence event loop
├── config.py             # Site-specific settings (credentials, display size, timing)
├── pvoutput_client.py    # PVOutput API v2 wrapper (getstatus + getoutput)
├── sense_client.py       # Sense Energy REST + WebSocket client
├── data_cache.py         # Reboot-resilient JSON cache (atomic writes)
├── display_renderer.py   # PIL image composition + Waveshare EPD driver
├── requirements.txt      # Python dependencies
└── cache.json            # Auto-generated — last known-good data (do not edit)
```

---

## How it works

### Dual refresh cadence

| Cadence | What happens |
|---|---|
| Every **2 minutes** | Fetch instantaneous solar W + home W from Sense WebSocket; re-draw display |
| Every **5 minutes** | Also fetch PVOutput interval history and Sense daily energy totals; update cache |

The 5-minute full refresh is timed to match PVOutput's upload interval, so the graph always shows the latest complete 5-minute window.

On full-refresh cycles, the Sense daily call also captures the current realtime reading at no extra API cost, so only one Sense connection is opened.

### Reboot resilience

On an unexpected reboot, the display would normally show zeros until WiFi connects and both APIs respond (up to 60–90 seconds on a Pi Zero W). To avoid this, `data_cache.py` atomically persists the last known-good data to `cache.json` after every successful fetch. On startup, the cached data is shown immediately (marked as "Cached — live at H:MM") until live data arrives.

Cache entries are keyed to the current date. At midnight the cache is automatically discarded so yesterday's totals never bleed into the new day.

### Peak power tracking

PVOutput's `getoutput.jsp` sometimes returns 0 for peak power (if the inverter doesn't upload instantaneous peaks). To compensate, the Sense WebSocket's real-time solar readings are tracked as a running daily maximum throughout the day. Whichever source records the higher value wins.

### Net energy

Net Energy = `to_grid − from_grid` from the Sense trends API. This reflects your net metering balance for the day:

- **Positive** — you exported more solar than you imported (net producer)
- **Negative** — you drew more from the grid than your solar produced (net importer, e.g. overnight or on cloudy days)

---

## Troubleshooting

**Display not found / SPI error**
Make sure SPI is enabled (`sudo raspi-config`) and the HAT is seated properly.

**PVOutput returns no data**
Before inverters wake at dawn, PVOutput returns a "No status found" response — this is normal. The display will show cached yesterday data (marked stale) until the first upload of the day arrives.

**Sense authentication fails**
Double-check `SENSE_EMAIL` and `SENSE_PASSWORD` in `config.py`. If you use two-factor authentication on your Sense account, you may need to use an app password.

**Net Energy shows wrong day**
The Sense trends API requires the `start` timestamp in UTC. The client converts local midnight to UTC automatically, but if your Pi's system clock or timezone is incorrect this will return the wrong day's data. Verify with `timedatectl`.

**Graph is empty**
PVOutput's `getstatus.jsp` returns all rows on one semicolon-delimited line. Check the log output for `getstatus.jsp: N rows` — if N is 1 or 0, PVOutput may not have any data for today yet, or the system ID is incorrect.

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | PVOutput and Sense REST API calls |
| `websocket-client` | Sense real-time WebSocket feed |
| `Pillow` | Image composition for the e-ink frame |
| `RPi.GPIO` | GPIO control (required by Waveshare library) |
| `spidev` | SPI bus communication (required by Waveshare library) |

The Waveshare e-Paper library itself is not on PyPI — it must be cloned from the [Waveshare GitHub repository](https://github.com/waveshare/e-Paper).

---

## License

MIT
