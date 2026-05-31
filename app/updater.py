#!/usr/bin/env python3
"""
PumpSleeper — self-update module.

Checks GitHub releases for new versions, downloads app files,
and restarts services. Supports auto-update (via systemd timer)
and manual update triggered from the dashboard.
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GITHUB_REPO    = "pgoutsos/pumpsleeper"
GITHUB_API     = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RAW_BASE       = f"https://raw.githubusercontent.com/{GITHUB_REPO}"
INSTALL_DIR    = os.environ.get("PUMPSPY_INSTALL_DIR", "/opt/pumpsleeper")
VERSION_FILE   = os.path.join(INSTALL_DIR, "VERSION")
APP_FILES      = ["server.py", "dashboard.py", "db.py", "mqtt.py",
                  "notifications.py", "updater.py"]
SERVICES       = ["pumpsleeper", "pumpsleeper-dashboard"]

# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def get_current_version() -> str:
    """Return the installed version tag, or 'unknown'."""
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"


def set_current_version(tag: str):
    """Write the installed version tag to disk."""
    try:
        with open(VERSION_FILE, "w") as f:
            f.write(tag.strip())
    except Exception as exc:
        log.warning(f"UPDATE  could not write VERSION file: {exc}")


# ---------------------------------------------------------------------------
# Auto-update setting
# ---------------------------------------------------------------------------

def get_auto_update() -> bool:
    """Return True if auto-update is enabled (default: True)."""
    from db import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'auto_update'"
        ).fetchone()
    return (row["value"] == "1") if row else True


def set_auto_update(enabled: bool):
    """Persist the auto-update preference."""
    from db import _connect, _write_lock
    with _write_lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_update', ?)",
                ("1" if enabled else "0",)
            )
            conn.commit()


# ---------------------------------------------------------------------------
# GitHub release check
# ---------------------------------------------------------------------------

def get_latest_release() -> dict:
    """
    Fetch the latest GitHub release.
    Returns dict with keys: tag, notes, published_at, url
    Returns None on failure.
    """
    import requests
    try:
        r = requests.get(
            GITHUB_API,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "tag":          data.get("tag_name", ""),
            "notes":        data.get("body", "").strip(),
            "published_at": data.get("published_at", ""),
            "url":          data.get("html_url", ""),
        }
    except Exception as exc:
        log.warning(f"UPDATE  could not fetch latest release: {exc}")
        return None


def update_available() -> bool:
    """Return True if a newer version is available on GitHub."""
    current = get_current_version()
    if current == "unknown":
        return False
    latest = get_latest_release()
    if not latest:
        return False
    return latest["tag"] != current


# ---------------------------------------------------------------------------
# Update state (in-memory, for UI polling)
# ---------------------------------------------------------------------------
_update_lock  = threading.Lock()
_update_state = {
    "running":    False,
    "phase":      "idle",   # idle | downloading | restarting | done | error
    "error":      None,
    "started_at": None,
    "version":    None,     # version being applied
}


def get_update_state() -> dict:
    with _update_lock:
        return dict(_update_state)


def _set_state(**kwargs):
    with _update_lock:
        _update_state.update(kwargs)


# ---------------------------------------------------------------------------
# Apply update
# ---------------------------------------------------------------------------

def _restart_services():
    """Restart both systemd services. Requires passwordless sudo."""
    for svc in SERVICES:
        subprocess.run(
            ["sudo", "systemctl", "restart", svc],
            capture_output=True, timeout=30,
        )
        log.info(f"UPDATE  restarted {svc}")


def do_update(tag: str = None) -> tuple[bool, str]:
    """
    Download app files for `tag` (defaults to latest) and restart services.
    Runs synchronously — call from a background thread.
    Returns (success, message).
    """
    import requests

    try:
        if tag is None:
            release = get_latest_release()
            if not release:
                return False, "Could not fetch latest release from GitHub"
            tag = release["tag"]

        _set_state(phase="downloading", version=tag, error=None)
        log.info(f"UPDATE  downloading {tag}...")

        ref = tag  # e.g. "v1.1"
        base = f"{RAW_BASE}/{ref}/app"

        # Download all files to a temp directory first
        with tempfile.TemporaryDirectory() as tmp:
            for fname in APP_FILES:
                url = f"{base}/{fname}"
                r = requests.get(url, timeout=30)
                if r.status_code == 404:
                    log.debug(f"UPDATE  {fname} not in release — skipping")
                    continue
                r.raise_for_status()
                dest = os.path.join(tmp, fname)
                with open(dest, "wb") as f:
                    f.write(r.content)
                log.info(f"UPDATE  downloaded {fname}")

            # Copy downloaded files into install dir
            for fname in os.listdir(tmp):
                src  = os.path.join(tmp, fname)
                dest = os.path.join(INSTALL_DIR, fname)
                with open(src, "rb") as sf, open(dest, "wb") as df:
                    df.write(sf.read())
                log.info(f"UPDATE  installed {fname}")

        # Update VERSION file
        set_current_version(tag)

        # Persist result to disk so it survives the dashboard restart
        import json as _json
        result_file = os.path.join(INSTALL_DIR, "data", "last_update.json")
        try:
            with open(result_file, "w") as f:
                _json.dump({
                    "tag": tag,
                    "ts":  datetime.now(timezone.utc).isoformat(),
                    "ok":  True,
                }, f)
        except Exception:
            pass

        # Restart server first, then dashboard last (killing this process)
        _set_state(phase="restarting")
        log.info("UPDATE  restarting services...")
        subprocess.run(["sudo", "systemctl", "restart", "pumpsleeper"],
                       capture_output=True, timeout=30)
        log.info("UPDATE  restarted pumpsleeper")
        log.info(f"UPDATE  updated to {tag} — restarting dashboard now")
        # This kills the current process — must be last
        subprocess.run(["sudo", "systemctl", "restart", "pumpsleeper-dashboard"],
                       capture_output=True, timeout=30)

        # Notify that update was installed
        try:
            import notifications as notif
            notif.notify(notif.EVENT_UPDATE_INSTALLED, f"PumpSleeper updated to {tag}")
        except Exception:
            pass

        return True, f"Updated to {tag}"

    except Exception as exc:
        _set_state(phase="error", error=str(exc), running=False)
        log.error(f"UPDATE  failed: {exc}")
        return False, str(exc)


def trigger_update(tag: str = None):
    """
    Start an update in a background thread.
    Returns immediately — poll get_update_state() for progress.
    """
    with _update_lock:
        if _update_state["running"]:
            return False, "Update already in progress"
        _update_state.update({
            "running":    True,
            "phase":      "downloading",
            "error":      None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "version":    tag,
        })

    threading.Thread(
        target=do_update, args=(tag,),
        daemon=True, name="updater"
    ).start()
    return True, "Update started"


# ---------------------------------------------------------------------------
# Auto-update entry point (called by systemd timer)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    latest = get_latest_release()
    current = get_current_version()
    avail = latest and latest["tag"] != current and current != "unknown"

    if not avail:
        log.info("UPDATE  already up to date")
    elif not get_auto_update():
        # Update available but auto-update is off — notify user
        log.info(f"UPDATE  new version available: {latest['tag']} (auto-update disabled)")
        try:
            import notifications as notif
            notif.notify(notif.EVENT_UPDATE_AVAILABLE,
                         f"PumpSleeper {latest['tag']} is available. Open the dashboard to update.")
        except Exception:
            pass
    else:
        log.info("UPDATE  applying available update...")
        ok, msg = do_update()
        log.info(f"UPDATE  {'OK' if ok else 'FAILED'}: {msg}")
