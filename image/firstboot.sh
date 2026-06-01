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
MQTT_HOST="${MQTT_HOST:-}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_USER="${MQTT_USER:-}"
MQTT_PASS="${MQTT_PASS:-}"

echo "Config loaded:"
echo "  Home WiFi    : ${HOME_WIFI_SSID:-not set}"
echo "  Hotspot SSID : $HOTSPOT_SSID"
echo "  Hotspot IP   : $HOTSPOT_IP"
echo "  MQTT host    : ${MQTT_HOST:-disabled}"
echo ""

# ── Change default SSH password ───────────────────────────────────────────────
echo "[1/8] Setting login password..."
PASS_HASH=$(echo "$SSH_PASS" | openssl passwd -6 -stdin)
usermod -p "$PASS_HASH" pumpsleeper 2>/dev/null \
    || echo "      WARNING: Could not set password now — will retry after boot."
echo "      Done."

# ── Connect to home WiFi ──────────────────────────────────────────────────────
echo "[2/8] Connecting to home WiFi..."
if [[ -z "$HOME_WIFI_SSID" ]]; then
    echo "      WARNING: HOME_WIFI_SSID not set in pumpsleeper.conf."
    echo "      Cannot connect to internet. Install will fail when downloading files."
else
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

# ── System dependencies ───────────────────────────────────────────────────────
echo "[3/8] Installing system packages..."
# Pre-answer iptables-persistent prompts so they don't block the install
echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean false | debconf-set-selections
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-pip \
    network-manager \
    iptables iptables-persistent \
    curl
echo "      Done."

# ── Python dependencies ───────────────────────────────────────────────────────
echo "[4/8] Installing Python packages..."
pip3 install --break-system-packages --quiet --root-user-action=ignore flask waitress requests
[[ -n "$MQTT_HOST" ]] && pip3 install --break-system-packages --quiet --root-user-action=ignore paho-mqtt
echo "      Done."

# ── cloudflared (optional web access via Cloudflare quick tunnel) ─────────────
echo "[4b/8] Installing cloudflared (for optional web access)..."
if ! command -v cloudflared >/dev/null 2>&1; then
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in armhf) CF_ARCH=arm ;; *) CF_ARCH="$ARCH" ;; esac
    if curl -fsSL --max-time 60 \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
        -o /usr/local/bin/cloudflared; then
        chmod +x /usr/local/bin/cloudflared
        echo "      cloudflared installed."
    else
        echo "      WARNING: cloudflared download failed — web access can be enabled later once it's installed."
    fi
else
    echo "      cloudflared already present."
fi

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
    wifi-sec.psk "$HOTSPOT_PASS"
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

cat > /etc/sudoers.d/pumpsleeper-hotspot \
    <<< "$RUN_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli con down PumpSleeper-Hotspot, /usr/bin/nmcli con up PumpSleeper-Hotspot"
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

# Write the installed version (baked in by GitHub Action, or fallback to 'dev')
VERSION=$(cat /usr/local/lib/pumpsleeper-version 2>/dev/null || echo "dev")
echo "$VERSION" > "$INSTALL_DIR/VERSION"

systemctl daemon-reload
systemctl enable pumpsleeper pumpsleeper-dashboard
systemctl start pumpsleeper pumpsleeper-dashboard
# Enable update timer only if files exist
if [[ -f /etc/systemd/system/pumpsleeper-update.timer ]]; then
    systemctl enable pumpsleeper-update.timer
    systemctl start pumpsleeper-update.timer
fi
echo "      Done."

# ── Disable firstboot service ─────────────────────────────────────────────────
systemctl disable pumpsleeper-firstboot.service 2>/dev/null || true

# ── Remove config from boot partition (contains passwords) ────────────────────
rm -f "$BOOT_DIR/pumpsleeper.conf"

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
