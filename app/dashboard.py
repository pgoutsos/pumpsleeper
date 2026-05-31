#!/usr/bin/env python3
"""
PumpSpy Dashboard — port 8080
Reads events.jsonl written by server.py and serves a live monitoring dashboard.
"""

import os
import subprocess
import threading
import requests as rlib
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, render_template_string
from db import load_events, init_db, get_mode_switched_ts, get_device_ip, get_hotspot_connected

SERVER_URL    = os.environ.get("PUMPSPY_SERVER_URL", "http://127.0.0.1:8081")
HOTSPOT_CON   = os.environ.get("PUMPSLEEPER_HOTSPOT_CON", "Hotspot")

app = Flask(__name__)

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

<!-- Stat cards -->
<div class="grid stats" id="stat-cards">
  <div class="card">
    <div class="stat-label" id="s-link-label">Cloud Link</div>
    <div class="stat-value" id="s-online">—</div>
    <div class="stat-sub" id="s-device-ip" style="font-family:monospace;letter-spacing:0.3px"></div>
    <div class="stat-sub" id="s-last-ping">—</div>
    <div style="margin-top:10px">
      <button class="cycle-btn" id="cycle-btn" onclick="cycleHotspot()">↺ Cycle Hotspot</button>
    </div>
    <div id="cycle-status" style="display:none;margin-top:8px"></div>
  </div>
  <div class="card">
    <div class="stat-label">Signal (RSSI)</div>
    <div class="stat-value" id="s-rssi">—</div>
    <div class="stat-sub">dBm</div>
  </div>
  <div class="card">
    <div class="stat-label">Backup Battery (12V)</div>
    <div class="stat-value" id="s-battery">—</div>
    <div class="stat-sub" id="s-battery-sub">—</div>
  </div>
  <div class="card">
    <div class="stat-label">Main Pump Today</div>
    <div class="stat-value" id="s-main-runs">—</div>
    <div class="stat-sub" id="s-main-runtime">—</div>
  </div>
  <div class="card">
    <div class="stat-label">Backup Pump Today</div>
    <div class="stat-value" id="s-backup-runs">—</div>
    <div class="stat-sub" id="s-backup-runtime">—</div>
  </div>
  <div class="card" id="s-op-card">
    <div class="stat-label">Operating Status</div>
    <div class="stat-value" id="s-op-status">—</div>
    <div class="stat-sub" id="s-op-sub">—</div>
  </div>
</div>

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
      <div class="scroll-table"><table id="pump-table">
        <thead><tr>
          <th class="sortable" data-col="ts"        onclick="sortPumpTable(this)">Run Date <span class="sort-icon"></span></th>
          <th class="sortable" data-col="pump"      onclick="sortPumpTable(this)">Pump <span class="sort-icon"></span></th>
          <th class="sortable" data-col="motor"     onclick="sortPumpTable(this)">State <span class="sort-icon"></span></th>
          <th class="sortable" data-col="duration"  onclick="sortPumpTable(this)">Duration <span class="sort-icon"></span></th>
          <th class="sortable" data-col="gallons"   onclick="sortPumpTable(this)">Est. Gallons <span class="sort-icon"></span></th>
          <th class="sortable" data-col="amps"      onclick="sortPumpTable(this)">Current <span class="sort-icon"></span></th>
          <th class="sortable" data-col="battery_v" onclick="sortPumpTable(this)">Batt V <span class="sort-icon"></span></th>
          <th class="sortable" data-col="loaded_v"  onclick="sortPumpTable(this)">Loaded V <span class="sort-icon"></span></th>
        </tr></thead>
        <tbody></tbody>
      </table></div>
    </div>
  </div>
</div>

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

</div><!-- end tab-dashboard -->

<!-- Settings tab -->
<div id="tab-settings" class="tab-panel">
<div class="settings-grid">

  <!-- ── Updates ──────────────────────────────────────────────────── -->
  <div class="card full-width" id="update-card">
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
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Release notes</div>
        <pre id="release-notes" style="font-family:inherit;font-size:12px;color:var(--text);white-space:pre-wrap;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;max-height:200px;overflow-y:auto"></pre>
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

  <!-- ── Save ──────────────────────────────────────────────────── -->
  <div class="full-width" style="display:flex;align-items:center;gap:14px">
    <button class="save-btn" onclick="saveSettings()">Save Settings</button>
    <span class="settings-msg" id="save-msg"></span>
    <span id="unsaved-msg" style="display:none;font-size:12px;color:var(--yellow)">⚠ Unsaved changes — save before sending a test</span>
  </div>

