#!/usr/bin/env bash
# Install or update the Argo CD refresh watcher on the Pi. Idempotent; run
# from the Pi's checkout:
#
#   bash apps/argocd-refresh/install.sh
#
# See refresh.sh for what it does and docs/argocd.md for why.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The first version ran from a timer; a loop is cheaper (see refresh.sh).
if [[ -e /etc/systemd/system/argocd-refresh.timer ]]; then
  sudo systemctl disable --now argocd-refresh.timer
  sudo rm /etc/systemd/system/argocd-refresh.timer
fi

sudo install -o root -g root -m 755 "$APP_DIR/refresh.sh" /usr/local/bin/argocd-refresh
sudo install -o root -g root -m 644 "$APP_DIR/argocd-refresh.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable argocd-refresh.service
sudo systemctl restart argocd-refresh.service
systemctl --no-pager --lines=0 status argocd-refresh.service | head -3
