#!/usr/bin/env python3
"""
PumpSleeper — MQTT publisher for Home Assistant integration.

Publishes HA MQTT auto-discovery on connect so all entities appear
automatically — no YAML config needed in HA.

Environment variables:
  PUMPSLEEPER_MQTT_HOST      Broker hostname/IP (required to enable MQTT)
  PUMPSLEEPER_MQTT_PORT      Default: 1883
  PUMPSLEEPER_MQTT_USER      Optional username
  PUMPSLEEPER_MQTT_PASSWORD  Optional password
  PUMPSLEEPER_MQTT_PREFIX    Topic prefix (default: pumpsleeper)
  PUMPSLEEPER_DEVICE_ID      Unique device ID for HA (default: pumpsleeper_01)
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta

log = logging.getLogger("pumpsleeper.mqtt")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MQTT_HOST   = os.environ.get("PUMPSLEEPER_MQTT_HOST", "").strip()
MQTT_PORT   = int(os.environ.get("PUMPSLEEPER_MQTT_PORT", "1883"))
MQTT_USER   = os.environ.get("PUMPSLEEPER_MQTT_USER", "").strip()
MQTT_PASS   = os.environ.get("PUMPSLEEPER_MQTT_PASSWORD", "").strip()
PREFIX      = os.environ.get("PUMPSLEEPER_MQTT_PREFIX", "pumpsleeper")
DEVICE_ID   = os.environ.get("PUMPSLEEPER_DEVICE_ID", "pumpsleeper_01")
DISC_PREFIX = "homeassistant"   # HA auto-discovery default

ENABLED = bool(MQTT_HOST)

DEVICE_INFO = {
    "identifiers": [DEVICE_ID],
    "name":         "PumpSleeper",
    "model":        "PumpSleeper",
    "manufacturer": "PumpSleeper",
    "sw_version":   "1.0",
}

_client   = None
_connected = False
_lock     = threading.Lock()

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
def _on_connect(client, userdata, flags, rc):
    global _connected
    if rc == 0:
        _connected = True
        log.info(f"MQTT   connected to {MQTT_HOST}:{MQTT_PORT}")
        _publish_raw(f"{PREFIX}/availability", "online", retain=True)
        # Subscribe to command topics so HA can control PumpSleeper
        client.subscribe(f"{PREFIX}/set/mode")
        log.info(f"MQTT   subscribed to {PREFIX}/set/mode")
        _publish_discovery()
        # Publish current mode and daily stats immediately on connect
        # so HA entities populate without waiting for the next pump run
        try:
            from db import get_mode
            _pub("mode", get_mode(), retain=True)
        except Exception as exc:
            log.error(f"MQTT   startup mode publish failed: {exc}")
        _refresh_daily_stats()
    else:
        _connected = False
        log.error(f"MQTT   connection failed rc={rc}")


def _on_message(client, userdata, msg):
    """Handle incoming MQTT commands (e.g. mode changes triggered from HA)."""
    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    log.info(f"MQTT   cmd  {topic} → {payload!r}")

    if topic == f"{PREFIX}/set/mode":
        try:
            from db import set_mode
            set_mode(payload)
            # publish_mode publishes both mode and mode_switched_ts (needed for pending logic)
            publish_mode(payload)
            log.info(f"MQTT   mode switched to '{payload}' via HA command")
        except Exception as exc:
            log.error(f"MQTT   mode command failed: {exc}")


def _on_disconnect(client, userdata, rc):
    global _connected
    _connected = False
    if rc != 0:
        log.warning(f"MQTT   unexpected disconnect rc={rc} — will auto-reconnect")

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init():
    """
    Start the MQTT client.  No-op (and no import) if MQTT_HOST is not set,
    so PumpSleeper works normally for users without HA/MQTT.
    """
    global _client
    if not ENABLED:
        log.info("MQTT   disabled (PUMPSLEEPER_MQTT_HOST not set)")
        return

    try:
        import paho.mqtt.client as mqtt_lib
    except ImportError:
        log.error("MQTT   paho-mqtt not installed — run: pip install paho-mqtt --break-system-packages")
        return

    _client = mqtt_lib.Client(client_id=DEVICE_ID, clean_session=False)
    _client.will_set(f"{PREFIX}/availability", "offline", retain=True)

    if MQTT_USER:
        _client.username_pw_set(MQTT_USER, MQTT_PASS or None)

    _client.on_connect    = _on_connect
    _client.on_disconnect = _on_disconnect
    _client.on_message    = _on_message
    _client.reconnect_delay_set(min_delay=2, max_delay=60)

    try:
        _client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        _client.loop_start()
        log.info(f"MQTT   client started → {MQTT_HOST}:{MQTT_PORT}")
    except Exception as exc:
        log.error(f"MQTT   startup failed: {exc}")


# ---------------------------------------------------------------------------
# Internal publish helper
# ---------------------------------------------------------------------------
def _publish_raw(topic, payload, retain=False):
    if not ENABLED or _client is None:
        return
    try:
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        _client.publish(topic, str(payload), retain=retain)
    except Exception as exc:
        log.error(f"MQTT   publish failed ({topic}): {exc}")


def _pub(subtopic, payload, retain=False):
    _publish_raw(f"{PREFIX}/{subtopic}", payload, retain)


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------
def _disc(component, obj_id, config):
    """Publish one HA MQTT auto-discovery config message."""
    topic = f"{DISC_PREFIX}/{component}/{DEVICE_ID}/{obj_id}/config"
    config.setdefault("availability_topic", f"{PREFIX}/availability")
    config["device"]    = DEVICE_INFO
    config["unique_id"] = f"{DEVICE_ID}_{obj_id}"
    _publish_raw(topic, json.dumps(config), retain=True)


def _publish_discovery():
    p = PREFIX

    # ── Binary sensors ────────────────────────────────────────────────────
    _disc("binary_sensor", "online", {
        "name":        "Device Online",
        "state_topic": f"{p}/availability",
        "payload_on":  "online",
        "payload_off": "offline",
        "device_class": "connectivity",
    })

    _disc("binary_sensor", "main_pump_running", {
        "name":        "Main Pump Running",
        "state_topic": f"{p}/main_pump/running",
        "payload_on":  "ON",
        "payload_off": "OFF",
        "device_class": "running",
    })

    _disc("binary_sensor", "wifi_connected", {
        "name":         "Device WiFi Connected",
        "state_topic":  f"{p}/wifi_connected",
        "payload_on":   "ON",
        "payload_off":  "OFF",
        "device_class": "connectivity",
        "icon":         "mdi:wifi",
    })

    # ── Signal / power sensors ────────────────────────────────────────────
    _disc("sensor", "rssi", {
        "name":                 "Signal Strength",
        "state_topic":          f"{p}/rssi",
        "unit_of_measurement":  "dBm",
        "device_class":         "signal_strength",
        "state_class":          "measurement",
    })

    _disc("sensor", "battery_voltage", {
        "name":                "Backup Battery Voltage",
        "state_topic":         f"{p}/battery_voltage",
        "unit_of_measurement": "V",
        "device_class":        "voltage",
        "state_class":         "measurement",
    })

    _disc("sensor", "loaded_voltage", {
        "name":                "Backup Loaded Voltage",
        "state_topic":         f"{p}/loaded_voltage",
        "unit_of_measurement": "V",
        "device_class":        "voltage",
        "state_class":         "measurement",
    })

    # ── Status sensors ────────────────────────────────────────────────────
    # Clear legacy mode sensor (replaced by select entity below)
    _publish_raw(f"{DISC_PREFIX}/sensor/{DEVICE_ID}/mode/config", "", retain=True)

    # Mode as a select so HA can both read and control it
    _disc("select", "mode", {
        "name":          "Mode",
        "state_topic":   f"{p}/mode",
        "command_topic": f"{p}/set/mode",
        "options":       ["proxy", "takeover"],
        "icon":          "mdi:swap-horizontal",
    })

    # Timestamps needed for pending/offline logic in the Lovelace card
    _disc("sensor", "last_ping_ts", {
        "name":         "Last Device Ping",
        "state_topic":  f"{p}/last_ping_ts",
        "device_class": "timestamp",
        "icon":         "mdi:clock-check-outline",
    })

    _disc("sensor", "mode_switched_ts", {
        "name":         "Mode Switched At",
        "state_topic":  f"{p}/mode_switched_ts",
        "device_class": "timestamp",
        "icon":         "mdi:clock-edit-outline",
    })

    _disc("sensor", "operating_status", {
        "name":        "Operating Status",
        "state_topic": f"{p}/operating_status",
        "icon":        "mdi:pump",
    })

    _disc("sensor", "device_ip", {
        "name":        "Device IP",
        "state_topic": f"{p}/device_ip",
        "icon":        "mdi:ip-network",
    })

    # ── Main pump ─────────────────────────────────────────────────────────
    _disc("sensor", "main_pump_runs_today", {
        "name":                "Main Pump Runs Today",
        "state_topic":         f"{p}/main_pump/runs_today",
        "unit_of_measurement": "runs",
        "state_class":         "total_increasing",
        "icon":                "mdi:counter",
    })

    _disc("sensor", "main_pump_runtime_today", {
        "name":                "Main Pump Runtime Today",
        "state_topic":         f"{p}/main_pump/runtime_today",
        "unit_of_measurement": "s",
        "device_class":        "duration",
        "state_class":         "total_increasing",
    })

    _disc("sensor", "main_pump_gallons_today", {
        "name":                "Main Pump Gallons Today",
        "state_topic":         f"{p}/main_pump/gallons_today",
        "unit_of_measurement": "gal",
        "state_class":         "total_increasing",
        "icon":                "mdi:water",
    })

    _disc("sensor", "main_pump_last_run", {
        "name":         "Main Pump Last Run",
        "state_topic":  f"{p}/main_pump/last_run",
        "device_class": "timestamp",
        "icon":         "mdi:clock-outline",
    })

    # ── Backup pump ───────────────────────────────────────────────────────
    _disc("sensor", "backup_pump_runs_today", {
        "name":                "Backup Pump Runs Today",
        "state_topic":         f"{p}/backup_pump/runs_today",
        "unit_of_measurement": "runs",
        "state_class":         "total_increasing",
        "icon":                "mdi:counter",
    })

    _disc("sensor", "backup_pump_runtime_today", {
        "name":                "Backup Pump Runtime Today",
        "state_topic":         f"{p}/backup_pump/runtime_today",
        "unit_of_measurement": "s",
        "device_class":        "duration",
        "state_class":         "total_increasing",
    })

    _disc("sensor", "backup_pump_gallons_today", {
        "name":                "Backup Pump Gallons Today",
        "state_topic":         f"{p}/backup_pump/gallons_today",
        "unit_of_measurement": "gal",
        "state_class":         "total_increasing",
        "icon":                "mdi:water",
    })

    _disc("sensor", "backup_pump_last_run", {
        "name":         "Backup Pump Last Run",
        "state_topic":  f"{p}/backup_pump/last_run",
        "device_class": "timestamp",
        "icon":         "mdi:clock-outline",
    })

    _disc("sensor", "backup_pump_last_trigger", {
        "name":        "Backup Pump Last Trigger",
        "state_topic": f"{p}/backup_pump/last_trigger",
        "icon":        "mdi:water-alert",
    })

    log.info("MQTT   auto-discovery published (%d entities)", 19)


# ---------------------------------------------------------------------------
# Public publish functions — called from server.py
# ---------------------------------------------------------------------------

def publish_ping(rssi=None, battery_v=None):
    """Heartbeat data from /pings handler."""
    _pub("last_ping_ts", datetime.now(timezone.utc).isoformat(), retain=True)
    if rssi is not None:
        _pub("rssi", rssi, retain=True)
    if battery_v is not None:
        _pub("battery_voltage", f"{battery_v:.3f}", retain=True)


def publish_main_pump_run(ts, duration, gallons, amps):
    """Main pump completed a run (bbs_json motor=1)."""
    _pub("main_pump/last_run",    ts,       retain=True)
    _pub("operating_status",      "main",   retain=True)
    _pub("main_pump/running",     "OFF",    retain=True)   # run just finished
    _pub("events/main_pump_run", {
        "ts":       ts,
        "duration": duration,
        "gallons":  gallons,
        "amps":     amps,
    })
    _refresh_daily_stats()


def publish_backup_pump_run(ts, duration, gallons, amps, trigger,
                             battery_v=None, loaded_v=None):
    """Backup pump completed a run (bbs_json motor=0)."""
    _pub("backup_pump/last_run",     ts,                  retain=True)
    _pub("backup_pump/last_trigger", trigger or "unknown", retain=True)
    _pub("operating_status",         "backup",            retain=True)
    if battery_v is not None:
        _pub("battery_voltage", f"{battery_v:.3f}", retain=True)
    if loaded_v is not None:
        _pub("loaded_voltage",  f"{loaded_v:.3f}",  retain=True)
    _pub("events/backup_pump_run", {
        "ts":        ts,
        "duration":  duration,
        "gallons":   gallons,
        "amps":      amps,
        "trigger":   trigger,
        "battery_v": battery_v,
        "loaded_v":  loaded_v,
    })
    _refresh_daily_stats()


def publish_main_pump_running(on: bool):
    """Main pump turned ON or OFF (pump_outlet_alerts type 105)."""
    _pub("main_pump/running", "ON" if on else "OFF", retain=True)


def publish_water_sensor(sensor_type: str, active: bool):
    """Water sensor triggered or cleared (bbs_json high_water / low_water)."""
    _pub(f"events/water_sensor", {
        "type":   sensor_type,
        "active": active,
    })


def publish_mode(mode: str):
    """Mode switched between proxy and takeover."""
    ts = datetime.now(timezone.utc).isoformat()
    _pub("mode",             mode, retain=True)
    _pub("mode_switched_ts", ts,   retain=True)
    _pub("events/mode_change", {"mode": mode, "ts": ts})


def publish_hotspot_status(connected: bool):
    """Publish WiFi hotspot presence check result."""
    _pub("wifi_connected", "ON" if connected else "OFF", retain=True)


def publish_device_ip(ip: str):
    """Publish the device's hotspot IP address."""
    _pub("device_ip", ip, retain=True)