</div>
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
      '<td><span class="badge ' + r.motor.toLowerCase() + '">' + r.motor + '</span></td>' +
      '<td>' + dur + '</td>' +
      '<td>' + gallons + '</td>' +
      '<td>' + amps + '</td>' +
      '<td>' + battV + '</td>' +
      '<td>' + loadedV + '</td>' +
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
    const diff = Math.floor((Date.now() - new Date(ts)) / 1000);
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
  // Dynamic link label based on mode
  document.getElementById('s-link-label').textContent =
    d.mode === 'takeover' ? 'Local Link' : 'Cloud Link';

  // Device link status card
  _linkStatus     = d.link_status || (d.online ? 'online' : 'offline');
  _lastPingTs     = d.last_ping_ts || null;
  _modeSwitchedTs = d.mode_switched_ts || null;

  const onEl = document.getElementById('s-online');
  const statusLabel = _linkStatus === 'online'  ? 'Online'
                    : _linkStatus === 'pending' ? 'Pending…'
                    : 'Offline';
  onEl.innerHTML = '<span class="dot ' + _linkStatus + '"></span>' + statusLabel;

  // Device IP + WiFi badge
  const ipEl = document.getElementById('s-device-ip');
  if (d.device_ip) {
    let wifiMark = '';
    if (d.hotspot_connected === true)       wifiMark = ' &nbsp;<span style="color:var(--green);font-family:sans-serif">WiFi ✓</span>';
    else if (d.hotspot_connected === false) wifiMark = ' &nbsp;<span style="color:var(--red);font-family:sans-serif">WiFi ✗</span>';
    ipEl.innerHTML = d.device_ip + wifiMark;
  } else {
    ipEl.textContent = '';
  }

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
  const backupBatt = (d.last_backup_battery_v !== null && d.last_backup_battery_v !== undefined)
    ? d.last_backup_battery_v.toFixed(3) : '—';
  document.getElementById('s-battery').textContent = backupBatt;
  document.getElementById('s-battery-sub').textContent =
    (d.last_backup_loaded_v !== null && d.last_backup_loaded_v !== undefined)
      ? 'Loaded: ' + d.last_backup_loaded_v.toFixed(3) + 'V' : '—';

  document.getElementById('s-main-runs').textContent = d.main_runs_today;
  document.getElementById('s-main-runtime').textContent = d.total_main_runtime_today > 0
    ? d.total_main_runtime_today + 's · ' + d.total_main_gallons_today + ' gal'
    : 'No runs today';

  document.getElementById('s-backup-runs').textContent = d.backup_runs_today;
  document.getElementById('s-backup-runtime').textContent = d.total_backup_runtime_today > 0
    ? d.total_backup_runtime_today + 's · ' + d.total_backup_gallons_today + ' gal'
    : 'No runs today';

  // Operating status
  const opCard = document.getElementById('s-op-card');
  const opEl   = document.getElementById('s-op-status');
  const opSub  = document.getElementById('s-op-sub');
  if (!d.op_status) {
    opEl.textContent  = 'No data';
    opSub.textContent = '—';
    opCard.style.borderColor = '';
  } else if (d.op_status.pump === 'main') {
    opEl.innerHTML = '<span style="color:var(--green)">&#x2714; Main Pump</span>';
    opSub.textContent = 'Last run ' + fmtAgo(d.op_status.ts);
    opCard.style.borderColor = 'var(--green)';
  } else {
    const trig = d.op_status.trigger ? d.op_status.trigger.replace('_', ' ') : 'backup';
    opEl.innerHTML = '<span style="color:var(--yellow)">&#x26A0; Backup Pump</span>';
    opSub.textContent = trig + ' · ' + fmtAgo(d.op_status.ts);
    opCard.style.borderColor = 'var(--yellow)';
  }

  // RSSI chart
  const labels = d.rssi_history.map(p => fmtTs(p.ts));
  const values = d.rssi_history.map(p => p.rssi);
  if (!rssiChart) {
    const ctx = document.getElementById('rssi-chart').getContext('2d');
    rssiChart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [{ label: 'RSSI (dBm)', data: values,
        borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)',
        pointRadius: 3, pointBackgroundColor: '#3b82f6', tension: 0.3, fill: true }] },
      options: { responsive: true, maintainAspectRatio: false,
        scales: {
          x: { ticks: { color: '#8892a4', maxTicksLimit: 8, maxRotation: 0 }, grid: { color: '#2a2d3a' } },
          y: { ticks: { color: '#8892a4' }, grid: { color: '#2a2d3a' } }
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

refresh();
fetchMode();
setInterval(refresh, 30000);
setInterval(fetchMode, 10000);

// ── Tab switching ─────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => {
    if (b.textContent.trim().toLowerCase() === name) b.classList.add('active');
  });
  if (name === 'settings') { loadSettings(); loadUpdateInfo(); }
}

