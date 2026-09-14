"""The reporter has to read restic's timestamps on the Mac's Python 3.9.

These run in CI under 3.9 on purpose. The bug they cover does not exist on
newer Pythons, whose fromisoformat accepts any number of fractional digits,
so a test run on a current interpreter would pass against the broken code.
"""
import datetime
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "publish", Path(__file__).resolve().parent.parent / "publish-backup-metrics.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)  # safe: the ssh and launchctl work is in main()


def expected(text):
    return datetime.datetime(2026, 9, 12, 3, 0, 14,
                             tzinfo=datetime.timezone(datetime.timedelta(hours=-7))).timestamp() + text


def test_the_timestamp_that_broke_the_report():
    """Five fractional digits, from the real 2026-09-12 snapshot."""
    assert publish.restic_time("2026-09-12T03:00:14.69541-07:00") == expected(0.69541)


def test_every_fraction_length_restic_can_write():
    # Go drops trailing zeros, so one to nine digits all occur in practice.
    for digits in range(1, 10):
        fraction = "1" + "0" * (digits - 1) if digits > 1 else "1"
        value = "2026-09-12T03:00:14.%s-07:00" % fraction[:digits]
        assert abs(publish.restic_time(value) - expected(0.1)) < 1e-6, value


def test_no_fraction_and_utc():
    assert publish.restic_time("2026-09-12T03:00:14-07:00") == expected(0)
    assert publish.restic_time("2026-09-12T10:00:14Z") == expected(0)


def test_one_unreadable_snapshot_does_not_hide_the_newer_ones():
    """The actual failure: the loop stopped at 12 Sep and reported 11 Sep as newest."""
    snaps = [
        {"short_id": "1ff161b9", "time": "2026-09-11T03:30:06.962601-07:00"},
        {"short_id": "broken", "time": "not a time"},
        {"short_id": "a03dbed2", "time": "2026-09-13T03:00:10.125767-07:00"},
    ]
    newest = publish.newest_snapshot(snaps)
    assert newest == publish.restic_time("2026-09-13T03:00:10.125767-07:00")


def test_the_real_repository_listing_from_the_mac():
    """All 18 timestamps restic listed on 2026-09-13, verbatim."""
    times = """2026-09-10T04:37:20.163512-07:00 2026-09-10T04:37:21.085834-07:00
    2026-09-10T04:39:06.222356-07:00 2026-09-10T04:39:07.205157-07:00
    2026-09-10T04:51:33.855795-07:00 2026-09-10T04:51:34.740298-07:00
    2026-09-10T17:32:12.948825-07:00 2026-09-10T17:32:13.901015-07:00
    2026-09-11T02:36:04.047729-07:00 2026-09-11T02:36:05.159844-07:00
    2026-09-11T03:00:10.685375-07:00 2026-09-11T03:00:11.736312-07:00
    2026-09-11T03:30:06.962601-07:00 2026-09-11T03:30:07.993326-07:00
    2026-09-12T03:00:14.69541-07:00 2026-09-12T03:00:15.622787-07:00
    2026-09-13T03:00:10.125767-07:00 2026-09-13T03:00:11.054443-07:00""".split()
    newest = publish.newest_snapshot([{"time": t} for t in times])
    assert newest == publish.restic_time("2026-09-13T03:00:11.054443-07:00")
