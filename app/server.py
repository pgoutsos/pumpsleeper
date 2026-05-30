#!/usr/bin/env python3
"""
PumpSpy local server — transparent proxy + local logger
Sits between the device and the real pumpspy.com cloud.

Flow:
  Device → (iptables DNAT) → Pi:8081 → [parse + record] → 206.80.104.221:8081
  Real server response → Pi:8081 → Device

The device talks to the real cloud normally; we intercept every request,
log and parse it for the local dashboard, then relay the real response.

Endpoints handled:
  POST /oauth/token                  — token refresh (every ~2 hours)
  GET  /tm                           — UTC time sync
  POST /pings                        — RSSI / battery heartbeat (~2 min)
  POST /bbs_json                     — pump event data
  POST /pump_outlet_alerts           — main pump current events
  GET  /bbs_parameters/<deviceid>    — device config fetch
  GET  /new_firmware/<deviceid>      — OTA check

Environment variables:
  PUMPSPY_DATA         Directory where pumpspy.db is stored (default: same dir as script)
  PUMPSPY_REAL_SERVER  Real cloud URL (default: http://206.80.104.221:8081)
  PUMPSPY_PROXY        Set to "0" to disable forwarding and answer locally (default: "1")
  PUMPSPY_PROXY_TIMEOUT  Seconds to wait for real server (default: 8)
"""

import json
import logging
import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response
import requests as rlib
from db import init_db, record, get_mode, set_device_ip, get_device_ip, set_hotspot_connected
import mqtt

# Persistent session with automatic retry — reuses connections but retries once on
# stale-connection failures (RemoteDisconnected) which are common with Keep-Alive.
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_retry = Retry(total=2, backoff_factor=0.3,
               status_forcelist=[],        # don't retry on HTTP errors, only network errors
               allowed_methods={"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"})
_proxy_session = rlib.Session()
_proxy_session.mount("http://",  HTTPAdapter(max_retries=_retry))
_proxy_session.mount("https://", HTTPAdapter(max_retries=_retry))

import threading as _threading

def _fire_and_forget(method, path, headers, body, query_string):
    """
    Forward a request to the real server in a background thread.
    Used for endpoints we must answer locally (to avoid device timeout/reboot)
    but still want pumpspy.com to receive — so their app stays in sync.
    Only fires when proxy mode is enabled.
    """
    def _send():
        if not proxy_enabled():
            return
        try:
            url = REAL_SERVER + path
            if query_string:
                url += '?' + query_string.decode('utf-8', errors='replace')
            skip = _HOP_BY_HOP | {'host', 'content-length'}
            fwd_headers = {k: v for k, v in headers if k.lower() not in skip}
            _proxy_session.request(
                method=method, url=url, headers=fwd_headers,
                data=body, timeout=PROXY_TIMEOUT, allow_redirects=False,
            )
        except Exception as exc:
            log.debug(f"FORWARD  background send failed ({method} {path}): {exc}")
    _threading.Thread(target=_send, daemon=True).start()

# ---------------------------------------------------------------------------
# Proxy config
# ---------------------------------------------------------------------------
REAL_SERVER   = os.environ.get("PUMPSPY_REAL_SERVER", "http://206.80.104.221:8081")
PROXY_TIMEOUT = int(os.environ.get("PUMPSPY_PROXY_TIMEOUT", "8"))
WIFI_IFACE    = os.environ.get("PUMPSPY_WIFI_IFACE", "wlan0")   # hotspot interface

def proxy_enabled() -> bool:
    """Read current mode from DB on every call — no restart needed to switch."""
    return get_mode() == "proxy"

# ---------------------------------------------------------------------------
# Auth failure tracking (in-memory — resets on server restart)
# ---------------------------------------------------------------------------
import threading as _threading
_auth_lock = _threading.Lock()
_auth_state = {
    "consecutive_failures": 0,
    "last_failure_ts": None,   # ISO string
    "last_success_ts": None,   # ISO string
}

def _record_auth_success():
    with _auth_lock:
        _auth_state["consecutive_failures"] = 0
        _auth_state["last_success_ts"] = datetime.now(timezone.utc).isoformat()

