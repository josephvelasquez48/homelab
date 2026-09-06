"""Tiny Prometheus exporter for AdGuard Home.

AdGuard has no native /metrics endpoint. This polls its /control/status
and /control/stats API and exposes exactly the metrics
kubernetes/monitoring/grafana.yaml's dashboard queries - not a
general-purpose AdGuard exporter, so field names/shapes below are
pinned to what those two endpoints actually returned on v0.107.79,
confirmed live before writing this rather than guessed from docs.
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request

from prometheus_client import Gauge, start_http_server

ADGUARD_URL = os.environ.get("ADGUARD_URL", "http://127.0.0.1:3000")
ADGUARD_USERNAME = os.environ["ADGUARD_USERNAME"]
ADGUARD_PASSWORD = os.environ["ADGUARD_PASSWORD"]
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9618"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adguard-exporter")

# Never logged, never included in an exception message below - only ever
# sent as this one request header.
_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{ADGUARD_USERNAME}:{ADGUARD_PASSWORD}".encode()
).decode()

# Scrape health, independent of AdGuard's own self-reported "running" state -
# this is what tells you the *other* gauges below are current, not stale
# values left over from the last successful poll (see poll()'s except branch).
up = Gauge("adguard_up", "Whether the last scrape of AdGuard's API succeeded")

running = Gauge("adguard_running", "Whether AdGuard Home is running")
protection_enabled = Gauge("adguard_protection_enabled", "Whether DNS filtering is enabled")
queries = Gauge("adguard_queries", "Total queries processed in the stats period")
queries_blocked = Gauge("adguard_queries_blocked", "Total queries blocked by filters")
avg_processing_time = Gauge(
    "adguard_avg_processing_time_seconds", "Average query processing time"
)
top_blocked_domains = Gauge(
    "adguard_top_blocked_domains", "Blocked query count by domain", ["domain"]
)
top_queried_domains = Gauge(
    "adguard_top_queried_domains", "Query count by domain", ["domain"]
)
top_upstream_response_time = Gauge(
    "adguard_top_upstreams_avg_response_time_seconds",
    "Average upstream response time",
    ["upstream"],
)
scrape_errors = Gauge("adguard_scrape_errors_total", "Failed polls against AdGuard's API")


def _get(path: str) -> dict:
    req = urllib.request.Request(
        f"{ADGUARD_URL}{path}", headers={"Authorization": _AUTH_HEADER}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _invalidate() -> None:
    """Called on a failed scrape so stale numbers can't be mistaken for
    current ones - NaN rather than 0, since 0 is a real, meaningfully
    different value for every one of these (e.g. "0 queries blocked" vs
    "we don't know"). Paired with up=0, which is the actual signal
    anything querying these should check first."""
    for gauge in (running, protection_enabled, queries, queries_blocked, avg_processing_time):
        gauge.set(float("nan"))
    top_blocked_domains.clear()
    top_queried_domains.clear()
    top_upstream_response_time.clear()


def poll() -> None:
    try:
        status = _get("/control/status")
        stats = _get("/control/stats")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        # Exception type only, never str(exc)/repr(exc) - urllib exceptions
        # can echo back request details, and the request here carries the
        # Authorization header. Nothing below ever touches ADGUARD_PASSWORD
        # or _AUTH_HEADER.
        log.warning("AdGuard scrape failed: %s", type(exc).__name__)
        scrape_errors.inc()
        up.set(0)
        _invalidate()
        return

    up.set(1)
    running.set(1 if status["running"] else 0)
    protection_enabled.set(1 if status["protection_enabled"] else 0)
    queries.set(stats["num_dns_queries"])
    queries_blocked.set(stats["num_blocked_filtering"])
    avg_processing_time.set(stats["avg_processing_time"])

    top_blocked_domains.clear()
    for entry in stats["top_blocked_domains"]:
        for domain, count in entry.items():
            top_blocked_domains.labels(domain=domain).set(count)

    top_queried_domains.clear()
    for entry in stats["top_queried_domains"]:
        for domain, count in entry.items():
            top_queried_domains.labels(domain=domain).set(count)

    top_upstream_response_time.clear()
    for entry in stats["top_upstreams_avg_time"]:
        for upstream, seconds in entry.items():
            top_upstream_response_time.labels(upstream=upstream).set(seconds)


def main() -> None:
    start_http_server(LISTEN_PORT)
    while True:
        poll()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
