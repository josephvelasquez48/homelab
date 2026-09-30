#!/usr/bin/env python3
"""Mac pull backup. Requires installed Pi collector and an initialized Restic repo.

With --if-due (how the LaunchAgent runs it: at 03:00, at login, and every 15
minutes) it backs up only when no backup has succeeded since the most recent
03:00, and only once the Pi answers. A Mac that was off or asleep at 03:00
catches up soon after it's back, instead of waiting for the next night.
"""
import datetime
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

PI = '192.168.1.253'
SCHEDULED_HOUR = 3  # 03:00 local, as in install-schedule.py

base = Path.home() / 'Backups/homelab'
config = Path.home() / '.config/homelab-backup'
# Written after a whole run succeeds: snapshot, recovery copy and check.
stamp = base / 'last-success'
restic = '/opt/homebrew/bin/restic'


def last_scheduled(now):
    """The most recent 03:00 at or before now."""
    today = now.replace(hour=SCHEDULED_HOUR, minute=0, second=0, microsecond=0)
    return today if now >= today else today - datetime.timedelta(days=1)


def is_due(now, last_success):
    """No success yet, or none since the most recent 03:00."""
    return last_success is None or last_success < last_scheduled(now)


def read_stamp(path):
    try:
        return datetime.datetime.fromtimestamp(float(path.read_text().strip()))
    except (OSError, ValueError):
        return None


def pi_reachable(timeout=5):
    try:
        socket.create_connection((PI, 22), timeout=timeout).close()
        return True
    except OSError:
        return False


def backup(env):
    # Clear locks left by restic processes that no longer exist. Every restic
    # call in these scripts has a subprocess timeout, and a timeout kills with
    # SIGKILL, which gives restic no chance to remove its lock. One left on
    # 2026-09-11 made every nightly integrity check fail for two nights with
    # "repository is already locked", though the snapshots themselves saved.
    # unlock only removes stale locks, so a rehearsal running at the same time
    # keeps its own; the flock in main() already rules out a second backup.
    subprocess.run([restic, 'unlock'], env=env, check=True, timeout=120)
    with tempfile.TemporaryDirectory(prefix='staging-', dir=base) as tmp:
        archive = Path(tmp) / 'pi.tar'
        with archive.open('wb') as out:
            subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', 'joe@' + PI, 'sudo -n /usr/bin/python3 /usr/local/lib/homelab-backup/pi-snapshot.py'], stdout=out, check=True, timeout=600)
        # stdin gives snapshots a stable path even though staging paths vary.
        with archive.open('rb') as data:
            subprocess.run([restic, 'backup', '--stdin', '--stdin-filename', 'pi.tar', '--tag', 'pi'], stdin=data, env=env, check=True, timeout=600)
    subprocess.run([restic, 'backup', str(config / 'recovery'), '--tag', 'recovery'], env=env, check=True, timeout=300)
    subprocess.run([restic, 'check', '--read-data'], env=env, check=True, timeout=600)
    # Retention is intentionally not automatic until a full recovery rehearsal.


def main(argv):
    os.umask(0o077)
    if_due = '--if-due' in argv
    base.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    if if_due and not is_due(now, read_stamp(stamp)):
        return 0  # every 15 minutes; say nothing
    if if_due and not pi_reachable():
        # Right after login the network is often not up yet. Exit 0, not a
        # failure: the next check is 15 minutes away, and a Pi that stays
        # unreachable shows up as HomelabBackupStale.
        print('%s backup due, waiting for the Pi' % now.isoformat(timespec='seconds'), flush=True)
        return 0
    env = dict(os.environ, RESTIC_REPOSITORY=str(base / 'repository'), RESTIC_PASSWORD_FILE=str(config / 'password'))
    with (base / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if if_due:
                return 0  # a backup is already running
            raise
        backup(env)
        stamp.write_text('%f\n' % datetime.datetime.now().timestamp())
    print('%s backup and full repository integrity check succeeded' % datetime.datetime.now().isoformat(timespec='seconds'), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
