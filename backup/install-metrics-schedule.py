#!/usr/bin/env python3
"""Register the hourly backup-health reporter as its own LaunchAgent.

Deliberately separate from local.homelab.backup. A reporter that runs as
part of the backup cannot report that the backup never ran, and "never
ran" is the failure mode that hid for fourteen minutes on 2026-09-11 while
backup.log still ended with "succeeded".

Hourly rather than daily so a stale report is itself visible: the metrics
carry their own generation time, so a reporter that stops is distinguish-
able from a backup that stops.

Pass --replace to reinstall over an existing agent.
"""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

# Homebrew's Python, for the reason given in install-schedule.py: Apple's
# /usr/bin/python3 stops working after every Xcode update until the license
# is accepted again. The reporter failing the same way as the backup would
# mean nothing notices, which is the one thing this agent exists to avoid.
PYTHON = '/opt/homebrew/bin/python3'

os.umask(0o077)
config = Path.home() / '.config/homelab-backup'
path = Path.home() / 'Library/LaunchAgents/local.homelab.backup-metrics.plist'
domain = 'gui/%d' % os.getuid()
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    if '--replace' not in sys.argv:
        raise RuntimeError('LaunchAgent already exists; inspect it, then rerun with --replace')
    subprocess.run(['launchctl', 'bootout', domain, str(path)])
data = {
    'Label': 'local.homelab.backup-metrics',
    'ProgramArguments': [PYTHON, str(config / 'publish-backup-metrics.py')],
    # StartInterval rather than a calendar entry: this is a heartbeat, and
    # launchd fires a missed StartInterval soon after wake instead of
    # skipping the slot entirely the way a calendar trigger does.
    'StartInterval': 3600,
    'RunAtLoad': True,
    'StandardOutPath': str(config / 'backup-metrics.log'),
    'StandardErrorPath': str(config / 'backup-metrics-error.log'),
    'Umask': 63,
}
with path.open('wb') as f:
    plistlib.dump(data, f)
subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
print('Hourly backup-health reporter registered, plus a run at login')
