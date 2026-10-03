#!/bin/sh
# Turn the Pi's Bluetooth on or off. Run by the homelab-bluetooth://on and
# ://off links the display's Bluetooth button follows (apps/dashboard,
# display.js). BlueZ lets any local user power the adapter, so no sudo.
# Off lasts until it's turned back on or the Pi restarts (BlueZ powers the
# adapter at boot).
case "$1" in
  *://off*) exec bluetoothctl power off ;;
  *://on*) exec bluetoothctl power on ;;
esac
