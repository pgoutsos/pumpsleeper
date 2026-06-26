#!/usr/bin/env bash
# =============================================================================
#  PumpSleeper — First Boot Installer
#  Runs automatically on first boot from the systemd firstboot service.
#  Reads config from /boot/firmware/pumpsleeper.conf (FAT32 boot partition).
#  Logs to /boot/firmware/pumpsleeper-install.log so you can check progress
#  by reading that file from any Mac or PC after install.
# =============================================================================

set -uo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
BOOT_DIR="/boot/firmware"
[[ -d "$BOOT_DIR" ]] || BOOT_DIR="/boot"

CONF_FILE="$BOOT_DIR/pumpsleeper.conf"
LOG_FILE="$BOOT_DIR/pumpsleeper-install.log"
INSTALL_DIR="/opt/pumpsleeper"
RUN_USER="pumpsleeper"
WIFI_IFACE="wlan0"
SERVER_PORT=8081
DASHBOARD_PORT=8080

# ── Redirect all output to log file ──────────────────────────────────────────
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "============================================"
echo " PumpSleeper First Boot Installer"
echo " $(date)"
echo "============================================"
echo ""

# ── Strip Windows line endings from config ────────────────────────────────────
sed -i 's/\r//' "$CONF_FILE" 2>/dev/null || true

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: $CONF_FILE not found. Cannot continue."
    exit 1
fi

source "$CONF_FILE"

HOME_WIFI_SSID="${HOME_WIFI_SSID:-}"
HOME_WIFI_PASS="${HOME_WIFI_PASS:-}"
SSH_PASS="${SSH_PASS:-pumpspy}"
HOTSPOT_SSID="${HOTSPOT_SSID:-PumpSpyLab}"
HOTSPOT_PASS="${HOTSPOT_PASS:-pumpspy123}"
HOTSPOT_IP="${HOTSPOT_IP:-192.168.50.1}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"
MQTT_HOST="${MQTT_HOST:-}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_USER="${MQTT_USER:-}"
MQTT_PASS="${MQTT_PASS:-}"

echo "Config loaded:"
echo "  Home WiFi    : ${HOME_WIFI_SSID:-not set}"
echo "  Hotspot SSID : $HOTSPOT_SSID"
echo "  Hotspot IP   : $HOTSPOT_IP"
echo "  Wi-Fi country: $WIFI_COUNTRY"
echo "  MQTT host    : ${MQTT_HOST:-disabled}"
echo ""

# ── Disable the systemd hardware watchdog ─────────────────────────────────────
# Pi OS Trixie arms a 1-minute watchdog that can hard-reset slow/low-RAM Pis
# (Zero 2 W) mid-install. The image disables it for the very first boot; this
# persists it for later boots and for install.sh-based installs. Named 99- so it
# sorts AFTER Pi OS's own /usr/lib/.../40-rpi-enable-watchdog.conf (last wins).
mkdir -p /etc/systemd/system.conf.d
rm -f /etc/systemd/system.conf.d/10-disable-watchdog.conf
printf '[Manager]\nRuntimeWatchdogSec=0\n' > /etc/systemd/system.conf.d/99-disable-watchdog.conf
# Apply to the ALREADY-RUNNING systemd now (a drop-in alone only takes effect on
# the next boot — re-exec makes PID 1 re-read it so the watchdog is off for THIS
# install, not just future boots).
systemctl daemon-reexec 2>/dev/null || true

# ── Change default SSH password ───────────────────────────────────────────────
echo "[1/8] Setting login password..."
PASS_HASH=$(echo "$SSH_PASS" | openssl passwd -6 -stdin)
usermod -p "$PASS_HASH" pumpsleeper 2>/dev/null \
    || echo "      WARNING: Could not set password now — will retry after boot."
echo "      Done."

# ── Wi-Fi country (regulatory domain) ─────────────────────────────────────────
# The Pi's Wi-Fi radio is rfkill-blocked until a country is set, which would stop
# the PumpSpyLab hotspot from starting. Set it before any Wi-Fi/hotspot use.
echo "[1b/8] Setting Wi-Fi country to ${WIFI_COUNTRY}..."
raspi-config nonint do_wifi_country "$WIFI_COUNTRY" 2>/dev/null \
    || iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
rfkill unblock wifi 2>/dev/null || true
echo "      Done."

