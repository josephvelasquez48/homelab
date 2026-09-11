import os
from pathlib import Path
import secrets
import subprocess
os.umask(0o077)
base = Path.home() / 'Backups/homelab'
config = Path.home() / '.config/homelab-backup'
for p in [base, config, config / 'recovery']:
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
password = config / 'password'
if not password.exists():
    with password.open('x') as f:
        f.write(secrets.token_urlsafe(48) + '\n')
    password.chmod(0o600)
env = dict(os.environ, RESTIC_REPOSITORY=str(base / 'repository'), RESTIC_PASSWORD_FILE=str(password))
if not (base / 'repository/config').exists():
    subprocess.run(['/opt/homebrew/bin/restic','init'], env=env, check=True)
print('Encrypted repository ready')
