#!/usr/bin/env python3
"""
PumpSleeper — notification dispatcher.

Supports:
  - Email via SMTP (Gmail, Outlook, or any provider)
  - Ntfy push notifications (ntfy.sh or self-hosted)

Settings are read from the SQLite `settings` table (managed by db.py).
Call notify(event, detail) from anywhere — it reads current config each
time so settings changes take effect immediately without a restart.
"""

import logging
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event names (used as keys for per-trigger toggles)
# ---------------------------------------------------------------------------
EVENT_BACKUP_PUMP      = "backup_pump_ran"
EVENT_MAIN_PUMP        = "main_pump_ran"
EVENT_HIGH_WATER       = "high_water"
EVENT_DEVICE_OFFLINE   = "device_offline"
EVENT_UPDATE_AVAILABLE = "update_available"
EVENT_UPDATE_INSTALLED = "update_installed"
# Sent whenever the Cloudflare quick-tunnel URL changes. Intentionally NOT in
# ALL_EVENTS (no per-event toggle) — if you turned on web access you want the
# new address, so it always sends through whatever channels are enabled.
EVENT_WEBACCESS_URL    = "webaccess_url"

ALL_EVENTS = [
    EVENT_BACKUP_PUMP, EVENT_MAIN_PUMP, EVENT_HIGH_WATER,
    EVENT_DEVICE_OFFLINE, EVENT_UPDATE_AVAILABLE, EVENT_UPDATE_INSTALLED,
]

EVENT_LABELS = {
    EVENT_BACKUP_PUMP:      "Backup pump ran",
    EVENT_MAIN_PUMP:        "Main pump ran",
    EVENT_HIGH_WATER:       "High water alert",
    EVENT_DEVICE_OFFLINE:   "Device offline",
    EVENT_UPDATE_AVAILABLE: "New version available",
    EVENT_UPDATE_INSTALLED: "New version installed",
}

# ---------------------------------------------------------------------------
# Settings helpers  (thin wrappers around db.get/set)
# ---------------------------------------------------------------------------

def _get(key, default=""):
    from db import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def _set(key, value):
    from db import _connect, _write_lock
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
            conn.commit()


def get_settings() -> dict:
    """Return all notification settings as a flat dict."""
    return {
        # Email
        "email_enabled":    _get("notif_email_enabled",  "0"),
        "email_smtp_host":  _get("notif_email_smtp_host", ""),
        "email_smtp_port":  _get("notif_email_smtp_port", "587"),
        "email_smtp_user":  _get("notif_email_smtp_user", ""),
        "email_smtp_pass":  _get("notif_email_smtp_pass", ""),
        "email_from":       _get("notif_email_from",      ""),
        "email_to":         _get("notif_email_to",        ""),
        # Ntfy
        "ntfy_enabled":     _get("notif_ntfy_enabled",   "0"),
        "ntfy_url":         _get("notif_ntfy_url",        "https://ntfy.sh"),
        "ntfy_topic":       _get("notif_ntfy_topic",      ""),
        "ntfy_token":       _get("notif_ntfy_token",      ""),
        # Per-event toggles (default all on)
        **{f"trigger_{e}": _get(f"notif_trigger_{e}", "1") for e in ALL_EVENTS},
    }


def save_settings(data: dict):
    """Persist notification settings from a flat dict (e.g. form POST)."""
    fields = [
        "email_enabled", "email_smtp_host", "email_smtp_port",
        "email_smtp_user", "email_smtp_pass", "email_from", "email_to",
        "ntfy_enabled", "ntfy_url", "ntfy_topic", "ntfy_token",
    ] + [f"trigger_{e}" for e in ALL_EVENTS]

    for field in fields:
        if field in data:
            _set(f"notif_{field}", str(data[field]))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str, cfg: dict):
    host = cfg["email_smtp_host"]
    port = int(cfg["email_smtp_port"] or 587)
    user = cfg["email_smtp_user"]
    pwd  = cfg["email_smtp_pass"]
    frm  = cfg["email_from"] or user
    to   = cfg["email_to"]

    if not (host and user and to):
        log.warning("NOTIF  email not configured — skipping")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = frm
    msg["To"]      = to
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(user, pwd)
            s.sendmail(frm, [to], msg.as_string())
        log.info(f"NOTIF  email sent → {to} ({subject})")
    except Exception as exc:
        log.error(f"NOTIF  email failed: {exc}")


def _send_ntfy(title: str, body: str, priority: str, cfg: dict):
    import requests
    base  = (cfg["ntfy_url"] or "https://ntfy.sh").rstrip("/")
    topic = cfg["ntfy_topic"]
    token = cfg["ntfy_token"]

    if not topic:
        log.warning("NOTIF  ntfy topic not configured — skipping")
        return

    url     = f"{base}/{topic}"
    headers = {
        "Title":    title,
        "Priority": priority,
        "Tags":     "water_pump",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.post(url, data=body.encode(), headers=headers, timeout=10)
        r.raise_for_status()
        log.info(f"NOTIF  ntfy sent → {url} ({title})")
    except Exception as exc:
        log.error(f"NOTIF  ntfy failed: {exc}")


def _dispatch(event: str, title: str, body: str, priority: str = "default"):
    """Internal — send to all enabled channels. Runs in a background thread."""
    cfg = get_settings()

    # Check per-event toggle
    if cfg.get(f"trigger_{event}", "1") != "1":
        return

    if cfg["email_enabled"] == "1":
        _send_email(f"PumpSleeper: {title}", body, cfg)

    if cfg["ntfy_enabled"] == "1":
        _send_ntfy(title, body, priority, cfg)


def notify(event: str, detail: str = ""):
    """
    Fire-and-forget notification. Call from server.py on any event.

    event  — one of the EVENT_* constants above
    detail — optional extra context (e.g. duration, gallons)
    """
    labels = {
        EVENT_BACKUP_PUMP:      ("Backup pump ran",        "urgent"),
        EVENT_MAIN_PUMP:        ("Main pump ran",          "default"),
        EVENT_HIGH_WATER:       ("⚠ High water alert",    "urgent"),
        EVENT_DEVICE_OFFLINE:   ("Device offline",         "high"),
        EVENT_UPDATE_AVAILABLE: ("Update available",       "default"),
        EVENT_UPDATE_INSTALLED: ("Update installed",       "default"),
        EVENT_WEBACCESS_URL:    ("Web access URL",         "high"),
    }
    title, priority = labels.get(event, (event, "default"))
    body = title + (f"\n\n{detail}" if detail else "")
    threading.Thread(
        target=_dispatch, args=(event, title, body, priority),
        daemon=True, name=f"notif-{event}"
    ).start()


# ---------------------------------------------------------------------------
# Test helper (called from dashboard "Send test" button)
# ---------------------------------------------------------------------------

def send_test(channel: str) -> tuple[bool, str]:
    """
    Send a test notification on the given channel ('email' or 'ntfy').
    Returns (success, message).
    """
    cfg = get_settings()
    try:
        if channel == "email":
            _send_email(
                "PumpSleeper test notification",
                "This is a test notification from PumpSleeper. Email is working correctly.",
                cfg,
            )
        elif channel == "ntfy":
            _send_ntfy(
                "PumpSleeper test",
                "This is a test notification from PumpSleeper. Ntfy is working correctly.",
                "default",
                cfg,
            )
        else:
            return False, f"Unknown channel: {channel}"
        return True, "Test notification sent"
    except Exception as exc:
        return False, str(exc)
