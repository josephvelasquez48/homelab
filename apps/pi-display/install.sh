#!/usr/bin/env bash
# Install or update the always-on display on the Pi. Idempotent; run as joe:
#
#   bash apps/pi-display/install.sh
#
# The page itself is served by the dashboard (apps/dashboard, /display);
# this only opens it full screen whenever the desktop is logged in.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CA="$APP_DIR/../../certificates/homelab-ca.crt"

# Chromium on the Pi checks certificates against its own NSS store, not the
# system CAs, so dashboard.home would be "not secure" without this. The
# homelab CA is name-constrained to .home, so trusting it vouches for
# nothing else.
dpkg -s libnss3-tools >/dev/null 2>&1 || sudo apt-get install -y libnss3-tools
mkdir -p "$HOME/.pki/nssdb"
[[ -f "$HOME/.pki/nssdb/cert9.db" ]] || certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password
certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n "homelab-ca" -i "$CA"

mkdir -p "$HOME/.config/systemd/user" "$HOME/.config/autostart"
cp "$APP_DIR/pi-display.service" "$HOME/.config/systemd/user/"
cp "$APP_DIR/pi-display.desktop" "$HOME/.config/autostart/"
systemctl --user daemon-reload

# Start it now if the desktop is already up; otherwise the next login does.
if [[ -S /tmp/.X11-unix/X0 || -S "/run/user/$(id -u)/wayland-0" ]]; then
  systemctl --user set-environment DISPLAY=:0 XAUTHORITY="$HOME/.Xauthority" WAYLAND_DISPLAY=wayland-0
  systemctl --user restart pi-display.service
fi
