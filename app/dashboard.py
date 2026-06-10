#!/usr/bin/env python3
"""
PumpSpy Dashboard — port 8080
Reads events.jsonl written by server.py and serves a live monitoring dashboard.
"""

import os
import re
import shutil
import socket
import subprocess
import threading
import time
import requests as rlib
from datetime import datetime, timezone, timedelta
from flask import (Flask, jsonify, request, render_template_string, make_response,
                   session, redirect, url_for)
from db import load_events, init_db, get_mode_switched_ts, get_device_ip, get_hotspot_connected

SERVER_URL    = os.environ.get("PUMPSPY_SERVER_URL", "http://127.0.0.1:8081")
HOTSPOT_CON   = os.environ.get("PUMPSLEEPER_HOTSPOT_CON", "Hotspot")
DASH_PORT     = int(os.environ.get("PUMPSLEEPER_DASH_PORT", "8080"))

app = Flask(__name__)

# Session signing key + cookie hardening. The key is persisted in the settings
# table so logins survive restarts. (Secure-only cookies are intentionally NOT
# forced: the dashboard is reached over plain http on the LAN as well as https
# via the tunnel, and a Secure cookie would break the LAN logins.)
init_db()
from db import get_secret_key as _get_secret_key
app.secret_key = _get_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Stay logged in across browser restarts. Sessions are marked permanent at
    # login and expire after this much *inactivity* (Flask refreshes the cookie
    # on each request), so regular use keeps you signed in.
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Record the dashboard's LAN address so notifications (which may be sent from the
# separate proxy process) can include a working link when no tunnel is up.
def _record_local_url():
    try:
        host = socket.gethostname() or "raspberrypi"
        if "." not in host:
            host += ".local"
        from db import _set_setting
        _set_setting("dashboard_local_url", f"http://{host}:{DASH_PORT}")
    except Exception:
        pass

_record_local_url()

# ---------------------------------------------------------------------------
# Login gate — every request requires a session, except the login page itself
# and Flask's static endpoint. (Login is always required, LAN included.)
# ---------------------------------------------------------------------------
@app.before_request
def _require_login():
    if request.endpoint in ("login", "static", "forgot", "reset"):
        return
    if session.get("authed"):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login", next=request.path))

# ---------------------------------------------------------------------------

def utc_ts_to_local_date(ts: str, tz_offset_minutes: int = 0) -> str:
    """
    Convert a UTC ISO timestamp to the user's local date string (YYYY-MM-DD).
    tz_offset_minutes: JS getTimezoneOffset() — minutes to subtract from local to get UTC.
    e.g. EDT = 240, so local = UTC - 240min.
    """
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt - timedelta(minutes=tz_offset_minutes)
        return local_dt.date().isoformat()
    except Exception:
        return ts[:10]


def compute_data(events, tz_offset_minutes: int = 0,
                 filter_date: str = None, filter_pump: str = None):
    now = datetime.now(timezone.utc)

    rssi_pings, batt_pings, outlet_alerts, backup_runs, main_bbs_runs, faults, alerts, unknowns, triggers = [], [], [], [], [], [], [], [], []

    for e in events:
        kind = e.get("kind")
        ts   = e.get("ts", "")
        data = e.get("data", {})

        if kind == "ping":
            dtype = data.get("idpings_data_type")
            value = data.get("value")
            if dtype == 1:
                rssi_pings.append({"ts": ts, "rssi": value})
            elif dtype == 3:
                # value is already in volts (e.g. 3.07)
                batt_pings.append({"ts": ts, "voltage": round(value, 3) if value is not None else None})

        elif kind == "pump_outlet_alert":
            alert_type = data.get("idPumpAlertType")
            value      = data.get("value", 0)
            if alert_type == 105:
                # value is AC current in milliamps (0 = pump off)
                outlet_alerts.append({
                    "ts":   ts,
                    "value": value,
                    "mamp":  value,
                    "type":  alert_type,
                })
            else:
                # Other alert types: high_water, ac_power_loss, excessive_current, etc.
                ALERT_NAMES = {
                    101: "high_water",
                    102: "ac_power_loss",
                    103: "excessive_current",
                    104: "excessive_run_time",
                    106: "pump_failure",
                }
                name = ALERT_NAMES.get(alert_type, f"alert_type_{alert_type}")
                alerts.append({
                    "ts":    ts,
                    "type":  alert_type,
                    "name":  name,
                    "state": "ON" if value else "OFF",
                    "value": value,
                })

        elif kind == "bbs_json":
            # Backup pump events
            inner = data.get("inner", {})
            if not isinstance(inner, dict):
                continue
            if "high_water" in inner:
                triggers.append({"ts": ts, "type": "high_water", "active": bool(inner["high_water"])})
            elif "low_water" in inner:
                triggers.append({"ts": ts, "type": "low_water",  "active": bool(inner["low_water"])})
            elif "motor_fail" in inner:
                faults.append({
                    "ts":    ts,
                    "state": "FAULT" if inner["motor_fail"] else "CLEARED",
                    "pump":  "backup",
                })
            elif "motor" in inner:
                batt_mv    = inner.get("battery_voltage", 0) or 0
                loaded_mv  = inner.get("loaded", 0) or 0
                mamp       = inner.get("mamp", 0) or 0
                ticks      = inner.get("time")
                duration_s = round(ticks / 10, 1)  if ticks is not None else None
                gallons    = round(ticks / 10.2, 1) if ticks is not None else None
                run = {
                    "ts":        ts,
                    "motor":     "STOPPED",
                    "ticks":     ticks,
                    "duration":  duration_s,   # ticks / 10 = seconds (confirmed)
                    "gallons":   gallons,       # ticks / 10.2 ≈ gallons (confirmed)
                    "amps":      round(mamp / 1000, 2),
                    "battery_v": round(batt_mv  / 1000, 3),
                    "loaded_v":  round(loaded_mv / 1000, 3),
                }
                # motor=1 → main pump ran; motor=0 → backup pump ran
                if inner["motor"]:
                    main_bbs_runs.append(run)
                else:
                    backup_runs.append(run)

        elif kind == "unknown":
            unknowns.append({
                "ts":     ts,
                "method": data.get("method"),
                "path":   data.get("path"),
                "body":   data.get("body", ""),
            })

    # --- Online / RSSI --------------------------------------------------------
    online         = False
    last_ping_ts   = None
    last_rssi      = None
    last_battery_v = None

    # Use the most recent ping of ANY type to determine online status
    all_pings_ts = []
    if rssi_pings:
        all_pings_ts.append(rssi_pings[-1]["ts"])
        last_rssi = rssi_pings[-1]["rssi"]
    if batt_pings:
        all_pings_ts.append(batt_pings[-1]["ts"])
        last_battery_v = batt_pings[-1]["voltage"]

    if all_pings_ts:
        last_ping_ts = max(all_pings_ts)
        try:
            lp = datetime.fromisoformat(last_ping_ts)
            online = (now - lp) < timedelta(minutes=5)
        except Exception:
            pass

    # --- Link status: pending after a mode switch until a new ping arrives ----
    PENDING_WINDOW = timedelta(minutes=3)
    PING_STALE     = timedelta(minutes=10)   # 5 missed 2-min pings → offline
    mode_switched_ts = get_mode_switched_ts()

    # If the hotspot checker has confirmed the device is not reachable on the
    # WiFi network, don't let a stale ping timestamp keep it showing as "online".
    _hotspot_ok = get_hotspot_connected()   # True / False / None
    if _hotspot_ok is False and not online:
        link_status = "offline"
    elif mode_switched_ts:
        try:
            switched_dt = datetime.fromisoformat(mode_switched_ts)
            if last_ping_ts:
                lp_dt = datetime.fromisoformat(last_ping_ts)
                if lp_dt > switched_dt and (now - lp_dt) < PING_STALE:
                    link_status = "online"   # recent ping after mode switch — confirmed
                elif (now - switched_dt) < PENDING_WINDOW:
                    link_status = "pending"  # switched recently, waiting for first contact
                else:
                    link_status = "offline"  # pings are stale or pre-date the switch
            elif (now - switched_dt) < PENDING_WINDOW:
                link_status = "pending"
            else:
                link_status = "offline"
        except Exception:
            link_status = "online" if online else "offline"
    else:
        link_status = "online" if online else "offline"

    # --- Main pump cycle detection from outlet alerts -------------------------
    # Only process type-105 alerts for run detection; other types are alerts/faults.
    # value = AC current in milliamps: 0 → OFF, >0 → ON (running, value = mA)
    main_pump_runs = []
    pending_start  = None   # ts string when pump turned ON
    peak_mamp      = 0      # track peak current during a run

    for alert in outlet_alerts:
        if alert.get("type") != 105:
            continue        # skip non-current alerts for run detection
        on   = bool(alert["value"])
        mamp = alert.get("mamp", 0) or 0
        if on and pending_start is None:
            pending_start = alert["ts"]
            peak_mamp = mamp
        elif on and pending_start is not None:
            # Still running — update peak current
            if mamp > peak_mamp:
                peak_mamp = mamp
        elif not on and pending_start is not None:
            # Pump stopped — compute duration
            try:
                t_start = datetime.fromisoformat(pending_start)
                t_stop  = datetime.fromisoformat(alert["ts"])
                duration_s = round((t_stop - t_start).total_seconds())
            except Exception:
                duration_s = None
            main_pump_runs.append({
                "ts":        alert["ts"],   # when it stopped
                "ts_start":  pending_start,
                "pump":      "main",
                "motor":     "STOPPED",
                "duration":  duration_s,
                "gallons":   duration_s,    # 1 gal/sec approximation
                "peak_mamp": peak_mamp,
                "amps":      round(peak_mamp / 1000, 2) if peak_mamp else None,
            })
            pending_start = None
            peak_mamp     = 0

    # If pump is currently ON, add a synthetic "RUNNING" entry
    if pending_start is not None:
        main_pump_runs.append({
            "ts":        pending_start,
            "ts_start":  pending_start,
            "pump":      "main",
            "motor":     "RUNNING",
            "duration":  None,
            "gallons":   None,
            "peak_mamp": peak_mamp,
            "amps":      round(peak_mamp / 1000, 2) if peak_mamp else None,
        })

    # --- Correlate water sensor triggers with backup runs --------------------
    # The trigger event (high_water/low_water) arrives up to ~60s before the run.
    def find_trigger(run_ts):
        # High water sensor sends a {"high_water":1} event before the run.
        # Low water sensor triggers silently — no preceding event.
        for t in reversed(triggers):
            if t["ts"] <= run_ts and t["active"]:
                try:
                    diff = (datetime.fromisoformat(run_ts) - datetime.fromisoformat(t["ts"])).total_seconds()
                    if diff <= 90:
                        return t["type"]   # "high_water"
                except Exception:
                    pass
        return "low_water"   # no trigger event → low water sensor

    # Build combined run list from bbs_json (primary source) + outlet-alert-derived runs
    combined_runs = []
    for r in backup_runs:
        combined_runs.append({
            "ts":        r["ts"],
            "ts_start":  r["ts"],
            "pump":      "backup",
            "motor":     "STOPPED",
            "duration":  r.get("duration"),
            "ticks":     r.get("ticks"),
            "gallons":   r.get("gallons"),
            "amps":      r.get("amps"),
            "battery_v": r.get("battery_v"),
            "loaded_v":  r.get("loaded_v"),
            "trigger":   find_trigger(r["ts"]),
        })
    for r in main_bbs_runs:
        combined_runs.append({
            "ts":        r["ts"],
            "ts_start":  r["ts"],
            "pump":      "main",
            "motor":     "STOPPED",
            "duration":  r.get("duration"),
            "ticks":     r.get("ticks"),
            "gallons":   r.get("gallons"),
            "amps":      r.get("amps"),
            "battery_v": r.get("battery_v"),
            "loaded_v":  r.get("loaded_v"),
        })
    for r in main_pump_runs:
        combined_runs.append(r)

    combined_runs.sort(key=lambda r: r["ts"], reverse=True)

    # --- Server-side filters (applied before slicing for the response) --------
    filtered_runs = combined_runs
    if filter_pump:
        filtered_runs = [r for r in filtered_runs if r.get("pump") == filter_pump]
    if filter_date:
        filtered_runs = [r for r in filtered_runs
                         if utc_ts_to_local_date(r["ts"], tz_offset_minutes) == filter_date]

    # --- Today's stats --------------------------------------------------------
    # Convert UTC "now" to the user's local date using the browser's tz offset.
    # tz_offset_minutes: JS getTimezoneOffset() e.g. EDT=240 → local = UTC - 240min
    local_now = now - timedelta(minutes=tz_offset_minutes)
    today = local_now.date().isoformat()

    def ts_local_date(ts):
        return utc_ts_to_local_date(ts, tz_offset_minutes)

    # bbs_json is the primary source for both pumps (motor=1=main, motor=0=backup)
    main_runs_today   = [r for r in main_bbs_runs if ts_local_date(r["ts"]) == today]
    backup_runs_today = [r for r in backup_runs   if ts_local_date(r["ts"]) == today]

    total_main_runtime_today   = round(sum(r["duration"] for r in main_runs_today   if r.get("duration") is not None), 1)
    total_backup_runtime_today = round(sum(r["duration"] for r in backup_runs_today if r.get("duration") is not None), 1)
    total_main_gallons_today   = round(sum(r["gallons"]  for r in main_runs_today   if r.get("gallons")  is not None), 1)
    total_backup_gallons_today = round(sum(r["gallons"]  for r in backup_runs_today if r.get("gallons")  is not None), 1)

    # --- Backup battery voltage from latest bbs_json STOPPED event ---------------
    last_backup_battery_v = None
    last_backup_loaded_v  = None
    for r in reversed(backup_runs):
        if r["motor"] == "STOPPED":
            last_backup_battery_v = r.get("battery_v")
            last_backup_loaded_v  = r.get("loaded_v")
            break

    # RSSI history — last 60 RSSI pings for chart
    rssi_history = [{"ts": p["ts"], "rssi": p["rssi"]} for p in rssi_pings[-60:]]

    # Merge bbs_json faults + non-105 outlet alerts into a single fault/alert log
    all_faults = sorted(
        faults + alerts,
        key=lambda x: x["ts"]
    )

    # --- Operating status: last pump that ran -----------------------------------
    last_run = combined_runs[0] if combined_runs else None
    if last_run:
        op_status = {
            "pump":    last_run["pump"],
            "ts":      last_run["ts"],
            "trigger": last_run.get("trigger"),
        }
    else:
        op_status = None

    return {
        "online":              online,
        "link_status":         link_status,
        "device_ip":           get_device_ip(),
        "hotspot_connected":   get_hotspot_connected(),
        "mode_switched_ts":    mode_switched_ts,
        "last_ping_ts":        last_ping_ts,
        "last_rssi":           last_rssi,
        "last_battery_v":           last_battery_v,
        "last_backup_battery_v":    last_backup_battery_v,
        "last_backup_loaded_v":     last_backup_loaded_v,
        "main_runs_today":     len(main_runs_today),
        "backup_runs_today":   len(backup_runs_today),
        "total_main_runtime_today":    total_main_runtime_today,
        "total_backup_runtime_today":  total_backup_runtime_today,
        "total_main_gallons_today":    total_main_gallons_today,
        "total_backup_gallons_today":  total_backup_gallons_today,
        "op_status":           op_status,
        "rssi_history":        rssi_history,
        "pump_runs":           filtered_runs[:200],
        "unknowns":            list(reversed(unknowns[-50:])),
        "server_time":         now.isoformat(),
    }

