# PumpSleeper

A local proxy and dashboard for **PumpSpy** sump pump monitors. PumpSleeper sits between your PumpSpy device and pumpspy.com, logging every event locally and giving you full control — even when the PumpSpy cloud is down.

## Features

- **Transparent proxy** — device keeps talking to pumpspy.com normally; you get a local copy of all data
- **Takeover mode** — answer the device locally when the cloud is unreachable
- **Local dashboard** — pump run history, stats, signal strength, battery voltage
- **Home Assistant integration** — 17 MQTT entities with auto-discovery; custom Lovelace card included
- **Mode toggle** — switch Proxy ↔ Takeover from the dashboard or HA card
- **Notifications** — HA automations for backup pump runs, device offline, and long main pump runs

---

## Hardware requirements

| Component | Requirement |
|-----------|-------------|
| Raspberry Pi | Pi 3B+ or newer recommended (Pi 4 ideal) |
| WiFi | Built-in or USB WiFi adapter |
| Storage | 8GB+ SD card |
| OS | Raspberry Pi OS (Bookworm or Bullseye) |

---

## Installation — Option A: Pi installer (recommended)

This is the easiest path. One script sets up everything: hotspot, iptables interception, Python services, and systemd.

```bash
# Download and run the installer
curl -fsSL https://raw.githubusercontent.com/pgoutsos/pumpsleeper/main/install.sh | sudo bash
```

Or clone the repo first:

```bash
git clone https://github.com/pgoutsos/pumpsleeper.git
cd pumpsleeper
sudo bash install.sh
```

The installer will prompt you for:
- WiFi interface name (usually `wlan0`)
- Hotspot SSID and password
- Optional MQTT broker details for Home Assistant

After installation, connect your PumpSpy device to the hotspot you configured. It will appear in the dashboard within a few minutes.

---

## Installation — Option B: Docker (any Linux machine)

Use this if you want to run PumpSleeper on a NAS, Mac Mini, or any Linux box. You still need a Raspberry Pi (or similar) to create the WiFi hotspot and intercept the device traffic — the Docker container just runs the server and dashboard.

### 1. Network setup (on the Pi)

Install the hotspot and iptables rules on the Pi:

```bash
# Set up WiFi hotspot
sudo nmcli con add type wifi ifname wlan0 con-name PumpSleeper-Hotspot \
    autoconnect yes ssid PumpSpyLab mode ap ipv4.method shared \
    ipv4.addresses 192.168.50.1/24 \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "pumpspy123"
sudo nmcli con up PumpSleeper-Hotspot

# Forward device traffic to your server machine (replace 192.168.0.100 with your server's IP)
SERVER_IP=192.168.0.100
for ip in 206.80.104.221 64.227.40.212 64.227.46.155 64.227.33.97 \
          64.225.50.52 64.225.51.200 64.225.50.148 64.225.50.146; do
    sudo iptables -t nat -A PREROUTING -i wlan0 -p tcp -d $ip --dport 8081 \
        -j DNAT --to-destination ${SERVER_IP}:8081
done
sudo iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE
sudo sysctl -w net.ipv4.ip_forward=1
sudo netfilter-persistent save
```

### 2. Run PumpSleeper on your server

```bash
git clone https://github.com/pgoutsos/pumpsleeper.git
cd pumpsleeper

# Optional: configure MQTT in docker-compose.app.yml first
docker compose -f docker-compose.app.yml up -d
```

Dashboard is available at `http://YOUR_SERVER_IP:8080`

---

## Home Assistant integration

PumpSleeper publishes 17 entities to your MQTT broker using HA auto-discovery — no YAML configuration needed in HA.

### 1. Run Mosquitto + Home Assistant

Use the included `homelab/docker-compose.yml` to run both on any machine:

```bash
cd homelab
docker compose up -d
```

### 2. Configure MQTT broker details

Set `PUMPSLEEPER_MQTT_HOST` to your broker's IP:

**Pi install:** Edit `/opt/pumpsleeper/pumpsleeper.env` and restart:
```bash
sudo nano /opt/pumpsleeper/pumpsleeper.env
# Set: PUMPSLEEPER_MQTT_HOST=192.168.0.55
sudo systemctl restart pumpsleeper
```

**Docker install:** Uncomment the MQTT env vars in `docker-compose.app.yml` and restart:
```bash
docker compose -f docker-compose.app.yml up -d
```

### 3. Connect HA to Mosquitto