// ── Notification settings ─────────────────────────────────────────────────
function _setMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'settings-msg ' + (ok ? 'ok' : 'err');
  setTimeout(() => { el.textContent = ''; el.className = 'settings-msg'; }, 5000);
}

let _settingsDirty = false;

function _markDirty() {
  _settingsDirty = true;
  document.getElementById('unsaved-msg').style.display = '';
}

function _markClean() {
  _settingsDirty = false;
  document.getElementById('unsaved-msg').style.display = 'none';
  // Clear any "save first" warnings on the test buttons
  ['email-test-msg','ntfy-test-msg'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.textContent.includes('Save')) { el.textContent = ''; el.className = 'settings-msg'; }
  });
}

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
    _markClean();
    // Attach dirty listeners after populating values
    document.querySelectorAll('#tab-settings input').forEach(el => {
      el.addEventListener('change', _markDirty);
      el.addEventListener('input',  _markDirty);
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
    if (d.ok) _markClean();
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

function _showLatest(latest, available) {
  document.getElementById('latest-version').textContent = latest.tag || '—';
  document.getElementById('latest-version').style.color = available ? 'var(--green)' : 'var(--muted)';
  if (latest.notes) {
    document.getElementById('release-notes').textContent = latest.notes;
    document.getElementById('release-notes-box').style.display = '';
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

async function applyUpdate() {
  if (!confirm('Apply the update now? Services will restart briefly.')) return;
  const btn = document.getElementById('apply-update-btn');
  btn.disabled = true;
  const prog = document.getElementById('update-progress');
  prog.style.display = '';
  prog.textContent = 'Starting update…';

  try {
    await fetch('/api/update/apply', { method: 'POST' });
  } catch(e) {
    _setMsg('update-status-msg', '✗ Failed to start update', false);
    btn.disabled = false;
    return;
  }

  let _updateStarted = true;
  let _pollFailCount = 0;
  if (_updatePoller) clearInterval(_updatePoller);
  _updatePoller = setInterval(async () => {
    try {
      const r = await fetch('/api/update/status');
      const s = await r.json();
      _pollFailCount = 0;

      if (s.phase === 'downloading') {
        prog.textContent = '⬇ Downloading update files…';
      } else if (s.phase === 'restarting') {
        prog.textContent = '↺ Restarting services — dashboard will reload shortly…';
      } else if (s.phase === 'done') {
        clearInterval(_updatePoller); _updatePoller = null;
        prog.style.display = 'none';
        btn.disabled = false;
        loadUpdateInfo();
      } else if (s.phase === 'error') {
        clearInterval(_updatePoller); _updatePoller = null;
        prog.style.display = 'none';
        _setMsg('update-status-msg', '✗ ' + (s.error || 'Update failed'), false);
        btn.disabled = false;
      } else if (s.phase === 'idle' && _updateStarted) {
        // Back to idle — dashboard restarted after update
        clearInterval(_updatePoller); _updatePoller = null;
        prog.style.display = 'none';
        btn.disabled = false;
        loadUpdateInfo();   // shows "✓ Successfully updated" from last_update.json
      }
    } catch(e) {
      // Fetch failed — dashboard is restarting
      _pollFailCount++;
      if (_pollFailCount >= 2) {
        prog.textContent = '↺ Dashboard restarting…';
      }
    }
  }, 1000);
}

async function sendTest(channel) {
  if (_settingsDirty) {
    _setMsg(channel + '-test-msg', '⚠ Save your settings first before sending a test.', false);
    return;
  }
  const msgId = channel + '-test-msg';
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

@app.route("/")
def index():
    return render_template_string(TEMPLATE)

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
    return jsonify(data)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=False)
