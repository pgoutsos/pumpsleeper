#!/usr/bin/env bash
# =============================================================================
#  PumpSleeper — Raspberry Pi Installer
#  https://github.com/pgoutsos/pumpsleeper
#
#  Installs and configures PumpSleeper on a Raspberry Pi running Raspberry Pi OS.
#  Sets up a WiFi hotspot, iptables interception, Python services, and
#  optional Home Assistant MQTT integration.
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/pgoutsos/pumpsleeper/main/install.sh | bash
#  Or:
#    chmod +x install.sh && sudo ./install.sh
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }
header()  { echo -e "\n${BOLD}── $* ──${RESET}"; }

# ── Must run as root ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Please run as root: sudo bash install.sh"

# ── Detect Raspberry Pi ───────────────────────────────────────────────────────
if ! grep -qi "raspberry pi" /proc/cpuinfo 2>/dev/null && \
   ! grep -qi "raspberry" /sys/firmware/devicetree/base/model 2>/dev/null; then
    warn "This does not appear to be a Raspberry Pi."
    read -rp "Continue anyway? [y/N] " _cont
    [[ ${_cont,,} == "y" ]] || exit 1
fi

# ── Banner ────────────────────────────────────────────────────────────────────
clear
echo -e "${BOLD}"
echo "  ██████  ██    ██ ███    ███ ██████  ███████ ██      ███████ ███████ ██████  ███████ ██████  "
echo "  ██   ██ ██    ██ ████  ████ ██   ██ ██      ██      ██      ██      ██   ██ ██      ██   ██ "
echo "  ██████  ██    ██ ██ ████ ██ ██████  ███████ ██      █████   █████   ██████  █████   ██████  "
echo "  ██      ██    ██ ██  ██  ██ ██           ██ ██      ██      ██      ██      ██      ██   ██ "
echo "  ██       ██████  ██      ██ ██      ███████ ███████ ███████ ███████ ██      ███████ ██   ██ "
echo -e "${RESET}"
echo -e "  Sump pump monitor — intercepts PumpSpy device traffic locally"
echo -e "  ─────────────────────────────────────────────────────────────\n"

# ── Gather config from user ───────────────────────────────────────────────────
header "Configuration"

# WiFi interface for the hotspot
echo -e "\nAvailable wireless interfaces:"
iw dev 2>/dev/null | awk '/Interface/{print "  " $2}' || ip link show | grep -E "wlan|wlp" | awk '{print "  " $2}' | tr -d ':'
echo ""
read -rp "WiFi interface for hotspot [wlan0]: " WIFI_IFACE
WIFI_IFACE=${WIFI_IFACE:-wlan0}

# Hotspot SSID / password
read -rp "Hotspot SSID [PumpSpyLab]: " HOTSPOT_SSID
HOTSPOT_SSID=${HOTSPOT_SSID:-PumpSpyLab}
read -rsp "Hotspot password [pumpspy123]: " HOTSPOT_PASS
HOTSPOT_PASS=${HOTSPOT_PASS:-pumpspy123}
echo ""

# Hotspot subnet
HOTSPOT_IP="192.168.50.1"
read -rp "Hotspot gateway IP [${HOTSPOT_IP}]: " _hip
HOTSPOT_IP=${_hip:-$HOTSPOT_IP}

# Install directory
INSTALL_DIR="/opt/pumpsleeper"
read -rp "Install directory [${INSTALL_DIR}]: " _idir
INSTALL_DIR=${_idir:-$INSTALL_DIR}

# Run user
RUN_USER="pumpsleeper"
read -rp "Service user [${RUN_USER}]: " _usr
RUN_USER=${_usr:-$RUN_USER}

# Ports
SERVER_PORT=8081
DASHBOARD_PORT=8080

