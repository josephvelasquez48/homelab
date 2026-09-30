"""When the LaunchAgent's frequent --if-due runs actually back up."""
import datetime
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "mac_backup", Path(__file__).resolve().parent.parent / "mac-backup.py"
)
mac_backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mac_backup)  # safe: the ssh and restic work is in main()

D = datetime.datetime


def test_last_scheduled_is_the_most_recent_three_am():
    assert mac_backup.last_scheduled(D(2026, 9, 30, 2, 59)) == D(2026, 9, 29, 3, 0)
    assert mac_backup.last_scheduled(D(2026, 9, 30, 3, 0)) == D(2026, 9, 30, 3, 0)
    assert mac_backup.last_scheduled(D(2026, 9, 30, 17, 0)) == D(2026, 9, 30, 3, 0)


def test_due_at_three_am_and_not_again_until_the_next():
    last = D(2026, 9, 29, 3, 1)  # last night's backup
    assert mac_backup.is_due(D(2026, 9, 30, 3, 0), last)  # tonight's slot
    assert not mac_backup.is_due(D(2026, 9, 29, 23, 45), last)  # checks during the day do nothing


def test_catches_up_when_the_mac_is_back_after_a_missed_night():
    last = D(2026, 9, 19, 3, 1)  # the Mac was off for ten days
    assert mac_backup.is_due(D(2026, 9, 29, 17, 30), last)
    # Once caught up at 17:45, nothing more until the next 03:00.
    assert not mac_backup.is_due(D(2026, 9, 29, 23, 0), D(2026, 9, 29, 17, 45))
    assert mac_backup.is_due(D(2026, 9, 30, 3, 0), D(2026, 9, 29, 17, 45))


def test_no_record_of_a_success_is_due(tmp_path):
    assert mac_backup.read_stamp(tmp_path / "missing") is None
    assert mac_backup.is_due(D(2026, 9, 29, 12, 0), None)
    (tmp_path / "junk").write_text("not a time")
    assert mac_backup.read_stamp(tmp_path / "junk") is None


def test_stamp_round_trip(tmp_path):
    p = tmp_path / "last-success"
    p.write_text("%f\n" % D(2026, 9, 29, 17, 45).timestamp())
    assert mac_backup.read_stamp(p) == D(2026, 9, 29, 17, 45)
