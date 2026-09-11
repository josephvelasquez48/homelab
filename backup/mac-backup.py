#!/usr/bin/env python3
"""Mac pull backup. Requires installed Pi collector and an initialized Restic repo."""
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile

os.umask(0o077)
base = Path.home() / 'Backups/homelab'
config = Path.home() / '.config/homelab-backup'
env = dict(os.environ, RESTIC_REPOSITORY=str(base / 'repository'), RESTIC_PASSWORD_FILE=str(config / 'password'))
restic = '/opt/homebrew/bin/restic'
base.mkdir(parents=True, exist_ok=True)
with (base / 'run.lock').open('w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with tempfile.TemporaryDirectory(prefix='staging-', dir=base) as tmp:
        archive = Path(tmp) / 'pi.tar'
        with archive.open('wb') as out:
            subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', 'joe@192.168.1.253', 'sudo -n /usr/bin/python3 /usr/local/lib/homelab-backup/pi-snapshot.py'], stdout=out, check=True, timeout=600)
        # stdin gives snapshots a stable path even though staging paths vary.
        with archive.open('rb') as data:
            subprocess.run([restic, 'backup', '--stdin', '--stdin-filename', 'pi.tar', '--tag', 'pi'], stdin=data, env=env, check=True, timeout=600)
    subprocess.run([restic, 'backup', str(config / 'recovery'), '--tag', 'recovery'], env=env, check=True, timeout=300)
    subprocess.run([restic, 'check', '--read-data'], env=env, check=True, timeout=600)
    # Retention is intentionally not automatic until a full recovery rehearsal.
print('Backup and full repository integrity check succeeded', flush=True)
