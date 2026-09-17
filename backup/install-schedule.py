"""Register the nightly backup as a LaunchAgent.

Pass --replace to reinstall over an existing agent, for example after a
change to the interpreter below. Without it an existing agent is left alone,
because it may have been edited by hand and deserves a look first.
"""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

# Homebrew's Python, not /usr/bin/python3. That one is a shim into whichever
# developer directory xcode-select points at, and when that is Xcode.app,
# every Xcode update - the App Store installs them automatically - makes it
# refuse to start until someone runs `sudo xcodebuild -license`. It exits 69
# and nothing runs. That stopped the backup on 2026-09-11 and again on
# 2026-09-16. restic already comes from Homebrew, so this adds no new kind
# of dependency. /opt/homebrew/bin/python3 is a symlink that brew keeps
# pointing at the current version, and these scripts use only the standard
# library, so a Python upgrade does not break them.
PYTHON = '/opt/homebrew/bin/python3'

os.umask(0o077)
config = Path.home() / '.config/homelab-backup'
path = Path.home() / 'Library/LaunchAgents/local.homelab.backup.plist'
domain = 'gui/%d' % os.getuid()
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    if '--replace' not in sys.argv:
        raise RuntimeError('LaunchAgent already exists; inspect it, then rerun with --replace')
    # Not loaded is fine here, so the exit code is not checked.
    subprocess.run(['launchctl', 'bootout', domain, str(path)])
data = {
    'Label': 'local.homelab.backup',
    'ProgramArguments': [PYTHON, str(config / 'mac-backup.py')],
    'StartCalendarInterval': {'Hour': 3, 'Minute': 0},
    'RunAtLoad': True,
    'StandardOutPath': str(config / 'backup.log'),
    'StandardErrorPath': str(config / 'backup-error.log'),
    'Umask': 63,
}
with path.open('wb') as f:
    plistlib.dump(data, f)
subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
print('Nightly 03:00 local-time backup registered, plus a run at login')