# ── Connect to home WiFi ──────────────────────────────────────────────────────
echo "[2/8] Setting up internet for install..."
if curl -fsSL --max-time 5 http://detectportal.firefox.com > /dev/null 2>&1; then
    echo "      Internet already available (ethernet) — skipping Wi-Fi."
elif [[ -z "$HOME_WIFI_SSID" ]]; then
    echo "      WARNING: no wired internet and HOME_WIFI_SSID not set in pumpsleeper.conf."
    echo "      Install will fail when downloading files."
else
    echo "      No wired internet detected — connecting to home Wi-Fi..."
    # Ensure WiFi radio is on and managed, then force a fresh scan
    nmcli radio wifi on 2>/dev/null || true
    nmcli dev set "$WIFI_IFACE" managed yes 2>/dev/null || true
    echo "      Scanning for networks..."
    nmcli dev wifi rescan ifname "$WIFI_IFACE" 2>/dev/null || true
    sleep 10   # give the scan time to complete

    # Add a connection profile rather than using 'connect' — this works
    # even if the SSID isn't visible in the scan yet
    nmcli con delete "HomeWiFi" 2>/dev/null || true
    nmcli con add type wifi ifname "$WIFI_IFACE" con-name "HomeWiFi" \
        ssid "$HOME_WIFI_SSID" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$HOME_WIFI_PASS" \
        connection.autoconnect no 2>&1
    nmcli con up "HomeWiFi" ifname "$WIFI_IFACE" 2>&1 || true

    echo "      Waiting for network (needed for apt-get)..."
    for i in $(seq 1 30); do
        sleep 3
        if curl -fsSL --max-time 5 http://detectportal.firefox.com > /dev/null 2>&1; then
            echo "      Network ready."
            break
        fi
        if [[ $i -eq 30 ]]; then
            echo "      WARNING: Could not confirm internet after 90 seconds — continuing anyway."
            echo "      Package installation may fail if network is unavailable."
        fi
    done
fi

