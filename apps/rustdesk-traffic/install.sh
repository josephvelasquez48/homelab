#!/usr/bin/env bash
# Install or update the RustDesk traffic counter on the Pi. Idempotent; run
# from the Pi's checkout:
#
#   bash apps/rustdesk-traffic/install.sh
#
# See traffic.py for what it measures and docs/rustdesk.md for the map.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo install -o root -g root -m 755 "$APP_DIR/traffic.py" /usr/local/bin/rustdesk-traffic
sudo install -o root -g root -m 644 "$APP_DIR/rustdesk-traffic.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rustdesk-traffic.service
sudo systemctl restart rustdesk-traffic.service
systemctl --no-pager --lines=0 status rustdesk-traffic.service | head -3