# ---------------------------------------------------------------------------

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PumpSleeper</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #8892a4; --green: #22c55e;
    --red: #ef4444; --yellow: #f59e0b; --blue: #3b82f6; --purple: #a855f7;
  }
  /* Light palette — applied for explicit light, or auto + OS light preference */
  :root[data-theme="light"] {
    --bg: #f4f6fa; --card: #ffffff; --border: #d9dee8;
    --text: #1d2430; --muted: #5b6675; --green: #16a34a;
    --red: #dc2626; --yellow: #d97706; --blue: #2563eb; --purple: #9333ea;
  }
  @media (prefers-color-scheme: light) {
    :root[data-theme="auto"] {
      --bg: #f4f6fa; --card: #ffffff; --border: #d9dee8;
      --text: #1d2430; --muted: #5b6675; --green: #16a34a;
      --red: #dc2626; --yellow: #d97706; --blue: #2563eb; --purple: #9333ea;
    }
  }
  .theme-btn.active { border-color: var(--blue); color: var(--blue); }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px;
           border-bottom: 1px solid var(--border); }
  header h1 { font-size: 18px; font-weight: 600; letter-spacing: 0.5px; }
  header h1 span { color: var(--blue); }
  #refresh-info { font-size: 12px; color: var(--muted); }
  .mode-toggle { display: flex; align-items: center; gap: 10px; }
  .mode-btn { padding: 6px 14px; border-radius: 6px; border: 1px solid var(--border);
              font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .mode-btn.active-proxy    { background: rgba(34,197,94,0.15);  color: var(--green); border-color: var(--green); }
  .mode-btn.active-takeover { background: rgba(239,68,68,0.15);  color: var(--red);   border-color: var(--red); }
  .mode-btn.inactive { background: transparent; color: var(--muted); }
  .mode-btn:hover { opacity: 0.8; }
  .grid { display: grid; gap: 16px; padding: 20px 24px; }
  .stats { grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted); margin-bottom: 8px; }
  .stat-value { font-size: 26px; font-weight: 700; }
  .stat-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  .dot.online { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.offline { background: var(--red); }
  .dot.pending { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted);
                   text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 12px; }
  .chart-wrap { position: relative; height: 200px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 10px; color: var(--muted); font-weight: 500;
       font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
       border-bottom: 1px solid var(--border); }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); color: var(--text); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge.running  { background: rgba(34,197,94,0.15);  color: var(--green); }
  .badge.stopped  { background: rgba(59,130,246,0.15);  color: var(--blue); }
  .badge.fault    { background: rgba(239,68,68,0.15);   color: var(--red); }
  .badge.cleared  { background: rgba(34,197,94,0.15);   color: var(--green); }
  .badge.unknown  { background: rgba(168,85,247,0.15);  color: var(--purple); }
  .badge.main     { background: rgba(59,130,246,0.12);  color: var(--blue); }
  .badge.backup   { background: rgba(245,158,11,0.15);  color: var(--yellow); }
  .empty { color: var(--muted); font-style: italic; padding: 12px 10px; }
  .two-col { grid-template-columns: 1fr 1fr; }
  @media (max-width: 700px) { .two-col { grid-template-columns: 1fr; } }
  .scroll-table { max-height: 280px; overflow-y: auto; }
  .scroll-table::-webkit-scrollbar { width: 4px; }
  .scroll-table::-webkit-scrollbar-track { background: transparent; }
  .scroll-table::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .body-preview { font-family: monospace; font-size: 11px; color: var(--muted);
                  max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .copy-btn { background: rgba(59,130,246,0.15); color: var(--blue); border: none;
              border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer; white-space: nowrap; }
  .copy-btn:hover { background: rgba(59,130,246,0.3); }
  .copy-btn.copied { background: rgba(34,197,94,0.15); color: var(--green); }
  .body-expand { background: var(--bg); border-top: 1px solid var(--border); }
  .body-expand td { padding: 10px; font-family: monospace; font-size: 12px;
                    white-space: pre-wrap; word-break: break-all; color: var(--text); }
  /* Collapsible widgets */
  .widget-header { display:flex; align-items:center; justify-content:space-between;
                   cursor:pointer; user-select:none; margin-bottom:12px; }
  .widget-header:hover .collapse-btn { color: var(--text); }
  .widget-header .section-title { margin-bottom:0; }
  .collapse-btn { font-size:12px; color:var(--muted); padding:2px 6px;
                  border:1px solid var(--border); border-radius:4px;
                  background:transparent; transition:transform 0.2s; }
  .collapsible-content { overflow:hidden; transition:opacity 0.15s; }
  .collapsed .collapsible-content { display:none; }
  .collapsed .collapse-btn { transform:rotate(-90deg); }
  .auth-banner { display:none; align-items:center; justify-content:space-between;
                 gap:12px; padding:10px 24px; background:rgba(245,158,11,0.12);
                 border-bottom:1px solid rgba(245,158,11,0.4); font-size:13px; }
  .auth-banner.visible { display:flex; }
  .auth-banner-msg { color: var(--yellow); }
  .auth-banner-msg strong { font-weight:700; }
  .auth-takeover-btn { padding:6px 16px; border-radius:6px; border:1px solid var(--red);
                       background:rgba(239,68,68,0.15); color:var(--red);
                       font-size:12px; font-weight:700; cursor:pointer; }
  .auth-takeover-btn:hover { background:rgba(239,68,68,0.3); }
  /* Sortable table headers */
  th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  th.sortable:hover { color: var(--text); }
  th.sortable .sort-icon { display:inline-block; margin-left:4px; opacity:0.3; font-size:10px; }
  th.sortable.asc  .sort-icon::after { content:'▲'; opacity:1; color:var(--blue); }
  th.sortable.desc .sort-icon::after { content:'▼'; opacity:1; color:var(--blue); }
  th.sortable:not(.asc):not(.desc) .sort-icon::after { content:'⇅'; }
  /* Filter bar */
  .filter-bar { display:flex; align-items:center; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
  .filter-bar label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }
  .filter-input { background:var(--bg); border:1px solid var(--border); border-radius:6px;
                  color:var(--text); font-size:12px; padding:5px 9px; outline:none; }
  .filter-input:focus { border-color:var(--blue); }
  .filter-input option { background:var(--card); }
  .filter-clear { font-size:11px; color:var(--muted); cursor:pointer; padding:5px 8px;
                  border:1px solid var(--border); border-radius:6px; background:transparent; }
  .filter-clear:hover { color:var(--text); border-color:var(--muted); }
  .filter-count { font-size:11px; color:var(--muted); margin-left:auto; }
  .cycle-btn { font-size:11px; color:var(--muted); background:transparent;
               border:1px solid var(--border); border-radius:5px; padding:4px 10px;
               cursor:pointer; transition:all 0.2s; }
  .cycle-btn:hover:not(:disabled) { color:var(--yellow); border-color:var(--yellow); }
  .cycle-btn:disabled { opacity:0.4; cursor:not-allowed; }
  .cycle-progress { font-size:12px; color:var(--yellow); }
  .cycle-done { font-size:12px; color:var(--green); }
  .cycle-error { font-size:12px; color:var(--red); }
  /* Tab nav */
  .tab-nav { display:flex; gap:4px; padding:0 24px; border-bottom:1px solid var(--border); background:var(--bg); }
  .tab-btn { padding:10px 18px; font-size:13px; font-weight:500; color:var(--muted);
             background:transparent; border:none; border-bottom:2px solid transparent;
             cursor:pointer; transition:color 0.15s; margin-bottom:-1px; }
  .tab-btn:hover { color:var(--text); }
  .tab-btn.active { color:var(--blue); border-bottom-color:var(--blue); }
  .tab-panel { display:none; }
  .tab-panel.active { display:block; }
  /* Sub-tabs within the Dashboard page (underline style) */
  .subtab-nav { display:flex; gap:22px; padding:14px 24px 0; margin-bottom:-1px;
                border-bottom:1px solid var(--border); }
  .subtab-btn { padding:8px 2px; font-size:13px; font-weight:500; color:var(--muted);
                background:transparent; border:none; border-bottom:2px solid transparent;
                cursor:pointer; transition:color 0.15s; margin-bottom:-1px; }
  .subtab-btn:hover { color:var(--text); }
  .subtab-btn.active { color:var(--blue); border-bottom-color:var(--blue); }
  .subtab-panel { display:none; }
  .subtab-panel.active { display:block; }
  /* Settings page */
  .settings-grid { display:grid; gap:16px; padding:20px 24px;
                   grid-template-columns: repeat(2, 1fr); }
  .settings-grid .full-width { grid-column: 1 / -1; }
  @media (max-width: 768px) { .settings-grid { grid-template-columns: 1fr; }
    .settings-grid .full-width { grid-column: 1; } }
  .settings-section { font-size:11px; text-transform:uppercase; letter-spacing:0.8px;
                      color:var(--muted); margin:8px 0 4px; }
  .form-row { display:flex; flex-direction:column; gap:4px; }
  .form-row label { font-size:12px; color:var(--muted); }
  .form-input { background:var(--bg); border:1px solid var(--border); border-radius:6px;
                color:var(--text); font-size:13px; padding:7px 10px; outline:none; width:100%; }
  .form-input:focus { border-color:var(--blue); }
  .form-row-inline { display:flex; align-items:center; gap:10px; }
  .toggle-label { display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; }
  .toggle-label input[type=checkbox] { width:16px; height:16px; accent-color:var(--blue); cursor:pointer; }
  .save-btn { padding:8px 22px; border-radius:6px; border:none; background:var(--blue);
              color:#fff; font-size:13px; font-weight:600; cursor:pointer; }
  .save-btn:hover { opacity:0.85; }
  .test-btn { padding:6px 14px; border-radius:6px; border:1px solid var(--border);
              background:transparent; color:var(--muted); font-size:12px; cursor:pointer; }
  .test-btn:hover { color:var(--text); border-color:var(--muted); }
  .settings-msg { font-size:12px; margin-top:6px; min-height:18px; }
  .settings-msg.ok  { color:var(--green); }
  .settings-msg.err { color:var(--red); }
  .two-row-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media (max-width:500px) { .two-row-grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header>
  <h1>Pump<span>Sleeper</span></h1>
  <div class="mode-toggle">
    <button class="mode-btn inactive" id="btn-proxy"    onclick="setMode('proxy')">Proxy</button>
    <button class="mode-btn inactive" id="btn-takeover" onclick="setMode('takeover')">Takeover</button>
    <span id="refresh-info">Loading…</span>
  </div>
</header>

<!-- Tab navigation -->
<div class="tab-nav">
  <button class="tab-btn active" onclick="showTab('dashboard')">Dashboard</button>
  <button class="tab-btn" onclick="showTab('settings')">Settings</button>
</div>

<div id="tab-dashboard" class="tab-panel active">

<!-- Auth failure banner -->
<div class="auth-banner" id="auth-banner">
  <span class="auth-banner-msg">
    ⚠ <strong>Real server authentication is failing.</strong>
    Switch to Takeover mode — the device will re-authenticate against your local server within seconds.
  </span>
  <button class="auth-takeover-btn" onclick="setMode('takeover')">Switch to Takeover</button>
</div>
<!-- /auth banner -->

<!-- Stat cards -->
<div class="grid" id="stat-cards" style="display:flex;flex-wrap:wrap;align-items:stretch">

  <!-- Hero: pump activity (runs + total gallons + operating status + device health footer) -->
  <div class="card" id="pump-card" style="flex:1.6 1 320px;display:flex;flex-direction:column">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap">
      <div class="stat-label" style="margin-bottom:0">Pump Activity · Today</div>
      <span id="op-pill" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:4px 10px;border-radius:6px;background:rgba(136,146,164,0.15);color:var(--muted)">
        <span id="op-dot" style="width:7px;height:7px;border-radius:50%;background:var(--muted)"></span>
        <span id="op-pill-text">—</span>
      </span>
    </div>
    <div style="flex:1;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;min-height:130px">
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;justify-content:center;gap:8px">
        <div class="stat-label" style="margin-bottom:0">Main Runs</div>
        <div id="s-main-runs" style="font-size:40px;font-weight:700;line-height:1">—</div>
        <div class="stat-sub" id="s-main-runtime" style="margin-top:0;line-height:1.5">—</div>
      </div>
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;justify-content:center;gap:8px">
        <div class="stat-label" style="margin-bottom:0">Backup Runs</div>
        <div id="s-backup-runs" style="font-size:40px;font-weight:700;line-height:1">—</div>
        <div class="stat-sub" id="s-backup-runtime" style="margin-top:0;line-height:1.5">—</div>
      </div>
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;justify-content:center;gap:8px">
        <div class="stat-label" style="margin-bottom:0">Total Today</div>
        <div style="line-height:1"><span id="s-total-gallons" style="font-size:40px;font-weight:700">—</span> <span style="font-size:16px;color:var(--muted);font-weight:600">gal</span></div>
        <div class="stat-sub" id="s-total-gallons-sub" style="margin-top:0;line-height:1.5">—</div>
      </div>
    </div>
    <!-- Device health footer (de-emphasised) -->
    <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--border);display:flex;align-items:center;gap:22px;flex-wrap:wrap">
      <span style="font-size:11px;text-transform:uppercase;letter-spacing:0.6px;color:var(--muted)">Device Health</span>
      <span style="font-size:13px;color:var(--muted)">Signal <strong id="s-rssi" style="color:var(--text);font-weight:600">—</strong> <span style="color:var(--muted)">dBm</span></span>
      <span style="font-size:13px;color:var(--muted)">Backup Battery <strong id="s-battery" style="color:var(--text);font-weight:600">—</strong> <span style="color:var(--muted)">V</span> <span id="s-battery-sub" style="color:var(--muted)"></span></span>
    </div>
  </div>

  <!-- Cloud link / connectivity (stretches to match the hero card height) -->
  <div class="card" style="flex:1 1 240px;display:flex;flex-direction:column">
    <div class="stat-label">PumpSpy Device</div>
    <div class="stat-value" id="s-online" style="margin-bottom:14px">—</div>
    <div style="font-size:13px">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Routed To</span><span id="s-link-label">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Device IP</span>
        <span id="s-device-ip" style="font-family:monospace;letter-spacing:0.3px">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Pump to Raspberry Pi</span><span id="s-hotspot">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Pi Internet</span><span id="s-uplink">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;padding:9px 0">
        <span style="color:var(--muted)">Last contact</span>
        <span id="s-last-ping" style="text-align:right">—</span>
      </div>
    </div>
    <div style="margin-top:auto;padding-top:16px">
      <button class="cycle-btn" id="cycle-btn" onclick="cycleHotspot()">↺ Cycle Raspberry Pi Hotspot</button>
    </div>
    <div id="cycle-status" style="display:none;margin-top:8px"></div>
  </div>
</div>
<!-- /stat cards -->

<!-- History sub-tabs (within the Dashboard page) -->
<div class="subtab-nav">
  <button class="subtab-btn active" onclick="showSubTab('pump')">Pump Run History</button>
  <button class="subtab-btn" onclick="showSubTab('signal')">Signal Strength</button>
</div>
<div id="subtab-pump" class="subtab-panel active">

<!-- Pump run history -->
<div class="grid" style="grid-template-columns:1fr; padding-top:0">
  <div class="card" id="widget-pump">
    <div class="widget-header" onclick="toggleWidget('widget-pump')">
      <div class="section-title">Pump Run History</div>
      <span class="collapse-btn">▼</span>
    </div>
    <div class="collapsible-content">
      <div class="filter-bar">
        <label>Run Date</label>
        <input type="date" id="filter-date" class="filter-input" onchange="refresh()">
        <label>Pump</label>
        <select id="filter-pump" class="filter-input" onchange="refresh()">
          <option value="">All</option>
          <option value="main">Main</option>
          <option value="backup">Backup</option>
        </select>
        <button class="filter-clear" onclick="clearFilters()">Clear</button>
        <span class="filter-count" id="filter-count"></span>
      </div>
      <div class="scroll-table" style="max-height:60vh"><table id="pump-table">
        <thead><tr>
          <th class="sortable" data-col="ts"        onclick="sortPumpTable(this)">Run Date <span class="sort-icon"></span></th>
          <th class="sortable" data-col="pump"      onclick="sortPumpTable(this)">Pump <span class="sort-icon"></span></th>
          <th class="sortable" data-col="duration"  onclick="sortPumpTable(this)">Duration <span class="sort-icon"></span></th>
          <th class="sortable" data-col="gallons"   onclick="sortPumpTable(this)">Est. Gallons <span class="sort-icon"></span></th>
          <th class="sortable col-extra" data-col="amps"      onclick="sortPumpTable(this)">Current <span class="sort-icon"></span></th>
          <th class="sortable col-extra" data-col="battery_v" onclick="sortPumpTable(this)">Batt V <span class="sort-icon"></span></th>
          <th class="sortable col-extra" data-col="loaded_v"  onclick="sortPumpTable(this)">Loaded V <span class="sort-icon"></span></th>
        </tr></thead>
        <tbody></tbody>
      </table></div>
    </div>
  </div>
</div>
<!-- /pump widget -->
</div>

<div id="subtab-signal" class="subtab-panel">
<!-- RSSI chart -->
<div class="grid" style="grid-template-columns:1fr; padding-top:0">
  <div class="card" id="widget-rssi">
    <div class="widget-header" onclick="toggleWidget('widget-rssi')">
      <div class="section-title">Signal Strength History</div>
      <span class="collapse-btn">▼</span>
    </div>
    <div class="collapsible-content">
      <div class="chart-wrap"><canvas id="rssi-chart"></canvas></div>
    </div>
  </div>
</div>
<!-- /rssi widget -->
</div>
<!-- /history subtabs -->

</div><!-- end tab-dashboard -->

<!-- Settings tab -->
<div id="tab-settings" class="tab-panel">
<div class="settings-grid">

  <!-- ── Appearance ───────────────────────────────────────────────── -->
  <div class="card full-width" id="appearance-card">
    <div class="section-title">Appearance</div>
    <div style="display:flex;align-items:center;gap:10px;margin-top:6px;flex-wrap:wrap">
      <span style="font-size:13px;color:var(--muted)">Theme</span>
      <button class="test-btn theme-btn" data-theme="auto"  onclick="setTheme('auto')">Auto</button>
      <button class="test-btn theme-btn" data-theme="dark"  onclick="setTheme('dark')">Dark</button>
      <button class="test-btn theme-btn" data-theme="light" onclick="setTheme('light')">Light</button>
      <span style="font-size:11px;color:var(--muted)">Auto follows this device's light/dark setting. Saved separately for desktop and mobile.</span>
    </div>
  </div>

  <!-- ── Security & Web Access ─────────────────────────────────────── -->
  <div class="card" id="security-card">
    <div class="section-title">Security &amp; Web Access</div>
    <div style="display:flex;flex-direction:column;gap:14px;margin-top:6px">
      <p style="font-size:12px;color:var(--muted);line-height:1.5">
        The dashboard requires a login. Change the default <strong>admin / admin</strong>
        credentials before exposing it to the web. &nbsp;<a href="/logout" style="color:var(--blue)">Log out</a>
      </p>
      <div class="form-row">
        <label>User Name</label>
        <input class="form-input" id="sec_username" autocomplete="username">
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <button class="test-btn" onclick="saveUsername()">Save User Name</button>
        <button class="test-btn" onclick="openPwModal()">Change Password</button>
        <span class="settings-msg" id="sec-msg" style="margin-top:0"></span>
      </div>

      <!-- Change-password modal -->
      <div id="pw-modal" onclick="if(event.target===this)closePwModal()" style="display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,0.6);align-items:center;justify-content:center;padding:20px">
        <div style="background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;width:100%;max-width:380px">
          <div class="section-title" style="margin-bottom:14px">Change Password</div>
          <div class="form-row" style="margin-bottom:12px">
            <label>New password (min 8 chars)</label>
            <input class="form-input" type="password" id="pw_new" autocomplete="new-password">
          </div>
          <div class="form-row">
            <label>Confirm new password</label>
            <input class="form-input" type="password" id="pw_new2" autocomplete="new-password">
          </div>
          <span class="settings-msg" id="pw-msg"></span>
          <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end">
            <button class="test-btn" onclick="closePwModal()">Cancel</button>
            <button class="save-btn" style="width:auto;padding:10px 20px" onclick="savePassword()">Save</button>
          </div>
        </div>
      </div>
      <hr style="border:none;border-top:1px solid var(--border);margin:2px 0">
      <div style="font-size:13px;font-weight:600;color:var(--text)">Web Access</div>

      <!-- Tunnel type -->
      <div style="display:flex;flex-direction:column;gap:8px">
        <label class="toggle-label" style="gap:8px;align-items:flex-start">
          <input type="radio" name="tunnel_mode" value="quick" onchange="onTunnelModeChange()" style="margin-top:3px">
          <span>Quick tunnel <span style="color:var(--muted);font-weight:400">— no account needed; the address changes each restart</span></span>
        </label>
        <label class="toggle-label" style="gap:8px;align-items:flex-start">
          <input type="radio" name="tunnel_mode" value="named" onchange="onTunnelModeChange()" style="margin-top:3px">
          <span>My Cloudflare tunnel <span style="color:var(--muted);font-weight:400">— stable address on your own domain</span></span>
        </label>
      </div>

      <!-- Named-tunnel fields -->
      <div id="named-fields" style="display:none;flex-direction:column;gap:10px">
        <div class="form-row">
          <label>Cloudflare tunnel token</label>
          <input class="form-input" type="password" id="cf_token" placeholder="eyJ…" autocomplete="off">
        </div>
        <div class="form-row">
          <label>Public hostname (e.g. pump.example.com)</label>
          <input class="form-input" id="cf_hostname" placeholder="pump.example.com">
        </div>
        <div>
          <button class="test-btn" onclick="saveTunnelConfig()">Save tunnel settings</button>
          <span class="settings-msg" id="tunnel-msg" style="margin-top:0"></span>
        </div>
        <p style="font-size:11px;color:var(--muted);line-height:1.5">In your Cloudflare Zero Trust dashboard (Networks → Tunnels), create a tunnel, route a public hostname to <strong>http://localhost:8080</strong>, then paste its token here.</p>
      </div>

      <label class="toggle-label" id="webaccess-row" style="opacity:0.5">
        <input type="checkbox" id="web_access" onchange="toggleWebAccess()" disabled>
        Enable web access
      </label>
      <div id="webaccess-hint" style="font-size:11px;color:var(--muted);line-height:1.5">
        Disabled until you change the default credentials.
      </div>
      <div id="tunnel-box" style="display:none;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted);margin-bottom:6px">Public URL</div>
        <a id="tunnel-url" href="#" target="_blank" rel="noopener" style="color:var(--blue);word-break:break-all;font-size:14px">—</a>
        <div id="tunnel-caveat" style="font-size:11px;color:var(--muted);margin-top:8px;line-height:1.5">This address changes each time the tunnel restarts (dashboard restart or reboot).</div>
      </div>
    </div>
  </div>

  <!-- ── Updates ──────────────────────────────────────────────────── -->
  <div class="card" id="update-card">
    <div class="section-title">Updates</div>
    <div style="display:flex;flex-direction:column;gap:12px;margin-top:4px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
        <div style="font-size:13px">
          Current version: <strong id="current-version" style="color:var(--blue)">—</strong>
          &nbsp;&nbsp;
          Latest: <strong id="latest-version" style="color:var(--muted)">checking…</strong>
        </div>
        <button class="test-btn" id="check-update-btn" onclick="checkForUpdates()">Check now</button>
      </div>
      <label class="toggle-label">
        <input type="checkbox" id="auto_update" onchange="saveAutoUpdate()">
        Automatically install updates overnight
      </label>
      <div id="release-notes-box" style="display:none">
        <div id="release-notes-title" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Release notes</div>
        <div id="release-notes" style="font-size:12.5px;line-height:1.5;color:var(--text);white-space:normal;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 12px;max-height:240px;overflow-y:auto"></div>
      </div>
      <div id="update-status-row" style="display:none;align-items:center;gap:12px">
        <button class="save-btn" id="apply-update-btn" onclick="applyUpdate()" style="background:var(--green)">Apply Update</button>
        <span id="update-status-msg" class="settings-msg"></span>
      </div>
      <div id="update-progress" style="display:none;font-size:12px;color:var(--yellow)"></div>
    </div>
  </div>

  <!-- ── Email ─────────────────────────────────────────────────── -->
  <div class="card">
    <div class="section-title">Email Notifications</div>
    <div style="display:flex;flex-direction:column;gap:12px;margin-top:4px">
      <label class="toggle-label">
        <input type="checkbox" id="email_enabled">
        Enable email notifications
      </label>
      <div class="two-row-grid">
        <div class="form-row">
          <label>SMTP Host</label>
          <input class="form-input" id="email_smtp_host" placeholder="smtp.gmail.com">
        </div>
        <div class="form-row">
          <label>SMTP Port</label>
          <input class="form-input" id="email_smtp_port" placeholder="587">
        </div>
      </div>
      <div class="two-row-grid">
        <div class="form-row">
          <label>Username</label>
          <input class="form-input" id="email_smtp_user" placeholder="you@gmail.com">
        </div>
        <div class="form-row">
          <label>Password / App password</label>
          <input class="form-input" type="password" id="email_smtp_pass" placeholder="••••••••">
        </div>
      </div>
      <div class="two-row-grid">
        <div class="form-row">
          <label>From address</label>
          <input class="form-input" id="email_from" placeholder="pumpsleeper@gmail.com">
        </div>
        <div class="form-row">
          <label>Send to</label>
          <input class="form-input" id="email_to" placeholder="you@example.com">
        </div>
      </div>
      <div>
        <button class="test-btn" id="email-test-btn" onclick="sendTest('email')">Send test email</button>
        <span class="settings-msg" id="email-test-msg"></span>
      </div>
    </div>
  </div>

  <!-- ── Ntfy ──────────────────────────────────────────────────── -->
  <div class="card">
    <div class="section-title">Ntfy Push Notifications</div>
    <div style="display:flex;flex-direction:column;gap:12px;margin-top:4px">
      <label class="toggle-label">
        <input type="checkbox" id="ntfy_enabled">
        Enable ntfy notifications
      </label>
      <div class="form-row">
        <label>Ntfy server URL</label>
        <input class="form-input" id="ntfy_url" placeholder="https://ntfy.sh">
      </div>
      <div class="two-row-grid">
        <div class="form-row">
          <label>Topic (keep this private)</label>
          <input class="form-input" id="ntfy_topic" placeholder="my-pumpsleeper-alerts">
        </div>
        <div class="form-row">
          <label>Access token (optional)</label>
          <input class="form-input" type="password" id="ntfy_token" placeholder="tk_...">
        </div>
      </div>
      <p style="font-size:11px;color:var(--muted)">
        Install the free <strong>ntfy</strong> app, subscribe to your topic, and you'll get instant push alerts.
        Use a unique random topic name so only you receive the notifications.
      </p>
      <div>
        <button class="test-btn" id="ntfy-test-btn" onclick="sendTest('ntfy')">Send test notification</button>
        <span class="settings-msg" id="ntfy-test-msg"></span>
      </div>
    </div>
  </div>

  <!-- ── Triggers ──────────────────────────────────────────────── -->
  <div class="card full-width">
    <div class="section-title">Notification Triggers</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:4px">
      <label class="toggle-label"><input type="checkbox" id="trigger_backup_pump_ran"> Backup pump ran</label>
      <label class="toggle-label"><input type="checkbox" id="trigger_main_pump_ran"> Main pump ran</label>
      <label class="toggle-label"><input type="checkbox" id="trigger_high_water"> High water alert</label>
      <label class="toggle-label"><input type="checkbox" id="trigger_device_offline"> Device offline</label>
      <label class="toggle-label"><input type="checkbox" id="trigger_update_available"> New version available</label>
      <label class="toggle-label"><input type="checkbox" id="trigger_update_installed"> New version installed</label>
    </div>
  </div>

  <!-- ── Backup & Restore ──────────────────────────────────────── -->
  <div class="card full-width" id="backup-card">
    <div class="section-title">Backup &amp; Restore</div>
    <div style="display:flex;flex-direction:column;gap:14px;margin-top:6px">
      <p style="font-size:12px;color:var(--muted);line-height:1.5">
        Save your settings (notifications, login, web access, theme) so you can restore them after reimaging the SD card. The backup file includes your saved credentials — keep it somewhere safe.
      </p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        <button class="test-btn" onclick="downloadBackup()">⬇ Download backup</button>
        <label class="test-btn" style="cursor:pointer;display:inline-flex;align-items:center">Restore from backup…
          <input type="file" id="restore-file" accept="application/json,.json" style="display:none" onchange="restoreBackup(this)">
        </label>
        <span class="settings-msg" id="backup-msg" style="margin-top:0"></span>
      </div>
      <hr style="border:none;border-top:1px solid var(--border);margin:2px 0">
      <label class="toggle-label">
        <input type="checkbox" id="backup_email_enabled" onchange="saveBackupAuto()">
        Email me a backup automatically (weekly)
      </label>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="test-btn" onclick="emailBackupNow()">Email backup now</button>
        <span style="font-size:11px;color:var(--muted)">Requires email notifications to be configured.</span>
        <span class="settings-msg" id="backup-email-msg" style="margin-top:0"></span>
      </div>
    </div>
  </div>

  <!-- ── Auto-save status ──────────────────────────────────────── -->
  <div class="full-width" style="display:flex;align-items:center;gap:10px">
    <span style="font-size:12px;color:var(--muted)">Changes are saved automatically.</span>
    <span class="settings-msg" id="save-msg" style="margin-top:0"></span>
  </div>

</div><!-- /settings grid -->

<!-- Debug section -->
<div class="grid" style="grid-template-columns:1fr; padding-top:0">
  <div class="section-title" style="margin-bottom:0">Debug</div>
</div>

<!-- Debug actions -->
<div class="grid" style="grid-template-columns:1fr; padding-top:0">
  <div class="card" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    <button class="test-btn" id="savelog-btn" onclick="downloadLog()">⬇ Save Log</button>
    <span style="font-size:12px;color:var(--muted)">Saves the last 2000 log lines from both PumpSleeper services to a file you choose.</span>
    <span class="settings-msg" id="savelog-msg" style="margin-top:0"></span>
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;border-left:1px solid var(--border);padding-left:14px">
      <input type="checkbox" id="capture-chk" onchange="toggleCapture(this)">
      <span>Capture pump traffic</span>
    </label>
    <span style="font-size:12px;color:var(--muted)">Records every message from your pump (works in proxy or takeover mode). Uncheck to download the log.</span>
    <span class="settings-msg" id="capture-msg" style="margin-top:0"></span>
  </div>
</div>
<!-- /debug actions -->

<!-- Unhandled requests (collapsed by default) -->
<div class="grid" style="grid-template-columns:1fr; padding-top:0">
  <div class="card collapsed" id="widget-unknown">
    <div class="widget-header" onclick="toggleWidget('widget-unknown')">
      <div class="section-title">Unhandled Requests</div>
      <span class="collapse-btn">▼</span>
    </div>
    <div class="collapsible-content">
      <div class="scroll-table"><table id="unknown-table">
        <thead><tr><th>Time</th><th>Method</th><th>Path</th><th>Body</th><th></th></tr></thead>
        <tbody></tbody>
      </table></div>
    </div>
  </div>
</div>
<!-- /unknown widget -->

</div><!-- end tab-settings -->

<script>
let rssiChart = null;
let _pumpRuns = [];
let _sortCol  = 'ts';
let _sortDir  = 'desc';
let _lastPingTs      = null;
let _modeSwitchedTs  = null;
let _linkStatus      = 'offline';
const PING_INTERVAL_MS  = 2 * 60 * 1000;   // ~2 min between pings
const PENDING_WINDOW_MS = 3 * 60 * 1000;   // 3 min pending before offline

function updatePendingCountdown() {
  if (_linkStatus !== 'pending') return;
  const pingSub = document.getElementById('s-last-ping');
  if (!pingSub) return;

  // Estimate next ping from last known ping + 2 min interval
  const nextExpected = _lastPingTs ? new Date(_lastPingTs).getTime() + PING_INTERVAL_MS : null;
  const now = Date.now();

  if (nextExpected && nextExpected > now) {
    const secsLeft = Math.ceil((nextExpected - now) / 1000);
    const m = Math.floor(secsLeft / 60);
    const s = secsLeft % 60;
    pingSub.textContent = 'Expected in ' + (m > 0 ? m + 'm ' : '') + s + 's';
  } else if (_modeSwitchedTs) {
    // Past expected — check if still within the 3-min pending window
    const elapsed = now - new Date(_modeSwitchedTs).getTime();
    const secsLeft = Math.ceil((PENDING_WINDOW_MS - elapsed) / 1000);
    if (secsLeft > 0) {
      const m = Math.floor(secsLeft / 60);
      const s = secsLeft % 60;
      pingSub.textContent = 'Overdue — offline in ' + (m > 0 ? m + 'm ' : '') + s + 's';
    } else {
      pingSub.textContent = 'No contact — checking…';
    }
  } else {
    pingSub.textContent = 'Waiting for first contact…';
  }
}

// Tick the countdown every second while pending
setInterval(updatePendingCountdown, 1000);

function sortPumpTable(th) {
  const col = th.dataset.col;
  _sortDir = (_sortCol === col && _sortDir === 'desc') ? 'asc' : 'desc';
  _sortCol = col;
  document.querySelectorAll('#pump-table th.sortable').forEach(h => {
    h.classList.remove('asc', 'desc');
    if (h.dataset.col === _sortCol) h.classList.add(_sortDir);
  });
  renderPumpTable();
}

function clearFilters() {
  document.getElementById('filter-date').value = '';
  document.getElementById('filter-pump').value = '';
  refresh();
}

function toggleWidget(id) {
  document.getElementById(id).classList.toggle('collapsed');
}

function renderPumpTable() {
  // Sorting only — filtering is done server-side
  let rows = _pumpRuns.slice();
  rows.sort((a, b) => {
    let av = a[_sortCol], bv = b[_sortCol];
    if (av === null || av === undefined) av = _sortDir === 'asc' ? Infinity : -Infinity;
    if (bv === null || bv === undefined) bv = _sortDir === 'asc' ? Infinity : -Infinity;
    if (typeof av === 'string') return _sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
    return _sortDir === 'asc' ? av - bv : bv - av;
  });

  document.getElementById('filter-count').textContent = rows.length + ' runs';

  setTbody('pump-table', rows.map(r => {
    const gallons  = (r.motor === 'STOPPED' && r.gallons   != null) ? r.gallons   + ' gal' : '—';
    const dur      = r.duration  != null ? r.duration  + 's' : '—';
    const amps     = r.amps      != null ? r.amps      + ' A'  : '—';
    const battV    = r.battery_v != null ? r.battery_v + ' V'  : '—';
    const loadedV  = r.loaded_v  != null ? r.loaded_v  + ' V'  : '—';
    const triggerLabel = (r.pump === 'backup' && r.trigger)
      ? '<br><span style="font-size:10px;color:var(--muted)">' + r.trigger.replace('_', ' ') + '</span>'
      : '';
    return '<tr>' +
      '<td>' + fmtTs(r.ts) + '</td>' +
      '<td><span class="badge ' + (r.pump || 'main') + '">' + (r.pump || 'main').toUpperCase() + '</span>' + triggerLabel + '</td>' +
      '<td>' + dur + '</td>' +
      '<td>' + gallons + '</td>' +
      '<td class="col-extra">' + amps + '</td>' +
      '<td class="col-extra">' + battV + '</td>' +
      '<td class="col-extra">' + loadedV + '</td>' +
      '</tr>';
  }));
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggleBody(rowId) {
  const row = document.getElementById(rowId);
  if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
}

function copyBody(btn, rowId) {
  const el = document.getElementById('btext-' + rowId);
  if (!el) return;
  const text = el.textContent;
  const done = () => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  };
  // navigator.clipboard requires HTTPS; fall back to execCommand for HTTP (local Pi)
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => execCopy(text, done));
  } else {
    execCopy(text, done);
  }
}

function execCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); done(); } catch(e) {}
  document.body.removeChild(ta);
}

function fmtTs(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  } catch { return ts; }
}

function fmtAgo(ts) {
  if (!ts) return '';
  try {
    let diff = Math.floor((Date.now() - new Date(ts)) / 1000);
    if (diff < 0) diff = 0;            // guard against minor server/browser clock skew
    if (diff < 5) return 'just now';
    if (diff < 60) return diff + 's ago';
    if (diff < 3600) return Math.floor(diff/60) + 'm ago';
    return Math.floor(diff/3600) + 'h ago';
  } catch { return ''; }
}

function setTbody(tableId, rows) {
  const tb = document.querySelector('#' + tableId + ' tbody');
  tb.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="10" class="empty">No data yet</td></tr>';
}

function update(d) {
  // Where the PumpSpy device's traffic is currently routed
  document.getElementById('s-link-label').textContent =
    d.mode === 'takeover' ? 'Local' : 'PumpSpy Servers';

  // Device link status card
  _linkStatus     = d.link_status || (d.online ? 'online' : 'offline');
  _lastPingTs     = d.last_ping_ts || null;
  _modeSwitchedTs = d.mode_switched_ts || null;

  const onEl = document.getElementById('s-online');
  const statusLabel = _linkStatus === 'online'  ? 'Online'
                    : _linkStatus === 'pending' ? 'Pending…'
                    : 'Offline';
  onEl.innerHTML = '<span class="dot ' + _linkStatus + '"></span>' + statusLabel;

  // Hotspot detail row
  const hsEl = document.getElementById('s-hotspot');
  if (hsEl) {
    if (d.hotspot_connected === true)       { hsEl.textContent = 'Connected';    hsEl.style.color = 'var(--green)'; }
    else if (d.hotspot_connected === false) { hsEl.textContent = 'Disconnected'; hsEl.style.color = 'var(--red)'; }
    else                                    { hsEl.textContent = '—';            hsEl.style.color = 'var(--muted)'; }
  }

  // Pi internet uplink row (how the Pi itself reaches the internet)
  const upEl = document.getElementById('s-uplink');
  if (upEl) {
    const upMap = { ethernet: 'Ethernet', 'usb-wifi': 'USB Wi‑Fi', wifi: 'Wi‑Fi' };
    if (d.pi_uplink && upMap[d.pi_uplink]) {
      upEl.textContent = upMap[d.pi_uplink]; upEl.style.color = 'var(--green)';
    } else {
      upEl.textContent = 'No internet'; upEl.style.color = 'var(--red)';
    }
  }

  // Device IP (hotspot row above carries the WiFi state)
  const ipEl = document.getElementById('s-device-ip');
  ipEl.textContent = d.device_ip || '—';

  const pingSub = document.getElementById('s-last-ping');
  if (_linkStatus === 'pending') {
    updatePendingCountdown();   // populate immediately; setInterval keeps it ticking
  } else if (_linkStatus === 'offline' && d.hotspot_connected === true) {
    pingSub.textContent = 'On WiFi — not pinging'
      + (d.last_ping_ts ? ' · last ' + fmtAgo(d.last_ping_ts) : '');
  } else if (_linkStatus === 'offline' && d.hotspot_connected === false) {
    pingSub.textContent = 'Not on hotspot'
      + (d.last_ping_ts ? ' · last seen ' + fmtAgo(d.last_ping_ts) : '');
  } else {
    pingSub.textContent = d.last_ping_ts
      ? fmtTs(d.last_ping_ts) + ' (' + fmtAgo(d.last_ping_ts) + ')' : 'Never';
  }

  document.getElementById('s-rssi').textContent = d.last_rssi !== null && d.last_rssi !== undefined ? d.last_rssi : '—';
  // The 12V backup-battery voltage is only reported by the device during a
  // backup-pump run (bbs_json) — same value as the Pump Run History "Batt V".
  // Show the most recent one; it stays "—" until the first backup run.
  const backupBatt = (d.last_backup_battery_v !== null && d.last_backup_battery_v !== undefined)
    ? d.last_backup_battery_v.toFixed(2) : '—';
  document.getElementById('s-battery').textContent = backupBatt;
  document.getElementById('s-battery-sub').textContent =
    (d.last_backup_loaded_v !== null && d.last_backup_loaded_v !== undefined)
      ? 'loaded ' + d.last_backup_loaded_v.toFixed(2) + ' V' : '';

  const runDetail = (rt, gal) => rt > 0
    ? rt + 's runtime<br>' + gal + ' gal pumped'
    : 'No runs today';

  document.getElementById('s-main-runs').textContent = d.main_runs_today;
  document.getElementById('s-main-runtime').innerHTML =
    runDetail(d.total_main_runtime_today, d.total_main_gallons_today);

  document.getElementById('s-backup-runs').textContent = d.backup_runs_today;
  document.getElementById('s-backup-runtime').innerHTML =
    runDetail(d.total_backup_runtime_today, d.total_backup_gallons_today);

  // Total gallons today (main + backup)
  const mainGal = d.total_main_gallons_today   || 0;
  const bkupGal = d.total_backup_gallons_today || 0;
  const totGal  = Math.round((mainGal + bkupGal) * 10) / 10;
  document.getElementById('s-total-gallons').textContent     = totGal;
  document.getElementById('s-total-gallons-sub').innerHTML   = mainGal + ' gal main<br>' + bkupGal + ' gal backup';

  // Operating status — consolidated into a pill on the pump card
  const pill = document.getElementById('op-pill');
  const dot  = document.getElementById('op-dot');
  const ptxt = document.getElementById('op-pill-text');
  if (!d.op_status) {
    ptxt.textContent     = 'No runs yet';
    dot.style.background  = 'var(--muted)';
    pill.style.background = 'rgba(136,146,164,0.15)';
    pill.style.color      = 'var(--muted)';
  } else if (d.op_status.pump === 'main') {
    ptxt.textContent     = 'Main pump · ' + fmtAgo(d.op_status.ts);
    dot.style.background  = 'var(--green)';
    pill.style.background = 'rgba(34,197,94,0.15)';
    pill.style.color      = 'var(--green)';
  } else {
    const trig = d.op_status.trigger ? d.op_status.trigger.replace('_', ' ') + ' · ' : '';
    ptxt.textContent     = 'Backup pump · ' + trig + fmtAgo(d.op_status.ts);
    dot.style.background  = 'var(--yellow)';
    pill.style.background = 'rgba(245,158,11,0.15)';
    pill.style.color      = 'var(--yellow)';
  }

  // RSSI chart
  const labels = d.rssi_history.map(p => fmtTs(p.ts));
  const values = d.rssi_history.map(p => p.rssi);
  if (!rssiChart) {
    const ctx = document.getElementById('rssi-chart').getContext('2d');
    rssiChart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [{ label: 'RSSI (dBm)', data: values,
        borderColor: cssVar('--blue'), backgroundColor: 'rgba(59,130,246,0.1)',
        pointRadius: 3, pointBackgroundColor: cssVar('--blue'), tension: 0.3, fill: true }] },
      options: { responsive: true, maintainAspectRatio: false,
        scales: {
          x: { ticks: { color: cssVar('--muted'), maxTicksLimit: 8, maxRotation: 0 }, grid: { color: cssVar('--border') } },
          y: { ticks: { color: cssVar('--muted') }, grid: { color: cssVar('--border') } }
        },
        plugins: { legend: { display: false } }
      }
    });
  } else {
    rssiChart.data.labels = labels;
    rssiChart.data.datasets[0].data = values;
    rssiChart.update('none');
  }

  // Pump run history — store server result, render (sort only client-side)
  _pumpRuns = d.pump_runs || [];
  renderPumpTable();

  // Unknown requests table — expandable body rows
  const unknownRows = [];
  d.unknowns.forEach((u, i) => {
    const rowId = 'unk-' + i;
    const body = u.body || '';
    const preview = body.length > 80 ? body.slice(0, 80) + '…' : body;
    unknownRows.push(
      '<tr style="cursor:pointer" data-row="' + rowId + '" onclick="toggleBody(this.dataset.row)">' +
      '<td>' + fmtTs(u.ts) + '</td>' +
      '<td><span class="badge unknown">' + (u.method || '?') + '</span></td>' +
      '<td style="font-family:monospace">' + escHtml(u.path || '') + '</td>' +
      '<td class="body-preview">' + escHtml(preview) + '</td>' +
      '<td><button class="copy-btn" data-row="' + rowId + '" onclick="event.stopPropagation();copyBody(this,this.dataset.row)">Copy</button></td>' +
      '</tr>' +
      '<tr class="body-expand" id="' + rowId + '" style="display:none">' +
      '<td colspan="5" id="btext-' + rowId + '">' + escHtml(body) + '</td>' +
      '</tr>'
    );
  });
  setTbody('unknown-table', unknownRows);

  document.getElementById('refresh-info').textContent = 'Updated ' + fmtAgo(d.server_time);
}

