#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$SCRIPT_DIR/../coredns/blocklist.hosts"
URL="https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"

TMP="$(mktemp)"
curl -fsSL "$URL" -o "$TMP"
mv "$TMP" "$DEST"

echo "$(date -Iseconds) blocklist updated: $(wc -l < "$DEST") lines"
