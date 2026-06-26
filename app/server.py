#!/usr/bin/env python3
"""
PumpSpy local server — transparent proxy + local logger
Sits between the device and the real pumpspy.com cloud.

Flow:
  Device → (iptables DNAT) → Pi:8081 → [parse + record] → www.pumpspy.com:8081
  Real server response → Pi:8081 → Device

The device talks to the real cloud normally; we intercept every request,
log and parse it for the local dashboard, then relay the real response.

Endpoints handled:
  POST /oauth/token                  — token refresh (every ~2 hours)
  GET  /tm                           — UTC time sync
  POST /pings                        — RSSI / battery heartbeat (~2 min)
  POST /bbs_json                     — pump event data            (BBS device)
  POST /pump_outlet_alerts           — main pump current events   (BBS device)
  GET  /bbs_parameters/<deviceid>    — device config fetch        (BBS device)
  GET  /rht_parameters/<deviceid>    — device config fetch        (SO1000 smart outlet)
  POST /rht_outlet_cycles            — pump-run reports           (SO1000 smart outlet)
  GET  /pump_outlet_parameters/<deviceid> — device config fetch   (SmartPump)
  POST /pump_outlet_cycles           — pump-run reports           (SmartPump)
  GET  /new_firmware/<deviceid>      — OTA check

Device types (user-selected in dashboard Settings, db key 'device_type'):
  'bbs'    — the original PumpSpy backup pump system (ESP32). Default.
  'so1000' — the PumpSpy smart outlet. Same /pings + /oauth/token + /new_firmware,
             but config comes from /rht_parameters and pump runs arrive as
             POST /rht_outlet_cycles. The SO1000-specific endpoints answer
             locally ONLY when 'so1000' is selected; otherwise they fall through
             to the catch-all (record + forward) so BBS behavior is untouched.
  'smartpump' — the PumpSpy SmartPump. A third variant decoded from a new-user
             capture (2026-06-26). Config comes from GET /pump_outlet_parameters
             and pump runs arrive as POST /pump_outlet_cycles. Same gating rule:
             these endpoints answer locally ONLY when 'smartpump' is selected,
             otherwise they fall through to the catch-all.
             UNIT NOTE: in the capture, cycleDuration is in SECONDS (values
             21-149), NOT milliseconds like the SO1000. cycleCurrent was 0 on
             every record so its unit is assumed mA (as SO1000) but UNVERIFIED.
             If a real run proves otherwise, flip SMARTPUMP_DURATION_IS_SECONDS
             below (and the matching dashboard pump_outlet_cycle branch).

SO1000 firmware quirks (decoded from a bypass capture against the real cloud,
2026-06-12 — see memory/pumpsleeper-so1000-investigation.md):
  * GET /rht_parameters declares Content-Length: 148 but NEVER sends a body.
    The real server ignores the CL and answers in ~25 ms. We must do the same —
    NEVER attempt to read the request body on this route, or werkzeug blocks
    until the device gives up (10 s) and the device never gets its config.
    Without the config (cycle_data:1) the device won't report pump runs AT ALL.
  * The request line is "POST  /rht_outlet_cycles" (two spaces). werkzeug's
    parser collapses whitespace, so routing works — don't switch to a WSGI
    server that parses the request line more strictly without re-testing.

Environment variables:
  PUMPSPY_DATA         Directory where pumpspy.db is stored (default: same dir as script)
  PUMPSPY_REAL_SERVER  Real cloud URL override (default: use device's Host header)
  PUMPSPY_PROXY        Set to "0" to disable forwarding and answer locally (default: "1")
  PUMPSPY_PROXY_TIMEOUT  Seconds to wait for real server (default: 8)
"""

import json
import logging
import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response
import requests as rlib
from db import (init_db, record, get_mode, set_device_ip, get_device_ip,
                set_hotspot_connected, is_capture_enabled, set_capture_enabled,
                get_device_type, CAPTURE_FILE)
