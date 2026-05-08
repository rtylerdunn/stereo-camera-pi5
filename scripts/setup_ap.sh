#!/bin/bash
# WiFi AP setup script. Run as root via: sudo bash setup_ap.sh
# wlan0 will briefly disconnect while switching from 5GHz to 2.4GHz.
set -euo pipefail
LOG=/home/tyler/stereo-camera/logs/ap_setup.log
exec > >(tee -a "$LOG") 2>&1

echo "=== StereoCam AP Setup: $(date) ==="

# ── 1. Switch wlan0 to 2.4GHz ──────────────────────────────────────────────
echo "[1] Switching wlan0 to 2.4GHz..."
NM_CON=$(nmcli -t -f NAME,DEVICE con show --active | grep ':wlan0$' | cut -d: -f1)
echo "    Active connection: $NM_CON"
nmcli con modify "$NM_CON" 802-11-wireless.band bg
nmcli con up "$NM_CON" || true
echo "[1] Waiting 15s for reconnect..."
sleep 15

# ── 2. Get current channel ─────────────────────────────────────────────────
CHANNEL=$(iw dev wlan0 info 2>/dev/null | awk '/^\tchannel/ {print $2}')
echo "[2] wlan0 now on channel $CHANNEL (2.4GHz)"
sed -i "s/^channel=.*/channel=$CHANNEL/" /etc/hostapd/hostapd.conf

# ── 3. Create uap0 ─────────────────────────────────────────────────────────
echo "[3] Creating uap0 virtual interface..."
if ip link show uap0 &>/dev/null; then
    ip link set uap0 down && iw dev uap0 del && sleep 1
fi
iw dev wlan0 interface add uap0 type __ap
ip addr add 192.168.4.1/24 dev uap0
ip link set uap0 up
echo "    uap0 up: $(ip addr show uap0 | grep 'inet ')"

# ── 4. Restart dnsmasq with AP config ─────────────────────────────────────
echo "[4] Restarting dnsmasq..."
systemctl restart dnsmasq
sleep 2
systemctl is-active dnsmasq && echo "    dnsmasq: running" || echo "    dnsmasq: check logs"

# ── 5. Start hostapd ───────────────────────────────────────────────────────
echo "[5] Starting hostapd..."
systemctl unmask hostapd
systemctl enable hostapd
systemctl restart hostapd
sleep 3
if systemctl is-active --quiet hostapd; then
    echo "    hostapd: running"
else
    echo "    hostapd: FAILED"
    journalctl -u hostapd -n 20 --no-pager
    exit 1
fi

# ── 6. IP forwarding + NAT ─────────────────────────────────────────────────
echo "[6] Enabling IP forwarding and NAT..."
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-stereocam-forward.conf
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -F POSTROUTING
iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE
iptables -F FORWARD
iptables -A FORWARD -i uap0 -o wlan0 -j ACCEPT
iptables -A FORWARD -i wlan0 -o uap0 -m state --state ESTABLISHED,RELATED -j ACCEPT
netfilter-persistent save

# ── 7. Enable boot services ────────────────────────────────────────────────
echo "[7] Enabling services at boot..."
systemctl daemon-reload
systemctl enable create-uap0.service
systemctl enable hostapd dnsmasq

echo ""
echo "=== AP Setup Complete ==="
echo "SSID     : StereoCamPi"
echo "Password : StereoCam2024"
echo "AP IP    : 192.168.4.1"
echo "Web UI   : http://192.168.4.1:8080"
echo "mDNS     : http://stereopi.local:8080 (after connecting to AP)"
