#!/usr/bin/env python3
"""Restore selected backup members into temporary, network-isolated containers."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

os.umask(0o077)

def run(args, **kwargs):
    return subprocess.check_output(args, timeout=300, **kwargs)

def isolated(image, mounts, command, probe, options=()):
    name = 'homelab-check-' + uuid.uuid4().hex[:12]
    try:
        run(['docker', 'run', '-d', '--name', name, '--network', 'none',
             '--memory', '512m', '--cpus', '1', *options, *mounts, image, *command])
        for attempt in range(60):
            result = subprocess.run(['docker', 'exec', name, *probe],
                                    capture_output=True, timeout=10)
            if result.returncode == 0:
                return result.stdout
            if run(['docker', 'inspect', '--format={{.State.Running}}', name]).strip() != b'true':
                raise RuntimeError(f'{image} stopped before its recovery probe passed')
            time.sleep(1)
        raise RuntimeError(f'{image} recovery probe timed out')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

with tempfile.TemporaryDirectory(prefix='homelab-service-restore-') as temp:
    root = Path(temp)
    archive = root / 'pi.tar'
    with archive.open('wb') as output:
        import shutil
        shutil.copyfileobj(sys.stdin.buffer, output)
    with tarfile.open(archive) as tar:
        # Extract only these regular files; never extract arbitrary archive paths.
        for source, target in [('homelab/redis.rdb', 'redis/dump.rdb'),
                               ('homelab/grafana.db', 'grafana/grafana.db'),
                               ('homelab/dns/AdGuardHome.yaml', 'adguard/conf/AdGuardHome.yaml')]:
            member = tar.getmember(source)
            if not member.isfile():
                raise RuntimeError('Expected a regular backup file')
            dest = root / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tar.extractfile(member).read())
    redis = root / 'redis'
    answer = isolated('redis:7-alpine', ['-v', f'{redis}:/data'],
                      ['redis-server', '--save', '', '--appendonly', 'no'],
                      ['redis-cli', 'ping'], options=['--user', '0:0'])
    if answer.strip() != b'PONG':
        raise RuntimeError('Restored Redis did not respond')
    print('Redis restored RDB and answered PONG', flush=True)
    grafana = root / 'grafana'
    for p in [grafana, grafana / 'grafana.db']:
        os.chown(p, 472, 472)
    answer = isolated('grafana/grafana:13.2.1', ['-v', f'{grafana}:/var/lib/grafana'], [],
                      ['wget', '-qO-', 'http://127.0.0.1:3000/api/health'],
                      options=['-e', 'GF_ANALYTICS_REPORTING_ENABLED=false', '-e', 'GF_ANALYTICS_CHECK_FOR_UPDATES=false', '-e', 'GF_PLUGINS_PREINSTALL_DISABLED=true'])
    if json.loads(answer).get('database') != 'ok':
        raise RuntimeError('Grafana database health failed')
    print('Grafana started on restored DB; database health OK', flush=True)
    config = root / 'adguard/conf'
    # Validation is separate from startup: network none cannot test upstream DNS.
    run(['docker', 'run', '--rm', '--network', 'none', '--memory', '256m',
         '-v', f'{config}:/opt/adguardhome/conf', 'adguard/adguardhome:v0.107.79',
         '--check-config', '-c', '/opt/adguardhome/conf/AdGuardHome.yaml'], stderr=subprocess.PIPE)
    answer = isolated('adguard/adguardhome:v0.107.79', ['-v', f'{config}:/opt/adguardhome/conf'],
                      ['--no-check-update', '-c', '/opt/adguardhome/conf/AdGuardHome.yaml'],
                      ['wget', '-qO-', 'http://127.0.0.1:3000/login.html'])
    if not answer:
        raise RuntimeError('AdGuard returned an empty login page')
    print('AdGuard config validated and restored instance served its login page', flush=True)
print('All temporary containers removed; production was untouched', flush=True)
