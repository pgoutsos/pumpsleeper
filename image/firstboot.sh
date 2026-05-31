#!/usr/bin/env bash
# =============================================================================
#  PumpSleeper — First Boot Installer
#  Runs automatically on first boot from the systemd firstboot service.
#  Reads config from /boot/firmware/pumpsleeper.conf (FAT32 boot partition).
#  Logs to /boot/firmware/pumpsleeper-install.log so you can check progress
#  by reading that file from any Mac or PC after install.
# =============================================================================

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
# Pi OS Bookworm uses /boot/firmware; older releases use /boot
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

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONF_FILE" ]]; then
    echo "ERROR: $CONF_FILE not found. Cannot continue."
    echo "Please add pumpsleeper.conf to the boot partition and reboot."
    exit 1
fi

source "$CONF_FILE"

HOTSPOT_SSID="${HOTSPOT_SSID:-PumpSpyLab}"
HOTSPOT_PASS="${HOTSPOT_PASS:-pumpspy123}"
HOTSPOT_IP="${HOTSPOT_IP:-192.168.50.1}"
MQTT_HOST="${MQTT_HOST:-}"
MQTT_PORT="${MQTT_PORT:-1883}"
MQTT_USER="${MQTT_USER:-}"
MQTT_PASS="${MQTT_PASS:-}"

echo "Config loaded:"
echo "  Hotspot SSID : $HOTSPOT_SSID"
echo "  Hotspot IP   : $HOTSPOT_IP"
echo "  MQTT host    : ${MQTT_HOST:-disabled}"
echo ""

# ── System dependencies ───────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip \
    network-manager \
    iptables iptables-persistent \
    curl
echo "      Done."

# ── Python dependencies ───────────────────────────────────────────────────────
echo "[2/7] Installing Python packages..."
pip3 install --break-system-packages --quiet flask waitress requests
[[ -n "$MQTT_HOST" ]] && pip3 install --break-system-packages --quiet paho-mqtt
echo "      Done."

# ── Service user ──────────────────────────────────────────────────────────────
echo "[3/7] Creating service user..."
if ! id "$RUN_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
fi
echo "      Done."

# ── Install app files ─────────────────────────────────────────────────────────
echo "[4/7] Downloading app files from GitHub..."
mkdir -p "$INSTALL_DIR/data"
BASE_URL="https://raw.githubusercontent.com/pgoutsos/pumpsleeper/main/app"
for f in server.py dashboard.py db.py mqtt.py notifications.py; do
    curl -fsSL "$BASE_URL/$f" -o "$INSTALL_DIR/$f"
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
echo "      Done."

# ── WiFi hotspot ──────────────────────────────────────────────────────────────
echo "[5/7] Configuring WiFi hotspot..."
nmcli device set "$WIFI_IFACE" managed yes 2>/dev/null || true
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

# ── iptables ──────────────────────────────────────────────────────────────────
echo "[6/7] Configuring iptables..."
iptables -t nat -D PREROUTING -i "$WIFI_IFACE" -p tcp --dport "$SERVER_PORT" \
    -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}" 2>/dev/null || true
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport "$SERVER_PORT" \
    -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}"
iptables -t nat -D POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE 2>/dev/null || true
iptables -t nat -A POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE
sysctl -w net.ipv4.ip_forward=1 > /dev/null
grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf \
    || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
netfilter-persistent save
echo "      Done."

# ── sudoers rule ──────────────────────────────────────────────────────────────
cat > /etc/sudoers.d/pumpsleeper-hotspot \
    <<< "$RUN_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli con down PumpSleeper-Hotspot, /usr/bin/nmcli con up PumpSleeper-Hotspot"
chmod 440 /etc/sudoers.d/pumpsleeper-hotspot

# ── systemd services ──────────────────────────────────────────────────────────
echo "[7/7] Installing systemd services..."

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

systemctl daemon-reload
systemctl enable pumpsleeper pumpsleeper-dashboard
systemctl start pumpsleeper pumpsleeper-dashboard
echo "      Done."

# ── Disable firstboot service so it doesn't run again ────────────────────────
systemctl disable pumpsleeper-firstboot.service 2>/dev/null || true
rm -f "$BOOT_DIR/pumpsleeper.conf"   # remove config so passwords don't sit on disk

# ── Done ──────────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "============================================"
echo " Installation complete!"
echo " Dashboard: http://${PI_IP}:${DASHBOARD_PORT}"
echo " Hotspot:   $HOTSPOT_SSID"
echo " $(date)"
echo "============================================"
echo ""
echo "This log file will remain here for reference."
echo "Connect your PumpSpy device to the '$HOTSPOT_SSID' WiFi network."
