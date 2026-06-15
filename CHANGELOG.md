# Changelog

All notable changes to PumpSleeper are documented here.

---

## [v4.6] — 2026-06-14

### New
- **Compact layout.** A new two-column dashboard view optimised for at-a-glance monitoring. Toggle between Detailed and Compact using the pill in the header — your preference is saved to your account and restored on every load. Compact shows a status card (device online/offline, runs today, estimated gallons, longest run, last run detail), a full pump run history table, a device card (model, IP, signal strength with bar indicator, water sensor, mode), and a notifications card (email/ntfy on/off, last sent time). The "Today" toggle in the history header filters to today's runs client-side without a page reload. Long-run rows (>45 s) are highlighted in red.

### Improved
- **Notification last-sent timestamp.** Every successful notification send (email or ntfy) now records a timestamp in the database, visible in the Compact layout's notifications card.
- **Refresh indicator.** The header briefly shows "↻ Refreshing…" while data is loading.

---

## [v4.5] — 2026-06-14

### Improved
- **"Save Logs" now downloads a zip of all three log files.** Previously the button downloaded a single text file from `journalctl`, which missed almost everything useful — the service writes to flat files, not the journal. The zip now contains `server.log` (proxy events, pump runs, real-event notifications), `dashboard.log` (test notification results, settings changes, API errors), and `journal.log` (service start/stop, crashes). ANSI color codes are stripped from the flat logs. This makes it straightforward to diagnose notification issues: open `dashboard.log` and search for `NOTIF`.

---

## [v4.4] — 2026-06-13

### Fixed
- **Ntfy notifications failed when the title contained emoji** (e.g. the ⚠ high-water alert). The header-based ntfy API encodes header values as latin-1, which can't represent characters outside that range. Switched to the ntfy JSON body API, which is fully UTF-8 and handles emoji and any non-ASCII characters in both title and message.

---

## [v4.3] — 2026-06-13

### Fixed
- **Gmail (and strict SMTP servers) rejected email notifications.** The SMTP handshake was missing a second `EHLO` after the TLS upgrade (`STARTTLS`). The correct sequence is `EHLO → STARTTLS → EHLO → LOGIN`; skipping the post-TLS EHLO causes some servers to refuse login. Fixed in both the notification sender and the backup email sender.

---

## [v4.2] — 2026-06-13

### Fixed
- **Proxy forwarding used the wrong pumpspy.com server.** The proxy was hardcoded to `206.80.104.221`, but the SO1000 device actually connects to `173.241.229.38` (verified via packet capture). Requests were going to a different backend with no session context for the device, so `rht_parameters` returned a config that didn't trigger cycle mode and `/rht_outlet_cycles` was never sent. The proxy now derives the target from the device's own `Host` header (`www.pumpspy.com:8081`) so it follows pumpspy.com regardless of IP changes.
- **Malformed proxy URL due to trailing space in Host header.** The device sends `Host: www.pumpspy.com:8081 ` with a trailing space; the proxy was concatenating it directly into the URL, producing `http://www.pumpspy.com:8081 /path` which failed to parse. The Host value is now stripped before use.

---

## [v4.1] — 2026-06-13

### New
- **PumpSpy SO1000 smart outlet support.** PumpSleeper now fully supports the PumpSpy smart outlet alongside the original backup pump system. Pump runs (`POST /rht_outlet_cycles` — duration and motor current), the water sensor (alert type 1004, trigger and clear), and the config poll (`GET /rht_parameters`) are all parsed, logged to the dashboard, published to MQTT/Home Assistant, and forwarded to pumpspy.com in proxy mode so the official app stays in sync. Works in both proxy and takeover modes.
- **Device selector in Settings.** A new "PumpSpy Device" card lets you pick which device is connected: *PumpSpy backup pump system* (default) or *PumpSpy smart outlet*. The smart-outlet endpoints answer locally only when selected; backup-pump behavior is completely unchanged. The choice is included in settings backups.
- **Water Sensor status on the dashboard.** With the smart outlet selected, the "Backup Runs" stat (not applicable to the outlet) is replaced by a live Water Sensor status — green *Dry* / red *HIGH* with the time of the last change. A water-sensor trigger also fires the existing high-water notification (email/ntfy) and MQTT state.