# ---------------------------------------------------------------------------
# Daily stats refresh — queries DB and publishes current totals
# ---------------------------------------------------------------------------
def _refresh_daily_stats():
    """Query today's pump run totals from DB and publish. Runs in background."""
    threading.Thread(target=_do_refresh_daily_stats, daemon=True).start()


def _do_refresh_daily_stats():
    try:
        from db import load_events
        events = load_events(days=7)   # look back 7 days so last_run always populates
        today  = datetime.now(timezone.utc).date().isoformat()

        main_runs = main_rt = main_gal = 0
        back_runs = back_rt = back_gal = 0
        main_last_run = back_last_run = back_last_trigger = None

        for e in events:
            if e["kind"] != "bbs_json":
                continue
            inner = e["data"].get("inner", {})
            if not isinstance(inner, dict) or "motor" not in inner:
                continue
            ticks = inner.get("time")
            dur   = round(ticks / 10, 1)  if ticks else 0
            gal   = round(ticks / 10.2, 1) if ticks else 0
            if inner["motor"]:  # main pump
                main_last_run = e["ts"]   # events sorted oldest-first; last wins = most recent
                if e["ts"][:10] == today:
                    main_runs += 1; main_rt += dur; main_gal += gal
            else:               # backup pump
                back_last_run = e["ts"]
                if e["ts"][:10] == today:
                    back_runs += 1; back_rt += dur; back_gal += gal

        _pub("main_pump/runs_today",      main_runs,           retain=True)
        _pub("main_pump/runtime_today",   round(main_rt,  1),  retain=True)
        _pub("main_pump/gallons_today",   round(main_gal, 1),  retain=True)
        _pub("backup_pump/runs_today",    back_runs,           retain=True)
        _pub("backup_pump/runtime_today", round(back_rt,  1),  retain=True)
        _pub("backup_pump/gallons_today", round(back_gal, 1),  retain=True)

        if main_last_run:
            _pub("main_pump/last_run",   main_last_run, retain=True)
        if back_last_run:
            _pub("backup_pump/last_run", back_last_run, retain=True)

    except Exception as exc:
        log.error(f"MQTT   daily stats refresh failed: {exc}")
