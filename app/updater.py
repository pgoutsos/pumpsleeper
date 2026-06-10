#!/usr/bin/env python3
"""
PumpSleeper — self-update module.

Checks GitHub releases for new versions, downloads app files,
and restarts services. Supports auto-update (via systemd timer)
and manual update triggered from the dashboard.

Design notes (why this looks the way it does)
----------------------------------------------
The update's final act is restarting the dashboard service — i.e. the
process that the dashboard-triggered update runs in gets killed. So update
status must NOT live in process memory; it is persisted to a JSON state
file on disk (update_state.json) that survives the restart. The dashboard's
/api/update/status simply reads that file.

The manual update runs in a *detached* child process (start_new_session)
launched by the dashboard, so the HTTP request returns immediately. The
nightly auto-update runs inside the separate `pumpsleeper-update.service`
oneshot, which is in its own cgroup and is unaffected by restarting the
dashboard.

Ordering is critical: we write VERSION, last_update.json, send the
"update installed" notification, and write phase="done" to the state file
*before* restarting the dashboard. That way the success state and the
notification are guaranteed even though the dashboard restart tears this
process down.
"""

import json
import logging
import os
import subprocess
import sys
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
DATA_DIR       = os.environ.get("PUMPSPY_DATA", os.path.join(INSTALL_DIR, "data"))
VERSION_FILE   = os.path.join(INSTALL_DIR, "VERSION")
STATE_FILE     = os.path.join(DATA_DIR, "update_state.json")
RESULT_FILE    = os.path.join(DATA_DIR, "last_update.json")
APP_FILES      = ["server.py", "dashboard.py", "db.py", "mqtt.py",
                  "notifications.py", "updater.py"]
SERVICES       = ["pumpsleeper", "pumpsleeper-dashboard"]

# A run is considered stale (crashed mid-update) after this long.
STALE_AFTER_S  = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            # Use `or ""` (not just a .get default): GitHub returns these as
            # explicit null when empty, so .get would yield None and .strip()
            # would crash — breaking the whole update check (shows "unavailable").
            "tag":          data.get("tag_name") or "",
            "notes":        (data.get("body") or "").strip(),
            "published_at": data.get("published_at") or "",
            "url":          data.get("html_url") or "",
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
# Update state — persisted to disk so it survives the dashboard restart.
#
# phases: idle | starting | downloading | installing | restarting | done | error
# ---------------------------------------------------------------------------
_IDLE_STATE = {
    "running":     False,
    "phase":       "idle",
    "error":       None,
    "started_at":  None,
    "finished_at": None,
    "version":     None,   # version being applied / last applied
    "from_version": None,  # version we're upgrading from
}
_state_lock = threading.Lock()


def get_update_state() -> dict:
    """Read the current update state from disk (single source of truth)."""
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        return dict(_IDLE_STATE)

    # Self-heal a crashed/stale run so the UI doesn't hang on "running" forever.
    if st.get("running") and st.get("started_at"):
        try:
            started = datetime.fromisoformat(st["started_at"])
            age = (datetime.now(timezone.utc) - started).total_seconds()
            if age > STALE_AFTER_S:
                st["running"] = False
                st["phase"]   = "error"
                st["error"]   = st.get("error") or "Update timed out"
        except Exception:
            pass
    return st


def _set_state(**kwargs):
    """Merge kwargs into the on-disk state file atomically."""
    with _state_lock:
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
        except Exception:
            st = dict(_IDLE_STATE)
        st.update(kwargs)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        except Exception as exc:
            log.warning(f"UPDATE  could not write state file: {exc}")
    return st


# ---------------------------------------------------------------------------
# Apply update
# ---------------------------------------------------------------------------

def _restart_service(svc: str):
    """Restart a systemd service. Requires the passwordless-sudo rule."""
    subprocess.run(
        ["sudo", "systemctl", "restart", svc],
        capture_output=True, timeout=30,
    )
    log.info(f"UPDATE  restarted {svc}")