### Fixed
- **Smart outlet was blocked from reporting anything while proxied.** The SO1000 declares a `Content-Length` on its config poll but never sends a body (firmware quirk); the proxy waited for the phantom body and answered `400` every cycle, so the device never received its configuration (`cycle_data: 1`) and never reported pump runs — to PumpSleeper *or* to pumpspy.com. The config poll is now answered instantly without touching the request body, exactly like the real server does (verified against a packet capture of the device talking directly to the PumpSpy cloud).

### Changed
- **Dependencies are now baked into the image — first boot is "flash and go."** The build pipeline installs the Python/iptables/tcpdump/NetworkManager packages and cloudflared into the image at build time (arm64 chroot), so first boot no longer runs `apt`, downloads nothing heavy, and skips the long memory-intensive install that destabilized the Pi Zero 2 W. This removes the whole class of first-boot problems at once (the OOM/reset loop, the clock→apt-signature failures, the 3–5 minute install). First boot now just applies your config (hotspot, Wi-Fi country, password), brings up the hotspot, and starts — in seconds. Confirmed on real hardware.
- **cloudflared is matched to the board.** The image bakes the Zero-2-W-safe `2025.2.0` build; on more capable boards (e.g. Pi 4) first boot upgrades it to the latest build, best-effort (a board with no internet keeps the working baked build).

### Fixed
- **Network packet capture now actually runs.** The capture helper started `tcpdump` as a plain background process, which Trixie's `sudo` (1.9) killed the moment the helper returned — so no packets were ever captured. It now launches `tcpdump` as a transient systemd unit (survives `sudo` exiting) and writes to a fixed filename (the old `-C/-W` rotation appended a numeric suffix the download never read).
- **Honest capture-download message.** When a capture produced no `.pcap`, the bundled summary always blamed a missing tcpdump. It now reports the real reason: helper not installed, no device connected to the hotspot, or no packets captured.

### Notes
- firstboot keeps its install steps as a **self-heal fallback** — if a baked dependency is ever missing, it still installs it on-device, so the image degrades gracefully rather than bricking.
- Existing installs can get the capture-helper fix without reflashing via `scripts/enable-netcapture.sh`.

---

## [v3.14] — 2026-06-11

### Fixed
- **First-boot install loop, take two.** v3.12 reordered the final steps but the Zero 2 W's intermittent reset still slipped into the gap between `systemctl enable` and the config removal (those are slow D-Bus calls). firstboot now enables the services by writing the systemd `wants` symlinks directly — instant file operations — and removes the config immediately after, shrinking the vulnerable window to microseconds. All slow, reset-prone `systemctl` calls (`daemon-reload`, `start`) now run *after* the config is gone, so a reset at the tail of the install can no longer trigger a re-install.

---

## [v3.13] — 2026-06-11

### Changed
- **Leaner first-boot install.** Dropped `python3-pip` from the base package install — it pulled in the entire Python dev/build toolchain (`python3-dev`, `libpython3.13-dev`, `zlib1g-dev`, etc.) that's never used, since all Python dependencies are installed from apt. pip is still installed on demand only if the apt path ever fails. Fewer packages means a faster, lighter install — easier on the 512 MB Pi Zero 2 W.

---

## [v3.12] — 2026-06-11

### Fixed
- **First boot no longer loops if the Pi resets while services start.** firstboot now marks the install complete (disables itself and removes the config) right after *enabling* the services and before *starting* them, so an intermittent hard-reset during startup can't trigger a re-install — everything the system needs is already in place, and the enabled services simply come up on the next boot. Turns the Pi Zero 2 W's "eventually installs after a few reboots" into a first-pass completion.
- **Swap is now provisioned reliably on first boot.** The earlier swap step keyed on `dphys-swapfile`, which newer Pi OS images don't ship (they use RAM-backed `zram` that gives no real headroom). firstboot now creates a real 2 GB disk-backed swapfile directly, so the 512 MB Zero 2 W has genuine backing store for the memory-heavy install.

---

## [v3.11] — 2026-06-11