import mqtt
import notifications as notif

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
            host_hdr = dict(headers).get("Host", "www.pumpspy.com:8081")
            real = REAL_SERVER or ("http://" + host_hdr.strip())
            url = real + path
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
REAL_SERVER   = os.environ.get("PUMPSPY_REAL_SERVER", "")  # empty = use device's Host header
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
    from db import get_hotspot_connected as _get_connected
    time.sleep(15)   # brief startup delay so DB is ready
    _prev_connected = None
    while True:
        try:
            device_ip = get_device_ip()
            if device_ip:
                connected = _check_hotspot_connected(device_ip)
                if connected is not None:
                    set_hotspot_connected(connected)
                    mqtt.publish_hotspot_status(connected)
                    log.debug(f"HOTSPOT  {device_ip}  connected={connected}")
                    # Fire offline notification on transition True → False only
                    if _prev_connected is True and connected is False:
                        notif.notify(notif.EVENT_DEVICE_OFFLINE,
                                     f"Device ({device_ip}) is no longer on the hotspot.")
                    _prev_connected = connected
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


# ---------------------------------------------------------------------------
# Raw pump-traffic capture (debug tool)
# ---------------------------------------------------------------------------
# When enabled from the dashboard's Debug section, every device transaction is
# appended to CAPTURE_FILE so we can inspect a pump whose messages the parser
# doesn't recognise. Works in BOTH proxy and takeover mode — it records what the
# pump SENDS (identical in both modes); the response section is the real cloud
# reply in proxy mode, or our local reply in takeover. The enabled flag is cached
# briefly so we don't hit the DB on every request.
_capture_flag_cache = {"value": False, "ts": 0.0}
_CAPTURE_FLAG_TTL = 2.0   # seconds

def _capture_on() -> bool:
    import time
    now = time.time()
    if now - _capture_flag_cache["ts"] > _CAPTURE_FLAG_TTL:
        try:
            _capture_flag_cache["value"] = is_capture_enabled()
        except Exception:
            _capture_flag_cache["value"] = False
        _capture_flag_cache["ts"] = now
    return _capture_flag_cache["value"]


@app.after_request
def _capture_traffic(resp):
    """Append the full request+response to the capture log while capture is on."""
    try:
        if request.path.startswith("/api/") or not _capture_on():
            return resp
        ts = datetime.now(timezone.utc).isoformat()
        try:
            # GETs: never trigger a body read here. The SO1000 declares a
            # Content-Length on GET /rht_parameters but sends no body; reading
            # it would block until the device gives up (~10 s).
            if request.method == "GET":
                req_body = ""
            else:
                req_body = request.get_data().decode("utf-8", "replace")
        except Exception:
            req_body = "<unavailable>"
        try:
            resp_body = resp.get_data().decode("utf-8", "replace")
        except Exception:
            resp_body = "<unavailable>"
        q = request.query_string.decode("utf-8", "replace")
        path_q = request.path + (("?" + q) if q else "")
        headers = "\n".join(f"  {k}: {v}" for k, v in request.headers)
        block = (
            "=" * 72 + "\n"
            f"{ts}  mode={get_mode()}  from={request.remote_addr}\n"
            f"{request.method} {path_q}  ->  {resp.status_code}\n"
            "--- request headers ---\n"
            f"{headers}\n"
            "--- request body ---\n"
            f"{req_body if req_body else '(empty)'}\n"
            "--- response body ---\n"
            f"{(resp_body[:4000] if resp_body else '(empty)')}\n\n"
        )
        with open(CAPTURE_FILE, "a", encoding="utf-8") as fh:
            fh.write(block)
    except Exception as exc:
        log.debug(f"CAPTURE  write failed: {exc}")
    return resp


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

    # Use configured server or fall back to the Host header the device sent.
    # The device hardcodes an IP but always sends Host: www.pumpspy.com:8081,
    # so using the Host header means we follow pumpspy.com even if their IP changes.
    real = REAL_SERVER or ("http://" + request.headers.get("Host", "www.pumpspy.com:8081").strip())
    url = real + request.path
    if request.query_string:
        url += '?' + request.query_string.decode('utf-8', errors='replace')

    # Strip hop-by-hop and host/content-length (requests library sets these)
    skip_req = _HOP_BY_HOP | {'host', 'content-length'}
    fwd_headers = {k: v for k, v in request.headers if k.lower() not in skip_req}

    # Get the raw request body.
    # If the handler already accessed request.form or request.get_json(), the WSGI
    # stream may be consumed. Fall back to reconstructing from the parsed data so
    # the forwarded body is never empty.
    # The device sometimes sends a malformed GET declaring a Content-Length
    # (e.g. 148) with no body (seen on /rht_parameters from the SO1000 smart
    # outlet); werkzeug raises a 400 on the short read — treat that as empty
    # so the request is still forwarded upstream.
    # GET requests never have meaningful bodies. Skip reading entirely to avoid
    # blocking on the SO1000's phantom Content-Length: 148 on /rht_parameters.
    if request.method == "GET":
        body_data = b""
    else:
        try:
            body_data = request.get_data()
            ct = (request.content_type or '').lower()
            if not body_data:
                if 'application/x-www-form-urlencoded' in ct and request.form:
                    from urllib.parse import urlencode
                    body_data = urlencode(list(request.form.items(multi=True))).encode('utf-8')
                elif 'application/json' in ct and request.json is not None:
                    body_data = json.dumps(request.json).encode('utf-8')
        except Exception:
            log.warning(f"BODY   {request.method} {request.path}: declared Content-Length "
                        f"{request.headers.get('Content-Length')} but body unreadable — forwarding without body")
            body_data = b""

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
            notif.notify(notif.EVENT_MAIN_PUMP,
                         f"Duration: {dur}s · Est. {gal} gal · {_amps:.2f}A")
        else:                # backup pump
            _batt_v   = (inner.get("battery_voltage", 0) / 1000) or None
            _loaded_v = (inner.get("loaded", 0) / 1000) or None
            mqtt.publish_backup_pump_run(iso_ts, dur, gal, _amps, None,
                                         battery_v=_batt_v, loaded_v=_loaded_v)
            notif.notify(notif.EVENT_BACKUP_PUMP,
                         f"Duration: {dur}s · Est. {gal} gal · {_amps:.2f}A"
                         + (f" · Battery: {_batt_v:.3f}V" if _batt_v else ""))
    elif "high_water" in inner:
        mqtt.publish_water_sensor("high_water", bool(inner["high_water"]))
        if inner["high_water"]:
            notif.notify(notif.EVENT_HIGH_WATER, "High water sensor triggered.")
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
        elif alert_type == 1004:
            # SO1000 smart outlet high-water sensor (verified live 2026-06-13):
            # value 1 = triggered, 0 = cleared. The BBS reports high water via
            # /bbs_json instead and never sends this type, so no gating needed.
            state = "TRIGGERED" if value else "CLEARED"
            log.warning(f"ALERT  high_water {state} (SO1000)  device_time={ts}")
            mqtt.publish_water_sensor("high_water", bool(value))
            if value:
                notif.notify(notif.EVENT_HIGH_WATER, "High water sensor triggered.")
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


