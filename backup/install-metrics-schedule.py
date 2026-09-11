#!/usr/bin/env python3
"""Register the hourly backup-health reporter as its own LaunchAgent.

Deliberately separate from local.homelab.backup. A reporter that runs as
part of the backup cannot report that the backup never ran, and "never
ran" is the failure mode that hid for fourteen minutes on 2026-09-11 while
backup.log still ended with "succeeded".

Hourly rather than daily so a stale report is itself visible: the metrics
carry their own generation time, so a reporter that stops is distinguish-
able from a backup that stops.
"""
import os
from pathlib import Path
import plistlib
import subprocess

os.umask(0o077)
config = Path.home() / '.config/homelab-backup'
path = Path.home() / 'Library/LaunchAgents/local.homelab.backup-metrics.plist'
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    raise RuntimeError('LaunchAgent already exists; inspect before replacing')
data = {
    'Label': 'local.homelab.backup-metrics',
    'ProgramArguments': ['/usr/bin/python3', str(config / 'publish-backup-metrics.py')],
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
subprocess.run(['launchctl', 'bootstrap', 'gui/%d' % os.getuid(), str(path)], check=True)
print('Hourly backup-health reporter registered, plus a run at login')
