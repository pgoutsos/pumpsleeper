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
