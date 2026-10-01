#!/bin/sh
# Ask Argo CD to look at git now, instead of on its next poll (up to 3
# minutes): every 15 s, when main has moved since the last check, annotate
# every Application with argocd.argoproj.io/refresh, which makes Argo CD
# fetch the repo and sync right away. Run by argocd-refresh.service.
#
# Outbound only: GitHub can't reach the LAN, so a webhook would need a
# public endpoint. git ls-remote, not the GitHub API: anonymous API calls
# are limited to 60 an hour, and even a 304 counts (measured). A loop, not
# a timer: starting the sandboxed unit cost ~0.07 s of CPU each time, as
# much as the check itself (~0.09 s). Total ~0.6% of one core. The repo is
# public, so HTTPS needs no key. See docs/argocd.md.
set -u

REPO=https://github.com/josephvelasquez48/homelab.git
STATE="${STATE_DIRECTORY:-/var/lib/argocd-refresh}/main"
INTERVAL=15

while :; do
  sha=$(timeout 20 git ls-remote "$REPO" refs/heads/main 2>/dev/null | cut -f1)
  last=$(cat "$STATE" 2>/dev/null || true)
  # Nothing back means GitHub was unreachable: try again next time.
  if [ -n "$sha" ] && [ "$sha" != "$last" ]; then
    if [ -z "$last" ]; then
      # The first check only records where main is; nothing has changed yet.
      echo "$sha" >"$STATE"
    elif k3s kubectl -n argocd annotate applications --all argocd.argoproj.io/refresh=normal --overwrite >/dev/null; then
      echo "main moved to ${sha%"${sha#???????}"}: asked Argo CD to refresh"
      echo "$sha" >"$STATE"
    fi # if the annotate failed, the state stays put and the next check retries
  fi
  sleep "$INTERVAL"
done