# ---------------------------------------------------------------------------
# SO1000 smart outlet endpoints — active only when device_type == 'so1000'
# ---------------------------------------------------------------------------
# Config the real cloud returned for Perry's SO1000 (bypass capture 2026-06-12).
# cycle_data:1 is the critical flag — without it the outlet never reports runs.
RHT_PARAMETERS = {
    "id_rht_parameters":   13012,
    "deviceid":            None,    # filled in per request
    "ping_interval":       120,
    "send_rht":            0,
    "cycle_data":          1,
    "high_temp":           99,
    "low_temp":            1,
    "high_humid":          99,
    "low_humid":           1,
    "temp_post_timer":     900000,
    "motor_current_limit": 14000,
    "motor_run_timeout":   300000,
}


@app.route("/rht_parameters/<int:device_id>", methods=["GET"])
def rht_parameters(device_id):
    """
    SO1000 config poll (every ~2 min). The device declares Content-Length: 148
    but never sends a body — do NOT read request data anywhere in this handler
    (it would block ~10 s and the device would close the connection unconfigured).
    The real server ignores the bogus CL and answers immediately; we answer
    locally in both modes (mirrors /bbs_parameters and /new_firmware precedent —
    latency-sensitive, and answering locally guarantees cycle_data stays 1).
    """
    if get_device_type() != "so1000":
        return _catch(f"rht_parameters/{device_id}", f"rht_parameters/{device_id}")
    log.info(f"PARAMS (SO1000) requested by device {device_id}")
    record("params_fetch", {"deviceid": device_id, "device_type": "so1000"})

    # In proxy mode, forward to the real server and return its response —
    # but only if it's a success. A non-200 (e.g. 401 if the token is stale
    # or the upstream rejects our source IP) must NOT be passed to the device
    # or it will stop reporting. Fall back to the canned response instead so
    # the device always gets a valid config.
    proxied = proxy_forward()
    if proxied is not None and proxied.status_code == 200:
        return proxied

    # Takeover mode, proxy unreachable, or upstream error: canned response.
    params = dict(RHT_PARAMETERS, deviceid=device_id)
    return jsonify(params), 200


