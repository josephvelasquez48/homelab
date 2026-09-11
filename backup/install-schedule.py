import os
from pathlib import Path
import plistlib
import subprocess
os.umask(0o077)
config=Path.home()/'.config/homelab-backup'
path=Path.home()/'Library/LaunchAgents/local.homelab.backup.plist'
path.parent.mkdir(parents=True,exist_ok=True)
if path.exists():
    raise RuntimeError('LaunchAgent already exists; inspect before replacing')
data={'Label':'local.homelab.backup','ProgramArguments':['/usr/bin/python3',str(config/'mac-backup.py')],'StartCalendarInterval':{'Hour':3,'Minute':0},'RunAtLoad':True,'StandardOutPath':str(config/'backup.log'),'StandardErrorPath':str(config/'backup-error.log'),'Umask':63}
with path.open('wb') as f:
    plistlib.dump(data,f)
subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
print('Nightly 03:00 local-time backup registered, plus a run at login')
