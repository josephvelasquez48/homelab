"""Install the webcam stream on the Mac as a LaunchAgent.

Renders mediamtx.yml into ~/.config/homelab-cam/ with the viewer password,
and (re)loads the local.homelab.cam agent, which keeps MediaMTX running for
as long as the user is logged in, and local.homelab.camfocus (focusd.py, the
focus slider's back end). Run it from the repo checkout:

    /opt/homebrew/bin/python3 apps/cam/mac/install.py

Safe to rerun after any change here: the config and the agent are both
generated, so they're replaced every time. The viewer password is the
exception - created once, then reused, because the cam app in the cluster
holds a copy.

A LaunchAgent and not a LaunchDaemon: macOS grants camera access per user
session, and a daemon has none to be granted in.
"""
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request

MEDIAMTX = '/opt/homebrew/opt/mediamtx/bin/mediamtx'
LABEL = 'local.homelab.cam'
FOCUS_LABEL = 'local.homelab.camfocus'
PYTHON = '/opt/homebrew/bin/python3'

os.umask(0o077)
here = Path(__file__).resolve().parent
config = Path.home() / '.config/homelab-cam'
config.mkdir(parents=True, exist_ok=True)

password_file = config / 'viewer-password'
if not password_file.exists():
    password_file.write_text(secrets.token_urlsafe(32) + '\n')
password = password_file.read_text().strip()

# Built here rather than shipped: the Command Line Tools' swiftc is already
# on the Mac, and a binary in git would be one nobody can read.
subprocess.run(['swiftc', '-O', str(here / 'capture.swift'), '-o', str(config / 'cam-capture')],
               check=True)
# Focus control (camctl.c); cam-capture runs `camctl apply` from beside itself.
subprocess.run(['clang', '-O2', '-Wall', '-framework', 'IOKit', '-framework', 'CoreFoundation',
                str(here / 'camctl.c'), '-o', str(config / 'camctl')], check=True)

template = (here / 'mediamtx.yml').read_text()
(config / 'mediamtx.yml').write_text(
    template.replace('{{VIEWER_PASSWORD}}', password).replace('{{CONFIG_DIR}}', str(config)))

domain = 'gui/%d' % os.getuid()


def agent(label: str, program: list[str], log: Path) -> None:
    """(Re)load one LaunchAgent, replacing its plist."""
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    # Not loaded is fine here (first install), so the exit code is not checked.
    subprocess.run(['launchctl', 'bootout', domain, str(plist)], stderr=subprocess.DEVNULL)
    with plist.open('wb') as f:
        plistlib.dump({
            'Label': label,
            'ProgramArguments': program,
            'RunAtLoad': True,
            'KeepAlive': True,
            'StandardOutPath': str(log),
            'StandardErrorPath': str(log),
        }, f)
    # Started fresh each install. launchd never rotates it, and a bad ffmpeg
    # flag once filled MediaMTX's with 5 MB of warnings in ten minutes.
    log.write_text('')
    subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True)


agent(LABEL, [MEDIAMTX, str(config / 'mediamtx.yml')], config / 'mediamtx.log')

# The focus slider's way to camctl (focusd.py), run from the config dir so
# it finds camctl and the password beside itself.
shutil.copy(here / 'focusd.py', config / 'focusd.py')
agent(FOCUS_LABEL, [PYTHON, str(config / 'focusd.py')], config / 'focusd.log')

# The API answering means the config parsed and every listener bound.
for _ in range(20):
    try:
        urllib.request.urlopen('http://127.0.0.1:9997/v3/paths/list', timeout=1)
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit('MediaMTX did not come up - see %s' % (config / 'mediamtx.log'))
for _ in range(20):
    try:
        urllib.request.urlopen('http://127.0.0.1:8890/focus', timeout=1)
    except urllib.error.HTTPError:
        break  # 401 without the login: it's up
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit('focusd did not come up - see %s' % (config / 'focusd.log'))
print('Webcam stream installed: WebRTC on :8889, camera on demand at path "cam", focus on :8890')
