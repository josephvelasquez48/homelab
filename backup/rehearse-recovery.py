#!/usr/bin/env python3
"""Restore the Pi's K3s control plane into a disposable VM and prove it works.

Everything else in this directory verifies that the *archive* is intact.
This verifies that the archive is *restorable*, which is a different claim:
a state.db can pass PRAGMA integrity_check and still not boot a control
plane, and those two outcomes look identical in every other check here.

What it proves, and the reason it is worth the minutes it takes:

  state.db + token is sufficient. K3s keeps its certificate authority in
  the datastore, encrypted by the server token, and regenerates
  /var/lib/rancher/k3s/server/tls on first start. That directory is
  deliberately not in the backup. If the token were ever lost or captured
  from a different cluster than the datastore, the restore would produce a
  cluster with a *different* CA - which still starts, still serves the API,
  and would still fail to admit any existing agent. Comparing CA
  fingerprints is what distinguishes those cases.

The VM is NAT-only, never bridged. It briefly holds the entire credential
set and a certificate authority identical to the live cluster's, so it is
destroyed in a finally block rather than at the end of the happy path.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

VM = 'restore-rehearsal'
PI = 'joe@192.168.1.253'
MULTIPASS = '/usr/local/bin/multipass'
RESTIC = '/opt/homebrew/bin/restic'
config = Path.home() / '.config/homelab-backup'
env = dict(os.environ,
           RESTIC_REPOSITORY=str(Path.home() / 'Backups/homelab/repository'),
           RESTIC_PASSWORD_FILE=str(config / 'password'))
failures = []


def mp(*args, **kw):
    return subprocess.run([MULTIPASS] + list(args), text=True, **kw)


def vm(script, check=True):
    return subprocess.run([MULTIPASS, 'exec', VM, '--', 'sudo', 'sh', '-c', script],
                          capture_output=True, text=True, check=check).stdout.strip()


if VM in mp('list', capture_output=True).stdout:
    sys.exit('%s already exists; delete it before rehearsing' % VM)

os.umask(0o077)
stage = Path(tempfile.mkdtemp(prefix='rehearsal-', dir='/tmp'))
try:
    archive = stage / 'pi.tar'
    with archive.open('wb') as out:
        subprocess.run([RESTIC, 'dump', '--tag', 'pi', 'latest', 'pi.tar'],
                       env=env, stdout=out, check=True, timeout=900)
    # Staged to a file rather than piped into the VM: a pipe through
    # `multipass exec` truncates silently, which extracts a partial tree and
    # looks like a corrupt backup rather than a broken transfer.
    print('restored archive: %.1f MiB' % (archive.stat().st_size / 1048576))

    # NAT only. A bridged VM would put a second control plane holding this
    # cluster's identity onto the real LAN.
    mp('launch', '24.04', '--name', VM, '--cpus', '2', '--memory', '3G',
       '--disk', '12G', check=True, capture_output=True, timeout=900)
    mp('transfer', str(archive), '%s:/tmp/pi.tar' % VM, check=True, timeout=900)
    vm('mkdir -p /restore && tar -xf /tmp/pi.tar -C /restore && rm -f /tmp/pi.tar')

    meta = json.loads(vm('cat /restore/homelab/metadata.json'))
    version = meta['k3s_version'].split()[2]
    print('backup taken %s, k3s %s' % (meta['created_utc'], version))

    vm('curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=%s '
       'INSTALL_K3S_SKIP_START=true '
       'INSTALL_K3S_EXEC="server --flannel-backend=wireguard-native" sh -' % version)
    vm('mkdir -p /var/lib/rancher/k3s/server/db && '
       'install -m 0600 /restore/homelab/k3s/token /var/lib/rancher/k3s/server/token && '
       'install -m 0600 /restore/homelab/k3s/db/state.db '
       '/var/lib/rancher/k3s/server/db/state.db')
    before = vm('ls /var/lib/rancher/k3s/server/tls 2>/dev/null | wc -l')
    if before != '0':
        failures.append('tls/ was not empty before start (%s files); '
                        'the CA check below proves nothing' % before)

    vm('systemctl start k3s', check=False)
    deadline = time.time() + 300
    ready = False
    while time.time() < deadline:
        if vm('k3s kubectl get --raw /readyz >/dev/null 2>&1 && echo yes || echo no',
              check=False) == 'yes':
            ready = True
            break
        time.sleep(10)
    if not ready:
        failures.append('API server never became ready')

    if ready:
        counts = {k: vm('k3s kubectl get %s -A --no-headers 2>/dev/null | wc -l' % k)
                  for k in ('deploy', 'secret', 'cm', 'pvc')}
        nodes = vm('k3s kubectl get nodes -o name 2>/dev/null').split()
        print('recovered: %s' % counts)
        print('node records: %s' % ' '.join(n.split('/')[-1] for n in nodes))
        if int(counts['secret']) == 0:
            failures.append('no secrets in the restored datastore')
        if not any(n.endswith('/joe') for n in nodes):
            failures.append('the original control-plane node record is missing')

        # The claim this whole exercise exists to test.
        fp = vm("openssl x509 -in /var/lib/rancher/k3s/server/tls/server-ca.crt "
                "-noout -fingerprint -sha256")
        live = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', PI,
             'sudo openssl x509 -in /var/lib/rancher/k3s/server/tls/server-ca.crt '
             '-noout -fingerprint -sha256'],
            capture_output=True, text=True)
        if live.returncode == 0:
            if live.stdout.strip() == fp:
                print('CA fingerprint matches the live cluster')
            else:
                failures.append('CA MISMATCH: restored cluster has a different '
                                'identity; existing agents would not rejoin')
        else:
            # Expected in a real disaster. Record it rather than assert it.
            print('live Pi unreachable, cannot compare CA. Restored: %s' % fp)
finally:
    mp('delete', VM, '--purge', capture_output=True)
    shutil.rmtree(stage, ignore_errors=True)
    print('VM purged, staged archive wiped')

if failures:
    sys.exit('REHEARSAL FAILED:\n  ' + '\n  '.join(failures))
print('Rehearsal passed: the backup restores a working control plane')
