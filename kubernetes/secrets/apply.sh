#!/bin/bash
# Decrypts and applies the SOPS-encrypted secrets in this directory.
# Deliberately NOT synced by Argo CD - see docs/secrets.md for why (no
# KSOPS plugin set up; Argo CD can't decrypt these on its own, so they're
# applied out-of-band instead of through the normal GitOps path).
#
# Requires: sops, kubectl (with KUBECONFIG pointed at the cluster), and
# the age private key at its default location
# (~/.config/sops/age/keys.txt on Linux, %APPDATA%\sops\age\keys.txt on
# Windows) or $SOPS_AGE_KEY_FILE pointing at it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The glob below is deliberately non-recursive. kubernetes/secrets/reference/
# holds SOPS-encrypted *reference copies* of credentials whose live source of
# truth lives elsewhere - Argo CD's admin password, for one, where the real
# value is a bcrypt hash inside a Secret the service manages itself. Those
# files are not Secret manifests, and applying one would strip keys Argo CD
# needs. Keeping them a directory deeper puts them out of reach of this loop
# by construction rather than by remembering to. See docs/secrets.md.
for f in "$SCRIPT_DIR"/*.enc.yaml; do
    echo "Applying $(basename "$f")..."
    sops --decrypt "$f" | kubectl apply -f -
done
