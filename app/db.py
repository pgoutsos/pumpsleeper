#!/usr/bin/env python3
"""
PumpSpy — shared SQLite database helpers.
Used by both server.py (writes) and dashboard.py (reads).

Schema
------
events
  id    INTEGER  primary key, auto-increment
  ts    TEXT     ISO-8601 UTC timestamp (indexed)
  kind  TEXT     event kind: ping | bbs_json | pump_outlet_alert |
                             auth | params_fetch | unknown (indexed)
  data  TEXT     JSON payload
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path — honours PUMPSPY_DATA env var (same as server.py / dashboard.py)
# ---------------------------------------------------------------------------
DB_FILE = os.path.join(
    os.environ.get("PUMPSPY_DATA", os.path.dirname(os.path.abspath(__file__))),
    "pumpspy.db"
)

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------
def _connect():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # allows concurrent reads during writes
    conn.execute("PRAGMA synchronous=NORMAL") # safe but faster than FULL
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db():
    """Create tables and indexes if they don't already exist."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    TEXT    NOT NULL,
                kind  TEXT    NOT NULL,
                data  TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_ts   ON events (ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # Default mode: proxy (transparent listen-in)
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('mode', 'proxy')"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Mode management
# ---------------------------------------------------------------------------
VALID_MODES = ("proxy", "takeover")

def get_mode() -> str:
    """Return current server mode: 'proxy' or 'takeover'."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'mode'"
        ).fetchone()
    return row["value"] if row else "proxy"

def set_mode(mode: str):
    """Persist a new mode and record the switch timestamp. Raises ValueError for unknown modes."""
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode {mode!r}. Valid: {VALID_MODES}")
    ts = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('mode', ?)", (mode,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('mode_switched_ts', ?)", (ts,)
            )
            conn.commit()

def get_mode_switched_ts():
    """Return the ISO timestamp of the last mode switch, or None if never switched."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'mode_switched_ts'"
        ).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Device tracking
# ---------------------------------------------------------------------------
def get_device_ip() -> str:
    """Return the last known hotspot IP of the PumpSpy device, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'device_ip'"
        ).fetchone()
    return row["value"] if row else None

def set_device_ip(ip: str):
    """Persist the device's hotspot IP address."""
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('device_ip', ?)", (ip,)
            )
            conn.commit()

def get_hotspot_connected() -> bool:
    """Return True/False for hotspot connection status, or None if never checked."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'hotspot_connected'"
        ).fetchone()
    if not row:
        return None
    return row["value"] == "1"

def set_hotspot_connected(connected: bool):
    """Persist the result of the most recent hotspot presence check."""
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('hotspot_connected', ?)",
                ("1" if connected else "0",)
            )
            conn.commit()


# ---------------------------------------------------------------------------
# UI theme — stored on the server, but kept SEPARATELY for the desktop and
# mobile layouts (keys: ui_theme_desktop / ui_theme_mobile).
# ---------------------------------------------------------------------------
VALID_THEMES  = ("auto", "dark", "light")
VALID_LAYOUTS = ("desktop", "mobile")

def _theme_key(layout: str) -> str:
    return "ui_theme_mobile" if layout == "mobile" else "ui_theme_desktop"

def get_ui_theme(layout: str = "desktop") -> str:
    """Return the theme for the given layout: 'auto' | 'dark' | 'light' (default 'auto')."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_theme_key(layout),)
        ).fetchone()
    return row["value"] if row and row["value"] in VALID_THEMES else "auto"

def set_ui_theme(theme: str, layout: str = "desktop"):
    """Persist the theme for one layout. Raises ValueError for unknown values."""
    if theme not in VALID_THEMES:
        raise ValueError(f"Unknown theme {theme!r}. Valid: {VALID_THEMES}")
    if layout not in VALID_LAYOUTS:
        raise ValueError(f"Unknown layout {layout!r}. Valid: {VALID_LAYOUTS}")
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (_theme_key(layout), theme)
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Generic settings helpers
# ---------------------------------------------------------------------------
def _get_setting(key: str, default=None):
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default

def _set_setting(key: str, value):
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value))
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Authentication + web-access gate
# ---------------------------------------------------------------------------
# A single dashboard user. Credentials start at the factory default admin/admin;
# the password is stored only as a salted PBKDF2 hash (werkzeug). Web access
# (Cloudflare quick tunnel) cannot be enabled until the default credentials are
# changed — see set_web_access().
from werkzeug.security import generate_password_hash, check_password_hash
import secrets as _secrets

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LEN = 8

