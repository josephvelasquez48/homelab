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

from prometheus_client import Counter, Gauge, start_http_server

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
# The two above are AdGuard's rolling 24-hour window: every hour the oldest
# hour falls off and they go *down*, which rate() reads as a counter reset -
# it counted the whole day as new, and the dashboard showed ~14,000 DNS
# queries a minute for five minutes after every hour (2026-10-05; the real
# rate was ~65). These are real counters, only ever going up, built from
# AdGuard's hourly buckets (HourlyCounter below). Use these for rates.
queries_total = Counter("adguard_dns_queries", "Queries processed, counted as they happen")
queries_blocked_total = Counter("adguard_dns_blocked", "Queries blocked by filters, counted as they happen")
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


class HourlyCounter:
    """Turns AdGuard's hourly buckets (the last one is the current hour)
    into increments for a real counter.

    Within an hour, the increment is how much the current bucket grew. When
    the hour rolls over the new bucket starts small again: the increment is
    what the old hour gained after the last poll (now the second-to-last
    bucket) plus all of the new one. The first poll only takes a reading.
    """

    def __init__(self):
        self.last: int | None = None

    def step(self, buckets: list[int]) -> int:
        current = buckets[-1]
        if self.last is None:
            delta = 0
        elif current >= self.last:
            delta = current - self.last
        else:  # a new hour
            delta = max(buckets[-2] - self.last, 0) + current
        self.last = current
        return delta


_queries_hourly = HourlyCounter()
_blocked_hourly = HourlyCounter()


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
    queries_total.inc(_queries_hourly.step(stats["dns_queries"]))
    queries_blocked_total.inc(_blocked_hourly.step(stats["blocked_filtering"]))
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