# ── Clock sync (the Pi has no real-time clock) ────────────────────────────────
# At first boot the clock is the image's build date. Debian Trixie's apt verifies
# repo signatures with sqv, which rejects signatures that aren't "live yet"
# relative to the system clock — so a clock in the past breaks apt entirely
# (404s / stale index). Set the real time BEFORE installing anything.
echo "[2b/8] Syncing clock (no RTC on the Pi)..."
timedatectl set-ntp true 2>/dev/null || true
# Fast + reliable: set from an HTTP Date header; NTP can refine afterward.
HTTP_DATE=$(curl -sI --max-time 15 http://deb.debian.org 2>/dev/null \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="date"{print $2; exit}')
if [ -n "$HTTP_DATE" ]; then
    date -s "$HTTP_DATE" >/dev/null 2>&1 && echo "      Clock set from network."
fi
# Give NTP a few seconds to confirm/refine (best effort).
for _ in $(seq 1 15); do
    [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ] && break
    sleep 1
done
echo "      Clock now: $(date)"

# ── Swap (the Zero 2 W has only 512 MB RAM) ──────────────────────────────────
# A large apt transaction can exhaust 512 MB and OOM-reset the board mid-install.
# Make sure there's at least ~1 GB of swap before the package install. (1 GB is
# plenty for the install and easier on the SD card than 2 GB; bump CONF_SWAPSIZE
# higher here if you want more.)
echo "[2c/8] Ensuring swap space..."
# The 512 MB Zero 2 W OOM-resets during the memory-heavy install without real
# swap. Pi OS images vary: some use zram (RAM-backed — does NOT relieve true
# memory pressure), some dphys-swapfile, some neither. Rather than depend on any
# of them, create a dedicated DISK-backed swapfile and activate it. (The rootfs
# is resized on first boot, so there's plenty of SD space here.)
SWAPFILE=/var/swap-pumpsleeper
NEED_MB=2048
# Count only real (non-zram) swap already active.
DISK_SWAP_MB=$(awk 'NR>1 && $1 !~ /zram/ {s+=$3} END{print int(s/1024)}' /proc/swaps 2>/dev/null || echo 0)
if [ "${DISK_SWAP_MB:-0}" -lt 1024 ] && [ ! -f "$SWAPFILE" ]; then
    if fallocate -l "${NEED_MB}M" "$SWAPFILE" 2>/dev/null \
       || dd if=/dev/zero of="$SWAPFILE" bs=1M count="$NEED_MB" status=none 2>/dev/null; then
        chmod 600 "$SWAPFILE"
        mkswap "$SWAPFILE" >/dev/null 2>&1 || true
        swapon "$SWAPFILE" 2>/dev/null || true
        grep -q "$SWAPFILE" /etc/fstab 2>/dev/null || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
    fi
fi
echo "      Swap now: $(free -m | awk '/Swap/{print $2" MB"}')"

# ── System dependencies ───────────────────────────────────────────────────────
# These are baked into the image at build time (v4.0+), so on a normal flash this
# whole step is a no-op. We keep it as a self-heal fallback: if a dependency is
# somehow missing (older/partial image), firstboot still installs it on-device.
echo "[3/8] Installing system packages..."
if command -v nmcli >/dev/null 2>&1 && command -v iptables >/dev/null 2>&1 \
   && command -v tcpdump >/dev/null 2>&1 && dpkg -s iptables-persistent >/dev/null 2>&1; then
    echo "      Already present (baked into image) — skipping."
else
    # Pre-answer iptables-persistent prompts so they don't block the install
    echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
    echo iptables-persistent iptables-persistent/autosave_v6 boolean false | debconf-set-selections
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 \
        network-manager \
        iptables iptables-persistent \
        tcpdump \
        curl
    echo "      Done."
fi

# ── Hostname ──────────────────────────────────────────────────────────────────
echo "[3b/8] Setting hostname to pumpsleeper..."
hostnamectl set-hostname pumpsleeper 2>/dev/null || echo "pumpsleeper" > /etc/hostname
if grep -q "127.0.1.1" /etc/hosts; then
    sed -i "s/^127.0.1.1.*/127.0.1.1\tpumpsleeper/" /etc/hosts
else
    printf "127.0.1.1\tpumpsleeper\n" >> /etc/hosts
fi
# Newer Pi OS (Trixie) uses cloud-init, which re-applies the hostname on every
# boot and would revert ours. Tell it to leave the hostname alone.
if [ -f /etc/cloud/cloud.cfg ]; then
    if grep -q '^preserve_hostname:' /etc/cloud/cloud.cfg; then
        sed -i 's/^preserve_hostname:.*/preserve_hostname: true/' /etc/cloud/cloud.cfg
    else
        echo "preserve_hostname: true" >> /etc/cloud/cloud.cfg
    fi
fi
systemctl restart avahi-daemon 2>/dev/null || true
echo "      Reachable at pumpsleeper.local once mDNS settles."

# ── Python dependencies ───────────────────────────────────────────────────────
echo "[4/8] Installing Python packages..."
# Baked into the image at build time (v4.0+), so this is a no-op on a normal
# flash. Self-heal fallback: if the imports don't work yet (older/partial image),
# install from Debian packages on-device, then pip as a last resort.
if python3 -c "import flask, waitress, requests" 2>/dev/null; then
    echo "      Already present (baked into image) — skipping."
else
    PY_PKGS="python3-flask python3-waitress python3-requests"
    [[ -n "$MQTT_HOST" ]] && PY_PKGS="$PY_PKGS python3-paho-mqtt"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PY_PKGS
    if ! python3 -c "import flask, waitress, requests" 2>/dev/null; then
        echo "      apt path incomplete — trying pip fallback..."
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip 2>/dev/null || true
        pip3 install --break-system-packages --quiet flask waitress requests 2>/dev/null || true
        [[ -n "$MQTT_HOST" ]] && pip3 install --break-system-packages --quiet paho-mqtt 2>/dev/null || true
    fi
fi
# VERIFY regardless — never print a false "Done" if Flask can't be imported.
if python3 -c "import flask, waitress, requests" 2>/dev/null; then
    echo "      Done."
else
    echo "      ERROR: Python dependencies failed to install — the dashboard will NOT start."
    echo "      Fix after boot: sudo apt-get install -y python3-flask python3-waitress python3-requests"
fi

# ── cloudflared (optional web access via Cloudflare tunnel) ───────────────────
# The image bakes cloudflared 2025.2.0 — the known-good build for the Pi Zero 2 W
# (newer builds segfault on it). On more capable boards we upgrade to the latest
# build at first boot, best effort: a board with no internet just keeps the
# working baked build. Validate by SIZE, never by executing (running the ~35MB
# binary during a memory-tight boot can segfault a small Pi).
echo "[4b/8] Setting up cloudflared..."
CF_BIN=/usr/local/bin/cloudflared
cf_valid() { [ -f "$1" ] && [ "$(stat -c%s "$1" 2>/dev/null || echo 0)" -gt 25000000 ]; }
ARCH=$(dpkg --print-architecture)
case "$ARCH" in armhf) CF_ARCH=arm ;; *) CF_ARCH="$ARCH" ;; esac
PI_MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "")
case "$PI_MODEL" in
    *"Zero 2"*|*"Zero W"*|*"Pi Zero"*)
        # Small/low-RAM board — keep the baked known-good 2025.2.0. Only download
        # if it's somehow missing, and pin 2025.2.0 (latest segfaults here).
        if cf_valid "$CF_BIN"; then
            echo "      $PI_MODEL — keeping baked cloudflared 2025.2.0 (newer builds crash on this board)."
        else
            CF_URL="https://github.com/cloudflare/cloudflared/releases/download/2025.2.0/cloudflared-linux-${CF_ARCH}"
            for attempt in 1 2 3; do
                curl -fsSL --max-time 180 "$CF_URL" -o "$CF_BIN" && cf_valid "$CF_BIN" && break
                sleep 3
            done
            chmod +x "$CF_BIN" 2>/dev/null || true
            cf_valid "$CF_BIN" && echo "      cloudflared 2025.2.0 installed." \
                || echo "      WARNING: cloudflared missing — web access can be set up later."
        fi
        ;;
    *)
        # Capable board (Pi 4, etc.) — upgrade to the latest build, best effort.
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
        CF_TMP=/tmp/cloudflared.new
        if curl -fsSL --max-time 180 "$CF_URL" -o "$CF_TMP" && cf_valid "$CF_TMP"; then
            install -m 0755 "$CF_TMP" "$CF_BIN"
            echo "      Upgraded cloudflared to the latest build for ${PI_MODEL:-this board}."
        elif cf_valid "$CF_BIN"; then
            echo "      Could not fetch latest — keeping baked cloudflared 2025.2.0."
        else
            echo "      WARNING: cloudflared unavailable — web access can be set up later."
        fi
        rm -f "$CF_TMP"
        ;;
