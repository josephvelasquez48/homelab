#!/bin/bash
# Verifies that the dashboard's gaming-mode endpoints are actually locked
# down *in the deployed environment* - not just in unit tests, which cannot
# see Traefik, the Ingress, the real Secret, or the cookie attributes a
# browser will enforce.
#
# Passing this is the gate for removing the /api/gpu/release exclusions from
# the ZAP and nuclei runs in docs/security-testing.md. Until it passes
# against the live host, keep the exclusions: an active scan that reaches
# these endpoints drains a node.
#
# Safe to run against production. Every request it sends is unauthenticated
# and is expected to be rejected, so a passing run changes nothing. A
# FAILING run is the case worth catching - which is the point.
#
# Usage:
#   ./scripts/verify-dashboard-auth.sh [host]
#   DASHBOARD_PASSWORD=... ./scripts/verify-dashboard-auth.sh   # also checks the positive path
set -uo pipefail

HOST="${1:-https://dashboard.home}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

pass=0
fail=0

check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        printf '  ok    %-52s %s\n' "$label" "$actual"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-52s got %s, want %s\n' "$label" "$actual" "$expected"
        fail=$((fail + 1))
    fi
}

status() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@"; }

echo "Verifying dashboard auth at $HOST"
echo

echo "Reachability"
check "GET / still serves the page" 200 "$(status "$HOST/")"
check "GET /health" 200 "$(status "$HOST/health")"

echo
echo "Authorization (the boundary)"
check "POST /api/gpu/release unauthenticated" 401 \
    "$(status -X POST -H 'Content-Type: application/json' -d '{}' "$HOST/api/gpu/release")"

# The drive-by request: no Content-Type, no body, no cookie. This is what a
# hostile page can make a LAN user's browser send, so it is the single most
# important line in this script.
check "POST /api/gpu/release as a CORS-simple request" 401 \
    "$(status -X POST "$HOST/api/gpu/release")"

check "POST /api/gpu/release form-encoded" 401 \
    "$(status -X POST -H 'Content-Type: application/x-www-form-urlencoded' -d 'x=1' "$HOST/api/gpu/release")"

check "POST /api/gpu/release with a forged session cookie" 401 \
    "$(status -X POST -H 'Content-Type: application/json' -d '{}' \
        -H 'Cookie: dashboard_session=eyJhdXRoZW50aWNhdGVkIjogdHJ1ZX0=' "$HOST/api/gpu/release")"

echo
echo "Cross-origin"
# No CORS middleware is registered, so the preflight a JSON POST requires
# must not succeed. A 200 with Access-Control-Allow-Origin here would mean
# someone added permissive CORS and reopened the browser-driven path.
preflight_acao="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X OPTIONS -H 'Origin: http://evil.example' \
    -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: content-type' \
    "$HOST/api/gpu/release")"
if [[ "$preflight_acao" == "200" ]]; then
    acao="$(curl -s -i --max-time 10 -X OPTIONS -H 'Origin: http://evil.example' \
        -H 'Access-Control-Request-Method: POST' "$HOST/api/gpu/release" \
        | grep -ci 'access-control-allow-origin' || true)"
    check "preflight grants no Access-Control-Allow-Origin" 0 "$acao"
else
    printf '  ok    %-52s %s\n' "preflight is not answered" "$preflight_acao"
    pass=$((pass + 1))
fi

echo
echo "Configuration"
# A dashboard with no Secret applied passes every check above for the wrong
# reason: nobody can log in, so nothing is reachable. That is safe but
# broken, and it should not be mistaken for a healthy result.
session_json="$(curl -s --max-time 10 "$HOST/api/session")"
if grep -q '"configured": *true' <<<"$session_json"; then
    printf '  ok    %-52s\n' "dashboard-auth Secret is applied"
    pass=$((pass + 1))
else
    # Counted as a failure, not a warning. Every check above passes in
    # this state - for the wrong reason. Nobody can log in, so nothing
    # is reachable, and a script that exited 0 here would report
    # "secure" for a deployment that is simply broken. The two
    # outcomes must not look alike.
    printf '  FAIL  %-52s %s\n' "dashboard-auth Secret is NOT applied" "$session_json"
    echo "        The endpoints are closed, but only because nobody can authenticate."
    echo "        Gaming mode is unusable until you apply it. See docs/dashboard.md."
    fail=$((fail + 1))
fi

if [[ -n "${DASHBOARD_PASSWORD:-}" ]]; then
    echo
    echo "Positive path"
    check "POST /api/login with the wrong password" 401 \
        "$(status -X POST -H 'Content-Type: application/json' -d '{"password":"wrong"}' "$HOST/api/login")"
    check "POST /api/login with the real password" 200 \
        "$(status -c "$JAR" -X POST -H 'Content-Type: application/json' \
            -d "{\"password\":\"$DASHBOARD_PASSWORD\"}" "$HOST/api/login")"
    # Deliberately stops at proving the session is live. Actually calling
    # /api/gpu/release here would evict a model mid-request for whoever is
    # using inference, which is not a verification script's business.
    if grep -q '"authenticated": *true' <<<"$(curl -s -b "$JAR" --max-time 10 "$HOST/api/session")"; then
        printf '  ok    %-52s\n' "session is established after login"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-52s\n' "session is established after login"
        fail=$((fail + 1))
    fi
fi

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
