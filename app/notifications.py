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

import html as _html
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

def _dashboard_link() -> str:
    """Best URL to reach the dashboard: the public tunnel address if web access
    is on, otherwise the dashboard's LAN address. May be '' if neither is known."""
    return _get("tunnel_public_url", "") or _get("dashboard_local_url", "")


def _send_email(subject: str, body: str, cfg: dict, link: str = "", link_label: str = "Open Dashboard"):
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

    plain = body + (f"\n\n{link_label}: {link}" if link else "")
    msg.attach(MIMEText(plain, "plain"))
    if link:
        body_html = _html.escape(body).replace("\n", "<br>")
        link_esc  = _html.escape(link, quote=True)
        label_esc = _html.escape(link_label)
        html_body = (
            '<html><body style="font-family:Segoe UI,system-ui,sans-serif;color:#1d2430">'
            f'<p style="font-size:15px">{body_html}</p>'
            f'<p><a href="{link_esc}" style="display:inline-block;padding:10px 18px;'
            'background:#3b82f6;color:#ffffff;text-decoration:none;border-radius:6px;'
            f'font-weight:600">{label_esc}</a></p>'
            f'<p style="font-size:12px;color:#888"><a href="{link_esc}" style="color:#888">{link_esc}</a></p>'
            '</body></html>'
        )
        msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(user, pwd)
            s.sendmail(frm, [to], msg.as_string())
        log.info(f"NOTIF  email sent → {to} ({subject})")
    except Exception as exc:
        log.error(f"NOTIF  email failed: {exc}")


def _send_ntfy(title: str, body: str, priority: str, cfg: dict, link: str = "", action_label: str = "Open Dashboard"):
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
    if link:
        # Tapping the notification opens the link; also add a button.
        headers["Click"]   = link
        headers["Actions"] = f"view, {action_label}, {link}"
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

    link = _dashboard_link()

    if cfg["email_enabled"] == "1":
        _send_email(f"PumpSleeper: {title}", body, cfg, link)

    if cfg["ntfy_enabled"] == "1":
        _send_ntfy(title, body, priority, cfg, link)


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
# Password reset link
# ---------------------------------------------------------------------------

def send_reset(reset_url: str) -> list:
    """Send a password-reset link over every enabled channel.
    Returns the list of channels used (empty if none are configured)."""
    cfg = get_settings()
    title = "PumpSleeper password reset"
    body  = ("A password reset was requested for your PumpSleeper dashboard. "
             "Use the link below within 30 minutes to set a new password. "
             "If this wasn't you, you can ignore this message.")
    sent = []
    if cfg["email_enabled"] == "1":
        _send_email("PumpSleeper: Password reset", body, cfg, reset_url, "Reset Password")
        sent.append("email")
    if cfg["ntfy_enabled"] == "1":
        _send_ntfy(title, body, "high", cfg, reset_url, "Reset Password")
        sent.append("ntfy")
    return sent


# ---------------------------------------------------------------------------
# Backup email (settings backup as an attachment)
# ---------------------------------------------------------------------------

def send_backup_email(data_bytes: bytes, filename: str):
    """Email the settings backup as a JSON attachment to the configured address.
    Returns (ok, message)."""
    cfg = get_settings()
    if cfg["email_enabled"] != "1":
        return False, "Email notifications are not enabled"
    host = cfg["email_smtp_host"]
    user = cfg["email_smtp_user"]
    to   = cfg["email_to"]
    if not (host and user and to):
        return False, "Email is not fully configured"

    from email.mime.base import MIMEBase
    from email import encoders

    msg = MIMEMultipart()
    msg["Subject"] = "PumpSleeper settings backup"
    msg["From"]    = cfg["email_from"] or user
    msg["To"]      = to
    msg.attach(MIMEText(
        "Attached is a backup of your PumpSleeper settings. Keep it somewhere "
        "safe — it contains your configuration, including saved credentials. To "
        "restore it, upload the file in Settings -> Backup & Restore after "
        "reimaging.", "plain"))

    part = MIMEBase("application", "json")
    part.set_payload(data_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    try:
        port = int(cfg["email_smtp_port"] or 587)
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.login(user, cfg["email_smtp_pass"])
            s.sendmail(msg["From"], [to], msg.as_string())
        log.info(f"NOTIF  backup email sent -> {to}")
        return True, "Backup emailed"
    except Exception as exc:
        log.error(f"NOTIF  backup email failed: {exc}")
        return False, str(exc)


# ---------------------------------------------------------------------------
# Test helper (called from dashboard "Send test" button)
# ---------------------------------------------------------------------------

def send_test(channel: str) -> tuple[bool, str]:
    """
    Send a test notification on the given channel ('email' or 'ntfy').
    Returns (success, message).
    """
    cfg = get_settings()
    link = _dashboard_link()
    try:
        if channel == "email":
            _send_email(
                "PumpSleeper test notification",
                "This is a test notification from PumpSleeper. Email is working correctly.",
                cfg,
                link,
            )
        elif channel == "ntfy":
            _send_ntfy(
                "PumpSleeper test",
                "This is a test notification from PumpSleeper. Ntfy is working correctly.",
                "default",
                cfg,
                link,
            )
        else:
            return False, f"Unknown channel: {channel}"
        return True, "Test notification sent"
    except Exception as exc:
        return False, str(exc)
