#!/usr/bin/env bash
# =============================================================================
# PumpSpy Provisioning Script
# =============================================================================
# Turns a fresh Raspberry Pi OS Lite (Bookworm, 64-bit) into a PumpSpy node.
#
# Usage (on a live Pi):
#   sudo bash provision.sh
#
# Usage (from the repo root, which copies local app files):
#   sudo bash image/provision.sh
#
# The script is also used as the CustomPiOS chroot script during image builds.
# It detects when it is running inside a chroot and skips steps that require
# a live kernel (service starts, sysctl, nmcli bring-up, etc.).
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Config — override with environment variables if needed
# -----------------------------------------------------------------------------
INSTALL_DIR="${PUMPSPY_INSTALL_DIR:-/opt/pumpspy}"
DATA_DIR="${PUMPSPY_DATA_DIR:-/var/lib/pumpspy}"
LOG_DIR="${PUMPSPY_LOG_DIR:-/var/log/pumpspy}"

AP_INTERFACE="${PUMPSPY_AP_IFACE:-wlan0}"
AP_SSID="${PUMPSPY_AP_SSID:-PumpSpy}"
AP_PASSWORD="${PUMPSPY_AP_PASSWORD:-pumpspy1234}"
AP_IP="${PUMPSPY_AP_IP:-192.168.4.1}"

CLOUD_IP="${PUMPSPY_CLOUD_IP:-206.80.104.221}"
CLOUD_PORT="${PUMPSPY_CLOUD_PORT:-8081}"
SERVER_PORT="${PUMPSPY_SERVER_PORT:-8081}"
DASHBOARD_PORT="${PUMPSPY_DASHBOARD_PORT:-8080}"

REPO_URL="${PUMPSPY_REPO_URL:-}"   # optional: git clone URL if running remotely

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

step()  { echo -e "\n${BLUE}${BOLD}==>${NC} $*"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}!${NC} $*"; }
die()   { echo -e "\n${RED}Error:${NC} $*" >&2; exit 1; }
skip()  { echo -e "  ${YELLOW}↷${NC} $* (skipped in chroot)"; }

# Detect chroot — in a chroot PID 1's root differs from /
in_chroot() {
    [ "$(stat -c %d:%i /)" != "$(stat -c %d:%i /proc/1/root/. 2>/dev/null)" ] && return 0
    [ -f /proc/1/environ ] && grep -q "container" /proc/1/environ 2>/dev/null && return 0
    return 1
}

[ "$EUID" -eq 0 ] || die "Must run as root:  sudo bash image/provision.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo -e "\n${BOLD}PumpSpy Provisioning${NC}"
echo    "  Install dir : $INSTALL_DIR"
echo    "  Data dir    : $DATA_DIR"
echo    "  AP SSID     : $AP_SSID"
echo    "  AP IP       : $AP_IP"
in_chroot && echo -e "  ${YELLOW}Running inside chroot — live steps will be skipped${NC}"

# =============================================================================
# 1. System packages
# =============================================================================
step "Installing system packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q \
    python3 \
    python3-pip \
    python3-venv \
    network-manager \
    iptables \
    netfilter-persistent \
    iptables-persistent \
    avahi-daemon \
    avahi-utils \
    git \
    curl \
    jq

ok "System packages installed"

# =============================================================================
# 2. Directory structure
# =============================================================================
step "Creating directory structure"

mkdir -p "$INSTALL_DIR/app"
mkdir -p "$DATA_DIR"
mkdir -p "$LOG_DIR"

# Ensure the pumpspy user exists (runs services, not root)
if ! id -u pumpspy &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin pumpspy
    ok "Created system user: pumpspy"
else
    ok "System user pumpspy already exists"
fi

chown -R pumpspy:pumpspy "$DATA_DIR" "$LOG_DIR"
ok "Directories: $INSTALL_DIR  $DATA_DIR  $LOG_DIR"

# =============================================================================
# 3. Python virtual environment + dependencies
# =============================================================================
step "Setting up Python environment"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask pyyaml

ok "Virtual env: $INSTALL_DIR/venv"
ok "Installed: flask, pyyaml"

# =============================================================================
# 4. Application files
# =============================================================================
step "Installing application files"

# Priority: local repo > PUMPSPY_REPO_URL > warn and continue
if [ -f "$REPO_ROOT/app/server.py" ]; then
    cp "$REPO_ROOT/app/server.py"    "$INSTALL_DIR/app/"
    cp "$REPO_ROOT/app/dashboard.py" "$INSTALL_DIR/app/"
    ok "Copied app files from local repo"
elif [ -n "$REPO_URL" ]; then
    TMP_CLONE=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$TMP_CLONE"
    cp "$TMP_CLONE/app/server.py"    "$INSTALL_DIR/app/"
    cp "$TMP_CLONE/app/dashboard.py" "$INSTALL_DIR/app/"
    rm -rf "$TMP_CLONE"
    ok "Cloned app files from $REPO_URL"
