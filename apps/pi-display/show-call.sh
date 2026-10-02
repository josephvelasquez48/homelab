#!/bin/sh
# Bring the Pi's call screen back after its Home button sent it away. Run by
# the homelab-call:// link the display's Call button follows; the link
# arrives as $1 and isn't needed. The phone service (apps/phone) trusts its
# agent token, read from its config; loopback, with the certificate checked
# against the homelab CA (install.sh fills in @CA@).
TOKEN=$(sed -n 's/^PHONE_AGENT_TOKEN=//p' "$HOME/.config/phone-bridge/env")
exec curl -sS --max-time 5 -o /dev/null -X POST \
  --resolve phone.home:8443:127.0.0.1 --cacert "@CA@" \
  -H "Authorization: Bearer $TOKEN" \
  https://phone.home:8443/api/agent/screen-show