In Home Assistant: **Settings → Devices & Services → Add Integration → MQTT**
- Broker: `mosquitto` (or your broker's IP)
- Port: `1883`
- No credentials needed (if using the included config)

The **PumpSleeper** device will appear automatically with all 17 entities.

### 4. Install the custom Lovelace card

Copy `homelab/homeassistant/www/pumpsleeper-card.js` to your HA config's `www/` folder, then register it as a resource:

**Settings → Dashboards → Resources → Add Resource**
- URL: `/local/pumpsleeper-card.js`
- Type: JavaScript module

Add the card to a dashboard with:
```yaml
type: custom:pumpsleeper-card
entities:
  device_online:        binary_sensor.pumpsleeper_device_online
  main_pump_running:    binary_sensor.pumpsleeper_main_pump_running
  signal_strength:      sensor.pumpsleeper_signal_strength
  mode:                 select.pumpsleeper_mode
  operating_status:     sensor.pumpsleeper_operating_status
  last_ping_ts:         sensor.pumpsleeper_last_device_ping
  mode_switched_ts:     sensor.pumpsleeper_mode_switched_at
  main_runs_today:      sensor.pumpsleeper_main_pump_runs_today
  main_runtime_today:   sensor.pumpsleeper_main_pump_runtime_today
  main_gallons_today:   sensor.pumpsleeper_main_pump_gallons_today
  main_last_run:        sensor.pumpsleeper_main_pump_last_run
  backup_runs_today:    sensor.pumpsleeper_backup_pump_runs_today
  backup_runtime_today: sensor.pumpsleeper_backup_pump_runtime_today
  backup_gallons_today: sensor.pumpsleeper_backup_pump_gallons_today
  backup_last_run:      sensor.pumpsleeper_backup_pump_last_run
  backup_last_trigger:  sensor.pumpsleeper_backup_pump_last_trigger
  battery_voltage:      sensor.pumpsleeper_backup_battery_voltage
  loaded_voltage:       sensor.pumpsleeper_backup_loaded_voltage
```

---

## Configuration reference

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `PUMPSPY_DATA` | script directory | Directory for SQLite database and logs |
| `PUMPSPY_REAL_SERVER` | `http://206.80.104.221:8081` | Real PumpSpy cloud server |
| `PUMPSPY_PROXY` | `1` | Set to `0` to disable cloud forwarding |
| `PUMPSPY_PROXY_TIMEOUT` | `8` | Seconds to wait for cloud response |
| `PUMPSLEEPER_MQTT_HOST` | *(disabled)* | MQTT broker IP — leave blank to disable MQTT |
| `PUMPSLEEPER_MQTT_PORT` | `1883` | MQTT broker port |
| `PUMPSLEEPER_MQTT_USER` | *(none)* | MQTT username |
| `PUMPSLEEPER_MQTT_PASSWORD` | *(none)* | MQTT password |
| `PUMPSLEEPER_MQTT_PREFIX` | `pumpsleeper` | MQTT topic prefix |
| `PUMPSLEEPER_DEVICE_ID` | `pumpsleeper_01` | Unique device ID for HA |

---

## Troubleshooting

**Device not appearing in dashboard**
- Confirm the device is connected to the PumpSleeper hotspot (not your main WiFi)
- Check iptables rules: `sudo iptables -t nat -L PREROUTING -n -v`
- Check server log: `sudo journalctl -u pumpsleeper -f` (Pi) or `docker logs pumpsleeper -f` (Docker)

**Dashboard showing wrong date for today's stats**
- The dashboard uses your browser's timezone. Make sure your Pi's timezone is set correctly: `sudo timedatectl set-timezone America/New_York`

**MQTT not connecting**
- Verify the broker is reachable: `mosquitto_pub -h YOUR_BROKER_IP -t test -m hello`
- Check server log for `MQTT connected` or error messages

**Switching to Takeover mode but device still shows offline in HA**
- The device needs to re-authenticate against PumpSleeper after mode switches
- The card shows a "Pending" countdown (up to 3 minutes) while waiting for the device to check in
- If it stays offline after 3 minutes, check that the device is still connected to the hotspot

---

## Project structure

```
pumpsleeper/
├── app/
│   ├── server.py        # Proxy server (port 8081)
│   ├── dashboard.py     # Web dashboard (port 8080)
│   ├── db.py            # SQLite helpers
│   ├── mqtt.py          # HA MQTT integration
│   └── requirements.txt
├── homelab/
│   ├── docker-compose.yml              # HA + Mosquitto
│   ├── mosquitto/config/mosquitto.conf
│   └── homeassistant/www/
│       └── pumpsleeper-card.js         # Custom Lovelace card
├── install.sh           # Pi installer
├── Dockerfile           # Docker image
├── docker-compose.app.yml  # Docker Compose (server + dashboard)
└── docker-entrypoint.sh
```

---

## License

MIT
