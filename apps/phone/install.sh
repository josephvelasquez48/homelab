#!/usr/bin/env bash
# Install or update the phone bridge on the Pi. Idempotent; run as joe:
#
#   bash apps/phone/install.sh
#
# Expects the TLS pair at ~/.config/phone-bridge/tls.{crt,key} already
# (certificates/issue-phone.py makes it). Generates a login password on
# first run and prints it once.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/.config/phone-bridge"
VENV="$HOME/.local/share/phone-bridge/venv"

mkdir -p "$CONF" "$HOME/.config/wireplumber/wireplumber.conf.d" "$HOME/.config/systemd/user"
chmod 700 "$CONF"

for f in tls.crt tls.key; do
  [[ -f "$CONF/$f" ]] || { echo "missing $CONF/$f - run certificates/issue-phone.py first" >&2; exit 1; }
done
chmod 600 "$CONF/tls.key"

if [[ ! -f "$CONF/env" ]]; then
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  umask 077
  printf 'PHONE_PASSWORD=%s\nPHONE_SESSION_SECRET=%s\nAPP_DIR=%s\n' \
    "$password" "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" "$APP_DIR" > "$CONF/env"
  echo "Login password (stored in $CONF/env): $password"
else
  # Keep the password and session key; only repoint APP_DIR.
  sed -i "s|^APP_DIR=.*|APP_DIR=$APP_DIR|" "$CONF/env"
fi
# For the iPhone's "Send to PC" Shortcut: it can only make drops. Not
# printed - read it when setting the Shortcut up (docs/phone.md, Drop).
if ! grep -q '^PHONE_DROP_TOKEN=' "$CONF/env"; then
  echo "PHONE_DROP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$CONF/env"
fi
if ! grep -q '^PHONE_AGENT_TOKEN=' "$CONF/env"; then
  # Read by the desktop ring agent (agent/phone_agent.pyw); it goes into
  # the agent's config on the PC.
  echo "PHONE_AGENT_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$CONF/env"
  echo "Ring agent token: $(grep '^PHONE_AGENT_TOKEN=' "$CONF/env" | cut -d= -f2-)"
fi

# Contacts and text notifications talk to the phone over OBEX.
dpkg -s bluez-obexd >/dev/null 2>&1 || sudo apt-get install -y bluez-obexd

# "Send call audio to iPhone" drops the audio link with an HCI command,
# which needs root: a root-owned script, and sudo for exactly that script.
# Reconnecting the iPhone's notifications link (LE) needs root the same
# way: the kernel's auto-connect list - see le-autoconnect.sh.
sudo install -o root -g root -m 755 "$APP_DIR/release-sco.sh" /usr/local/sbin/phone-bridge-release-sco
sudo install -o root -g root -m 755 "$APP_DIR/le-autoconnect.sh" /usr/local/sbin/phone-bridge-le-autoconnect
sudoers_tmp="$(mktemp)"
{
  echo "$USER ALL=(root) NOPASSWD: /usr/local/sbin/phone-bridge-release-sco"
  echo "$USER ALL=(root) NOPASSWD: /usr/local/sbin/phone-bridge-le-autoconnect"
} > "$sudoers_tmp"
sudo visudo -cf "$sudoers_tmp" >/dev/null
sudo install -o root -g root -m 440 "$sudoers_tmp" /etc/sudoers.d/phone-bridge
rm -f "$sudoers_tmp"

[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade "$APP_DIR"

wp_conf="$HOME/.config/wireplumber/wireplumber.conf.d/51-phone-bridge.conf"
if ! cmp -s "$APP_DIR/wireplumber/51-phone-bridge.conf" "$wp_conf"; then
  cp "$APP_DIR/wireplumber/51-phone-bridge.conf" "$wp_conf"
  # Drops a call's audio if one is on the Pi right now - only on change.
  systemctl --user restart wireplumber
fi

# Realtime priority for PipeWire's audio thread, through rtkit - see
# pipewire/60-realtime.conf and pipewire/50-rtkit-joe.rules.
dpkg -s rtkit >/dev/null 2>&1 || sudo apt-get install -y rtkit
sudo install -o root -g root -m 644 "$APP_DIR/pipewire/50-rtkit-joe.rules" /etc/polkit-1/rules.d/
pw_conf="$HOME/.config/pipewire/pipewire.conf.d/60-realtime.conf"
mkdir -p "$(dirname "$pw_conf")"
if ! cmp -s "$APP_DIR/pipewire/60-realtime.conf" "$pw_conf"; then
  cp "$APP_DIR/pipewire/60-realtime.conf" "$pw_conf"
  # Drops a call's audio if one is on the Pi right now - only on change.
  systemctl --user restart pipewire pipewire-pulse wireplumber
fi

# Bluetooth LE privacy: the Pi's LE identity is its own, so the iPhone pairs
# it as a notification accessory (Share System Notifications) and reconnects
# it - docs/phone.md, The iPhone's notifications. Restarting bluetoothd
# drops calls and music for ~10 s - only when the setting changes.
if ! grep -q '^Privacy = device$' /etc/bluetooth/main.conf; then
  sudo sed -i 's/^#\?Privacy = .*/Privacy = device/' /etc/bluetooth/main.conf
  # No Privacy line at all: it belongs in [General], not at the end.
  grep -q '^Privacy = device$' /etc/bluetooth/main.conf || sudo sed -i '/^\[General\]/a Privacy = device' /etc/bluetooth/main.conf
  sudo systemctl restart bluetooth
fi

# obexd as a client only - see obex-override.conf.
mkdir -p "$HOME/.config/systemd/user/obex.service.d"
if ! cmp -s "$APP_DIR/obex-override.conf" "$HOME/.config/systemd/user/obex.service.d/phone-bridge.conf"; then
  cp "$APP_DIR/obex-override.conf" "$HOME/.config/systemd/user/obex.service.d/phone-bridge.conf"
  systemctl --user daemon-reload
  systemctl --user restart obex 2>/dev/null || true  # D-Bus activated; may not be running
fi

cp "$APP_DIR/phone-bridge.service" "$HOME/.config/systemd/user/phone-bridge.service"
# Without linger the user's PipeWire session - and this service - only
# exists while someone is logged in.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable phone-bridge.service
systemctl --user restart phone-bridge.service
systemctl --user --no-pager status phone-bridge.service | head -5

# Calls on the Pi's own touchscreen (screen/phone_screen.py). Harmless
# without a display: it only opens Chromium while a call is up.
cp "$APP_DIR/screen/phone-screen.service" "$HOME/.config/systemd/user/phone-screen.service"
systemctl --user daemon-reload
systemctl --user enable --now phone-screen.service
