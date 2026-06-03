# Changelog

All notable changes to PumpSleeper are documented here.

---

## [v2.6] — 2026-06-02

### Changed
- Clearer device-card labels: the "Hotspot" status row is now **Pump to Raspberry Pi**, and the **Cycle Raspberry Pi Hotspot** button (was "Cycle Hotspot")

### Fixed
- Backup battery voltage is sourced from the most recent backup-pump run again (the 12 V value, matching the Pump Run History). This reverts the v2.5 attempt to read it from routine pings, which is a different, lower-voltage measurement

---

## [v2.5] — 2026-06-02

### Improved
- The dashboard now keeps you signed in across browser restarts — the login uses a 30-day sliding session (refreshed each time you use it) instead of logging you out when the browser closes the tab

### Fixed
- Backup battery voltage now shows the latest value from the device's routine pings, instead of staying blank until the first backup-pump run
- The "Updated" timestamp no longer briefly reads a negative time (e.g. "-1s ago") when the Pi's clock is slightly ahead of the browser; it now reads "just now"

---

## [v2.4] — 2026-06-01

### New
- Bring-your-own Cloudflare tunnel — Settings → Security now lets you choose between the zero-config **Quick tunnel** and **your own Cloudflare tunnel**. Paste your tunnel token and public hostname to expose the dashboard at a stable address on your own domain (instead of a random URL that changes on every restart)

---

## [v2.3] — 2026-06-01

### Improved
- Notification settings now save automatically as you edit them — the Save button is gone, and the Test buttons always use your latest values
- New installs set the hostname to `pumpsleeper`, so the dashboard is reachable at `http://pumpsleeper.local:8080`

---

## [v2.2] — 2026-06-01

### New
- Forgot Password on the login screen — sends a single-use reset link (valid for 30 minutes) over your configured email and/or ntfy channels, letting you set a new password without being locked out
- Notifications now include a tap-through link to the dashboard — an "Open Dashboard" button in email, and a tap action plus button in ntfy. The link uses the public web-access URL when the Cloudflare tunnel is on, otherwise the local address

### Improved
- Settings layout: the Security & Web Access and Updates cards now sit side by side on desktop for better use of space

---

## [v2.1] — 2026-06-01

### New
- Dashboard login — a single-user sign-in is now required on every visit. The default login is `admin / admin`; you'll be prompted to change it
- Settings → Security: rename the dashboard user and change the password (via a Change Password dialog), with repeated-failure lockout protecting the login
- Web access toggle — expose the dashboard over the internet on a public HTTPS address using a Cloudflare quick tunnel, with no Cloudflare account required. The generated URL is shown in Settings and the switch stays locked until you change the default password
- The current web-access URL is sent to you over email/ntfy whenever it changes (the address is regenerated on every restart)
- Save Log button in Settings → Debug — downloads the last 2000 log lines from both services as a text file to help with troubleshooting

### Improved
- The installer now sets up `cloudflared` and grants the service account access to the system journal automatically, so web access and the log export work out of the box on new installs

---

## [v2.0] — 2026-06-01

### New
- Redesigned dashboard header: a consolidated "Pump Activity · Today" card showing main runs, backup runs, and total gallons, with the operating-status indicator folded in as a colored pill and a quiet device-health footer
- "PumpSpy Device" connection card with clear labeled rows — Routed To, Device IP, Hotspot, Last contact
- Pump Run History and Signal Strength moved into in-page sub-tabs on the Dashboard, giving Pump Run History the bulk of the screen
- Pump Run History date filter now defaults to today on load and persists across tabs

### Improved
- Pump Run History columns reordered (Run Date, Pump, Duration, Est. Gallons, Current, Batt V, Loaded V) and the State column removed
- On phones, Pump Run History is now a real table: Run Date, Pump, Duration, Est. Gallons in portrait, with the remaining columns shown in landscape
- Signal strength and backup battery de-emphasized into a secondary device-health readout
- Unhandled Requests moved to a collapsed Debug section at the bottom of Settings

---

## [v1.9] — 2026-05-31

### Fixed
- Dashboard failed to load on v1.8 — a regex in the new release-notes renderer was mangled into an invalid pattern, which halted all dashboard JavaScript. Hotfix restores the dashboard

---

## [v1.8] — 2026-05-31

### New
- Appearance setting with Auto / Dark / Light theme — stored on the server and kept separately for the desktop and mobile layouts; Auto follows the device's OS light/dark preference
- Release-notes preview — when an update is available, the dashboard shows a "What's new in <version>" summary of the next release's notes before you apply it

### Fixed
- Update status now progresses reliably from start to finish: progress is persisted to disk so it survives the dashboard restart, and the "update installed" notification is sent before the restart so it actually fires
- Dashboard now shows a clean completion (and auto-reloads onto the new version) instead of stalling mid-update

### Improved
- Manual updates run in a detached process, decoupled from the dashboard service that restarts during the update

---

## [v1.4] — 2026-05-31

### New
- Notification triggers for new version available and new version installed
- Updates section moved to top of Settings tab for easier access
- Update progress now shown in the dashboard UI with live status during download and restart
- Last update result persisted across service restarts so the UI always reflects what happened

### Improved
- Auto-update disabled but update available now sends a notification prompting user to update manually
- Settings page redesigned with two-column layout on desktop, single column on mobile
- Notification trigger checkboxes use a flowing grid layout on wider screens

---

## [v1.1] — 2026-05-31

### New
- Email (SMTP) and ntfy push notifications for backup pump runs, main pump runs, high water alerts, and device offline
- Settings tab in dashboard to configure notifications, with per-event toggles and test buttons
- Self-update system — check for updates and apply from the dashboard; optional nightly auto-update
- Pre-built Raspberry Pi image — flash, edit one config file, boot, and PumpSleeper installs itself automatically
- App files baked into the image at build time — no internet required during firstboot for app installation
- Device IP display and real-time WiFi hotspot presence check in dashboard
- Hotspot cycle button — reconnect a stuck PumpSpy device without SSH
- VERSION file tracking so the dashboard always knows what release is installed

### Improved
- Online/offline status now uses hotspot presence check to avoid false "Online" readings
- Hotspot cycle detects whether device came back online, WiFi-only, or unreachable after cycle
- nmcli errors during hotspot cycle now surface immediately in the UI instead of silently failing
- Sudoers rule for hotspot cycling now included in installer and pre-built image
- iptables-persistent no longer prompts during firstboot install
- IP forwarding now written to `/etc/sysctl.d/` for Bookworm compatibility
- pip warnings suppressed during firstboot package install

### Fixed
- False "Online" status at the 10-minute ping stale boundary when hotspot checker confirmed device was gone
- WiFi connection during firstboot now uses connection profile instead of direct connect, fixing "SSID not found" error on early boot
- App file download 404 errors — files are now baked into the image rather than downloaded at runtime

---

## [v1.0] — 2026-05-20

### New
- Transparent HTTP proxy intercepting PumpSpy device traffic
- Takeover mode — answer device locally when cloud is unreachable
- Local web dashboard with pump run history, signal strength, battery voltage, and today's stats
- SQLite event storage with WAL mode for concurrent reads
- Home Assistant integration — 19 MQTT entities with auto-discovery
- Custom Lovelace card for Home Assistant
- Proxy ↔ Takeover mode toggle from dashboard and HA card
- Auth failure detection with banner prompt to switch to Takeover mode
- Backup pump run detection with duration, gallons, current, battery voltage
- Main pump run detection from outlet current alerts
- Signal strength history chart
- Pi installer script (`install.sh`) with interactive setup
- Docker Compose deployment option
