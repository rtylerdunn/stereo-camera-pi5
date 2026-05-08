#!/bin/bash
# Install the stereo-camera project on a fresh Raspberry Pi 5.
# Run as root: sudo bash install.sh
# Assumes: Raspberry Pi OS (Debian trixie), Pi Camera Module 3 x2,
#          user 'tyler' exists, project files already copied to /home/tyler/stereo-camera/
set -euo pipefail

PROJECT_DIR="/home/tyler/stereo-camera"
SERVICE_USER="tyler"
LOG_DIR="$PROJECT_DIR/logs"
IMAGES_DIR="$PROJECT_DIR/images"

echo "=== StereoCam Install: $(date) ==="

# ── 1. System packages ─────────────────────────────────────────────────────
echo "[1] Installing system packages..."
apt-get update -qq
apt-get install -y \
    python3-picamera2 \
    python3-flask \
    python3-opencv \
    python3-numpy \
    python3-pip \
    network-manager \
    iw \
    rfkill

# ── 2. Project directories ─────────────────────────────────────────────────
echo "[2] Creating project directories..."
mkdir -p "$LOG_DIR" "$IMAGES_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR"

# ── 3. User groups (camera access) ────────────────────────────────────────
echo "[3] Adding $SERVICE_USER to video and render groups..."
usermod -aG video,render "$SERVICE_USER"

# ── 4. Passwordless sudo for NM commands ──────────────────────────────────
echo "[4] Configuring passwordless sudo..."
SUDOERS_FILE="/etc/sudoers.d/stereocam"
cat > "$SUDOERS_FILE" << 'EOF'
tyler ALL=(ALL) NOPASSWD: ALL
EOF
chmod 440 "$SUDOERS_FILE"

# ── 5. Systemd service ────────────────────────────────────────────────────
echo "[5] Installing stereocam systemd service..."
cp "$PROJECT_DIR/services/stereocam.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable stereocam
systemctl restart stereocam
sleep 3
if systemctl is-active --quiet stereocam; then
    echo "    stereocam: running"
else
    echo "    stereocam: FAILED"
    journalctl -u stereocam -n 30 --no-pager
    exit 1
fi

# ── 6. WiFi auto-switch (NM native AP fallback) ───────────────────────────
echo "[6] Configuring WiFi auto-switch..."
echo "    Edit SSID/password below before running, or configure manually:"
echo "    sudo nmcli con add type wifi ifname wlan0 con-name 'StereoCamPi-AP' \\"
echo "      ssid 'StereoCamPi' 802-11-wireless.mode ap 802-11-wireless.band bg \\"
echo "      802-11-wireless-security.key-mgmt wpa-psk \\"
echo "      802-11-wireless-security.psk 'YOUR_PASSWORD' \\"
echo "      ipv4.method shared ipv4.addresses '192.168.4.1/24' \\"
echo "      connection.autoconnect yes connection.autoconnect-priority 1"
echo "    sudo nmcli con modify 'YOUR_HOME_SSID' connection.autoconnect-priority 10"

echo ""
echo "=== Install Complete ==="
echo "Web UI: http://$(hostname).fritz.box:8080  (home WiFi)"
echo "Web UI: http://192.168.4.1:8080            (StereoCamPi AP)"