esac

# ── Service user ──────────────────────────────────────────────────────────────
echo "[5/8] Creating service user..."
if ! id "$RUN_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
fi
# Allow the service user to read the system journal so the dashboard's
# "Save Log" button can export journalctl output for both services.
usermod -aG systemd-journal "$RUN_USER" 2>/dev/null || true
echo "      Done."

# ── Install app files ─────────────────────────────────────────────────────────
echo "[6/8] Installing app files..."
mkdir -p "$INSTALL_DIR/data"
for f in server.py dashboard.py db.py mqtt.py notifications.py updater.py; do
    if [[ -f "/usr/local/lib/pumpsleeper/$f" ]]; then
        cp "/usr/local/lib/pumpsleeper/$f" "$INSTALL_DIR/$f"
        echo "      Installed $f"
    else
        echo "      WARNING: $f not found in image"
    fi
done

cat > "$INSTALL_DIR/pumpsleeper.env" <<EOF
PUMPSPY_DATA=$INSTALL_DIR/data
PUMPSLEEPER_MQTT_HOST=$MQTT_HOST
PUMPSLEEPER_MQTT_PORT=$MQTT_PORT
PUMPSLEEPER_MQTT_USER=$MQTT_USER
PUMPSLEEPER_MQTT_PASSWORD=$MQTT_PASS
PUMPSLEEPER_HOTSPOT_CON=PumpSleeper-Hotspot
PUMPSPY_WIFI_IFACE=$WIFI_IFACE
EOF
chmod 600 "$INSTALL_DIR/pumpsleeper.env"
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"
chmod 644 "$INSTALL_DIR"/*.py 2>/dev/null || true
chmod 644 "$INSTALL_DIR/VERSION" 2>/dev/null || true
echo "      Done."

# ── WiFi hotspot ──────────────────────────────────────────────────────────────
echo "[7/8] Configuring WiFi hotspot..."
nmcli con delete "PumpSleeper-Hotspot" 2>/dev/null || true
nmcli con add type wifi ifname "$WIFI_IFACE" con-name "PumpSleeper-Hotspot" \
    autoconnect yes ssid "$HOTSPOT_SSID" \
    mode ap \
    ipv4.method shared \
    ipv4.addresses "${HOTSPOT_IP}/24" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$HOTSPOT_PASS" \
    802-11-wireless.band bg \
    802-11-wireless.channel 6
# Pin the AP to 2.4 GHz / channel 6. Without this, NetworkManager lets the
# driver auto-select band/channel, and the Pi Zero 2 W's brcmfmac WiFi firmware
# hard-resets the board the moment the hotspot comes up (confirmed on real hw).
nmcli con up "PumpSleeper-Hotspot"
echo "      Done."

# ── iptables + sudoers ────────────────────────────────────────────────────────
echo "[8/8] Configuring iptables and services..."
iptables -t nat -D PREROUTING -i "$WIFI_IFACE" -p tcp --dport "$SERVER_PORT" \
    -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}" 2>/dev/null || true
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport "$SERVER_PORT" \
    -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}"
iptables -t nat -D POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE 2>/dev/null || true
iptables -t nat -A POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE
sysctl -w net.ipv4.ip_forward=1 > /dev/null
# Bookworm uses /etc/sysctl.d/ instead of /etc/sysctl.conf
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-pumpsleeper.conf
netfilter-persistent save

# Packet-capture helper for the dashboard's "Capture pump traffic" debug tool.
# Lets the non-root service user run a tightly-scoped tcpdump (via the sudoers
# rule below) to record ALL of the device's traffic — including ports the
# app-level proxy never sees (e.g. pump events sent on a different port).
cat > /usr/local/bin/pumpsleeper-netcapture <<'NETCAP'
#!/bin/bash
set -u
UNIT=pumpsleeper-netcapture
TCPDUMP="$(command -v tcpdump || echo /usr/bin/tcpdump)"
case "${1:-}" in
  start)
    IFACE="${2:?}"; DEVIP="${3:?}"; OUT="${4:?}"
    [[ "$IFACE" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad iface" >&2; exit 3; }
    # DEVIP is a single device IP, or "all" to capture every hotspot client
    # (needed when more than one PumpSpy device is connected).
    [[ "$DEVIP" =~ ^([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+|all)$ ]] || { echo "bad ip" >&2; exit 3; }
    systemctl stop "$UNIT" 2>/dev/null || true
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    rm -f "$OUT"
    # Run tcpdump as a transient systemd unit so it survives this wrapper + sudo
    # exiting (sudo 1.9 kills a plain backgrounded child). Write to exactly $OUT
    # (no -C/-W rotation, which appends a numeric suffix the dashboard wouldn't
    # find). Hotspot traffic is tiny, so an uncapped file is fine.
    FILTER=()
    [ "$DEVIP" != all ] && FILTER=(host "$DEVIP")
    systemd-run --quiet --unit="$UNIT" --collect \
        "$TCPDUMP" -i "$IFACE" -nn -s 0 -w "$OUT" "${FILTER[@]}"
    ;;
  stop)
    OUT="${2:?}"; OWNER="${3:-}"
    systemctl stop "$UNIT" 2>/dev/null || true
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    sleep 1
    [ -f "$OUT" ] && [ -n "$OWNER" ] && chown "$OWNER:$OWNER" "$OUT" 2>/dev/null || true
    ;;
  *) echo "usage: $0 start <iface> <devip> <outfile> | stop <outfile> <owner>" >&2; exit 2 ;;
esac
NETCAP
chmod 755 /usr/local/bin/pumpsleeper-netcapture

cat > /etc/sudoers.d/pumpsleeper-hotspot \
    <<< "$RUN_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli con down PumpSleeper-Hotspot, /usr/bin/nmcli con up PumpSleeper-Hotspot, /usr/local/bin/pumpsleeper-netcapture, /usr/bin/systemctl restart pumpsleeper, /usr/bin/systemctl restart pumpsleeper-dashboard"
chmod 440 /etc/sudoers.d/pumpsleeper-hotspot

# ── systemd services ──────────────────────────────────────────────────────────
cat > /etc/systemd/system/pumpsleeper.service <<EOF
[Unit]
Description=PumpSleeper proxy server
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/pumpsleeper.env
ExecStart=/usr/bin/python3 $INSTALL_DIR/server.py
Restart=on-failure
RestartSec=5
LimitNOFILE=65535
StandardOutput=append:$INSTALL_DIR/data/server.log
StandardError=append:$INSTALL_DIR/data/server.log

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pumpsleeper-dashboard.service <<EOF
[Unit]
Description=PumpSleeper dashboard
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/pumpsleeper.env
ExecStart=/usr/bin/python3 $INSTALL_DIR/dashboard.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$INSTALL_DIR/data/dashboard.log
StandardError=append:$INSTALL_DIR/data/dashboard.log

[Install]
WantedBy=multi-user.target
EOF

# ── Auto-update timer (files baked into image by GitHub Action) ───────────────
if [[ -f /usr/local/lib/pumpsleeper-update.service ]]; then
    cp /usr/local/lib/pumpsleeper-update.service /etc/systemd/system/pumpsleeper-update.service
    cp /usr/local/lib/pumpsleeper-update.timer   /etc/systemd/system/pumpsleeper-update.timer
fi

# Write the installed version (baked in by GitHub Action, or fallback to 'dev').
# This runs as root AFTER the earlier `chown -R`, so hand VERSION back to the
# service user — otherwise the auto-updater (which runs as $RUN_USER) can't
# overwrite it and the dashboard keeps showing the old version after updating.
VERSION=$(cat /usr/local/lib/pumpsleeper-version 2>/dev/null || echo "dev")
echo "$VERSION" > "$INSTALL_DIR/VERSION"
chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR/VERSION"

# ── Enable services + mark the install COMPLETE using only fast FILE ops ──────
# The Zero 2 W intermittently hard-resets at the tail of first boot, and the
# v3.12 reorder still lost the race because `systemctl enable/disable` are slow
# D-Bus round-trips — the reset landed between them and the config removal. So we
# now "enable" via direct wants-symlinks (identical to what `systemctl enable`
# writes, but instant) and mark the install complete (remove the config + the
# firstboot auto-start symlink) immediately after — a microsecond window. ALL the
# slow, reset-prone systemctl calls (daemon-reload, start) happen AFTER the config
# is gone, where a reset can no longer trigger a re-install. (The firstboot
# service also has ConditionPathExists on the config, so removing it alone stops
# the loop even if the disable symlink lingers.)
WANTS=/etc/systemd/system/multi-user.target.wants
TWANTS=/etc/systemd/system/timers.target.wants
mkdir -p "$WANTS" "$TWANTS"
ln -sf /etc/systemd/system/pumpsleeper.service           "$WANTS/pumpsleeper.service"
ln -sf /etc/systemd/system/pumpsleeper-dashboard.service "$WANTS/pumpsleeper-dashboard.service"
[ -f /etc/systemd/system/pumpsleeper-update.timer ] && \
    ln -sf /etc/systemd/system/pumpsleeper-update.timer  "$TWANTS/pumpsleeper-update.timer"

# Mark complete (instant file ops): drop the firstboot auto-start symlink and the
# config. After this point a reset cannot cause an install re-run.
rm -f "$WANTS/pumpsleeper-firstboot.service"
rm -f "$BOOT_DIR/pumpsleeper.conf"

# Now the slow / reset-prone part — safe, because the install is already marked
# complete and the services are enabled via the symlinks above.
systemctl daemon-reload 2>/dev/null || true
systemctl start pumpsleeper pumpsleeper-dashboard 2>/dev/null || true
[ -f /etc/systemd/system/pumpsleeper-update.timer ] && \
    systemctl start pumpsleeper-update.timer 2>/dev/null || true
echo "      Done."

# ── Done ──────────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "============================================"
echo " Installation complete!"
echo " Dashboard : http://${PI_IP}:${DASHBOARD_PORT}"
echo " SSH       : ssh pumpsleeper@${PI_IP}"
echo " Hotspot   : $HOTSPOT_SSID (password: $HOTSPOT_PASS)"
echo " $(date)"
echo "============================================"
echo ""
echo "Connect your PumpSpy device to the '$HOTSPOT_SSID' WiFi network."
echo "This log file will remain here for reference."
