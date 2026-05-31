# Changelog

All notable changes to PumpSleeper are documented here.

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
