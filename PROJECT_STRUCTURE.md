# PumpSpy — Packaged Solution Design

## Goals
- Single codebase that runs on Raspberry Pi, macOS, Windows, and Docker
- One config file drives all behaviour (no hardcoded values)
- SQLite replaces events.jsonl for proper querying and performance
- Simple CLI to start, stop, and check status
- Platform setup scripts handle the OS-specific network redirect once at install time

---

## Directory Layout

```
pumpspy/
│
├── docker-compose.yml          # spin up both services with one command
├── Dockerfile                  # builds the app image (used by compose)
├── config.yaml                 # user-editable: IPs, ports, token, params
│
├── pumpspy.py                  # CLI entrypoint: start / stop / status / logs
│
├── app/
│   ├── __init__.py
│   ├── db.py                   # all SQLite reads/writes (replaces events.jsonl)
│   ├── server.py               # device API on port 8081
│   ├── dashboard.py            # web dashboard on port 8080
│   └── templates/
│       └── dashboard.html      # Jinja2 template (extracted from current inline HTML)
│
├── setup/
│   ├── setup-linux.sh          # Pi / Ubuntu: hostapd, iptables DNAT, systemd units
│   ├── setup-mac.sh            # macOS: pf redirect, enable Internet Sharing
│   └── setup-windows.ps1       # Windows: Mobile Hotspot, netsh portproxy
│
├── systemd/                    # for direct Linux installs (no Docker)
│   ├── pumpspy-server.service
│   └── pumpspy-dashboard.service
│
└── data/                       # gitignored — persisted data lives here
    └── pumpspy.db              # SQLite database (auto-created on first run)
```

---

## config.yaml

All the values currently hardcoded in server.py move here.
Both the server and dashboard read this on startup.

```yaml
device:
  ip: "192.168.50.117"           # PumpSpy device IP on your AP
  cloud_ip: "206.80.104.221"     # IP the device tries to reach (for iptables)
  cloud_port: 8081

network:
  ap_interface: "wlan0"          # interface the device connects to
  ap_ip: "192.168.50.1"          # this machine's IP on that interface

server:
  host: "0.0.0.0"
  port: 8081
  bearer_token: "15e3409a-2a8c-4266-a669-bab98bc930de"

dashboard:
  host: "0.0.0.0"
  port: 8080

device_params:                   # sent to device on /bbs_parameters
  p1:  3000
  p2:  12500
  p3:  11000
  p4:  10000
  p5:  15
  p6:  7000
  p7:  10000
  p8:  48
  p9:  20000
  p10: 15000
```

---

## app/db.py  (replaces events.jsonl)

Single module that owns all database interaction.
Both server.py and dashboard.py import from here — neither touches SQLite directly.

```
Tables
──────
pings        ts, device_id, rssi
pump_events  ts, device_id, motor_state, duration_s, milliamps, battery_mv, loaded_mv
faults       ts, device_id, state (FAULT | CLEARED)
auth_events  ts, username, grant_type
raw_events   ts, method, path, body   ← catch-all / unknowns
```

Key functions:
- `record_ping(ts, device_id, rssi)`
- `record_pump_event(ts, device_id, inner_dict)`
- `record_fault(ts, device_id, state)`
- `record_auth(ts, username, grant_type)`
- `record_unknown(ts, method, path, body)`
- `get_dashboard_data(hours=24)` — single call the dashboard API uses

---

## pumpspy.py  (CLI)

Thin wrapper so users never need to remember docker-compose commands.

```
Usage
─────
python pumpspy.py start          # docker-compose up -d  (or systemctl start on bare metal)
python pumpspy.py stop           # docker-compose down
python pumpspy.py status         # show service health + last ping time
python pumpspy.py logs [server|dashboard]
python pumpspy.py setup          # detect OS, run the right setup script
python pumpspy.py reset-db       # wipe data/pumpspy.db and start fresh
```

---

## Docker

### Dockerfile
```
Base:    python:3.12-slim
Exposes: 8081 (server), 8080 (dashboard)
Volume:  /data  →  maps to ./data on host (persists pumpspy.db)
Config:  /app/config.yaml  →  maps to ./config.yaml on host
CMD:     starts both Flask apps via a simple process supervisor (e.g. supervisord or honcho)
```

### docker-compose.yml
```yaml
services:
  server:
    build: .
    ports: ["8081:8081"]
    volumes:
      - ./data:/data
      - ./config.yaml:/app/config.yaml
    restart: unless-stopped

  dashboard:
    build: .
    ports: ["8080:8080"]
    volumes:
      - ./data:/data
      - ./config.yaml:/app/config.yaml
    depends_on: [server]
    restart: unless-stopped
```

The two services share the same SQLite file via the mounted `./data` volume.
SQLite handles concurrent reads fine; writes come only from the server container.

---

## Platform Setup Scripts

Each script does three things:
1. Configures the WiFi AP (so the PumpSpy device has somewhere to connect)
2. Sets up the traffic redirect (so the device's cloud calls hit our server)
3. Makes both persistent across reboots

### Linux / Pi  (`setup/setup-linux.sh`)
- Verifies NetworkManager shared AP is configured on wlan0
- Adds iptables DNAT rule to `/etc/iptables/rules.v4`
- Installs systemd units if not using Docker

### macOS  (`setup/setup-mac.sh`)
- Enables Internet Sharing (wlan0 → en0) via `networksetup`
- Writes a `pf` anchor rule to redirect `206.80.104.221:8081 → 127.0.0.1:8081`
- Loads the rule with `pfctl` and adds it to `/etc/pf.conf` for persistence

### Windows  (`setup/setup-windows.ps1`)
- Enables the built-in Mobile Hotspot via `Set-NetConnectionSharing`
- Adds a portproxy rule:
  `netsh interface portproxy add v4tov4 listenport=8081 connectaddress=127.0.0.1`
- Registers the portproxy rule to persist via a scheduled task at logon

---

## Migration from Current Setup

The current Pi setup stays running untouched while we build this alongside it.
Migration steps when ready:

1. Add `config.yaml` — populate from current hardcoded values
2. Add `app/db.py` — write the SQLite schema and migration script to import `events.jsonl`
3. Refactor `server.py` to read config + use `db.py` instead of `events.jsonl`
4. Refactor `dashboard.py` same way
5. Add `Dockerfile` + `docker-compose.yml`
6. Test Docker locally on Mac first
7. Deploy to Pi via `./deploy.sh`, retire old systemd units in favour of compose

---

## What Stays the Same

- Flask for both the device API and the dashboard — no reason to change
- Chart.js for the RSSI chart
- The device protocol and all endpoints — the device doesn't care what's running behind the IP
- The dark dashboard UI — just moves to a proper Jinja2 template file
