#!/bin/sh
# Hand a call's audio back to the iPhone by dropping the Pi's audio link.
#
# Installed root-owned as /usr/local/sbin/phone-bridge-release-sco by
# install.sh, with a sudoers rule (/etc/sudoers.d/phone-bridge) letting
# joe run exactly this, no arguments. PipeWire's telephony API can pull
# audio onto the Pi (Activate) but has nothing to release it, and the
# hands-free side releases audio by disconnecting the (e)SCO link - an
# HCI Disconnect, which needs root. The phone keeps the call and moves
# its audio to the handset; the bridge refuses the link (RejectSCO) until
# the call ends or the PC asks for it back.
set -eu

# `hcitool con` lines look like: "< eSCO A0:EE:1A:96:E6:9E handle 6 state 1 lm CENTRAL"
handle=$(hcitool -i hci0 con | awk '$2 ~ /^e?SCO$/ { for (i = 1; i < NF; i++) if ($i == "handle") { print $(i + 1); exit } }')
if [ -z "$handle" ]; then
    echo "no audio link to release" >&2
    exit 1
fi
lo=$(printf '0x%02x' $((handle & 255)))
hi=$(printf '0x%02x' $((handle >> 8)))
# OGF 0x01 (link control), OCF 0x0006 (Disconnect), reason 0x13 (remote user terminated)
hcitool -i hci0 cmd 0x01 0x0006 "$lo" "$hi" 0x13 >/dev/null
echo "released audio link $handle"