# MQTT (optional)
echo ""
read -rp "Configure Home Assistant MQTT integration now? [y/N] " _mqtt
if [[ ${_mqtt,,} == "y" ]]; then
    read -rp "  MQTT broker host (e.g. 192.168.0.55): " MQTT_HOST
    read -rp "  MQTT broker port [1883]: " _mport
    MQTT_PORT=${_mport:-1883}
    read -rp "  MQTT username (leave blank if anonymous): " MQTT_USER
    if [[ -n "$MQTT_USER" ]]; then
        read -rsp "  MQTT password: " MQTT_PASS; echo ""
    else
        MQTT_PASS=""
    fi
    MQTT_ENABLED=true
else
    MQTT_HOST=""
    MQTT_PORT=1883
    MQTT_USER=""
    MQTT_PASS=""
    MQTT_ENABLED=false
fi

# Confirm
echo ""
echo -e "${BOLD}Summary:${RESET}"
echo "  WiFi interface   : $WIFI_IFACE"
echo "  Hotspot SSID     : $HOTSPOT_SSID"
echo "  Hotspot IP       : $HOTSPOT_IP"
echo "  Install directory: $INSTALL_DIR"
echo "  Service user     : $RUN_USER"
echo "  MQTT             : ${MQTT_HOST:-disabled}"
echo ""
read -rp "Proceed with installation? [Y/n] " _go
[[ ${_go,,} != "n" ]] || exit 0

# ── System dependencies ───────────────────────────────────────────────────────
header "System dependencies"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip \
    network-manager \
    iptables iptables-persistent \
    curl
success "System packages installed"

# ── Python dependencies ───────────────────────────────────────────────────────
header "Python dependencies"
pip3 install --break-system-packages --quiet \
    flask waitress requests
if [[ "$MQTT_ENABLED" == "true" ]]; then
    pip3 install --break-system-packages --quiet paho-mqtt
fi
success "Python packages installed"

# ── Create service user ───────────────────────────────────────────────────────
header "Service user"
if ! id "$RUN_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
    success "User '$RUN_USER' created"
else
    info "User '$RUN_USER' already exists"
fi

# ── Install app files ─────────────────────────────────────────────────────────
header "Installing app files"
mkdir -p "$INSTALL_DIR"

# If run from the repo directory, copy local files; otherwise download them
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/app/server.py" ]]; then
    info "Copying from local repo..."
    cp "$SCRIPT_DIR/app/server.py"    "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/app/dashboard.py" "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/app/db.py"        "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/app/mqtt.py"      "$INSTALL_DIR/"
else
    info "Downloading app files from GitHub..."
    BASE_URL="https://raw.githubusercontent.com/pgoutsos/pumpsleeper/main/app"
    curl -fsSL "$BASE_URL/server.py"    -o "$INSTALL_DIR/server.py"
    curl -fsSL "$BASE_URL/dashboard.py" -o "$INSTALL_DIR/dashboard.py"
    curl -fsSL "$BASE_URL/db.py"        -o "$INSTALL_DIR/db.py"
    curl -fsSL "$BASE_URL/mqtt.py"      -o "$INSTALL_DIR/mqtt.py"
fi

# Data directory (SQLite DB lives here)
mkdir -p "$INSTALL_DIR/data"
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
success "App files installed to $INSTALL_DIR"

# ── Environment file ──────────────────────────────────────────────────────────
header "Environment configuration"
cat > "$INSTALL_DIR/pumpsleeper.env" <<EOF
# PumpSleeper environment — edit and restart services to apply changes
PUMPSPY_DATA=$INSTALL_DIR/data
PUMPSLEEPER_MQTT_HOST=$MQTT_HOST
PUMPSLEEPER_MQTT_PORT=$MQTT_PORT
PUMPSLEEPER_MQTT_USER=$MQTT_USER
PUMPSLEEPER_MQTT_PASSWORD=$MQTT_PASS
EOF
chmod 600 "$INSTALL_DIR/pumpsleeper.env"
success "Environment file written to $INSTALL_DIR/pumpsleeper.env"

# ── WiFi hotspot ──────────────────────────────────────────────────────────────
header "WiFi hotspot"

# Make sure NetworkManager manages the interface
nmcli device set "$WIFI_IFACE" managed yes 2>/dev/null || true

