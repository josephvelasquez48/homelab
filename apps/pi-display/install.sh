#!/usr/bin/env bash
# Install or update the always-on display on the Pi. Idempotent; run as joe:
#
#   bash apps/pi-display/install.sh
#
# The page itself is served by the dashboard (apps/dashboard, /display);
# this only opens it full screen whenever the desktop is logged in, and
# wires up the page's Desktop button and the icon that brings it back.
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

# The display's Desktop button follows a homelab-desktop:// link. Make
# show-desktop.sh its handler (the xdg-open here splits Exec on spaces, so
# it names a script rather than an inline command).
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/Desktop"
install -m 755 "$APP_DIR/show-desktop.sh" "$HOME/.local/bin/pi-display-show-desktop"
sed "s|@BIN@|$HOME/.local/bin|" "$APP_DIR/homelab-desktop.desktop" > "$HOME/.local/share/applications/homelab-desktop.desktop"
xdg-mime default homelab-desktop.desktop x-scheme-handler/homelab-desktop
update-desktop-database "$HOME/.local/share/applications"
# And the way back: "Homelab display" in the menu and on the desktop.
cp "$APP_DIR/pi-display.desktop" "$HOME/.local/share/applications/"
cp "$APP_DIR/pi-display.desktop" "$HOME/Desktop/Homelab display.desktop"
gio set "$HOME/Desktop/Homelab display.desktop" metadata::trusted true 2>/dev/null || true

# Let the display open that link without Chromium asking first. Chromium
# rewrites Preferences when it exits, so edit it only while it's stopped.
PREFS="$HOME/.local/share/pi-display/chromium/Default/Preferences"
systemctl --user stop pi-display.service 2>/dev/null || true
mkdir -p "$(dirname "$PREFS")"
python3 - "$PREFS" <<'EOF'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
prefs = json.loads(path.read_text()) if path.exists() else {}
pairs = prefs.setdefault("protocol_handler", {}).setdefault("allowed_origin_protocol_pairs", {})
pairs.setdefault("https://dashboard.home", {})["homelab-desktop"] = True
path.write_text(json.dumps(prefs))
EOF

# Start it now if the desktop is already up; otherwise the next login does.
if [[ -S /tmp/.X11-unix/X0 || -S "/run/user/$(id -u)/wayland-0" ]]; then
  systemctl --user set-environment DISPLAY=:0 XAUTHORITY="$HOME/.Xauthority" WAYLAND_DISPLAY=wayland-0
  systemctl --user restart pi-display.service
fi