else
    # During image builds, app files are pre-staged into the module's files/ dir
    # and land at /opt/pumpspy/app/ automatically — so this is a warning, not an error.
    warn "No local app files found — expected at $INSTALL_DIR/app/ (OK during image build)"
fi

chown -R pumpspy:pumpspy "$INSTALL_DIR/app"

# =============================================================================
# 5. Default config.yaml
# =============================================================================
step "Writing default config.yaml"

# Only write if it doesn't already exist so re-running won't clobber user edits
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
    cat > "$INSTALL_DIR/config.yaml" << EOF
# PumpSpy configuration
# Edit this file to customise your installation.
# Changes take effect after restarting the services:
#   sudo systemctl restart pumpspy-server pumpspy-dashboard

device:
  cloud_ip:   "$CLOUD_IP"   # IP the PumpSpy device calls home to
  cloud_port: $CLOUD_PORT

network:
  ap_interface: "$AP_INTERFACE"
  ap_ip:        "$AP_IP"
  ap_ssid:      "$AP_SSID"
  ap_password:  "$AP_PASSWORD"

server:
  host: "0.0.0.0"
  port: $SERVER_PORT
  # Bearer token issued to the device on every auth refresh.
  # The device never validates this — keep it as-is.
  bearer_token: "15e3409a-2a8c-4266-a669-bab98bc930de"

dashboard:
  host: "0.0.0.0"
  port: $DASHBOARD_PORT

# Parameters sent to the device when it requests its config (GET /bbs_parameters)
device_params:
  p1:  3000
  p2:  12500
  p3:  11000
  p4:  10000
  p5:  15
  p6:  7000
  p7:  10000
  p8:  48
  p9:  20000
  p10: 15000
EOF
    chown pumpspy:pumpspy "$INSTALL_DIR/config.yaml"
    ok "Config written to $INSTALL_DIR/config.yaml"
else
    ok "Config already exists — skipping (not overwritten)"
fi

# =============================================================================
# 6. WiFi AP — NetworkManager connection profile
# =============================================================================
step "Configuring WiFi AP (NetworkManager)"

# Remove existing profile if present so we can recreate cleanly
nmcli connection delete "PumpSpy-AP" 2>/dev/null && warn "Removed existing PumpSpy-AP profile" || true

# Write the profile as a keyfile so it works inside a chroot too
NM_CONF_DIR="/etc/NetworkManager/system-connections"
mkdir -p "$NM_CONF_DIR"

cat > "$NM_CONF_DIR/PumpSpy-AP.nmconnection" << EOF
[connection]
id=PumpSpy-AP
type=wifi
interface-name=$AP_INTERFACE
autoconnect=true
autoconnect-priority=100

[wifi]
mode=ap
ssid=$AP_SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$AP_PASSWORD

[ipv4]
method=shared
address1=$AP_IP/24

[ipv6]
method=disabled
EOF

chmod 600 "$NM_CONF_DIR/PumpSpy-AP.nmconnection"
ok "AP profile written: SSID=$AP_SSID  password=$AP_PASSWORD  IP=$AP_IP"

# DHCP reservation — ensures the PumpSpy device always gets the same IP.
# The MAC address of the known PumpSpy device is written here during provisioning.
# The setup wizard will update this file if a different device is detected.
DNSMASQ_DIR="/etc/NetworkManager/dnsmasq-shared.d"
mkdir -p "$DNSMASQ_DIR"
DEVICE_MAC="${PUMPSPY_DEVICE_MAC:-}"   # set via env var during provisioning if known
DEVICE_IP="${PUMPSPY_DEVICE_IP:-${AP_IP%.*}.100}"  # default: .100 in AP subnet

if [ -n "$DEVICE_MAC" ]; then
    echo "dhcp-host=$DEVICE_MAC,$DEVICE_IP,pumpspy-device,infinite" \
        > "$DNSMASQ_DIR/pumpspy-reservations.conf"
    ok "DHCP reservation: $DEVICE_MAC → $DEVICE_IP"
else
    # Write a placeholder — setup wizard fills in the MAC after device discovery
    cat > "$DNSMASQ_DIR/pumpspy-reservations.conf" << 'RESEOF'
# PumpSpy device DHCP reservation — filled in by the setup wizard on first boot.
# Format: dhcp-host=<mac>,<ip>,pumpspy-device,infinite
RESEOF
    ok "DHCP reservations file created (setup wizard will populate MAC after first scan)"
fi

if ! in_chroot; then
    nmcli connection reload
    nmcli connection up "PumpSpy-AP" 2>/dev/null && ok "AP brought up" || warn "AP could not start yet (may need reboot)"
else
    skip "nmcli connection up"
fi

# =============================================================================
# 7. IP forwarding
# =============================================================================
step "Enabling IP forwarding"

# Persist across reboots
if grep -q "^#.*net.ipv4.ip_forward" /etc/sysctl.conf; then
    sed -i 's/^#.*net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf
elif grep -q "^net.ipv4.ip_forward" /etc/sysctl.conf; then
    sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf
else
    echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
fi

if ! in_chroot; then
    sysctl -p /etc/sysctl.conf > /dev/null
    ok "IP forwarding active"
else
    skip "sysctl -p"
    ok "IP forwarding configured in /etc/sysctl.conf"
fi

# =============================================================================
# 8. iptables DNAT redirect
# =============================================================================
step "Configuring iptables DNAT redirect"

# Rule: any device connecting through our AP that tries to reach
# the PumpSpy cloud IP gets redirected to our local server instead.
# No source-IP restriction — works regardless of what DHCP assigns the device.

# Flush existing PREROUTING NAT rules to avoid duplicates on re-run
iptables -t nat -F PREROUTING 2>/dev/null || true

iptables -t nat -A PREROUTING \
    -i "$AP_INTERFACE" \
    -d "$CLOUD_IP" \
    -p tcp --dport "$CLOUD_PORT" \
    -j DNAT --to-destination "$AP_IP:$SERVER_PORT"

# Allow forwarded traffic through the AP interface
iptables -C FORWARD -i "$AP_INTERFACE" -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -i "$AP_INTERFACE" -j ACCEPT
iptables -C FORWARD -o "$AP_INTERFACE" -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -o "$AP_INTERFACE" -j ACCEPT

# Persist
netfilter-persistent save
ok "DNAT rule: $CLOUD_IP:$CLOUD_PORT → $AP_IP:$SERVER_PORT (via $AP_INTERFACE)"
ok "iptables rules saved"

# =============================================================================
# 9. Systemd service units
# =============================================================================
step "Installing systemd service units"

cat > /etc/systemd/system/pumpspy-server.service << EOF
[Unit]
Description=PumpSpy Device API Server
Documentation=https://github.com/your-org/pumpspy
After=network.target NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=pumpspy
WorkingDirectory=$DATA_DIR
Environment=PUMPSPY_CONFIG=$INSTALL_DIR/config.yaml
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/app/server.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/server.log
StandardError=append:$LOG_DIR/server.log

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pumpspy-dashboard.service << EOF
[Unit]
Description=PumpSpy Web Dashboard
Documentation=https://github.com/your-org/pumpspy
After=pumpspy-server.service
Requires=pumpspy-server.service

[Service]
Type=simple
User=pumpspy
WorkingDirectory=$DATA_DIR
Environment=PUMPSPY_CONFIG=$INSTALL_DIR/config.yaml
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/app/dashboard.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/dashboard.log
StandardError=append:$LOG_DIR/dashboard.log

[Install]
WantedBy=multi-user.target
EOF

ok "pumpspy-server.service written"
ok "pumpspy-dashboard.service written"

# =============================================================================
# 10. Hostname + mDNS
# =============================================================================
step "Setting hostname and enabling mDNS"

hostnamectl set-hostname pumpspy 2>/dev/null || echo "pumpspy" > /etc/hostname

# Ensure 127.0.1.1 entry is correct
if grep -q "127.0.1.1" /etc/hosts; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tpumpspy/' /etc/hosts
else
    echo "127.0.1.1	pumpspy" >> /etc/hosts
fi

systemctl enable avahi-daemon 2>/dev/null || true
ok "Hostname: pumpspy"
ok "mDNS: pumpspy.local (avahi-daemon enabled)"

# =============================================================================
# 11. First-boot flag
# =============================================================================
step "Setting first-boot flag"

# The setup wizard checks for this file. It deletes it on completion
# so the wizard never runs again after initial setup.
touch "$INSTALL_DIR/.first-boot"
chown pumpspy:pumpspy "$INSTALL_DIR/.first-boot"
ok "First-boot flag: $INSTALL_DIR/.first-boot"

# =============================================================================
# 12. Enable + start services
# =============================================================================
step "Enabling services"

systemctl daemon-reload
systemctl enable pumpspy-server pumpspy-dashboard
ok "pumpspy-server enabled"
ok "pumpspy-dashboard enabled"

if ! in_chroot; then
    systemctl start pumpspy-server
    systemctl start pumpspy-dashboard
    ok "Services started"
else
    skip "systemctl start (will start on first boot)"
fi

# =============================================================================
# Done
# =============================================================================
echo -e "\n${GREEN}${BOLD}✓ PumpSpy provisioning complete${NC}\n"

if ! in_chroot; then
    echo -e "  Dashboard  → ${BLUE}http://pumpspy.local:$DASHBOARD_PORT${NC}"
    echo -e "             → ${BLUE}http://$AP_IP:$DASHBOARD_PORT${NC}"
    echo -e "  Device WiFi  SSID: ${BOLD}$AP_SSID${NC}  Password: ${BOLD}$AP_PASSWORD${NC}"
    echo -e "\n  Connect your PumpSpy device to the ${BOLD}$AP_SSID${NC} network."
    echo -e "  Open the dashboard URL to verify it is reporting in.\n"
fi
