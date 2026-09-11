#!/usr/bin/env python3
"""Run as root on the Pi; emit a tar archive to stdout, never secret text."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

os.umask(0o077)
K = ['k3s', 'kubectl']

def run(args):
    return subprocess.check_output(args, timeout=180)

def sqlite_copy(source, dest):
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src:
        with sqlite3.connect(dest) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite integrity check failed')

with tempfile.TemporaryDirectory(prefix='homelab-snapshot-') as tmp:
    root = Path(tmp)
    (root / 'k3s/db').mkdir(parents=True)
    sqlite_copy('/var/lib/rancher/k3s/server/db/state.db', root / 'k3s/db/state.db')
    shutil.copy2('/var/lib/rancher/k3s/server/token', root / 'k3s/token')
    for source in ['/etc/rancher/k3s', '/etc/systemd/system/k3s.service', '/etc/systemd/system/k3s.service.env', '/etc/NetworkManager/system-connections', '/etc/ufw']:
        p = Path(source)
        if p.exists():
            target = root / 'host' / p.relative_to('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            if p.is_dir():
                shutil.copytree(p, target)
            else:
                shutil.copy2(p, target)
    (root / 'postgres.dump').write_bytes(run(K + ['-n', 'data', 'exec', 'deploy/postgres', '--', 'pg_dump', '-U', 'homelab', '-d', 'homelab', '-Fc']))
    (root / 'postgres-roles.sql').write_bytes(run(K + ['-n', 'data', 'exec', 'deploy/postgres', '--', 'pg_dumpall', '-U', 'homelab', '--roles-only']))
    # The replication snapshot is consistent without copying a live RDB/AOF.
    remote_rdb = '/tmp/homelab-backup-' + str(os.getpid()) + '.rdb'
    redis = K + ['-n', 'backend', 'exec', 'deploy/redis', '--']
    try:
        run(redis + ['redis-cli', '--rdb', remote_rdb])
        (root / 'redis.rdb').write_bytes(run(redis + ['cat', remote_rdb]))
    finally:
        run(redis + ['rm', '-f', remote_rdb])
    claim = json.loads(run(K + ['-n', 'monitoring', 'get', 'pvc', 'grafana-data', '-o', 'json']))
    pv = json.loads(run(K + ['get', 'pv', claim['spec']['volumeName'], '-o', 'json']))
    spec = pv['spec']
    volume = Path((spec.get('hostPath') or spec.get('local'))['path'])
    sqlite_copy(volume / 'grafana.db', root / 'grafana.db')
    dns = Path('/home/joe/apps/homelab/docker/dns')
    shutil.copytree(dns / 'coredns', root / 'dns/coredns')
    config = dns / 'adguard/conf/AdGuardHome.yaml'
    content = config.read_bytes()
    (root / 'dns/AdGuardHome.yaml').write_bytes(content)
    if config.read_bytes() != content:
        raise RuntimeError('AdGuard config changed during capture; retry')
    shutil.copy2(dns / 'docker-compose.yml', root / 'dns/docker-compose.yml')
    metadata = {'created_utc': datetime.now(timezone.utc).isoformat(), 'k3s_version': run(['k3s', '--version']).decode(), 'exclusions': ['Prometheus history', 'AdGuard query history, statistics, and filter cache', 'Ollama models', 'Grafana plugin binaries'], 'consistency': 'Each database snapshot is consistent independently; snapshots are not atomic across services.'}
    (root / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        archive.add(root, arcname='homelab')
