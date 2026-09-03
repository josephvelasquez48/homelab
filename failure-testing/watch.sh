#!/bin/bash
# Continuous health-check watcher used during failure injection - logs a
# timestamped status code (and curl error, if any) once per second so a
# failure scenario's actual downtime window and error responses are
# captured directly, not estimated from before/after snapshots.
#
# Usage:
#   ./watch.sh http://api.home/health > run.log &
#   WATCH_PID=$!
#   ... inject failure, wait, confirm recovery ...
#   kill $WATCH_PID
#
# Log line format: <unix_ts> <http_status_or_000> <curl_error_or_ok>
set -euo pipefail

URL="${1:?usage: watch.sh <url>}"

while true; do
    ts=$(date +%s.%N)
    body_and_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$URL" 2>&1) || body_and_code="000"
    echo "$ts $body_and_code"
    sleep 1
done
