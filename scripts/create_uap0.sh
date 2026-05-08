#!/bin/bash
# Creates the uap0 virtual AP interface alongside wlan0.
# wlan0 stays connected to the home WiFi as a client.
# uap0 broadcasts StereoCamPi on the same channel.
set -euo pipefail

# Remove leftover from previous run, if any
/usr/sbin/iw dev uap0 del 2>/dev/null || true

# Wait until wlan0 is connected and has a channel
for i in $(seq 1 20); do
    CHAN=$(/usr/sbin/iw dev wlan0 info 2>/dev/null | awk '/channel/ {print $2}')
    [ -n "$CHAN" ] && break
    sleep 2
done

if [ -z "$CHAN" ]; then
    echo "ERROR: wlan0 has no channel after 40s, cannot create uap0" >&2
    exit 1
fi

# Create the virtual AP interface
/usr/sbin/iw dev wlan0 interface add uap0 type __ap

# Assign static IP
/usr/sbin/ip addr add 192.168.4.1/24 dev uap0
/usr/sbin/ip link set uap0 up

# Sync hostapd channel and hw_mode to match wlan0
if [ "$CHAN" -le 14 ]; then
    HW_MODE=g
else
    HW_MODE=a
fi
sed -i "s/^channel=.*/channel=$CHAN/" /etc/hostapd/hostapd.conf
sed -i "s/^hw_mode=.*/hw_mode=$HW_MODE/" /etc/hostapd/hostapd.conf

echo "uap0 ready: 192.168.4.1/24 on channel $CHAN (hw_mode=$HW_MODE)"
