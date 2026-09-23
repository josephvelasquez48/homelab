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
if ! grep -q '^PHONE_AGENT_TOKEN=' "$CONF/env"; then
  # Read by the desktop ring agent (agent/phone_agent.pyw); it goes into
  # the agent's config on the PC.
  echo "PHONE_AGENT_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$CONF/env"
  echo "Ring agent token: $(grep '^PHONE_AGENT_TOKEN=' "$CONF/env" | cut -d= -f2-)"
fi

[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade "$APP_DIR"

wp_conf="$HOME/.config/wireplumber/wireplumber.conf.d/51-phone-bridge.conf"
if ! cmp -s "$APP_DIR/wireplumber/51-phone-bridge.conf" "$wp_conf"; then
  cp "$APP_DIR/wireplumber/51-phone-bridge.conf" "$wp_conf"
  # Drops a call's audio if one is on the Pi right now - only on change.
  systemctl --user restart wireplumber
fi

cp "$APP_DIR/phone-bridge.service" "$HOME/.config/systemd/user/phone-bridge.service"
# Without linger the user's PipeWire session - and this service - only
# exists while someone is logged in.
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable phone-bridge.service
systemctl --user restart phone-bridge.service
systemctl --user --no-pager status phone-bridge.service | head -5