### New
- **Capture pump traffic now includes a full network capture.** The Debug → *Capture pump traffic* checkbox previously logged only the requests that reached the proxy on port 8081. It now also runs a `tcpdump` of *all* the device's traffic, so pump events a device sends on other ports/endpoints (which the proxy never sees) are visible. Unticking downloads a zip with the app-level log, the `.pcap`, and a connection summary listing every host:port the device contacted. Built into fresh images; existing installs can turn it on with `scripts/enable-netcapture.sh` (no reflash).

---

## [v3.10] — 2026-06-10

### Fixed
- **Dashboard kept showing the old version after a successful update.** On existing installs the `VERSION` file was owned by root (firstboot wrote it after chowning the install dir), so the auto-updater — which runs as the service user — couldn't rewrite it; the update applied but the version label never changed. The updater now recreates the `VERSION` file instead of overwriting it in place, which **self-heals existing installs through a normal update (no reflash needed)**, and firstboot now hands the file to the service user on fresh images.

---

## [v3.9] — 2026-06-09

### Fixed
- **Update check reported "Latest: unavailable" when a release had empty notes.** The GitHub API returns the release body as `null` when it's blank (e.g. right after publishing, before the build fills in the notes), and the updater's `data.get("body", "").strip()` crashed on the `None` — failing the whole check. It now tolerates empty/missing fields so the latest version always shows.

---

## [v3.8] — 2026-06-09

### New
- **Capture pump traffic (Debug).** A new *Capture pump traffic* checkbox sits beside Save Log in Settings → Debug. Tick it to record every message your PumpSpy device sends — the full request (headers + raw body) and the response — and untick it to stop and download the log. It works in **both proxy and takeover mode**, and records **all** traffic, including endpoints PumpSleeper doesn't normally handle, so a device whose messages differ from what the parser expects shows up clearly. Each captured transaction is stamped with the current mode. The log is cleared after you download it (so each session starts fresh), and capture resets off whenever the server restarts so it never runs unattended.

---

## [v3.7] — 2026-06-05

### Fixed
- **WiFi hotspot crashed the Pi Zero 2 W on first boot.** The hotspot was created without a fixed band/channel, so NetworkManager let the driver auto‑select — and the Zero 2 W's Broadcom WiFi firmware (`brcmfmac`) hard‑resets the board the instant the AP comes up that way. This was the root cause of the looping first‑boot install (the Pi rebooted at the "Configuring WiFi hotspot" step before the install could finish). The hotspot is now pinned to **2.4 GHz, channel 6**, which the Zero 2 W handles cleanly.
- **Broken `apt` on first boot due to a wrong clock.** The Pi has no real‑time clock, so at first boot it runs at the image's build date. Debian Trixie's `apt` verifies repo signatures with `sqv`, which rejects signatures that aren't "live yet" relative to the clock — so a clock in the past failed every repo fetch (stale index / 404s) and the Python dependencies never installed. Firstboot now syncs the clock (HTTP date + NTP) **before** installing packages.

### Improved
- **Swap on the Pi Zero 2 W.** The 512 MB board can run out of memory during a large `apt` transaction. Firstboot now ensures at least ~1 GB of swap before installing packages, so the install can't OOM‑reset the board.
- **Watchdog now actually disabled during the install.** Firstboot wrote the `RuntimeWatchdogSec=0` drop‑in but never re‑read it into the running systemd, so the 1‑minute watchdog stayed armed for the first boot. It now runs `systemctl daemon-reexec` to apply it immediately.

---

## [v3.5] — 2026-06-05

### Fixed
- Disabled the systemd hardware watchdog. Raspberry Pi OS Trixie arms a 1‑minute watchdog, and on slow/low‑RAM Pis (e.g. the Zero 2 W) the heavy first‑boot install could starve systemd enough to miss the watchdog ping — hard‑resetting the Pi and causing a failed/looping install. It's now disabled in the image (so the first boot is safe) and by firstboot for subsequent boots.

---

## [v3.4] — 2026-06-05

### Fixed
- First boot's cloudflared "is it complete?" check now requires the binary to be at least ~25 MB (a full cloudflared is ~35 MB). The previous 10 MB threshold let a **truncated download** (e.g. a partial 16 MB file) pass as installed, leaving a cloudflared that won't run — which broke web access on the Pi Zero 2 W.