# Create hotspot connection
if nmcli con show "PumpSleeper-Hotspot" &>/dev/null; then
    info "Hotspot connection already exists — updating..."
    nmcli con delete "PumpSleeper-Hotspot"
fi

nmcli con add type wifi ifname "$WIFI_IFACE" con-name "PumpSleeper-Hotspot" \
    autoconnect yes ssid "$HOTSPOT_SSID" \
    mode ap \
    ipv4.method shared \
    ipv4.addresses "${HOTSPOT_IP}/24" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$HOTSPOT_PASS"

nmcli con up "PumpSleeper-Hotspot"
success "Hotspot '$HOTSPOT_SSID' active on $WIFI_IFACE ($HOTSPOT_IP)"

# ── iptables DNAT ─────────────────────────────────────────────────────────────
header "iptables traffic interception"

# Flush existing PumpSleeper rules to avoid duplicates
iptables -t nat -D PREROUTING -i "$WIFI_IFACE" -p tcp --dport "$SERVER_PORT" \
    -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}" 2>/dev/null || true

# The real PumpSpy server IPs (add all known IPs)
REAL_IPS=(
    "206.80.104.221"
    "64.227.40.212"
    "64.227.46.155"
    "64.227.33.97"
    "64.225.50.52"
    "64.225.51.200"
    "64.225.50.148"
    "64.225.50.146"
)

for ip in "${REAL_IPS[@]}"; do
    iptables -t nat -D PREROUTING -i "$WIFI_IFACE" -p tcp -d "$ip" --dport "$SERVER_PORT" \
        -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}" 2>/dev/null || true
    iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp -d "$ip" --dport "$SERVER_PORT" \
        -j DNAT --to-destination "${HOTSPOT_IP}:${SERVER_PORT}"
done

iptables -t nat -D POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE 2>/dev/null || true
iptables -t nat -A POSTROUTING -o "$WIFI_IFACE" -j MASQUERADE

# Enable IP forwarding
sysctl -w net.ipv4.ip_forward=1 > /dev/null
grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

# Persist iptables rules
netfilter-persistent save
success "iptables rules applied and persisted"

# ── systemd services ──────────────────────────────────────────────────────────
header "systemd services"

# Server service
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

# Dashboard service
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
systemctl restart pumpsleeper pumpsleeper-dashboard
success "Services enabled and started"

# ── Verify ────────────────────────────────────────────────────────────────────
header "Verifying installation"
sleep 3

server_ok=false
dashboard_ok=false

systemctl is-active --quiet pumpsleeper       && server_ok=true
systemctl is-active --quiet pumpsleeper-dashboard && dashboard_ok=true

$server_ok    && success "pumpsleeper service: running" \
              || error   "pumpsleeper service: FAILED (check $INSTALL_DIR/data/server.log)"
$dashboard_ok && success "pumpsleeper-dashboard service: running" \
              || error   "pumpsleeper-dashboard service: FAILED (check $INSTALL_DIR/data/dashboard.log)"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}Installation complete!${RESET}"
echo ""
echo -e "  Dashboard : ${BOLD}http://$(hostname -I | awk '{print $1}'):${DASHBOARD_PORT}${RESET}"
echo -e "  Hotspot   : ${BOLD}${HOTSPOT_SSID}${RESET} (password: ${HOTSPOT_PASS})"
echo -e "  Mode      : Proxy (forwarding to pumpspy.com)"
echo ""
echo -e "  Connect your PumpSpy device to the '${HOTSPOT_SSID}' WiFi network."
echo -e "  It will appear in the dashboard within a few minutes."
echo ""
echo -e "  Logs      : $INSTALL_DIR/data/server.log"
echo -e "  Config    : $INSTALL_DIR/pumpsleeper.env"
echo ""
if [[ "$MQTT_ENABLED" == "false" ]]; then
    echo -e "  ${YELLOW}MQTT not configured.${RESET} To enable Home Assistant integration later:"
    echo -e "  Edit $INSTALL_DIR/pumpsleeper.env and set PUMPSLEEPER_MQTT_HOST,"
    echo -e "  then run: sudo systemctl restart pumpsleeper"
    echo ""
fi
