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
#
# With "off" (a call has started): take them off the list again and close
# the LE link, so nothing shares the radio with the call (2026-10-29: the
# call's link kept timing out with the LE link up).
set -eu
mode=${1:-on}
case "$mode" in on|off) ;; *) echo "usage: $0 [on|off]" >&2; exit 2 ;; esac

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
    if [ "$mode" = off ]; then
        sleep 1 | timeout 5 btmgmt remove-device -t "$type" "$addr" >/dev/null 2>&1 || true
        echo "auto-connect off: $addr"
        continue
    fi
    # btmgmt reads commands from a stdin that isn't a terminal: from the
    # bridge's (/dev/null) it hung and did nothing (2026-10-27). A pipe
    # held open 2 s lets it run the one command, then it exits.
    out=$(sleep 2 | timeout 5 btmgmt add-device -a 2 -t "$type" "$addr" 2>&1) || true
    case "$out" in
        *failed*|*Failed*|"") echo "couldn't add $addr: $out" >&2; exit 1 ;;
    esac
    echo "auto-connect: $addr"
done

if [ "$mode" = off ]; then
    # `hcitool con` lines look like: "> LE A0:EE:1A:96:E6:9E handle 64 state 1 lm PERIPHERAL"
    hcitool -i hci0 con | awk '$2 == "LE" { for (i = 1; i < NF; i++) if ($i == "handle") print $(i + 1) }' |
        while read -r handle; do
            hcitool -i hci0 ledc "$handle" 2>/dev/null || true  # it reports an error, but disconnects
            echo "closed LE link $handle"
        done
fi
