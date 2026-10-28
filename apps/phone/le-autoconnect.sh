#!/bin/sh
# Let the kernel reconnect the iPhone over Bluetooth LE - its notifications'
# link (app/ancs.py) - whenever it hears it.
#
# Installed root-owned as /usr/local/sbin/phone-bridge-le-autoconnect by
# install.sh, with a sudoers rule (/etc/sudoers.d/phone-bridge) letting
# joe run exactly this, no arguments. iOS doesn't bring the LE link back
# by itself, but it does accept one the Pi opens (2026-10-27): this puts
# each paired LE device with an identity key (an iPhone, not a speaker) on
# the kernel's auto-connect list - a mgmt Add Device, which needs root.
# bluetoothd clears that list when it starts and BlueZ has no D-Bus call
# for it, so the bridge runs this before each of its scans; adding a
# device that's already there changes nothing.
set -eu

for info in /var/lib/bluetooth/*/*/info; do
    [ -f "$info" ] || continue
    grep -q '^SupportedTechnologies=.*LE' "$info" || continue
    grep -q '^\[IdentityResolvingKey\]' "$info" || continue
    addr=$(basename "$(dirname "$info")")
    # -t 1: LE public, 2: LE random (static). -a 2: always connect.
    case "$(sed -n 's/^AddressType=//p' "$info")" in
        public) type=1 ;;
        *) type=2 ;;
    esac
    # btmgmt reads commands from a stdin that isn't a terminal: from the
    # bridge's (/dev/null) it hung and did nothing (2026-10-27). A pipe
    # held open 2 s lets it run the one command, then it exits.
    out=$(sleep 2 | timeout 5 btmgmt add-device -a 2 -t "$type" "$addr" 2>&1) || true
    case "$out" in
        *failed*|*Failed*|"") echo "couldn't add $addr: $out" >&2; exit 1 ;;
    esac
    echo "auto-connect: $addr"
done