async function refresh() {
  try {
    const tzOffset  = new Date().getTimezoneOffset();
    const filterDate = document.getElementById('filter-date').value;   // YYYY-MM-DD or ''
    const filterPump = document.getElementById('filter-pump').value;   // 'main'|'backup'|''
    let url = '/api/data?tz_offset=' + tzOffset;
    if (filterDate) url += '&filter_date=' + encodeURIComponent(filterDate);
    if (filterPump) url += '&filter_pump=' + encodeURIComponent(filterPump);
    const r = await fetch(url);
    const d = await r.json();
    update(d);
  } catch(e) {
    document.getElementById('refresh-info').textContent = 'Error fetching data';
  }
}

function updateModeButtons(d) {
  const mode     = d.mode || d; // accept full object or bare mode string
  const proxy    = document.getElementById('btn-proxy');
  const takeover = document.getElementById('btn-takeover');
  proxy.className    = 'mode-btn ' + (mode === 'proxy'    ? 'active-proxy'    : 'inactive');
  takeover.className = 'mode-btn ' + (mode === 'takeover' ? 'active-takeover' : 'inactive');

  // Show auth failure banner only in proxy mode with consecutive failures
  const banner   = document.getElementById('auth-banner');
  const failures = d.auth_failures || 0;
  if (mode === 'proxy' && failures >= 2) {
    banner.classList.add('visible');
  } else {
    banner.classList.remove('visible');
  }
}

async function fetchMode() {
  try {
    const r = await fetch('/api/mode');
    const d = await r.json();
    updateModeButtons(d);
  } catch(e) {}
}

async function setMode(mode) {
  try {
    const r = await fetch('/api/mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode})
    });
    const d = await r.json();
    // Refresh data immediately so link_status flips to pending without waiting
    await Promise.all([fetchMode(), refresh()]);
  } catch(e) {
    alert('Failed to switch mode — is the server running?');
  }
}

// ── Hotspot cycle ─────────────────────────────────────────────────────────
let _cyclePoller = null;
const CYCLE_DOWN_SECS = 30;

async function cycleHotspot() {
  if (!confirm('Cycle the WiFi hotspot? The PumpSpy device will disconnect for ~30 seconds then reconnect automatically.')) return;
  const btn    = document.getElementById('cycle-btn');
  const status = document.getElementById('cycle-status');
  btn.disabled = true;
  status.style.display = '';
  status.className = 'cycle-progress';
  status.textContent = 'Starting hotspot cycle…';

  try {
    await fetch('/api/hotspot/cycle', { method: 'POST' });
  } catch(e) {
    status.className = 'cycle-error';
    status.textContent = 'Failed to start cycle — is the dashboard server running?';
    btn.disabled = false;
    return;
  }

  if (_cyclePoller) clearInterval(_cyclePoller);
  _cyclePoller = setInterval(async () => {
    try {
      const r = await fetch('/api/hotspot/cycle');
      const s = await r.json();
      const elapsed = s.elapsed || 0;

      // ── Active phases ──────────────────────────────────────────────────
      if (s.phase === 'down' || s.phase === 'starting') {
        const secsLeft = Math.max(0, CYCLE_DOWN_SECS - elapsed);
        status.className = 'cycle-progress';
        status.textContent = '📡 Hotspot down — coming back in ' + secsLeft + 's…';

      } else if (s.phase === 'up') {
        status.className = 'cycle-progress';
        status.textContent = '📡 Hotspot coming back up…';

      } else if (s.phase === 'checking') {
        const checkSecs = Math.max(0, elapsed - CYCLE_DOWN_SECS - 3);
        const wifiMark  = s.wifi_reachable === true  ? ' · WiFi ✓'
                        : s.wifi_reachable === false ? ' · WiFi ✗'
                        : '';
        status.className = 'cycle-progress';
        status.textContent = '🔍 Checking device' + wifiMark + ' — ' + checkSecs + 's…';
        refresh();   // keep the dashboard live during the check

      // ── Terminal phases ────────────────────────────────────────────────
      } else if (s.phase === 'online') {
        _stopCyclePoller();
        status.className = 'cycle-done';
        status.textContent = '✓ Device back online and pinging';
        btn.disabled = false;
        refresh();
        setTimeout(() => { status.style.display = 'none'; }, 10000);

      } else if (s.phase === 'wifi_only') {
        _stopCyclePoller();
        status.className = 'cycle-progress';  // amber
        status.innerHTML = '⚠ Device on WiFi but not pinging HTTP.<br>'
          + '<span style="font-size:11px;color:var(--muted)">Try power cycling the PumpSpy device.</span>';
        btn.disabled = false;
        refresh();

      } else if (s.phase === 'unreachable') {
        _stopCyclePoller();
        status.className = 'cycle-error';
        status.innerHTML = '✗ Device not responding after hotspot cycle.<br>'
          + '<span style="font-size:11px;color:var(--muted)">Power cycle the PumpSpy device to recover.</span>';
        btn.disabled = false;
        refresh();

      } else if (s.phase === 'error') {
        _stopCyclePoller();
        status.className = 'cycle-error';
        status.textContent = '✗ Error: ' + (s.error || 'unknown');
        btn.disabled = false;
      }
    } catch(e) { /* dashboard briefly unreachable during cycle is fine */ }
  }, 1000);
}

function _stopCyclePoller() {
  if (_cyclePoller) { clearInterval(_cyclePoller); _cyclePoller = null; }
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function _highlightThemeButtons(pref) {
  document.querySelectorAll('.theme-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === pref));
}
function refreshChartTheme() {
  if (!rssiChart) return;
  const blue = cssVar('--blue'), muted = cssVar('--muted'), border = cssVar('--border');
  const ds = rssiChart.data.datasets[0];
  ds.borderColor = blue; ds.pointBackgroundColor = blue;
  rssiChart.options.scales.x.ticks.color = muted;
  rssiChart.options.scales.y.ticks.color = muted;
  rssiChart.options.scales.x.grid.color  = border;
  rssiChart.options.scales.y.grid.color  = border;
  rssiChart.update('none');
}
function applyTheme(pref) {
  document.documentElement.dataset.theme = pref;   // CSS resolves 'auto' via media query
  _highlightThemeButtons(pref);
  refreshChartTheme();
}
async function setTheme(pref) {
  applyTheme(pref);
  try {
    await fetch('/api/settings/theme', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({theme: pref})
    });
  } catch(e) {}
}
async function loadTheme() {
  try {
    const r = await fetch('/api/settings/theme');
    const d = await r.json();
    applyTheme(d.theme || 'auto');
  } catch(e) {
    _highlightThemeButtons(document.documentElement.dataset.theme || 'auto');
  }
}
// Recolor the chart when the OS theme flips while in 'auto'.
if (window.matchMedia) {
  try {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
      if ((document.documentElement.dataset.theme || 'auto') === 'auto') refreshChartTheme();
    });
  } catch(e) {}
}

// Default the Pump Run History date filter to today (local) on first load.
(function initDateFilter() {
  const fd = document.getElementById('filter-date');
  if (fd && !fd.value) {
    const d = new Date();
    fd.value = d.getFullYear() + '-' +
               String(d.getMonth() + 1).padStart(2, '0') + '-' +
               String(d.getDate()).padStart(2, '0');
  }
})();

refresh();
fetchMode();
loadTheme();
setInterval(refresh, 30000);
setInterval(fetchMode, 10000);

// ── Tab switching ─────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => {
    if ((b.getAttribute('onclick') || '').indexOf("'" + name + "'") !== -1) b.classList.add('active');
  });
  if (name === 'settings') { loadSettings(); loadUpdateInfo(); loadTheme(); loadSecurity(); loadBackup(); }
}

