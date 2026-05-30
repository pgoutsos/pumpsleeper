#!/usr/bin/env python3
"""
One-time migration: import events.jsonl → pumpspy.db (SQLite).

Run once on the Pi:
    python3 /home/pgoutsos/pumpspy/migrate.py

Safe to re-run — duplicate rows won't be inserted if the database
already contains data (it checks row count first).
"""

import json
import os
import sys

# Ensure db.py is importable from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import DB_FILE, _connect, init_db

DATA_DIR   = os.environ.get("PUMPSPY_DATA", os.path.dirname(os.path.abspath(__file__)))
JSONL_FILE = os.path.join(DATA_DIR, "events.jsonl")


def main():
    print(f"Database : {DB_FILE}")
    print(f"Source   : {JSONL_FILE}")

    # --- Initialise schema -------------------------------------------------
    init_db()

    # --- Check if already migrated ----------------------------------------
    with _connect() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if existing > 0:
        print(f"Database already contains {existing} rows.")
        ans = input("Re-import anyway? This will add duplicates [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    # --- Read JSONL --------------------------------------------------------
    if not os.path.exists(JSONL_FILE):
        print("No events.jsonl found — nothing to migrate.")
        return

    events = []
    skipped = 0
    with open(JSONL_FILE) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                events.append((e["ts"], e["kind"], json.dumps(e["data"])))
            except Exception as ex:
                print(f"  Skipping line {i}: {ex}")
                skipped += 1

    print(f"Read {len(events)} events ({skipped} skipped).")

    # --- Insert in one transaction ----------------------------------------
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO events (ts, kind, data) VALUES (?, ?, ?)",
            events
        )
        conn.commit()

    print(f"Done — {len(events)} events inserted into {DB_FILE}")


if __name__ == "__main__":
    main()
