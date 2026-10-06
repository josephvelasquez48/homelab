import math
import os
import urllib.error
from unittest.mock import patch

# main.py reads these at import time (fails fast on real misconfiguration) -
# set before import so the test suite doesn't need real credentials.
os.environ.setdefault("ADGUARD_USERNAME", "admin")
os.environ.setdefault("ADGUARD_PASSWORD", "test")

import main


def _sample_status():
    return {"running": True, "protection_enabled": True}


def _sample_stats():
    return {
        "num_dns_queries": 706,
        "num_blocked_filtering": 151,
        "avg_processing_time": 0.152776,
        "top_blocked_domains": [{"browser-intake-us5-datadoghq.com": 114}],
        "top_queried_domains": [{"mozilla.cloudflare-dns.com": 71}],
        "top_upstreams_avg_time": [{"1.1.1.1:53": 0.025}],
        "dns_queries": [300, 400, 6],
        "blocked_filtering": [60, 90, 1],
    }


def _metric_value(gauge, **labels):
    for sample in gauge.collect()[0].samples:
        if sample.labels == labels:
            return sample.value
    raise AssertionError(f"no sample with labels {labels}")


def test_poll_populates_gauges_from_real_field_shapes():
    with patch("main._get", side_effect=[_sample_status(), _sample_stats()]):
        main.poll()

    assert _metric_value(main.up) == 1
    assert _metric_value(main.running) == 1
    assert _metric_value(main.protection_enabled) == 1
    assert _metric_value(main.queries) == 706
    assert _metric_value(main.queries_blocked) == 151
    assert _metric_value(main.avg_processing_time) == 0.152776
    assert (
        _metric_value(main.top_blocked_domains, domain="browser-intake-us5-datadoghq.com")
        == 114
    )
    assert (
        _metric_value(main.top_queried_domains, domain="mozilla.cloudflare-dns.com") == 71
    )
    assert (
        _metric_value(main.top_upstream_response_time, upstream="1.1.1.1:53") == 0.025
    )


def test_poll_increments_scrape_errors_on_failure_without_raising():
    before = _metric_value(main.scrape_errors)
    with patch("main._get", side_effect=TimeoutError):
        main.poll()
    assert _metric_value(main.scrape_errors) == before + 1


def test_failed_scrape_sets_up_zero_and_does_not_leave_stale_healthy_values():
    with patch("main._get", side_effect=[_sample_status(), _sample_stats()]):
        main.poll()
    assert _metric_value(main.queries) == 706  # sanity: a real value is there first

    with patch("main._get", side_effect=TimeoutError):
        main.poll()

    assert _metric_value(main.up) == 0
    assert math.isnan(_metric_value(main.running))
    assert math.isnan(_metric_value(main.protection_enabled))
    assert math.isnan(_metric_value(main.queries))
    assert math.isnan(_metric_value(main.queries_blocked))
    assert math.isnan(_metric_value(main.avg_processing_time))
    assert main.top_blocked_domains.collect()[0].samples == []
    assert main.top_queried_domains.collect()[0].samples == []
    assert main.top_upstream_response_time.collect()[0].samples == []


def test_failure_message_never_includes_the_password():
    with patch(
        "main._get", side_effect=urllib.error.URLError(f"auth failed for {main.ADGUARD_PASSWORD}")
    ):
        with patch("main.log") as mock_log:
            main.poll()

    logged_text = " ".join(str(call) for call in mock_log.warning.call_args_list)
    assert main.ADGUARD_PASSWORD not in logged_text
    assert main._AUTH_HEADER not in logged_text


def test_hourly_buckets_become_a_counter_that_never_goes_down():
    hourly = main.HourlyCounter()
    assert hourly.step([4573, 3800]) == 0  # the first poll only takes a reading
    assert hourly.step([4573, 3850]) == 50  # the current hour grew
    assert hourly.step([4573, 3889]) == 39
    # The hour rolls over: the old hour's last 0 after our reading, plus the
    # new hour's 126 - not the 69,000 the rolling window's drop looked like.
    assert hourly.step([3889, 126]) == 126
    assert hourly.step([3880, 3889, 130]) == 4
    # Rolled over with some of the old hour unseen: that's counted too.
    hourly.last = 100
    assert hourly.step([150, 20]) == 50 + 20


def test_poll_feeds_the_counters():
    before = _metric_value(main.queries_total)
    with patch("main._get", side_effect=[_sample_status(), _sample_stats()]):
        main.poll()
    stats = _sample_stats()
    stats["dns_queries"] = [300, 400, 16]
    with patch("main._get", side_effect=[_sample_status(), stats]):
        main.poll()
    assert _metric_value(main.queries_total) >= before + 10
