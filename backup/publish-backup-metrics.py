#!/usr/bin/env python3
"""Publish backup health to the Pi's node_exporter as a Prometheus textfile.

Runs on the Mac, on its own schedule, deliberately separate from
mac-backup.py. A reporter that only runs when the backup runs cannot
report that the backup did not run, which is the failure this exists to
catch: on 2026-09-11 the agent exited 69 for fourteen minutes while
backup.log still ended with "succeeded", because failing runs died before
writing anything.

Reports state rather than events, so the metrics are meaningful even when
nothing has happened for days. Uses the existing Mac-to-Pi SSH key, so
there is no new listener on the Mac and no inbound access to it.
"""
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

# restic writes Go's RFC3339Nano, which drops trailing zeros from the
# fraction, so a timestamp can carry anywhere from one to nine digits after
# the seconds. Python 3.9's fromisoformat - /usr/bin/python3 on the Mac,
# which ran this until 2026-09-16 - accepts exactly three or six.
# 2026-09-12T03:00:14.69541-07:00 has five. The agents now use Homebrew's
# newer Python, but this stays correct on 3.9 in case it runs there again.
_RESTIC_TIME = re.compile(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def restic_time(value):
    """Unix time for a restic snapshot timestamp, on any Python from 3.9."""
    match = _RESTIC_TIME.match(value)
    if not match:
        raise ValueError("unrecognised restic timestamp: %r" % value)
    base, fraction, offset = match.groups()
    fraction = ((fraction or "") + "000000")[:6]
    offset = "+00:00" if offset == "Z" else offset
    return datetime.datetime.fromisoformat("%s.%s%s" % (base, fraction, offset)).timestamp()


def newest_snapshot(snaps):
    """Newest snapshot time, skipping any timestamp that cannot be read.

    Skipping one rather than abandoning the list is the point. From
    2026-09-12 a single five-digit fraction raised out of the loop, so every
    snapshot after it was ignored: the report said the newest backup was
    sixty-five hours old and the repository unreadable, while a good
    snapshot had been taken every night.
    """
    newest = 0
    for s in snaps:
        try:
            newest = max(newest, restic_time(s["time"]))
        except (KeyError, ValueError) as exc:
            print("skipping snapshot %s: %s" % (s.get("short_id", "?"), exc), file=sys.stderr)
    return newest

PI = 'joe@192.168.1.253'
TEXTFILE = '/var/lib/node_exporter/textfile/homelab_backup.prom'
config = Path.home() / '.config/homelab-backup'


def main():
    os.umask(0o077)
    env = dict(os.environ,
               RESTIC_REPOSITORY=str(Path.home() / 'Backups/homelab/repository'),
               RESTIC_PASSWORD_FILE=str(config / 'password'))

    # Snapshot age is the metric that matters. It stays true whatever went
    # wrong - a crashed run, an unloaded agent, a Mac that never woke - where
    # "did the last run succeed" only describes runs that happened.
    repo_ok = 1
    try:
        out = subprocess.run(['/opt/homebrew/bin/restic', 'snapshots', '--json'],
                             env=env, capture_output=True, text=True,
                             check=True, timeout=120).stdout
        snaps = json.loads(out)
    except Exception:
        # Only a failure to list is "unreadable". A timestamp this script
        # cannot parse is this script's problem, not the repository's.
        repo_ok, snaps = 0, []
    count = len(snaps)
    newest = newest_snapshot(snaps)

    # launchd's own record of the last run. Distinguishes "ran and failed"
    # from "never ran", which the snapshot age alone cannot.
    exit_code, agent_loaded = -1, 0
    try:
        out = subprocess.run(
            ['launchctl', 'print', 'gui/%d/local.homelab.backup' % os.getuid()],
            capture_output=True, text=True, timeout=30).stdout
        if out.strip():
            agent_loaded = 1
            for line in out.splitlines():
                if 'last exit code' in line:
                    value = line.split('=')[-1].strip().split(':')[0]
                    exit_code = int(value) if value.isdigit() else -1
    except Exception:
        pass

    body = """# HELP homelab_backup_last_snapshot_timestamp_seconds Newest restic snapshot, unix time.
# TYPE homelab_backup_last_snapshot_timestamp_seconds gauge
homelab_backup_last_snapshot_timestamp_seconds %d
# HELP homelab_backup_snapshot_count Snapshots in the repository.
# TYPE homelab_backup_snapshot_count gauge
homelab_backup_snapshot_count %d
# HELP homelab_backup_repository_readable 1 if restic could list the repository.
# TYPE homelab_backup_repository_readable gauge
homelab_backup_repository_readable %d
# HELP homelab_backup_last_exit_code Last exit code of the backup LaunchAgent, -1 if unknown.
# TYPE homelab_backup_last_exit_code gauge
homelab_backup_last_exit_code %d
# HELP homelab_backup_agent_loaded 1 if the LaunchAgent is loaded in launchd.
# TYPE homelab_backup_agent_loaded gauge
homelab_backup_agent_loaded %d
# HELP homelab_backup_report_timestamp_seconds When this report was generated, unix time.
# TYPE homelab_backup_report_timestamp_seconds gauge
homelab_backup_report_timestamp_seconds %d
""" % (newest, count, repo_ok, exit_code, agent_loaded, time.time())

    # Rename into place on the Pi rather than writing directly: node_exporter
    # reads this directory on every scrape and a half-written file is a parse
    # error, which would look like the exporter breaking rather than a slow copy.
    remote = """set -eu
umask 022
dir=$(dirname %s)
mkdir -p "$dir"
tmp=$(mktemp "%s.XXXXXX")
cat > "$tmp"
chmod 0644 "$tmp"
mv "$tmp" %s
""" % (shlex.quote(TEXTFILE), TEXTFILE, shlex.quote(TEXTFILE))

    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'ConnectTimeout=15', PI, 'sh -c ' + shlex.quote(remote)],
                   input=body, text=True, check=True, timeout=120)

    age = (time.time() - newest) / 3600 if newest else -1
    print('published: %d snapshots, newest %.1fh old, last exit %d'
          % (count, age, exit_code), flush=True)


if __name__ == '__main__':
    main()