def _record_auth_failure():
    with _auth_lock:
        _auth_state["consecutive_failures"] += 1
        _auth_state["last_failure_ts"] = datetime.now(timezone.utc).isoformat()

def get_auth_status() -> dict:
    with _auth_lock:
        return dict(_auth_state)

# ---------------------------------------------------------------------------
# Device IP tracking — record IP from any device-originated request
# ---------------------------------------------------------------------------
_device_ip_cache = None

# ---------------------------------------------------------------------------
# Hotspot presence check — runs in background thread every 30 s
# ---------------------------------------------------------------------------
import subprocess as _subprocess

def _check_hotspot_connected(device_ip: str):
    """
    Returns True if device is associated with the WiFi hotspot, False if not,
    None if we cannot determine (e.g. running outside a Pi / iw not available).

    Strategy:
      1. iw dev <iface> station dump — lists all currently associated WiFi clients
         by MAC address. This is a Layer-2 check that doesn't require the device
         to respond to pings.
      2. ip neigh show dev <iface> — maps MACs to IPs so we can match device_ip.
      3. Fallback: ping — works anywhere but slightly slower.
    """
    # Step 1: get all associated MACs from the AP
    # iw lives in /usr/sbin which may not be in PATH for service users
    import shutil as _shutil
    _iw = _shutil.which("iw") or "/usr/sbin/iw"

    station_macs = set()
    try:
        r = _subprocess.run(
            [_iw, "dev", WIFI_IFACE, "station", "dump"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.strip().startswith("Station "):
                    station_macs.add(line.split()[1].lower())
        else:
            raise RuntimeError(r.stderr.strip())
    except FileNotFoundError:
        # iw not installed — fall through to ping
        pass
    except Exception as exc:
        log.debug(f"HOTSPOT  iw check failed: {exc}")

    if station_macs:
        # Step 2: find which station has device_ip
        try:
            r = _subprocess.run(
                ["ip", "neigh", "show", "dev", WIFI_IFACE],
                capture_output=True, text=True, timeout=3,
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if "lladdr" in parts:
                    ip  = parts[0]
                    mac = parts[parts.index("lladdr") + 1].lower()
                    if ip == device_ip:
                        return mac in station_macs
        except Exception as exc:
            log.debug(f"HOTSPOT  ip neigh failed: {exc}")
        # device_ip not found in neighbor table → not connected
        return False

    # Step 3: fallback — ping (works on any OS, less definitive)
    try:
        r = _subprocess.run(
            ["ping", "-c", "1", "-W", "2", "-q", device_ip],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return None   # can't determine


def _hotspot_checker_loop():
    """Background thread: poll hotspot every 30 s and update DB + MQTT."""
    import time
    time.sleep(15)   # brief startup delay so DB is ready
    while True:
        try:
            device_ip = get_device_ip()
            if device_ip:
                connected = _check_hotspot_connected(device_ip)
                if connected is not None:
                    set_hotspot_connected(connected)
                    mqtt.publish_hotspot_status(connected)
                    log.debug(f"HOTSPOT  {device_ip}  connected={connected}")
        except Exception as exc:
            log.error(f"HOTSPOT  checker error: {exc}")
        time.sleep(30)


app = Flask(__name__)


@app.before_request
def _track_device_ip():
    """Record the PumpSpy device's hotspot IP from any non-API request."""
    global _device_ip_cache
    if request.path.startswith("/api/"):
        return
    ip = request.remote_addr
    if ip and not ip.startswith("127.") and ip != _device_ip_cache:
        _device_ip_cache = ip
        set_device_ip(ip)
        log.info(f"DEVICE  IP: {ip}")
        mqtt.publish_device_ip(ip)


# --- Logging setup -----------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pumpspy")

# ---------------------------------------------------------------------------
# Transparent proxy helper
# ---------------------------------------------------------------------------
# Headers that must not be forwarded between proxy hops
_HOP_BY_HOP = {'transfer-encoding', 'connection', 'keep-alive',
               'proxy-authenticate', 'proxy-authorization', 'te',
               'trailers', 'upgrade'}

def proxy_forward():
    """
    Forward the current Flask request verbatim to the real pumpspy server.
    Returns a Flask Response with the real server's status, headers, and body.
    Returns None if proxying is disabled or the upstream call fails.
    """
    if not proxy_enabled():
        return None

    url = REAL_SERVER + request.path
    if request.query_string:
        url += '?' + request.query_string.decode('utf-8', errors='replace')

    # Strip hop-by-hop and host/content-length (requests library sets these)
    skip_req = _HOP_BY_HOP | {'host', 'content-length'}
    fwd_headers = {k: v for k, v in request.headers if k.lower() not in skip_req}

    # Get the raw request body.
    # If the handler already accessed request.form or request.get_json(), the WSGI
    # stream may be consumed. Fall back to reconstructing from the parsed data so
    # the forwarded body is never empty.
    body_data = request.get_data()
    ct = (request.content_type or '').lower()
    if not body_data:
        if 'application/x-www-form-urlencoded' in ct and request.form:
            from urllib.parse import urlencode
            body_data = urlencode(list(request.form.items(multi=True))).encode('utf-8')
        elif 'application/json' in ct and request.json is not None:
            body_data = json.dumps(request.json).encode('utf-8')

    try:
        resp = _proxy_session.request(
            method=request.method,
            url=url,
            headers=fwd_headers,
            data=body_data,
            timeout=PROXY_TIMEOUT,
            allow_redirects=False,
        )
        log.info(f"PROXY  ← {resp.status_code}  {request.method} {request.path}  body={resp.content[:200]!r}")

        skip_resp = _HOP_BY_HOP | {'content-encoding'}
        resp_headers = [(k, v) for k, v in resp.headers.items()
                        if k.lower() not in skip_resp]
        return Response(resp.content, status=resp.status_code, headers=resp_headers)

    except Exception as exc:
        log.error(f"PROXY  forward failed ({request.method} {request.path}): {exc}")
        return None   # fall through to local fallback response

# ---------------------------------------------------------------------------
# Fallback responses (used only when proxy is disabled or unreachable)
# ---------------------------------------------------------------------------
BEARER_TOKEN   = "15e3409a-2a8c-4266-a669-bab98bc930de"
REFRESH_TOKEN  = "2ffd27d4-130d-428f-ae5b-1a2a6b3d01f7"

DEVICE_PARAMS = {
    "p1":  3000,  "p2":  12500, "p3":  11000, "p4":  10000,
    "p5":  15,    "p6":  7000,  "p7":  10000, "p8":  48,
    "p9":  20000, "p10": 15000,
}

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/oauth/token", methods=["POST"])
def oauth_token():
    form       = request.form
    username   = form.get("username", "")
    grant_type = form.get("grant_type", "")
    log.info(f"AUTH   token refresh  grant_type={grant_type}  user={username}")
    record("auth", {"grant_type": grant_type, "username": username})

    proxied = proxy_forward()
    if proxied is not None:
        if proxied.status_code < 500:
            _record_auth_success()
        else:
            _record_auth_failure()
        return proxied

    if proxy_enabled():
        # Proxy mode but upstream failed — tell the device to retry in 10 seconds.
        # Short Retry-After means that as soon as the operator switches to takeover
        # mode the device will re-authenticate against our local server promptly.
        _record_auth_failure()
        failures = get_auth_status()["consecutive_failures"]
        log.error(f"AUTH   proxy failed (failure #{failures}) — returning 503 with Retry-After:10")
        resp = jsonify({"error": "proxy_unavailable"})
        resp.status_code = 503
        resp.headers["Retry-After"] = "60"
        return resp

    # Takeover mode — issue our local token so the device can operate fully offline.
    _record_auth_success()
    log.info("AUTH   takeover mode — issuing local token")
    return jsonify({
        "access_token":  BEARER_TOKEN,
        "token_type":    "bearer",
        "refresh_token": REFRESH_TOKEN,
        "scope":         "read",
    }), 200


@app.route("/tm", methods=["GET"])
def tm():
    # The real server does not implement /tm — always answer locally.
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    log.info(f"TIME   request → {ts_ms}")
    return jsonify({"utctime": ts_ms}), 200


@app.route("/pings", methods=["POST"])
def pings():
    body = request.get_json(force=True, silent=True) or []
    for ping in body:
        data_type = ping.get("idpings_data_type")
        value     = ping.get("value")
        ts_ms     = ping.get("utcunixtime", 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")

        if data_type == 1:
            log.info(f"PING   rssi={value} dBm  device_time={ts}")
        elif data_type == 3:
            log.info(f"PING   battery={value:.3f}V  device_time={ts}")
        else:
            log.info(f"PING   type={data_type}  value={value}  device_time={ts}")

        record("ping", ping)

    # Publish heartbeat values to MQTT (extract rssi and battery from the batch)
    _rssi = _batt = None
    for _ping in body:
        if _ping.get("idpings_data_type") == 1:
            _rssi = _ping.get("value")
        elif _ping.get("idpings_data_type") == 3:
            _batt = _ping.get("value")
    mqtt.publish_ping(rssi=_rssi, battery_v=_batt)

    # Answer the device locally (avoids the proxy latency that was causing reboots),
    # then forward to pumpspy.com in the background so their app stays in sync.
    _fire_and_forget(request.method, request.path,
                     request.headers, request.get_data(), request.query_string)

    response = []
    for ping in body:
        response.append({
            "idpings":           None,
            "deviceid":          ping.get("deviceid"),
            "utcunixtime":       ping.get("utcunixtime"),
            "idpings_data_type": ping.get("idpings_data_type"),
            "value":             ping.get("value"),
            "date_time":         None,
        })
    return jsonify(response), 200


@app.route("/bbs_json", methods=["POST"])
def bbs_json():
    body = request.get_json(force=True, silent=True) or {}
    inner_raw = body.get("json", "{}")
    if isinstance(inner_raw, dict):
        inner = inner_raw
    else:
        try:
            brace = inner_raw.index("{")
            inner = json.loads(inner_raw[brace:])
        except Exception:
            inner = {"raw": inner_raw}

    ts_ms = body.get("utcunixtime", 0)
    ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")

    if "motor_fail" in inner:
        state = "FAULT" if inner["motor_fail"] else "FAULT CLEARED"
        log.info(f"BBS    {state}  device_time={ts}")
    elif "motor" in inner:
        # motor=1 → main pump ran; motor=0 → backup pump ran
        pump_label = "MAIN  " if inner["motor"] else "BACKUP"
        duration = inner.get("time", "?")
        amps     = inner.get("mamp", 0) / 1000
        batt     = inner.get("battery_voltage", 0) / 1000
        loaded   = inner.get("loaded", 0) / 1000
        log.info(
            f"{pump_label} ran  ticks={duration}  "
            f"current={amps:.2f}A  batt={batt:.3f}V  loaded={loaded:.3f}V  "
            f"device_time={ts}"
        )
    elif "high_water" in inner:
        state = "TRIGGERED" if inner["high_water"] else "CLEARED"
        log.info(f"BBS    high_water {state}  device_time={ts}")
    elif "low_water" in inner:
        state = "TRIGGERED" if inner["low_water"] else "CLEARED"
        log.info(f"BBS    low_water {state}  device_time={ts}")
    else:
        log.info(f"BBS    unknown  inner={inner}  device_time={ts}")

    record("bbs_json", {"outer": body, "inner": inner})

    # Publish pump events to MQTT
    iso_ts = (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
              if ts_ms else datetime.now(timezone.utc).isoformat())
    if "motor" in inner:
        ticks = inner.get("time", 0)
        dur   = round(ticks / 10,   1) if ticks else 0
        gal   = round(ticks / 10.2, 1) if ticks else 0
        _amps = inner.get("mamp", 0) / 1000
        if inner["motor"]:   # main pump
            mqtt.publish_main_pump_run(iso_ts, dur, gal, _amps)
        else:                # backup pump
            _batt_v   = (inner.get("battery_voltage", 0) / 1000) or None
            _loaded_v = (inner.get("loaded", 0) / 1000) or None
            mqtt.publish_backup_pump_run(iso_ts, dur, gal, _amps, None,
                                         battery_v=_batt_v, loaded_v=_loaded_v)
    elif "high_water" in inner:
        mqtt.publish_water_sensor("high_water", bool(inner["high_water"]))
    elif "low_water" in inner:
        mqtt.publish_water_sensor("low_water", bool(inner["low_water"]))

    proxied = proxy_forward()
    if proxied is not None:
        return proxied
    return jsonify(body), 200


@app.route("/pump_outlet_alerts", methods=["POST"])
def pump_outlet_alerts():
    body = request.get_json(force=True, silent=True) or []
    if not isinstance(body, list):
        body = [body]

    for alert in body:
        device_id  = alert.get("deviceID")
        ts_ms      = alert.get("utcunixTime", 0)
        value      = alert.get("value", 0)
        alert_type = alert.get("idPumpAlertType")
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")

        if alert_type == 105:
            if value:
                amps = value / 1000
                log.info(f"MAIN   pump=ON   current={amps:.2f}A  device_time={ts}")
            else:
                log.info(f"MAIN   pump=OFF  device_time={ts}")
            mqtt.publish_main_pump_running(bool(value))
        else:
            state = "ON" if value else "OFF"
            log.warning(f"ALERT  type={alert_type}  state={state}  value={value}  device_time={ts}")

        record("pump_outlet_alert", alert)

    # Answer locally, and forward to pumpspy.com in the background so their
    # app reflects current pump state (main pump on/off events).
    _fire_and_forget(request.method, request.path,
                     request.headers, request.get_data(), request.query_string)

    response = []
    for alert in body:
        response.append({
            "idpump_outlet_alerts": None,
            "idPumpAlertType":      alert.get("idPumpAlertType"),
            "deviceID":             alert.get("deviceID"),
            "recordNumber":         alert.get("recordNumber", 0),
            "utcunixTime":          alert.get("utcunixTime", 0),
            "value":                alert.get("value", 0),
            "date_time":            None,
        })
    return jsonify(response), 200


@app.route("/bbs_parameters/<int:device_id>", methods=["GET"])
def bbs_parameters(device_id):
    log.info(f"PARAMS requested by device {device_id}")
    record("params_fetch", {"deviceid": device_id})
    # Real server returns 400 for this endpoint when proxied — always answer locally.
    return jsonify(DEVICE_PARAMS), 200


@app.route("/new_firmware/<int:device_id>", methods=["GET"])
def new_firmware(device_id):
    # Answer immediately — real server responds too slowly (no HTTP body before device TCP timeout),
    # causing the device to reboot. Real server returns [] anyway (no firmware pending).
    log.info(f"OTA    check from device {device_id}")
    return jsonify([]), 200


# --- Mode API (called by dashboard to switch between proxy and takeover) -----

@app.route("/api/mode", methods=["GET"])
def api_mode_get():
    from db import get_mode
    auth = get_auth_status()
    return jsonify({
        "mode": get_mode(),
        "auth_failures": auth["consecutive_failures"],
        "auth_last_failure_ts": auth["last_failure_ts"],
        "auth_last_success_ts": auth["last_success_ts"],
    })

@app.route("/api/mode", methods=["POST"])
def api_mode_set():
    from db import set_mode
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode", "")
    try:
        set_mode(mode)
        mqtt.publish_mode(mode)
        log.info(f"MODE   switched to '{mode}'")
        return jsonify({"mode": mode})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# --- Catch-all for anything unexpected ---------------------------------------

ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

@app.route("/", methods=ALL_METHODS)
def catch_root():
    return _catch("(root)", "")

@app.route("/<path:path>", methods=ALL_METHODS)
def catch_all(path):
    return _catch(path, path)

def _catch(log_path, record_path):
    body = request.get_data(as_text=True)
    log.warning(f"UNKNOWN  {request.method} /{log_path}  body={body[:200]}")
    record("unknown", {"method": request.method, "path": record_path, "body": body})

    proxied = proxy_forward()
    if proxied is not None:
        return proxied
    return jsonify({"status": "ok"}), 200


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    from db import DB_FILE, get_mode
    init_db()
    mqtt.init()
    _threading.Thread(target=_hotspot_checker_loop, daemon=True, name="hotspot-checker").start()
    log.info(f"PumpSpy local server starting on 0.0.0.0:8081  mode={get_mode()}  real_server={REAL_SERVER}  db={DB_FILE}")
    app.run(host="0.0.0.0", port=8081, debug=False, threaded=True)
