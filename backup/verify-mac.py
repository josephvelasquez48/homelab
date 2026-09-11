import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
os.umask(0o077)
config = Path.home()/'.config/homelab-backup'
env = dict(os.environ,RESTIC_REPOSITORY=str(Path.home()/'Backups/homelab/repository'),RESTIC_PASSWORD_FILE=str(config/'password'))
with tempfile.TemporaryDirectory(prefix='homelab-restore-') as tmp:
    archive = Path(tmp)/'pi.tar'
    with archive.open('wb') as output:
        subprocess.run(['/opt/homebrew/bin/restic','dump','--tag','pi','latest','pi.tar'],env=env,stdout=output,check=True)
    with tarfile.open(archive) as tar:
        for member in ['homelab/k3s/db/state.db','homelab/grafana.db']:
            dest = Path(tmp)/Path(member).name
            dest.write_bytes(tar.extractfile(member).read())
            with sqlite3.connect(dest) as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            print(member + ': restored SQLite integrity OK',flush=True)
        dump = tar.extractfile('homelab/postgres.dump').read()
    subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','joe@192.168.1.253','sudo -n /usr/bin/python3 /usr/local/lib/homelab-backup/verify-postgres.py'],input=dump,check=True,timeout=600)
    with archive.open('rb') as data:
        subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','joe@192.168.1.253','sudo -n /usr/bin/python3 /usr/local/lib/homelab-backup/verify-services.py'],stdin=data,check=True,timeout=900)
print('Isolated application recovery checks succeeded')