function showSubTab(name) {
  document.querySelectorAll('.subtab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.subtab-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('subtab-' + name);
  if (panel) panel.classList.add('active');
  document.querySelectorAll('.subtab-btn').forEach(b => {
    if ((b.getAttribute('onclick') || '').indexOf("'" + name + "'") !== -1) b.classList.add('active');
  });
  if (name === 'signal' && rssiChart) rssiChart.resize();
}

// ── Debug: download service logs ──────────────────────────────────────────
function downloadLog() {
  const msg = document.getElementById('savelog-msg');
  if (msg) { msg.textContent = 'Preparing log…'; msg.className = 'settings-msg'; }
  // Navigating to an attachment URL triggers a download without leaving the page;
  // the browser then prompts for (or uses the configured) save location.
  window.location.href = '/api/debug/log';
  setTimeout(() => { if (msg) { msg.textContent = ''; } }, 4000);
}

// ── Debug: capture raw pump traffic ───────────────────────────────────────
function toggleCapture(chk) {
  const msg = document.getElementById('capture-msg');
  const on  = chk.checked;
  chk.disabled = true;
  fetch('/api/debug/capture', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: on }),
  }).then(r => r.json()).then(d => {
    chk.disabled = false;
    if (!d.ok) {
      chk.checked = !on;
      if (msg) { msg.textContent = 'Error: ' + (d.error || 'failed'); msg.className = 'settings-msg err'; }
      return;
    }
    if (on) {
      if (msg) { msg.textContent = '● Recording… reproduce a pump run, then uncheck to download.'; msg.className = 'settings-msg'; }
    } else {
      if (msg) { msg.textContent = 'Saving capture…'; msg.className = 'settings-msg'; }
      // Download the captured log (browser prompts for save location).
      window.location.href = '/api/debug/capture/download';
      setTimeout(() => { if (msg) { msg.textContent = 'Capture saved.'; } }, 1500);
    }
  }).catch(e => {
    chk.disabled = false;
    chk.checked = !on;
    if (msg) { msg.textContent = 'Error: ' + e; msg.className = 'settings-msg err'; }
  });
}

// Reflect the current capture state (e.g. after a page refresh while recording).
function initCaptureState() {
  const chk = document.getElementById('capture-chk');
  if (!chk) return;
  fetch('/api/debug/capture').then(r => r.json()).then(d => {
    chk.checked = !!d.enabled;
    const msg = document.getElementById('capture-msg');
    if (d.enabled && msg) { msg.textContent = '● Recording… uncheck to download.'; msg.className = 'settings-msg'; }
  }).catch(() => {});
}
initCaptureState();

// ── Settings backup & restore ─────────────────────────────────────────────
function downloadBackup() {
  const m = document.getElementById('backup-msg');
  if (m) { m.textContent = 'Preparing…'; m.className = 'settings-msg'; }
  window.location.href = '/api/settings/backup';
  setTimeout(() => { if (m) m.textContent = ''; }, 3000);
}

async function restoreBackup(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  if (!confirm('Restore settings from this backup? This overwrites your current settings (notifications, login, web access, theme).')) { input.value = ''; return; }
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch('/api/settings/restore', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.ok) {
      _setMsg('backup-msg', '✓ Restored ' + d.restored + ' settings' + (d.skipped ? ' (' + d.skipped + ' skipped)' : ''), true);
      if (d.warning) alert(d.warning);
      setTimeout(() => location.reload(), 1500);
    } else {
      _setMsg('backup-msg', d.error || 'Restore failed', false);
    }
  } catch(e) { _setMsg('backup-msg', 'Restore failed', false); }
  input.value = '';
}

async function loadBackup() {
  try {
    const r = await fetch('/api/settings/backup/auto');
    const d = await r.json();
    const cb = document.getElementById('backup_email_enabled');
    if (cb) cb.checked = !!d.enabled;
  } catch(e) {}
}

async function saveBackupAuto() {
  const en = document.getElementById('backup_email_enabled').checked;
  try {
    await fetch('/api/settings/backup/auto', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({enabled: en})
    });
    _setMsg('backup-email-msg', en ? '✓ Weekly backup email on' : 'Weekly backup email off', true);
  } catch(e) { _setMsg('backup-email-msg', 'Failed', false); }
}

async function emailBackupNow() {
  const m = document.getElementById('backup-email-msg');
  if (m) { m.textContent = 'Sending…'; m.className = 'settings-msg'; }
  try {
    const r = await fetch('/api/settings/backup/email', { method: 'POST' });
    const d = await r.json();
    _setMsg('backup-email-msg', d.ok ? '✓ Backup emailed' : ('✗ ' + (d.error || 'failed')), d.ok);
  } catch(e) { _setMsg('backup-email-msg', 'Request failed', false); }
}

// ── Security & web access ─────────────────────────────────────────────────
async function loadSecurity() {
  try {
    const r = await fetch('/api/settings/security');
    const d = await r.json();
    const userEl = document.getElementById('sec_username');
    if (userEl && document.activeElement !== userEl) userEl.value = d.username || '';
    const cb   = document.getElementById('web_access');
    const row  = document.getElementById('webaccess-row');
    const hint = document.getElementById('webaccess-hint');
    if (cb) {
      cb.checked  = !!d.web_access;
      cb.disabled = !d.creds_changed;
      if (row) row.style.opacity = d.creds_changed ? '1' : '0.5';
      if (hint) {
        if (!d.creds_changed)              hint.textContent = 'Disabled until you change the default credentials.';
        else if (!d.cloudflared_installed) hint.textContent = 'cloudflared is not installed on the Pi — install it to enable web access.';
        else if (d.tunnel_mode === 'named') hint.textContent = 'When on, your Cloudflare tunnel exposes this dashboard at your hostname below.';
        else                                hint.textContent = 'When on, a quick Cloudflare tunnel exposes this dashboard at the URL below.';
      }
    }
    // Tunnel type + named-tunnel fields
    const modeVal = d.tunnel_mode || 'quick';
    document.querySelectorAll('input[name="tunnel_mode"]').forEach(rd => { rd.checked = (rd.value === modeVal); });
    const hn = document.getElementById('cf_hostname');
    if (hn && document.activeElement !== hn) hn.value = d.cf_tunnel_hostname || '';
    const tk = document.getElementById('cf_token');
    if (tk && document.activeElement !== tk) { tk.value = ''; tk.placeholder = d.cf_tunnel_token_saved ? '(saved)' : 'eyJ…'; }
    _applyTunnelMode(modeVal);
    _renderTunnel(d);
  } catch(e) { console.error('Failed to load security', e); }
}

function _renderTunnel(d) {
  const box = document.getElementById('tunnel-box');
  const a   = document.getElementById('tunnel-url');
  if (!box || !a) return;
  if (d.web_access && d.tunnel_url) {
    box.style.display = '';
    a.href = d.tunnel_url; a.textContent = d.tunnel_url;
  } else if (d.web_access && d.tunnel_running) {
    box.style.display = '';
    a.removeAttribute('href'); a.textContent = 'Starting tunnel… reload in a few seconds';
  } else {
    box.style.display = 'none';
  }
}

function _applyTunnelMode(mode) {
  const nf = document.getElementById('named-fields');
  if (nf) nf.style.display = (mode === 'named') ? 'flex' : 'none';
  const cav = document.getElementById('tunnel-caveat');
  if (cav) cav.textContent = (mode === 'named')
    ? 'Stable address served by your Cloudflare tunnel.'
    : 'This address changes each time the tunnel restarts (dashboard restart or reboot).';
}

function onTunnelModeChange() {
  const sel = document.querySelector('input[name="tunnel_mode"]:checked');
  _applyTunnelMode(sel ? sel.value : 'quick');
  saveTunnelConfig();
}

async function saveTunnelConfig() {
  const sel = document.querySelector('input[name="tunnel_mode"]:checked');
  const mode = sel ? sel.value : 'quick';
  const token = document.getElementById('cf_token').value;
  const hostname = document.getElementById('cf_hostname').value.trim();
  const body = { mode, hostname };
  if (token) body.token = token;
  try {
    const r = await fetch('/api/settings/tunnel', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const d = await r.json();
    if (d.ok) { _setMsg('tunnel-msg', '✓ Saved', true); loadSecurity(); }
    else      { _setMsg('tunnel-msg', d.error || 'Failed', false); }
  } catch(e) { _setMsg('tunnel-msg', 'Request failed', false); }
}

async function saveUsername() {
  const u = document.getElementById('sec_username').value.trim();
  if (!u) { _setMsg('sec-msg', 'User Name cannot be empty', false); return; }
  try {
    const r = await fetch('/api/settings/security/username', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username: u})
    });
    const d = await r.json();
    if (d.ok) { _setMsg('sec-msg', '✓ User Name updated', true); loadSecurity(); }
    else      { _setMsg('sec-msg', d.error || 'Failed', false); }
  } catch(e) { _setMsg('sec-msg', 'Request failed', false); }
}

function openPwModal() {
  document.getElementById('pw_new').value  = '';
  document.getElementById('pw_new2').value = '';
  const m = document.getElementById('pw-msg');
  if (m) { m.textContent = ''; m.className = 'settings-msg'; }
  document.getElementById('pw-modal').style.display = 'flex';
  setTimeout(() => document.getElementById('pw_new').focus(), 50);
}

function closePwModal() {
  document.getElementById('pw-modal').style.display = 'none';
}

async function savePassword() {
  const p  = document.getElementById('pw_new').value;
  const p2 = document.getElementById('pw_new2').value;
  if (p !== p2)     { _setMsg('pw-msg', 'Passwords do not match', false); return; }
  if (p.length < 8) { _setMsg('pw-msg', 'Password must be at least 8 characters', false); return; }
  try {
    const r = await fetch('/api/settings/security/password', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({new_password: p})
    });
    const d = await r.json();
    if (d.ok) {
      closePwModal();
      _setMsg('sec-msg', '✓ Password updated', true);
      loadSecurity();
    } else {
      _setMsg('pw-msg', d.error || 'Failed', false);
    }
  } catch(e) { _setMsg('pw-msg', 'Request failed', false); }
}

async function toggleWebAccess() {
  const cb = document.getElementById('web_access');
  const enabled = cb.checked;
  cb.disabled = true;
  try {
    const r = await fetch('/api/settings/web-access', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enabled})
    });
    const d = await r.json();
    if (!d.ok) {
      cb.checked = !enabled;
      _setMsg('sec-msg', d.error || 'Failed', false);
    } else {
      _renderTunnel(d);
      if (enabled && !d.tunnel_url) { setTimeout(loadSecurity, 2500); setTimeout(loadSecurity, 6000); }
    }
  } catch(e) {
    cb.checked = !enabled;
    _setMsg('sec-msg', 'Request failed', false);
  } finally {
    cb.disabled = false;
  }
}

// ── Notification settings ─────────────────────────────────────────────────
function _setMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'settings-msg ' + (ok ? 'ok' : 'err');
  setTimeout(() => { el.textContent = ''; el.className = 'settings-msg'; }, 5000);
}

// Notification settings auto-save on change — there is no manual Save button.

async function loadSettings() {
  try {
    const r = await fetch('/api/settings/notifications');
    const d = await r.json();
    const fields = ['email_smtp_host','email_smtp_port','email_smtp_user',
                    'email_from','email_to','ntfy_url','ntfy_topic'];
    fields.forEach(f => { if (document.getElementById(f)) document.getElementById(f).value = d[f] || ''; });
    // passwords — only set placeholder if saved, never expose value
    ['email_smtp_pass','ntfy_token'].forEach(f => {
      const el = document.getElementById(f);
      if (el) { el.value = ''; el.placeholder = d[f + '_saved'] ? '(saved)' : ''; }
    });
    document.getElementById('email_enabled').checked = d.email_enabled === '1';
    document.getElementById('ntfy_enabled').checked  = d.ntfy_enabled  === '1';
    ['backup_pump_ran','main_pump_ran','high_water','device_offline','update_available','update_installed'].forEach(ev => {
      const el = document.getElementById('trigger_' + ev);
      if (el) el.checked = d['trigger_' + ev] !== '0';
    });
    // Auto-save: persist on change for each notification field (bound once).
    const autoSaveIds = ['email_enabled','email_smtp_host','email_smtp_port',
      'email_smtp_user','email_smtp_pass','email_from','email_to',
      'ntfy_enabled','ntfy_url','ntfy_topic','ntfy_token',
      'trigger_backup_pump_ran','trigger_main_pump_ran','trigger_high_water',
      'trigger_device_offline','trigger_update_available','trigger_update_installed'];
    autoSaveIds.forEach(id => {
      const el = document.getElementById(id);
      if (el && !el.dataset.autosave) {
        el.dataset.autosave = '1';
        el.addEventListener('change', () => saveSettings());
      }
    });
  } catch(e) { console.error('Failed to load settings', e); }
}

async function saveSettings() {
  const data = {
    email_enabled:   document.getElementById('email_enabled').checked  ? '1' : '0',
    ntfy_enabled:    document.getElementById('ntfy_enabled').checked    ? '1' : '0',
    email_smtp_host: document.getElementById('email_smtp_host').value,
    email_smtp_port: document.getElementById('email_smtp_port').value,
    email_smtp_user: document.getElementById('email_smtp_user').value,
    email_from:      document.getElementById('email_from').value,
    email_to:        document.getElementById('email_to').value,
    ntfy_url:        document.getElementById('ntfy_url').value,
    ntfy_topic:      document.getElementById('ntfy_topic').value,
  };
  // Only send passwords if user typed something new
  const ep = document.getElementById('email_smtp_pass').value;
  const nt = document.getElementById('ntfy_token').value;
  if (ep) data.email_smtp_pass = ep;
  if (nt) data.ntfy_token = nt;
  ['backup_pump_ran','main_pump_ran','high_water','device_offline','update_available','update_installed'].forEach(ev => {
    data['trigger_' + ev] = document.getElementById('trigger_' + ev).checked ? '1' : '0';
  });
  try {
    const r = await fetch('/api/settings/notifications', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
    });
    const d = await r.json();
    _setMsg('save-msg', d.ok ? '✓ Saved' : ('Error: ' + d.error), d.ok);
  } catch(e) { _setMsg('save-msg', 'Save failed', false); }
}

// ── Updates ───────────────────────────────────────────────────────────────────
let _updatePoller = null;

async function loadUpdateInfo() {
  try {
    const r = await fetch('/api/update');
    const d = await r.json();
    document.getElementById('current-version').textContent = d.current_version || 'unknown';
    document.getElementById('auto_update').checked = d.auto_update !== false;
    if (d.latest) _showLatest(d.latest, d.update_available);
    else document.getElementById('latest-version').textContent = '—';
    // Show last update result if recently completed
    if (d.last_update && d.last_update.ok) {
      const ago = fmtAgo(d.last_update.ts);
      const prog = document.getElementById('update-progress');
      prog.style.display = '';
      prog.style.color = 'var(--green)';
      prog.textContent = '✓ Successfully updated to ' + d.last_update.tag + ' · ' + ago;
    }
  } catch(e) {}
}

// Minimal, XSS-safe markdown renderer for GitHub release bodies.
function _renderNotes(md) {
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const inline = s => esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="background:var(--card);padding:1px 4px;border-radius:3px">$1</code>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener" style="color:var(--blue)">$1</a>');
  const out = []; let inList = false;
  md.split(/\\r?\\n/).forEach(line => {
    const t = line.trim();
    const li = t.match(/^[-*]\s+(.*)$/);
    const h  = t.match(/^(#{1,6})\s+(.*)$/);
    if (li) {
      if (!inList) { out.push('<ul style="margin:4px 0 4px 18px;padding:0">'); inList = true; }
      out.push('<li style="margin:2px 0">' + inline(li[1]) + '</li>');
      return;
    }
    if (inList) { out.push('</ul>'); inList = false; }
    if (h)            out.push('<div style="font-weight:700;margin:8px 0 2px">' + inline(h[2]) + '</div>');
    else if (t === '') out.push('<div style="height:6px"></div>');
    else               out.push('<div>' + inline(t) + '</div>');
  });
  if (inList) out.push('</ul>');
  return out.join('');
}

function _showLatest(latest, available) {
  document.getElementById('latest-version').textContent = latest.tag || '—';
  document.getElementById('latest-version').style.color = available ? 'var(--green)' : 'var(--muted)';

  const box   = document.getElementById('release-notes-box');
  const title = document.getElementById('release-notes-title');
  const body  = document.getElementById('release-notes');
  if (available) {
    // Preview what the pending update contains.
    title.textContent = "What's new in " + (latest.tag || 'the next version');
    title.style.color = 'var(--green)';
    body.innerHTML = latest.notes
      ? _renderNotes(latest.notes)
      : '<span style="color:var(--muted)">No release notes were provided for this version.</span>';
    box.style.display = '';
  } else if (latest.notes) {
    title.textContent = 'Release notes' + (latest.tag ? ' · ' + latest.tag : '');
    title.style.color = 'var(--muted)';
    body.innerHTML = _renderNotes(latest.notes);
    box.style.display = '';
  } else {
    box.style.display = 'none';
  }

  const row = document.getElementById('update-status-row');
  if (available) {
    row.style.display = 'flex';
    document.getElementById('apply-update-btn').textContent = 'Apply Update ' + latest.tag;
    _setMsg('update-status-msg', '', true);
  } else {
    row.style.display = 'none';
  }
}

async function checkForUpdates() {
  const btn = document.getElementById('check-update-btn');
  btn.disabled = true;
  document.getElementById('latest-version').textContent = 'checking…';
  document.getElementById('latest-version').style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/update/check');
    const d = await r.json();
    if (d.latest) _showLatest(d.latest, d.update_available);
    else document.getElementById('latest-version').textContent = 'unavailable';
  } catch(e) {
    document.getElementById('latest-version').textContent = 'error';
  } finally {
    btn.disabled = false;
  }
}

async function saveAutoUpdate() {
  const enabled = document.getElementById('auto_update').checked;
  try {
    await fetch('/api/update/auto', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled})
    });
  } catch(e) {}
}

