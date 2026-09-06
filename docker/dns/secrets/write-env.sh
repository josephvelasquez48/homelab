#!/bin/bash
# Decrypts adguard-admin.enc.yaml and ships docker/dns/.env to the Pi -
# same out-of-band pattern as kubernetes/secrets/apply.sh, adapted for
# Docker Compose (no `kubectl apply` equivalent to pipe into; it just
# wants a plain KEY=value file). Run this from wherever sops + the age
# private key actually live (the operator's machine - confirmed neither
# exists on the Pi itself, same as kubectl's kubeconfig for apply.sh
# living off-cluster), NOT on the Pi. Re-run after rotating the password
# (docs/secrets.md), then on the Pi: `docker compose up -d
# adguard-exporter` to pick up the new value.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_HOST="joe@192.168.1.253"

PASSWORD="$(sops --decrypt "$SCRIPT_DIR/adguard-admin.enc.yaml" | grep '^password:' | cut -d' ' -f2-)"

ssh "$PI_HOST" "cat > ~/apps/homelab/docker/dns/.env && chmod 600 ~/apps/homelab/docker/dns/.env" <<EOF
ADGUARD_PASSWORD=$PASSWORD
EOF

echo "Wrote docker/dns/.env on $PI_HOST"