---

## [v3.3] — 2026-06-05

### Fixed
- `cloudflared` now installs reliably on the **Pi Zero 2 W**. Newer cloudflared builds segfault on that board — even `cloudflared --version` — which made the web-access toggle and the named tunnel fail to start. First boot now installs a known-good older cloudflared (2025.2.0) on the Zero 2 W and the latest version everywhere else, and validates the download by file size rather than executing the binary during the memory-tight first boot.

---

## [v3.2] — 2026-06-05

### Fixed
- First boot now installs the Python dependencies (Flask, Waitress, Requests) from Debian packages and **verifies Flask is importable** before reporting success. Previously it relied on `pip3`, which isn't reliably present on Raspberry Pi OS Trixie — so the step silently did nothing and the dashboard failed to start with "No module named 'flask'." It now falls back to pip if needed and prints a clear error if the dependencies can't be installed, instead of a false "Done."
- First boot's `cloudflared` install is now reliable: it retries the download (with a longer timeout) and **verifies the binary actually runs** before declaring success, removing a partial download instead of leaving a broken file. Fixes the dashboard reporting "cloudflared not installed" when choosing the web-access option after a fresh install.
- The `pumpsleeper` hostname now sticks across reboots — first boot tells cloud-init (used by Pi OS Trixie) to preserve the hostname, so it's no longer reverted to `raspberrypi` on later boots.

---

## [v3.1] — 2026-06-05

### Added
- `WIFI_COUNTRY` setting (default `US`) in `pumpsleeper.conf`. First boot now sets the Wi-Fi regulatory country and unblocks the radio **before** starting the hotspot — so the PumpSpyLab hotspot comes up reliably on a fresh install instead of staying rfkill-blocked until you set the country by hand.

### Changed
- Setup guide updated for the ethernet-first behavior: `HOME_WIFI_*` is now marked optional (only needed without ethernet), the hotspot is clarified as the PumpSpy device's network, and the `pumpsleeper.local` address is shown.

---

## [v3.0] — 2026-06-05

### Changed
- Removed the experimental USB Wi-Fi auto-configuration from the installer (added in v2.7). It was unstable on the Pi Zero 2 W, so we're back to a simple, reliable baseline: the built-in radio runs the PumpSpyLab hotspot and internet comes from ethernet.
- Pinned the image base to the known-good Raspberry Pi OS **Trixie** release (2026-04-21) that earlier working builds used, reverting the v2.9 Bookworm pin — the instability wasn't the OS. The base is now pinned to a fixed version so builds are reproducible.

### Improved
- First boot now prefers a wired (ethernet) connection and only brings up Wi-Fi for the install if no wired internet is detected — it no longer spins up Wi-Fi when ethernet is already connected.

---

## [v2.9] — 2026-06-05

### Fixed
- Pinned the Pi image to the last Raspberry Pi OS **Bookworm** release. The image build was silently pulling "latest," which had moved to **Trixie** (Debian 13) and boot-looped on the Pi Zero 2 W. Images are now built on a fixed, known-good base for reproducibility.

---

## [v2.8] — 2026-06-02

### New
- Settings backup & restore — Settings → Backup & Restore lets you download your configuration (notifications, login, web access, theme — including saved credentials) as a file and restore it after reimaging the SD card. Restores are version-aware: only known settings are applied, with a count of what was restored vs skipped and a warning if the backup came from a newer version. You can also have PumpSleeper email you a backup automatically each week.

---

## [v2.7] — 2026-06-02

### New
- Optional USB Wi-Fi internet — if a USB Wi-Fi adapter is plugged into the Pi, the installer now auto-detects it and joins your home Wi-Fi on that adapter for internet, while the built-in radio keeps running the PumpSpyLab hotspot. This lets a Pi Zero 2 W get online (and auto-update) without an ethernet adapter. It reuses your existing home-Wi-Fi settings and falls back to ethernet / hotspot-only when no adapter is present. (Applies to newly flashed images.)
- The dashboard's device card now shows a "Pi Internet" row indicating how the Pi itself is reaching the internet — Ethernet, USB Wi‑Fi, or "No internet" — so you can confirm the active uplink at a glance (handy after swapping a USB dongle)

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