def do_update(tag: str = None) -> tuple:
    """
    Download app files for `tag` (defaults to latest) and restart services.
    Runs synchronously in a dedicated process/thread; progress is written to
    the on-disk state file as it goes. Returns (success, message).
    """
    import requests

    from_version = get_current_version()
    try:
        if tag is None:
            release = get_latest_release()
            if not release:
                _set_state(running=False, phase="error",
                           error="Could not fetch latest release from GitHub",
                           finished_at=_now())
                return False, "Could not fetch latest release from GitHub"
            tag = release["tag"]

        _set_state(running=True, phase="downloading", version=tag,
                   from_version=from_version, error=None,
                   started_at=_now(), finished_at=None)
        log.info(f"UPDATE  downloading {tag}...")

        base = f"{RAW_BASE}/{tag}/app"

        # Download everything to a temp dir first; only touch the install dir
        # once all files are safely on disk (avoids a half-applied update).
        with tempfile.TemporaryDirectory() as tmp:
            for fname in APP_FILES:
                url = f"{base}/{fname}"
                r = requests.get(url, timeout=30)
                if r.status_code == 404:
                    log.debug(f"UPDATE  {fname} not in release — skipping")
                    continue
                r.raise_for_status()
                with open(os.path.join(tmp, fname), "wb") as f:
                    f.write(r.content)
                log.info(f"UPDATE  downloaded {fname}")

            _set_state(phase="installing")
            for fname in os.listdir(tmp):
                src  = os.path.join(tmp, fname)
                dest = os.path.join(INSTALL_DIR, fname)
                with open(src, "rb") as sf, open(dest, "wb") as df:
                    df.write(sf.read())
                log.info(f"UPDATE  installed {fname}")

        # Record the new version.
        set_current_version(tag)

        # Persist the success result (read by /api/update across restarts).
        try:
            with open(RESULT_FILE, "w") as f:
                json.dump({"tag": tag, "ts": _now(), "ok": True}, f)
        except Exception:
            pass

        # Notify BEFORE any restart — the dashboard restart below tears this
        # process down, so anything after it is not guaranteed to run.
        try:
            import notifications as notif
            notif.notify(notif.EVENT_UPDATE_INSTALLED,
                         f"PumpSleeper updated to {tag}")
            log.info("UPDATE  sent update-installed notification")
        except Exception as exc:
            log.warning(f"UPDATE  notification failed: {exc}")

        # Mark done on disk BEFORE restarting so the UI sees a clean
        # completion even though the dashboard process is about to die.
        _set_state(running=False, phase="done", error=None, finished_at=_now())

        # Restart services: server first, dashboard last (dashboard restart
        # may kill this process, but all state above is already persisted).
        _set_state(phase="restarting")
        log.info("UPDATE  restarting services...")
        _restart_service("pumpsleeper")
        # Re-assert done so a poll landing here still sees the terminal state.
        _set_state(running=False, phase="done", finished_at=_now())
        log.info(f"UPDATE  updated to {tag} — restarting dashboard now")
        _restart_service("pumpsleeper-dashboard")  # may terminate this process

        return True, f"Updated to {tag}"

    except Exception as exc:
        _set_state(running=False, phase="error", error=str(exc),
                   finished_at=_now())
        log.error(f"UPDATE  failed: {exc}")
        return False, str(exc)


def trigger_update(tag: str = None) -> tuple:
    """
    Launch an update in a DETACHED child process and return immediately.
    The child runs `updater.py --apply [tag]` in its own session, writing
    progress to the on-disk state file. Poll get_update_state() for progress.
    """
    st = get_update_state()
    if st.get("running"):
        return False, "Update already in progress"

    # Seed the state so the UI shows progress instantly, before the child
    # process has had a chance to start writing.
    _set_state(running=True, phase="starting", error=None,
               version=tag, from_version=get_current_version(),
               started_at=_now(), finished_at=None)

    updater_path = os.path.join(INSTALL_DIR, "updater.py")
    cmd = [sys.executable, updater_path, "--apply"]
    if tag:
        cmd.append(tag)

    try:
        logf = open(os.path.join(DATA_DIR, "update.log"), "ab")
    except Exception:
        logf = subprocess.DEVNULL

    try:
        subprocess.Popen(
            cmd,
            cwd=INSTALL_DIR,
            stdout=logf,
            stderr=logf,
            start_new_session=True,   # detach from the dashboard's process group
            close_fds=True,
        )
    except Exception as exc:
        _set_state(running=False, phase="error",
                   error=f"Could not start updater: {exc}", finished_at=_now())
        return False, str(exc)

    return True, "Update started"


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def _auto_update_main():
    """Nightly auto-update path (run by pumpsleeper-update.service)."""
    latest = get_latest_release()
    current = get_current_version()
    avail = latest and latest["tag"] != current and current != "unknown"

    if not avail:
        log.info("UPDATE  already up to date")
    elif not get_auto_update():
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "--apply":
        # Detached manual-update worker launched by trigger_update().
        _tag = sys.argv[2] if len(sys.argv) > 2 else None
        ok, msg = do_update(_tag)
        log.info(f"UPDATE  apply {'OK' if ok else 'FAILED'}: {msg}")
    else:
        _auto_update_main()