@app.route("/rht_outlet_cycles", methods=["POST"])
def rht_outlet_cycles():
    """
    SO1000 pump-run report. JSON array body, e.g.:
      [{"deviceID": ..., "recordNumber": 0, "utcunixTime": 1781310395000,
        "cycleDuration": 25071, "cycleCurrent": 8603}]
    cycleDuration is ms, cycleCurrent is mA. Sent ~35 s after the physical run.
    Answer locally + forward to pumpspy.com in the background (same pattern as
    /pings and /pump_outlet_alerts) so their app stays in sync.
    """
    if get_device_type() != "so1000":
        return _catch("rht_outlet_cycles", "rht_outlet_cycles")

    body = request.get_json(force=True, silent=True) or []
    if not isinstance(body, list):
        body = [body]

    for cycle in body:
        ts_ms  = cycle.get("utcunixTime", 0)
        dur_s  = round(cycle.get("cycleDuration", 0) / 1000, 2)
        amps   = cycle.get("cycleCurrent", 0) / 1000
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
        log.info(f"MAIN   ran (SO1000)  duration={dur_s}s  current={amps:.2f}A  device_time={ts}")
        record("rht_outlet_cycle", cycle)

        # Publish to MQTT + notify, mirroring the BBS main-pump path.
        iso_ts = (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                  if ts_ms else datetime.now(timezone.utc).isoformat())
        gal = round(dur_s / 1.02, 1) if dur_s else 0   # same flow estimate as BBS (ticks/10.2)
        mqtt.publish_main_pump_run(iso_ts, dur_s, gal, amps)
        notif.notify(notif.EVENT_MAIN_PUMP,
                     f"Duration: {dur_s}s · Est. {gal} gal · {amps:.2f}A")

    _fire_and_forget(request.method, request.path,
                     request.headers, request.get_data(), request.query_string)

    # Local response mirrors the real server's enriched-record shape.
    response = []
    for cycle in body:
        response.append({
            # Real server returns a non-null DB primary key here. The SO1000
            # firmware appears to treat null as "not acknowledged" and may stall
            # future run reports. Derive a stable non-zero integer from the run
            # timestamp so the device treats the record as confirmed.
            "idPumpOutletCycleData": (cycle.get("utcunixTime", 0) % 2147483647) or 1,
            "date_time":     None, "year_num":      None, "month_num": None,
            "week_num":      None, "day_num":       None, "total_count": None,
            "total_average": None, "gallons":       None,
            "cycleCurrent":  float(cycle.get("cycleCurrent", 0)),
            "cycleDuration": cycle.get("cycleDuration"),
            "deviceID":      cycle.get("deviceID"),
            "utcunixTime":   cycle.get("utcunixTime"),
            "recordNumber":  cycle.get("recordNumber", 0),
        })
    return jsonify(response), 200


# ---------------------------------------------------------------------------
# SmartPump endpoints — active only when device_type == 'smartpump'
# ---------------------------------------------------------------------------
# Decoded from a new-user capture (2026-06-26). The SmartPump is a third
# reporting variant: config is fetched via GET /pump_outlet_parameters and pump
# runs arrive as POST /pump_outlet_cycles (cf. SO1000's /rht_* equivalents).
#
# UNITS (from the capture): cycleDuration is in SECONDS (observed 21-149 — these
# would be implausible sub-second runs if treated as ms like the SO1000).
# cycleCurrent was 0 on every record, so its unit is ASSUMED mA (matching the
# SO1000) but is UNVERIFIED. Flip this flag if a real run proves ms.
SMARTPUMP_DURATION_IS_SECONDS = True

# We have no real-cloud config capture for this device. In the new-user capture
# the SmartPump kept reporting runs after receiving the catch-all's
# {"status":"ok"} for /pump_outlet_parameters, so that's a safe takeover
# fallback. In proxy mode we forward and return the real config when available.
PUMP_OUTLET_PARAMETERS_FALLBACK = {"status": "ok"}