def _ensure_auth_defaults():
    """Seed factory admin/admin credentials on first run (idempotent)."""
    if _get_setting("auth_username") is None:
        _set_setting("auth_username", DEFAULT_USERNAME)
    if _get_setting("auth_password_hash") is None:
        _set_setting("auth_password_hash", generate_password_hash(DEFAULT_PASSWORD))
    if _get_setting("auth_creds_changed") is None:
        _set_setting("auth_creds_changed", "0")

def get_auth_username() -> str:
    """Return the current dashboard username."""
    _ensure_auth_defaults()
    return _get_setting("auth_username", DEFAULT_USERNAME)

def verify_password(username: str, password: str) -> bool:
    """True if username + password match the stored single user."""
    _ensure_auth_defaults()
    stored_user = _get_setting("auth_username", DEFAULT_USERNAME)
    stored_hash = _get_setting("auth_password_hash", "")
    if username != stored_user or not stored_hash:
        # Run a dummy check to keep the timing roughly constant.
        check_password_hash(generate_password_hash("x"), password or "")
        return False
    return check_password_hash(stored_hash, password or "")

def is_default_credentials() -> bool:
    """True while the dashboard is still on the factory admin/admin login."""
    _ensure_auth_defaults()
    return _get_setting("auth_creds_changed", "0") != "1"

def set_credentials(new_username: str, new_password: str):
    """Set a new username + password. Raises ValueError on bad input.

    A password of at least MIN_PASSWORD_LEN characters is required, which also
    rules out reverting to the 5-character factory password 'admin'. Once set,
    the dashboard is marked as no longer using default credentials, which is the
    precondition for enabling web access.
    """
    new_username = (new_username or "").strip()
    new_password = new_password or ""
    if not new_username:
        raise ValueError("Username cannot be empty")
    if len(new_password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auth_username', ?)",
                (new_username,)
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auth_password_hash', ?)",
                (generate_password_hash(new_password),)
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auth_creds_changed', '1')"
            )
            conn.commit()

def set_username(new_username: str):
    """Update only the username (does not touch the password or the
    default-credentials gate — changing the password is what unlocks web access)."""
    new_username = (new_username or "").strip()
    if not new_username:
        raise ValueError("Username cannot be empty")
    _set_setting("auth_username", new_username)

def set_password(new_password: str):
    """Update only the password (PBKDF2 hash) and mark credentials as changed.
    Requires at least MIN_PASSWORD_LEN characters, so it can never be the
    factory password 'admin'."""
    new_password = new_password or ""
    if len(new_password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auth_password_hash', ?)",
                (generate_password_hash(new_password),)
            )
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auth_creds_changed', '1')"
            )
            conn.commit()

def get_web_access() -> bool:
    """True if the dashboard is configured to be exposed to the web."""
    return _get_setting("web_access", "0") == "1"

def set_web_access(enabled: bool):
    """Persist the web-access intent. Refuses to enable while on default creds."""
    if enabled and is_default_credentials():
        raise ValueError("Change the default username and password before enabling web access")
    _set_setting("web_access", "1" if enabled else "0")

def get_secret_key() -> str:
    """Return a stable Flask session-signing key, generating one on first use."""
    key = _get_setting("flask_secret_key")
    if not key:
        key = _secrets.token_hex(32)
        _set_setting("flask_secret_key", key)
    return key


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def record(kind: str, payload: dict) -> str:
    """
    Insert one event and return its ISO timestamp.
    Thread-safe — uses a module-level write lock so Flask's threaded server
    never gets an SQLite 'database is locked' error.
    """
    ts   = datetime.now(timezone.utc).isoformat()
    data = json.dumps(payload)
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, data) VALUES (?, ?, ?)",
                (ts, kind, data)
            )
            conn.commit()
    return ts


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def load_events(days: int = 7) -> list:
    """
    Return events from the last `days` days, sorted oldest-first.
    No row limit — SQLite on the Pi handles tens of thousands of rows easily.
    Same dict shape as the old JSONL loader so compute_data() needs no changes.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ts, kind, data
            FROM   events
            WHERE  ts >= ?
            ORDER  BY ts ASC
            """,
            (cutoff,)
        ).fetchall()
    return [
        {"ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])}
        for r in rows
    ]