const _PHASE_LABEL = {
  starting:   '◷ Starting update…',
  downloading:'⬇ Downloading update files…',
  installing: '⚙ Installing new version…',
  restarting: '↺ Restarting services — dashboard will reload shortly…',
};

function _finishUpdate(prog, btn, ok, version) {
  if (_updatePoller) { clearInterval(_updatePoller); _updatePoller = null; }
  prog.style.display = '';
  prog.style.color = ok ? 'var(--green)' : 'var(--red)';
  prog.textContent = ok
    ? '✓ Update complete' + (version ? ' — now on ' + version : '') + '. Reloading…'
    : '✗ Update failed';
  if (ok) {
    // The dashboard has (or is about to) restart on the new code — reload so
    // the page reflects the new version once it's back up.
    setTimeout(() => window.location.reload(), 4000);
  } else {
    btn.disabled = false;
  }
}

async function applyUpdate() {
  if (!confirm('Apply the update now? Services will restart briefly.')) return;
  const btn = document.getElementById('apply-update-btn');
  btn.disabled = true;
  _setMsg('update-status-msg', '', true);
  const prog = document.getElementById('update-progress');
  prog.style.display = '';
  prog.style.color = 'var(--yellow)';
  prog.textContent = 'Starting update…';

  try {
    const r = await fetch('/api/update/apply', { method: 'POST' });
    const d = await r.json();
    if (!d.ok) {
      _setMsg('update-status-msg', '✗ ' + (d.message || 'Failed to start update'), false);
      prog.style.display = 'none';
      btn.disabled = false;
      return;
    }
  } catch(e) {
    _setMsg('update-status-msg', '✗ Failed to start update', false);
    prog.style.display = 'none';
    btn.disabled = false;
    return;
  }

  // Status now lives in a disk-backed state file, so it survives the dashboard
  // restart. We poll it; "done"/"error" are terminal. While the dashboard is
  // restarting the fetch fails transiently — we keep polling until it returns.
  let _sawRunning = false;
  if (_updatePoller) clearInterval(_updatePoller);
  _updatePoller = setInterval(async () => {
    try {
      const r = await fetch('/api/update/status');
      const s = await r.json();

      if (['starting','downloading','installing','restarting'].includes(s.phase)) {
        _sawRunning = true;
        prog.style.color = 'var(--yellow)';
        prog.textContent = _PHASE_LABEL[s.phase] || ('… ' + s.phase);
      } else if (s.phase === 'done') {
        _finishUpdate(prog, btn, true, s.version);
      } else if (s.phase === 'error') {
        if (_updatePoller) { clearInterval(_updatePoller); _updatePoller = null; }
        prog.style.display = 'none';
        _setMsg('update-status-msg', '✗ ' + (s.error || 'Update failed'), false);
        btn.disabled = false;
      }
      // phase === 'idle' before we ever saw it run: keep waiting for the
      // detached worker to seed the state. Do nothing.
    } catch(e) {
      // Dashboard is restarting — surface that and keep polling; the state
      // file will still say "done" once it's back.
      if (_sawRunning) prog.textContent = '↺ Dashboard restarting…';
    }
  }, 1000);
}

async function sendTest(channel) {
  const msgId = channel + '-test-msg';
  // Persist the latest edits before the server sends a test.
  await saveSettings();
  document.getElementById(msgId).textContent = 'Sending…';
  try {
    const r = await fetch('/api/settings/notifications/test', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({channel})
    });
    const d = await r.json();
    _setMsg(msgId, d.ok ? '✓ Sent!' : ('✗ ' + d.error), d.ok);
  } catch(e) { _setMsg(msgId, '✗ Request failed', false); }
}
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Mobile layout — a separate, phone-optimised design that REUSES the desktop
# JavaScript and the functional widget markup verbatim, so behaviour can never
# drift between the two. Only the page chrome (header / nav) and CSS differ.
# ---------------------------------------------------------------------------
import re as _re


def _slice_between(text: str, start: str, end: str) -> str:
    """Return the substring of `text` from `start` up to (not including) `end`."""
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


# The inline <script> block (everything except the Chart.js CDN <script src=…>).
_script_match = _re.search(r"<script>\n(.*)\n</script>\n</body>\n</html>", TEMPLATE, _re.S)
SHARED_SCRIPT = _script_match.group(1) if _script_match else ""

# Functional fragments lifted straight from the desktop template.
_AUTH_BANNER    = _slice_between(TEMPLATE, "<!-- Auth failure banner -->",                 "<!-- /auth banner -->")
_STAT_CARDS     = _slice_between(TEMPLATE, "<!-- Stat cards -->",                           "<!-- /stat cards -->")
_PUMP_WIDGET    = _slice_between(TEMPLATE, "<!-- Pump run history -->",                     "<!-- /pump widget -->")
_RSSI_WIDGET    = _slice_between(TEMPLATE, "<!-- RSSI chart -->",                           "<!-- /rssi widget -->")
_UNKNOWN_WIDGET = _slice_between(TEMPLATE, "<!-- Unhandled requests (collapsed by default) -->", "<!-- /unknown widget -->")
_DEBUG_ACTIONS  = _slice_between(TEMPLATE, "<!-- Debug actions -->",                        "<!-- /debug actions -->")
_SETTINGS_INNER = _slice_between(TEMPLATE, '<div class="settings-grid">',                   "</div><!-- /settings grid -->")