@app.route("/pump_outlet_parameters/<int:device_id>", methods=["GET"])
def pump_outlet_parameters(device_id):
    """
    SmartPump config poll. Like the SO1000's /rht_parameters, the capture shows
    the device declaring a Content-Length (142) on a GET while sending NO body —
    so do NOT read request data anywhere in this handler (it would block until
    the device gives up). proxy_forward() already skips the body on GET.
    """
    if get_device_type() != "smartpump":
        return _catch(f"pump_outlet_parameters/{device_id}",
                      f"pump_outlet_parameters/{device_id}")
    log.info(f"PARAMS (SmartPump) requested by device {device_id}")
    record("params_fetch", {"deviceid": device_id, "device_type": "smartpump"})

    # Prefer the real cloud's config in proxy mode; only pass it through if it's
    # a success (a non-200 must not reach the device or it may stop reporting).
    proxied = proxy_forward()
    if proxied is not None and proxied.status_code == 200:
        return proxied

    # Takeover mode, proxy unreachable, or upstream error: canned fallback.
    return jsonify(PUMP_OUTLET_PARAMETERS_FALLBACK), 200


@app.route("/pump_outlet_cycles", methods=["POST"])
def pump_outlet_cycles():
    """
    SmartPump pump-run report. JSON array body, e.g.:
      [{"deviceID": ..., "recordNumber": 0, "utcunixTime": 1782436918000,
        "cycleDuration": 102, "cycleCurrent": 0}]
    cycleDuration is SECONDS (see SMARTPUMP_DURATION_IS_SECONDS), cycleCurrent mA.
    Answer locally + forward to pumpspy.com in the background (same pattern as
    /pings, /pump_outlet_alerts and /rht_outlet_cycles) so their app stays in sync.
    """
    if get_device_type() != "smartpump":
        return _catch("pump_outlet_cycles", "pump_outlet_cycles")

    body = request.get_json(force=True, silent=True) or []
    if not isinstance(body, list):
        body = [body]

    for cycle in body:
        ts_ms = cycle.get("utcunixTime", 0)
        raw   = cycle.get("cycleDuration", 0) or 0
        dur_s = round(raw if SMARTPUMP_DURATION_IS_SECONDS else raw / 1000, 2)
        amps  = (cycle.get("cycleCurrent", 0) or 0) / 1000
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
        log.info(f"MAIN   ran (SmartPump)  duration={dur_s}s  current={amps:.2f}A  device_time={ts}")
        record("pump_outlet_cycle", cycle)

        # Publish to MQTT + notify, mirroring the SO1000 main-pump path.
        iso_ts = (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
                  if ts_ms else datetime.now(timezone.utc).isoformat())
        gal = round(dur_s / 1.02, 1) if dur_s else 0   # same flow estimate as BBS/SO1000
        mqtt.publish_main_pump_run(iso_ts, dur_s, gal, amps)
        notif.notify(notif.EVENT_MAIN_PUMP,
                     f"Duration: {dur_s}s · Est. {gal} gal · {amps:.2f}A")

    _fire_and_forget(request.method, request.path,
                     request.headers, request.get_data(), request.query_string)

    # Local response mirrors the SO1000 enriched-record shape. Derive a stable
    # non-zero ack id from the timestamp (null may stall future run reports).
    response = []
    for cycle in body:
        response.append({
            "idPumpOutletCycleData": (cycle.get("utcunixTime", 0) % 2147483647) or 1,
            "date_time":     None, "year_num":      None, "month_num": None,
            "week_num":      None, "day_num":       None, "total_count": None,
            "total_average": None, "gallons":       None,
            "cycleCurrent":  float(cycle.get("cycleCurrent", 0)),
            "cycleDuration": cycle.get("cycleDuration"),
            "deviceID":      cycle.get("deviceID"),
            "utcunixTime":   cycle.get("utcunixTime"),
            "recordNumber":  cycle.get("recordNumber", 0),
        })
    return jsonify(response), 200


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
    try:
        body = request.get_data(as_text=True)
    except Exception:
        # Malformed device GET: Content-Length declared but no body sent.
        # Without this, werkzeug 400s here and the request is never recorded
        # or forwarded (seen on /rht_parameters from the SO1000 smart outlet).
        body = ""
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
    set_capture_enabled(False)   # never resume a debug capture across a restart
    mqtt.init()
    _threading.Thread(target=_hotspot_checker_loop, daemon=True, name="hotspot-checker").start()
    log.info(f"PumpSpy local server starting on 0.0.0.0:8081  mode={get_mode()}  real_server={REAL_SERVER}  db={DB_FILE}")
    app.run(host="0.0.0.0", port=8081, debug=False, threaded=True)