MOBILE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1117">
<title>PumpSleeper</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #8892a4; --green: #22c55e;
    --red: #ef4444; --yellow: #f59e0b; --blue: #3b82f6; --purple: #a855f7;
    --nav-h: 60px; --bar-bg: rgba(15,17,23,0.94);
  }
  /* Light palette — explicit light, or auto + OS light preference */
  :root[data-theme="light"] {
    --bg: #f4f6fa; --card: #ffffff; --border: #d9dee8;
    --text: #1d2430; --muted: #5b6675; --green: #16a34a;
    --red: #dc2626; --yellow: #d97706; --blue: #2563eb; --purple: #9333ea;
    --bar-bg: rgba(244,246,250,0.94);
  }
  @media (prefers-color-scheme: light) {
    :root[data-theme="auto"] {
      --bg: #f4f6fa; --card: #ffffff; --border: #d9dee8;
      --text: #1d2430; --muted: #5b6675; --green: #16a34a;
      --red: #dc2626; --yellow: #d97706; --blue: #2563eb; --purple: #9333ea;
      --bar-bg: rgba(244,246,250,0.94);
    }
  }
  .theme-btn.active { border-color: var(--blue); color: var(--blue); }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
         font-size: 15px; padding-bottom: calc(var(--nav-h) + env(safe-area-inset-bottom)); }

  /* ── Sticky top bar ─────────────────────────────────────────── */
  header { position: sticky; top: 0; z-index: 20; background: var(--bar-bg);
           backdrop-filter: blur(8px); border-bottom: 1px solid var(--border);
           padding: 12px 16px calc(12px + env(safe-area-inset-top)); }
  .topline { display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; letter-spacing: 0.5px; }
  header h1 span { color: var(--blue); }
  #refresh-info { font-size: 11px; color: var(--muted); }
  .mode-toggle { display: flex; gap: 8px; margin-top: 10px; }
  .mode-btn { flex: 1; padding: 9px 0; border-radius: 8px; border: 1px solid var(--border);
              font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .mode-btn.active-proxy    { background: rgba(34,197,94,0.15);  color: var(--green); border-color: var(--green); }
  .mode-btn.active-takeover { background: rgba(239,68,68,0.15);  color: var(--red);   border-color: var(--red); }
  .mode-btn.inactive { background: transparent; color: var(--muted); }

  /* ── Generic cards / layout ─────────────────────────────────── */
  .grid { display: grid; gap: 12px; padding: 14px 14px 0; }
  .stats { grid-template-columns: 1fr 1fr; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; }
  .stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.7px; color: var(--muted); margin-bottom: 6px; }
  .stat-value { font-size: 24px; font-weight: 700; line-height: 1.15; }
  .stat-sub { font-size: 11px; color: var(--muted); margin-top: 4px; word-break: break-word; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  .dot.online { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.offline { background: var(--red); }
  .dot.pending { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted);
                   text-transform: uppercase; letter-spacing: 0.6px; }
  .chart-wrap { position: relative; height: 200px; }

  /* The "Cycle Hotspot" button — make it a comfortable tap target. */
  .cycle-btn { font-size: 13px; color: var(--muted); background: transparent;
               border: 1px solid var(--border); border-radius: 8px; padding: 9px 14px;
               cursor: pointer; width: 100%; }
  .cycle-btn:disabled { opacity: 0.4; }
  .cycle-progress { font-size: 12px; color: var(--yellow); }
  .cycle-done  { font-size: 12px; color: var(--green); }
  .cycle-error { font-size: 12px; color: var(--red); }

  /* ── Badges ─────────────────────────────────────────────────── */
  .badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 11px; font-weight: 600; }
  .badge.running  { background: rgba(34,197,94,0.15);  color: var(--green); }
  .badge.stopped  { background: rgba(59,130,246,0.15);  color: var(--blue); }
  .badge.fault    { background: rgba(239,68,68,0.15);   color: var(--red); }
  .badge.cleared  { background: rgba(34,197,94,0.15);   color: var(--green); }
  .badge.unknown  { background: rgba(168,85,247,0.15);  color: var(--purple); }
  .badge.main     { background: rgba(59,130,246,0.12);  color: var(--blue); }
  .badge.backup   { background: rgba(245,158,11,0.15);  color: var(--yellow); }
  .empty { color: var(--muted); font-style: italic; padding: 14px 4px; text-align: center; }

  /* ── Collapsible widgets ────────────────────────────────────── */
  .widget-header { display:flex; align-items:center; justify-content:space-between;
                   cursor:pointer; user-select:none; margin-bottom:12px; }
  .collapse-btn { font-size:13px; color:var(--muted); padding:4px 8px;
                  border:1px solid var(--border); border-radius:6px;
                  background:transparent; transition:transform 0.2s; }
  .collapsible-content { overflow:hidden; }
  .collapsed .collapsible-content { display:none; }
  .collapsed .collapse-btn { transform:rotate(-90deg); }

  /* ── Auth banner ────────────────────────────────────────────── */
  .auth-banner { display:none; flex-direction:column; gap:10px; margin:14px 14px 0;
                 padding:12px 14px; background:rgba(245,158,11,0.12);
                 border:1px solid rgba(245,158,11,0.4); border-radius:12px; font-size:13px; }
  .auth-banner.visible { display:flex; }
  .auth-banner-msg { color: var(--yellow); }
  .auth-banner-msg strong { font-weight:700; }
  .auth-takeover-btn { padding:10px; border-radius:8px; border:1px solid var(--red);
                       background:rgba(239,68,68,0.15); color:var(--red);
                       font-size:13px; font-weight:700; cursor:pointer; width:100%; }

  /* ── Filter bar ─────────────────────────────────────────────── */
  .filter-bar { display:grid; grid-template-columns:auto 1fr; align-items:center; gap:8px 10px; margin-bottom:14px; }
  .filter-bar label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }
  .filter-input { background:var(--bg); border:1px solid var(--border); border-radius:8px;
                  color:var(--text); font-size:16px; padding:9px 10px; outline:none; width:100%; }
  .filter-input:focus { border-color:var(--blue); }
  .filter-input option { background:var(--card); }
  .filter-clear { grid-column:1 / -1; font-size:13px; color:var(--muted); cursor:pointer; padding:9px;
                  border:1px solid var(--border); border-radius:8px; background:transparent; }
  .filter-count { grid-column:1 / -1; font-size:11px; color:var(--muted); text-align:right; }

  /* ── Pump run history → compact real table on phones ─────────── */
  #widget-pump .scroll-table { overflow:auto; -webkit-overflow-scrolling:touch; }
  #pump-table { font-size:13px; }
  #pump-table th, #pump-table td { padding:7px 8px; white-space:nowrap; }
  .scroll-table { overflow:auto; }

  /* ── Unhandled requests: keep a real (scrollable) table ──────── */
  #widget-unknown .scroll-table { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  #unknown-table { min-width:520px; border-collapse:collapse; font-size:13px; }
  #unknown-table th { text-align:left; padding:8px 10px; color:var(--muted); font-weight:500;
                      font-size:11px; text-transform:uppercase; border-bottom:1px solid var(--border); white-space:nowrap; }
  #unknown-table td { padding:8px 10px; border-bottom:1px solid var(--border); }
  .body-preview { font-family:monospace; font-size:11px; color:var(--muted);
                  max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .copy-btn { background:rgba(59,130,246,0.15); color:var(--blue); border:none;
              border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer; white-space:nowrap; }
  .copy-btn.copied { background:rgba(34,197,94,0.15); color:var(--green); }
  .body-expand td { font-family:monospace; font-size:12px; white-space:pre-wrap; word-break:break-all; }

  /* ── Settings (single column, big touch targets) ────────────── */
  .settings-grid { display:flex; flex-direction:column; gap:14px; padding:14px; }
  .settings-grid .full-width { width:100%; }
  .two-row-grid { display:grid; grid-template-columns:1fr; gap:12px; }
  .form-row { display:flex; flex-direction:column; gap:5px; }
  .form-row label { font-size:12px; color:var(--muted); }
  .form-input { background:var(--bg); border:1px solid var(--border); border-radius:8px;
                color:var(--text); font-size:16px; padding:10px 12px; outline:none; width:100%; }
  .form-input:focus { border-color:var(--blue); }
  .toggle-label { display:flex; align-items:center; gap:10px; cursor:pointer; font-size:14px; padding:4px 0; }
  .toggle-label input[type=checkbox] { width:20px; height:20px; accent-color:var(--blue); flex:0 0 auto; }
  .save-btn { padding:12px 22px; border-radius:8px; border:none; background:var(--blue);
              color:#fff; font-size:15px; font-weight:600; cursor:pointer; width:100%; }
  .test-btn { padding:10px 14px; border-radius:8px; border:1px solid var(--border);
              background:transparent; color:var(--muted); font-size:13px; cursor:pointer; }
  .settings-section { font-size:11px; text-transform:uppercase; letter-spacing:0.8px; color:var(--muted); }
  .settings-msg { font-size:12px; margin-top:6px; min-height:18px; display:block; }
  .settings-msg.ok  { color:var(--green); }
  .settings-msg.err { color:var(--red); }

  /* ── Tab panels + bottom nav ────────────────────────────────── */
  .tab-panel { display:none; }
  .tab-panel.active { display:block; padding-bottom:18px; }
  /* Sub-tabs within the Dashboard page (underline style) */
  .subtab-nav { display:flex; gap:24px; padding:14px 16px 0; margin-bottom:-1px;
                border-bottom:1px solid var(--border); }
  .subtab-btn { padding:8px 2px; font-size:14px; font-weight:600; color:var(--muted);
                background:transparent; border:none; border-bottom:2px solid transparent;
                cursor:pointer; margin-bottom:-1px; }
  .subtab-btn.active { color:var(--blue); border-bottom-color:var(--blue); }
  .subtab-panel { display:none; }
  .subtab-panel.active { display:block; }
  /* Portrait phones: show only Run Date, Pump, Duration, Est. Gallons */
  @media (orientation: portrait) {
    #pump-table .col-extra { display:none; }
  }
  .bottom-nav { position:fixed; bottom:0; left:0; right:0; z-index:30; display:flex;
                height:var(--nav-h); padding-bottom:env(safe-area-inset-bottom);
                background:var(--bar-bg); backdrop-filter:blur(8px);
                border-top:1px solid var(--border); }
  .tab-btn { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center;
             gap:3px; background:transparent; border:none; color:var(--muted);
             font-size:12px; font-weight:500; cursor:pointer; }
  .tab-btn .nav-icon { font-size:19px; line-height:1; }
  .tab-btn.active { color:var(--blue); }

  .desktop-link { display:block; text-align:center; color:var(--muted); font-size:12px;
                  padding:16px; text-decoration:none; }
  .desktop-link:active { color:var(--text); }
</style>
</head>
<body class="mobile">
<header>
  <div class="topline">
    <h1>Pump<span>Sleeper</span></h1>
    <span id="refresh-info">Loading…</span>
  </div>
  <div class="mode-toggle">
    <button class="mode-btn inactive" id="btn-proxy"    onclick="setMode('proxy')">Proxy</button>
    <button class="mode-btn inactive" id="btn-takeover" onclick="setMode('takeover')">Takeover</button>
  </div>
</header>

<div id="tab-dashboard" class="tab-panel active">
""" + _AUTH_BANNER + _STAT_CARDS + """
<div class="subtab-nav">
  <button class="subtab-btn active" onclick="showSubTab('pump')">Pump Run History</button>
  <button class="subtab-btn" onclick="showSubTab('signal')">Signal Strength</button>
</div>
<div id="subtab-pump" class="subtab-panel active">
""" + _PUMP_WIDGET + """</div>
<div id="subtab-signal" class="subtab-panel">
""" + _RSSI_WIDGET + """</div>
</div><!-- end tab-dashboard -->

<div id="tab-settings" class="tab-panel">
""" + _SETTINGS_INNER + """</div>
<div class="grid" style="padding-top:0"><div class="section-title" style="margin-bottom:0">Debug</div></div>
""" + _DEBUG_ACTIONS + _UNKNOWN_WIDGET + """
</div><!-- end tab-settings -->

<a class="desktop-link" href="/?desktop=1">View desktop site →</a>

<nav class="bottom-nav">
  <button class="tab-btn active" onclick="showTab('dashboard')"><span class="nav-icon">▦</span>Dashboard</button>
  <button class="tab-btn" onclick="showTab('settings')"><span class="nav-icon">⚙</span>Settings</button>
</nav>

<script>
""" + SHARED_SCRIPT + """
</script>
</body>
</html>"""


# Phones (not tablets) get the mobile layout by default.
_MOBILE_UA_RE = _re.compile(
    r"iPhone|iPod|Android.*Mobile|Windows Phone|IEMobile|BlackBerry|BB10|Opera Mini|Mobi",
    _re.IGNORECASE,
)


def _wants_mobile(req) -> bool:
    """Decide whether to serve the mobile layout for this request."""
    forced = req.args.get("desktop")
    if forced == "1":
        return False          # explicit desktop override
    if forced == "0":
        return True           # explicit mobile override
    cookie = req.cookies.get("view")
    if cookie in ("mobile", "desktop"):
        return cookie == "mobile"
    return bool(_MOBILE_UA_RE.search(req.headers.get("User-Agent", "")))


# ---------------------------------------------------------------------------
# Hotspot cycle — runs nmcli in a background thread, UI polls for status
# ---------------------------------------------------------------------------
_cycle_lock = threading.Lock()
_cycle_state = {
    "running": False, "phase": "idle",
    "started_at": None, "error": None,
    "wifi_reachable": None,   # True/False/None after checking phase
}
HOTSPOT_DOWN_SECS  = 30
CHECK_TIMEOUT_SECS = 90   # how long to wait for device to come back
CHECK_INTERVAL     = 3    # seconds between checks


def _ping_ok(ip: str) -> bool:
    """Return True if the device responds to a single ping."""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", "-q", ip],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _has_new_events(since_ts: str) -> bool:
    """Return True if any device event arrived after since_ts."""
    try:
        from db import _connect
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE ts > ? AND kind != 'unknown' LIMIT 1",
                (since_ts,)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _do_hotspot_cycle():
    import time
    global _cycle_state
    started_at = _cycle_state["started_at"]
    try:
        # ── 1. Bring hotspot down ──────────────────────────────────────────
        _cycle_state.update({"phase": "down", "error": None, "wifi_reachable": None})
        r = subprocess.run(["sudo", "/usr/bin/nmcli", "con", "down", HOTSPOT_CON],
                           capture_output=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError(
                "nmcli con down failed: " + (r.stderr.decode().strip() or r.stdout.decode().strip() or f"exit {r.returncode}")
            )

        # ── 2. Wait ────────────────────────────────────────────────────────
        for _ in range(HOTSPOT_DOWN_SECS):
            time.sleep(1)

        # ── 3. Bring hotspot back up ───────────────────────────────────────
        _cycle_state["phase"] = "up"
        r = subprocess.run(["sudo", "/usr/bin/nmcli", "con", "up", HOTSPOT_CON],
                           capture_output=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(
                "nmcli con up failed: " + (r.stderr.decode().strip() or r.stdout.decode().strip() or f"exit {r.returncode}")
            )

        # ── 4. Check whether the device comes back ─────────────────────────
        _cycle_state["phase"] = "checking"
        device_ip = get_device_ip()
        deadline  = time.time() + CHECK_TIMEOUT_SECS
        wifi_ok   = False

        while time.time() < deadline:
            time.sleep(CHECK_INTERVAL)

            # Priority 1: HTTP events arriving → fully online
            if _has_new_events(started_at):
                _cycle_state.update({"phase": "online", "wifi_reachable": True})
                return

            # Priority 2: ping responds → WiFi layer OK, app layer not yet
            if device_ip and _ping_ok(device_ip):
                wifi_ok = True
                _cycle_state["wifi_reachable"] = True
            else:
                _cycle_state["wifi_reachable"] = wifi_ok  # keep True once seen

        # Timed out — report what we found
        if wifi_ok:
            _cycle_state["phase"] = "wifi_only"   # on hotspot, IP works, no HTTP
        else:
            _cycle_state["phase"] = "unreachable"  # not reachable at all

    except Exception as exc:
        _cycle_state["error"] = str(exc)
        _cycle_state["phase"] = "error"
    finally:
        _cycle_state["running"] = False


@app.route("/api/hotspot/cycle", methods=["POST"])
def api_hotspot_cycle():
    with _cycle_lock:
        if _cycle_state["running"]:
            return jsonify({"error": "cycle already in progress", "state": _cycle_state}), 409
        _cycle_state.update({
            "running": True,
            "phase": "starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        })
    threading.Thread(target=_do_hotspot_cycle, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/hotspot/cycle", methods=["GET"])
def api_hotspot_cycle_status():
    state = dict(_cycle_state)
    # Include elapsed seconds so the UI can drive the countdown
    if state["started_at"]:
        try:
            elapsed = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(state["started_at"])).total_seconds()
            state["elapsed"] = round(elapsed)
        except Exception:
            state["elapsed"] = 0
    return jsonify(state)


@app.route("/api/mode", methods=["GET"])
def api_mode_get():
    try:
        r = rlib.get(f"{SERVER_URL}/api/mode", timeout=3)
        return jsonify(r.json())
    except Exception:
        return jsonify({"mode": "unknown", "error": "server unreachable"}), 502

@app.route("/api/mode", methods=["POST"])
def api_mode_set():
    try:
        body = request.get_json(force=True, silent=True) or {}
        r = rlib.post(f"{SERVER_URL}/api/mode", json=body, timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({"error": "server unreachable"}), 502

@app.route("/api/update", methods=["GET"])
def api_update_info():
    import json as _json
    from updater import get_current_version, get_latest_release, get_auto_update
    current  = get_current_version()
    latest   = get_latest_release()
    avail    = (latest is not None) and (latest["tag"] != current) and (current != "unknown")
    # Read last update result from disk (persists across restarts)
    last_update = None
    try:
        result_file = os.path.join(
            os.environ.get("PUMPSPY_INSTALL_DIR", "/opt/pumpsleeper"), "data", "last_update.json"
        )
        with open(result_file) as f:
            last_update = _json.load(f)
    except Exception:
        pass
    return jsonify({
        "current_version":  current,
        "auto_update":      get_auto_update(),
        "latest":           latest,
        "update_available": avail,
        "last_update":      last_update,
    })

@app.route("/api/update/check", methods=["GET"])
def api_update_check():
    from updater import get_current_version, get_latest_release
    current = get_current_version()
    latest  = get_latest_release()
    avail   = (latest is not None) and (latest["tag"] != current) and (current != "unknown")
    return jsonify({"latest": latest, "update_available": avail})

@app.route("/api/update/auto", methods=["POST"])
def api_update_auto():
    from updater import set_auto_update
    data = request.get_json(force=True, silent=True) or {}
    set_auto_update(bool(data.get("enabled", True)))
    return jsonify({"ok": True})

@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    from updater import trigger_update
    ok, msg = trigger_update()
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/update/status", methods=["GET"])
def api_update_status():
    from updater import get_update_state
    return jsonify(get_update_state())

@app.route("/api/settings/theme", methods=["GET"])
def api_theme_get():
    from db import get_ui_theme
    layout = "mobile" if _wants_mobile(request) else "desktop"
    return jsonify({"theme": get_ui_theme(layout), "layout": layout})

@app.route("/api/settings/theme", methods=["POST"])
def api_theme_set():
    from db import set_ui_theme
    data   = request.get_json(force=True, silent=True) or {}
    theme  = data.get("theme", "auto")
    layout = "mobile" if _wants_mobile(request) else "desktop"
    try:
        set_ui_theme(theme, layout)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "theme": theme, "layout": layout})

@app.route("/api/settings/notifications", methods=["GET"])
def api_settings_get():
    from notifications import get_settings
    cfg = get_settings()
    # Never expose raw passwords — just signal whether they're saved
    cfg["email_smtp_pass_saved"] = bool(cfg.get("email_smtp_pass"))
    cfg["ntfy_token_saved"]      = bool(cfg.get("ntfy_token"))
    cfg.pop("email_smtp_pass", None)
    cfg.pop("ntfy_token", None)
    return jsonify(cfg)

@app.route("/api/settings/notifications", methods=["POST"])
def api_settings_save():
    from notifications import save_settings
    data = request.get_json(force=True, silent=True) or {}
    try:
        save_settings(data)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/settings/notifications/test", methods=["POST"])
def api_settings_test():
    from notifications import send_test
    data    = request.get_json(force=True, silent=True) or {}
    channel = data.get("channel", "")
    ok, msg = send_test(channel)
    return jsonify({"ok": ok, "error": msg if not ok else None})

@app.route("/api/debug/log")
def api_debug_log():
    """Export recent service logs as a downloadable text file for troubleshooting."""
    units = ["pumpsleeper", "pumpsleeper-dashboard"]
    cmd = ["journalctl"]
    for u in units:
        cmd += ["-u", u]
    cmd += ["-n", "2000", "--no-pager", "-o", "short-iso"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        body = proc.stdout or ""
        if not body.strip():
            body = ("(journalctl returned no entries. The dashboard service user may not "
                    "have permission to read the system journal — add it to the "
                    "'systemd-journal' group to enable full logs.)\n\nstderr:\n"
                    + (proc.stderr or ""))
    except FileNotFoundError:
        body = "journalctl is not available on this host."
    except subprocess.TimeoutExpired:
        body = "Timed out while collecting logs."
    except Exception as exc:
        body = f"Failed to collect logs: {exc}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"pumpsleeper-log-{stamp}.txt"
    head  = ("PumpSleeper service log export\n"
             f"Generated: {stamp}\n"
             f"Units: {', '.join(units)} (last 2000 lines)\n"
             + "=" * 60 + "\n\n")
    resp = make_response(head + body)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


@app.route("/api/debug/capture", methods=["GET"])
def api_debug_capture_get():
    """Report whether raw pump-traffic capture is on, and how much is recorded."""
    from db import is_capture_enabled, CAPTURE_FILE
    try:
        size = os.path.getsize(CAPTURE_FILE)
    except OSError:
        size = 0
    return jsonify({"enabled": is_capture_enabled(), "bytes": size})

@app.route("/api/debug/capture", methods=["POST"])
def api_debug_capture_set():
    """Start/stop capture. Starting truncates the log to a fresh session; the
    server process (port 8081) does the actual per-request writing."""
    from db import set_capture_enabled, CAPTURE_FILE
    data    = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled"))
    if enabled:
        # Begin a fresh capture: write a header, THEN flip the flag on so the
        # server only starts appending after the file is reset.
        try:
            with open(CAPTURE_FILE, "w", encoding="utf-8") as fh:
                fh.write("PumpSleeper raw pump-traffic capture\n"
                         f"Started: {datetime.now().isoformat()}\n"
                         "Every request the pump makes is recorded below — including\n"
                         "endpoints PumpSleeper doesn't normally handle.\n"
                         + "=" * 72 + "\n\n")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        set_capture_enabled(True)
    else:
        set_capture_enabled(False)
    return jsonify({"ok": True, "enabled": enabled})

@app.route("/api/debug/capture/download")
def api_debug_capture_download():
    """Serve the captured traffic log as a downloadable text file, then clear it
    so the next capture session starts from a fresh, empty log."""
    from db import CAPTURE_FILE
    had_file = True
    try:
        with open(CAPTURE_FILE, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError:
        had_file = False
        body = "(No pump traffic was captured.)\n"
    # Clear the log now that it's been handed to the user.
    if had_file:
        try:
            open(CAPTURE_FILE, "w").close()
        except OSError:
            pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"pumpsleeper-pump-capture-{stamp}.txt"
    resp = make_response(body)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp

# ---------------------------------------------------------------------------
# Cloudflare quick-tunnel management
# ---------------------------------------------------------------------------
_tunnel_proc = None
_tunnel_url  = None
_tunnel_lock = threading.Lock()
_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

def _tunnel_running() -> bool:
    return _tunnel_proc is not None and _tunnel_proc.poll() is None

def _notify_url(url):
    try:
        from notifications import notify, EVENT_WEBACCESS_URL
        notify(EVENT_WEBACCESS_URL, f"Your PumpSleeper dashboard is now reachable at:\n{url}")
    except Exception:
        pass

def _read_tunnel_output(proc, known_url=None):
    """Read cloudflared's output. For a quick tunnel (known_url=None) scrape the
    random trycloudflare.com URL and notify on change. For a named tunnel
    (known_url set) the address is already known, so just notify once the tunnel
    registers a connection."""
    global _tunnel_url
    last_notified = None
    named_notified = False
    try:
        for line in proc.stdout:
            if known_url:
                if not named_notified and "registered tunnel connection" in line.lower():
                    named_notified = True
                    _notify_url(known_url)
                continue
            m = _TUNNEL_URL_RE.search(line)
            if m:
                url = m.group(0)
                _tunnel_url = url
                try:
                    from db import _set_setting
                    _set_setting("tunnel_public_url", url)
                except Exception:
                    pass
                if url != last_notified:
                    last_notified = url
                    _notify_url(url)
    except Exception:
        pass

def start_tunnel():
    """Spawn cloudflared for the configured tunnel mode.

    Returns (ok, error). ok=False errors: 'not installed' (cloudflared missing),
    'no token' (named mode without a token), or an exception string.
    """
    global _tunnel_proc, _tunnel_url
    with _tunnel_lock:
        if _tunnel_running():
            return True, None
        cf = shutil.which("cloudflared")
        if not cf:
            return False, "not installed"
        from db import get_tunnel_mode, get_tunnel_token, get_tunnel_hostname, _set_setting
        mode = get_tunnel_mode()
        _tunnel_url = None
        if mode == "named":
            token = get_tunnel_token()
            if not token:
                return False, "no token"
            host   = get_tunnel_hostname()
            public = f"https://{host}" if host else ""
            cmd = [cf, "tunnel", "--no-autoupdate", "run", "--token", token]
            try:
                _tunnel_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except Exception as exc:
                _tunnel_proc = None
                return False, str(exc)
            # The named-tunnel address is fixed and known up front.
            if public:
                _tunnel_url = public
                try:
                    _set_setting("tunnel_public_url", public)
                except Exception:
                    pass
            threading.Thread(target=_read_tunnel_output,
                             args=(_tunnel_proc, public or None), daemon=True).start()
            return True, None
        # quick tunnel (default)
        try:
            _tunnel_proc = subprocess.Popen(
                [cf, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{DASH_PORT}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as exc:
            _tunnel_proc = None
            return False, str(exc)
        threading.Thread(target=_read_tunnel_output, args=(_tunnel_proc, None), daemon=True).start()
        return True, None

def stop_tunnel():
    global _tunnel_proc, _tunnel_url
    with _tunnel_lock:
        if _tunnel_proc is not None:
            try:
                _tunnel_proc.terminate()
                try:
                    _tunnel_proc.wait(timeout=5)
                except Exception:
                    _tunnel_proc.kill()
            except Exception:
                pass
        _tunnel_proc = None
        _tunnel_url = None
        try:
            from db import _set_setting
            _set_setting("tunnel_public_url", "")
        except Exception:
            pass

def _wait_for_tunnel_url(timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _tunnel_url:
            return _tunnel_url
        time.sleep(0.25)
    return _tunnel_url


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------
_login_attempts = {}          # ip -> [fail_count, locked_until_epoch]
LOGIN_MAX_TRIES = 5
LOGIN_LOCK_SECS = 300

LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PumpSleeper — Sign in</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --border:#2a2d3a; --text:#e2e8f0;
          --muted:#8892a4; --blue:#3b82f6; --red:#ef4444; --yellow:#f59e0b; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif;
         min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }
  .login-card { background:var(--card); border:1px solid var(--border); border-radius:14px;
                padding:32px 28px; width:100%; max-width:360px; }
  h1 { font-size:22px; font-weight:600; letter-spacing:0.5px; margin-bottom:4px; }
  h1 span { color:var(--blue); }
  .sub { font-size:13px; color:var(--muted); margin-bottom:22px; }
  label { display:block; font-size:12px; color:var(--muted); margin:14px 0 5px; }
  input { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:8px;
          color:var(--text); font-size:16px; padding:11px 12px; outline:none; }
  input:focus { border-color:var(--blue); }
  button { width:100%; margin-top:22px; padding:12px; border:none; border-radius:8px;
           background:var(--blue); color:#fff; font-size:15px; font-weight:600; cursor:pointer; }
  .err { margin-top:16px; font-size:13px; color:var(--red); }
  .warn { margin-top:16px; font-size:12px; color:var(--yellow); line-height:1.5; }
</style>
</head>
<body>
  <form class="login-card" method="POST">
    <h1>Pump<span>Sleeper</span></h1>
    <div class="sub">Sign in to continue</div>
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username" autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
    <div style="margin-top:14px;text-align:center"><a href="/forgot" style="color:var(--blue);font-size:13px;text-decoration:none">Forgot password?</a></div>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    {% if default_warn %}<div class="warn">&#9888; Still using the default <strong>admin / admin</strong> login. Change it in Settings &rarr; Security after signing in.</div>{% endif %}
  </form>
</body>
</html>"""

def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"

@app.route("/login", methods=["GET", "POST"])
def login():
    from db import verify_password, is_default_credentials
    error = None
    ip  = _client_ip()
    now = time.time()
    rec = _login_attempts.get(ip)
    locked = bool(rec and rec[1] > now)
    if request.method == "POST":
        if locked:
            error = "Too many failed attempts. Try again in a few minutes."
        else:
            u = request.form.get("username", "")
            p = request.form.get("password", "")
            if verify_password(u, p):
                session.clear()
                session.permanent = True          # persist across browser restarts
                session["authed"] = True
                session["user"]   = u
                _login_attempts.pop(ip, None)
                nxt = request.args.get("next") or "/"
                if not nxt.startswith("/"):
                    nxt = "/"
                return redirect(nxt)
            cnt = (rec[0] if rec else 0) + 1
            lock_until = now + LOGIN_LOCK_SECS if cnt >= LOGIN_MAX_TRIES else 0
            _login_attempts[ip] = [cnt, lock_until]
            error = "Invalid username or password."
    elif locked:
        error = "Too many failed attempts. Try again in a few minutes."
    return render_template_string(LOGIN_TEMPLATE, error=error,
                                  default_warn=is_default_credentials())

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Forgot / reset password — the reset link is delivered over the configured
# notification channels (email and/or ntfy). Token is single-use, 30-min TTL.
# ---------------------------------------------------------------------------
_AUTH_CSS = """
  :root { --bg:#0f1117; --card:#1a1d27; --border:#2a2d3a; --text:#e2e8f0;
          --muted:#8892a4; --blue:#3b82f6; --red:#ef4444; --green:#22c55e; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif;
         min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }
  .login-card { background:var(--card); border:1px solid var(--border); border-radius:14px;
                padding:32px 28px; width:100%; max-width:360px; }
  h1 { font-size:22px; font-weight:600; letter-spacing:0.5px; margin-bottom:4px; }
  h1 span { color:var(--blue); }
  .sub { font-size:13px; color:var(--muted); margin-bottom:22px; }
  label { display:block; font-size:12px; color:var(--muted); margin:14px 0 5px; }
  input { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:8px;
          color:var(--text); font-size:16px; padding:11px 12px; outline:none; }
  input:focus { border-color:var(--blue); }
  button { width:100%; margin-top:22px; padding:12px; border:none; border-radius:8px;
           background:var(--blue); color:#fff; font-size:15px; font-weight:600; cursor:pointer; }
  .err { margin-top:16px; font-size:13px; color:var(--red); }
  .ok  { margin-top:16px; font-size:13px; color:var(--green); line-height:1.5; }
  .backlink { margin-top:16px; text-align:center; }
  .backlink a { color:var(--blue); font-size:13px; text-decoration:none; }
"""

FORGOT_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PumpSleeper — Forgot password</title>
<style>""" + _AUTH_CSS + """</style>
</head><body>
  <form class="login-card" method="POST">
    <h1>Pump<span>Sleeper</span></h1>
    <div class="sub">Reset your password</div>
    <p style="font-size:13px;color:var(--muted);line-height:1.6">We'll send a reset link to the notification channels you've configured (email and/or ntfy). The link expires in 30 minutes.</p>
    <button type="submit">Send reset link</button>
    {% if msg %}<div class="ok">{{ msg }}</div>{% endif %}
    {% if err %}<div class="err">{{ err }}</div>{% endif %}
    <div class="backlink"><a href="/login">Back to sign in</a></div>
  </form>
</body></html>"""

RESET_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PumpSleeper — Reset password</title>
<style>""" + _AUTH_CSS + """</style>
</head><body>
  {% if valid %}
  <form class="login-card" method="POST">
    <h1>Pump<span>Sleeper</span></h1>
    <div class="sub">Set a new password</div>
    <input type="hidden" name="token" value="{{ token }}">
    <label for="password">New password (min 8 chars)</label>
    <input id="password" name="password" type="password" autocomplete="new-password" autofocus>
    <label for="password2">Confirm new password</label>
    <input id="password2" name="password2" type="password" autocomplete="new-password">
    <button type="submit">Set password</button>
    {% if err %}<div class="err">{{ err }}</div>{% endif %}
  </form>
  {% else %}
  <div class="login-card">
    <h1>Pump<span>Sleeper</span></h1>
    <div class="sub">Reset password</div>
    <div class="err">{{ err }}</div>
    <div class="backlink"><a href="/login">Back to sign in</a></div>
  </div>
  {% endif %}
</body></html>"""

_last_forgot_ts = 0.0
FORGOT_COOLDOWN = 60   # seconds between reset-link sends

@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    global _last_forgot_ts
    msg = err = None
    if request.method == "POST":
        from notifications import get_settings as _notif_settings, send_reset
        cfg = _notif_settings()
        if cfg.get("email_enabled") != "1" and cfg.get("ntfy_enabled") != "1":
            err = ("No notification channel is configured, so a reset link can't be sent. "
                   "Set up email or ntfy first.")
        elif time.time() - _last_forgot_ts < FORGOT_COOLDOWN:
            msg = "A reset link was just sent — check your notifications."
        else:
            import secrets as _secrets
            from db import set_reset_token, _get_setting
            token = _secrets.token_urlsafe(32)
            set_reset_token(token, 1800)
            base = _get_setting("tunnel_public_url", "") or _get_setting("dashboard_local_url", "")
            reset_url = f"{base}/reset?token={token}"
            channels = send_reset(reset_url)
            _last_forgot_ts = time.time()
            if channels:
                msg = "A reset link was sent to your " + " and ".join(channels) + "."
            else:
                err = "No notification channel is configured."
    return render_template_string(FORGOT_TEMPLATE, msg=msg, err=err)

@app.route("/reset", methods=["GET", "POST"])
def reset():
    from db import verify_reset_token, set_password, clear_reset_token
    if request.method == "POST":
        token = request.form.get("token", "")
        p  = request.form.get("password", "")
        p2 = request.form.get("password2", "")
        if not verify_reset_token(token):
            return render_template_string(RESET_TEMPLATE, token="", valid=False,
                                          err="This reset link is invalid or has expired.")
        if p != p2:
            return render_template_string(RESET_TEMPLATE, token=token, valid=True,
                                          err="Passwords do not match.")
        try:
            set_password(p)
        except ValueError as exc:
            return render_template_string(RESET_TEMPLATE, token=token, valid=True, err=str(exc))
        clear_reset_token()
        return redirect(url_for("login"))
    token = request.args.get("token", "")
    valid = verify_reset_token(token)
    return render_template_string(RESET_TEMPLATE, token=token, valid=valid,
                                  err=None if valid else "This reset link is invalid or has expired.")


# ---------------------------------------------------------------------------
# Security settings + web access (Cloudflare quick tunnel)
# ---------------------------------------------------------------------------
@app.route("/api/settings/security", methods=["GET"])
def api_security_get():
    from db import (get_auth_username, is_default_credentials, get_web_access,
                    get_tunnel_mode, get_tunnel_token, get_tunnel_hostname)
    return jsonify({
        "username": get_auth_username(),
        "creds_changed": not is_default_credentials(),
        "web_access": get_web_access(),
        "tunnel_running": _tunnel_running(),
        "tunnel_url": _tunnel_url,
        "cloudflared_installed": shutil.which("cloudflared") is not None,
        "tunnel_mode": get_tunnel_mode(),
        "cf_tunnel_hostname": get_tunnel_hostname(),
        "cf_tunnel_token_saved": bool(get_tunnel_token()),
    })

@app.route("/api/settings/tunnel", methods=["POST"])
def api_tunnel_config():
    """Save tunnel type/token/hostname. If web access is currently on, restart
    the tunnel so the change takes effect immediately."""
    from db import set_tunnel_config, get_web_access, get_tunnel_mode
    data = request.get_json(force=True, silent=True) or {}
    mode     = data.get("mode")
    hostname = data.get("hostname")
    token    = data.get("token")          # only update when a non-empty value is sent
    try:
        set_tunnel_config(
            mode=mode if mode in ("quick", "named") else None,
            token=token if token else None,
            hostname=hostname if hostname is not None else None,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    resp = {"ok": True}
    if get_web_access():
        stop_tunnel()
        ok, err = start_tunnel()
        if not ok:
            return jsonify({"ok": False,
                            "error": "Saved, but the tunnel could not restart: " + str(err)}), 400
        if get_tunnel_mode() == "named":
            time.sleep(2.5)
        else:
            _wait_for_tunnel_url(timeout=12)
        resp.update({"tunnel_running": _tunnel_running(), "tunnel_url": _tunnel_url})
    return jsonify(resp)

@app.route("/api/settings/security/username", methods=["POST"])
def api_security_username():
    from db import set_username
    data = request.get_json(force=True, silent=True) or {}
    new_user = (data.get("username") or "").strip()
    try:
        set_username(new_user)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    session["user"] = new_user
    return jsonify({"ok": True})

@app.route("/api/settings/security/password", methods=["POST"])
def api_security_password():
    from db import set_password
    data = request.get_json(force=True, silent=True) or {}
    try:
        set_password(data.get("new_password") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    session["authed"] = True
    return jsonify({"ok": True})

@app.route("/api/settings/web-access", methods=["POST"])
def api_web_access():
    from db import is_default_credentials, set_web_access, get_tunnel_mode
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled"))
    if enabled:
        if is_default_credentials():
            return jsonify({"ok": False,
                            "error": "Change the default username and password first."}), 400
        mode = get_tunnel_mode()
        ok, err = start_tunnel()
        if not ok:
            if err == "not installed":
                return jsonify({"ok": False,
                                "error": "cloudflared is not installed on the Pi. Install it, then try again."}), 400
            if err == "no token":
                return jsonify({"ok": False,
                                "error": "Enter and save your Cloudflare tunnel token first."}), 400
            return jsonify({"ok": False, "error": f"Failed to start tunnel: {err}"}), 500
        if mode == "named":
            # A bad token makes cloudflared exit quickly — verify it stayed up.
            time.sleep(2.5)
            if not _tunnel_running():
                stop_tunnel()
                return jsonify({"ok": False,
                                "error": "The tunnel failed to start — check your Cloudflare token."}), 400
        else:
            _wait_for_tunnel_url(timeout=12)
        set_web_access(True)
        return jsonify({"ok": True, "web_access": True,
                        "tunnel_running": _tunnel_running(), "tunnel_url": _tunnel_url})
    stop_tunnel()
    set_web_access(False)
    return jsonify({"ok": True, "web_access": False,
                    "tunnel_running": False, "tunnel_url": None})


# ---------------------------------------------------------------------------
# Settings backup / restore (+ optional weekly email)
# ---------------------------------------------------------------------------
def _make_backup_payload() -> str:
    import json as _json
    from db import export_settings, BACKUP_SCHEMA
    try:
        from updater import get_current_version
        ver = get_current_version()
    except Exception:
        ver = "unknown"
    payload = {
        "pumpsleeper_backup": True,
        "app_version": ver,
        "schema": BACKUP_SCHEMA,
        "created": datetime.now(timezone.utc).isoformat(),
        "settings": export_settings(),
    }
    return _json.dumps(payload, indent=2)

@app.route("/api/settings/backup")
def api_settings_backup():
    body  = _make_backup_payload()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="pumpsleeper-backup-{stamp}.json"'
    return resp

@app.route("/api/settings/restore", methods=["POST"])
def api_settings_restore():
    import json as _json
    from db import import_settings, BACKUP_SCHEMA
    if request.files.get("file"):
        raw = request.files["file"].read().decode("utf-8", "replace")
    else:
        raw = request.get_data(as_text=True)
    try:
        obj = _json.loads(raw)
    except Exception:
        return jsonify({"ok": False, "error": "That doesn't look like a backup file (invalid JSON)."}), 400
    if not isinstance(obj, dict) or not obj.get("pumpsleeper_backup") or not isinstance(obj.get("settings"), dict):
        return jsonify({"ok": False, "error": "Not a PumpSleeper backup file."}), 400
    try:
        from_schema = int(obj.get("schema", 1))
    except (TypeError, ValueError):
        from_schema = 1
    warning = None
    if from_schema > BACKUP_SCHEMA:
        warning = (f"This backup was made by a newer version (schema {from_schema}) than this "
                   f"install (schema {BACKUP_SCHEMA}). Unrecognized settings were skipped.")
    restored, skipped = import_settings(obj["settings"], from_schema=from_schema)
    return jsonify({"ok": True, "restored": restored, "skipped": skipped,
                    "backup_version": obj.get("app_version", "unknown"), "warning": warning})

@app.route("/api/settings/backup/email", methods=["POST"])
def api_settings_backup_email():
    from notifications import send_backup_email
    from db import _set_setting
    body  = _make_backup_payload()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ok, msg = send_backup_email(body.encode("utf-8"), f"pumpsleeper-backup-{stamp}.json")
    if ok:
        _set_setting("backup_email_last_ts", str(time.time()))
    return jsonify({"ok": ok, "error": None if ok else msg})

@app.route("/api/settings/backup/auto", methods=["GET", "POST"])
def api_settings_backup_auto():
    from db import _get_setting, _set_setting
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        _set_setting("backup_email_enabled", "1" if data.get("enabled") else "0")
        return jsonify({"ok": True})
    return jsonify({"enabled": _get_setting("backup_email_enabled", "0") == "1",
                    "last_ts": _get_setting("backup_email_last_ts", "")})


@app.route("/")
def index():
    from db import get_ui_theme
    mobile = _wants_mobile(request)
    theme  = get_ui_theme("mobile" if mobile else "desktop")
    html = render_template_string(MOBILE_TEMPLATE if mobile else TEMPLATE)
    # Inject the saved theme on <html> so the correct palette paints with no flash.
    html = html.replace('<html lang="en">',
                        f'<html lang="en" data-theme="{theme}">', 1)
    resp = make_response(html)
    # Remember an explicit override so manual reloads keep the chosen layout.
    forced = request.args.get("desktop")
    if forced in ("0", "1"):
        resp.set_cookie("view", "mobile" if forced == "0" else "desktop",
                        max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp

_uplink_cache = {"val": None, "ts": 0.0}

def _detect_uplink():
    """How the Pi currently reaches the internet, from the default-route device:
    'ethernet', 'usb-wifi', 'wifi', or None (no internet). Cached ~30s."""
    now = time.time()
    if now - _uplink_cache["ts"] < 30:
        return _uplink_cache["val"]
    val = None
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            dev = m.group(1)
            if dev.startswith("wl"):  # a Wi-Fi client (wlan0 is the hotspot, no route)
                try:
                    link = os.path.realpath(f"/sys/class/net/{dev}/device")
                    val = "usb-wifi" if "/usb" in link else "wifi"
                except Exception:
                    val = "wifi"
            else:
                val = "ethernet"   # eth0/eth1/enx…/usb0 etc.
    except Exception:
        val = None
    _uplink_cache.update(val=val, ts=now)
    return val

@app.route("/api/data")
def api_data():
    from db import get_mode
    events = load_events()
    try:
        tz_offset_minutes = int(request.args.get("tz_offset", 0))
    except (TypeError, ValueError):
        tz_offset_minutes = 0
    filter_date = request.args.get("filter_date", "").strip() or None
    filter_pump = request.args.get("filter_pump", "").strip() or None
    data = compute_data(events,
                        tz_offset_minutes=tz_offset_minutes,
                        filter_date=filter_date,
                        filter_pump=filter_pump)
    data["mode"] = get_mode()
    data["pi_uplink"] = _detect_uplink()
    return jsonify(data)

def _backup_email_loop():
    """Once enabled, email a settings backup about weekly (checks hourly).
    Persisted last-sent timestamp survives restarts so it never double-sends."""
    INTERVAL = 7 * 24 * 3600
    while True:
        try:
            from db import _get_setting, _set_setting
            if _get_setting("backup_email_enabled", "0") == "1":
                last = float(_get_setting("backup_email_last_ts", "0") or 0)
                if time.time() - last >= INTERVAL:
                    from notifications import send_backup_email
                    body  = _make_backup_payload()
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    ok, _m = send_backup_email(body.encode("utf-8"), f"pumpsleeper-backup-{stamp}.json")
                    if ok:
                        _set_setting("backup_email_last_ts", str(time.time()))
        except Exception:
            pass
        time.sleep(3600)

threading.Thread(target=_backup_email_loop, daemon=True, name="backup-email").start()


if __name__ == "__main__":
    init_db()
    # If web access was left enabled, bring the tunnel back up (new URL each time).
    try:
        from db import get_web_access
        if get_web_access():
            start_tunnel()
    except Exception:
        pass
    app.run(host="0.0.0.0", port=DASH_PORT, debug=False)
