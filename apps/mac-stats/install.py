"""Install the Mac's stats exporter (macmon) as a LaunchAgent.

macmon reads the M1's CPU temperature, CPU activity and memory without
sudo and serves them in Prometheus format on :9101/metrics, which the
node-mac job in kubernetes/monitoring/prometheus.yaml scrapes. The Pi's
screen shows them next to the Pi's own (apps/dashboard). node_exporter
isn't used here: on Apple Silicon it has no temperatures.

    brew install macmon
    /opt/homebrew/bin/python3 apps/mac-stats/install.py

Safe to rerun: the agent is generated and replaced every time. Not
`macmon serve --install`, so the port and interval live here in the repo.
Port 9101 because node_exporter's 9100 is the convention for the Pi's.
"""
import os
from pathlib import Path
import plistlib
import subprocess
import time
import urllib.request

MACMON = '/opt/homebrew/bin/macmon'
LABEL = 'local.homelab.macmon'
PORT = 9101
# Prometheus scrapes every 15 s; sampling every 5 s keeps a scrape fresh
# without macmon itself showing up in the CPU figure.
INTERVAL_MS = 5000

log = Path.home() / '.config/homelab-mac-stats/macmon.log'
log.parent.mkdir(parents=True, exist_ok=True)
plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
plist.parent.mkdir(parents=True, exist_ok=True)
domain = 'gui/%d' % os.getuid()

# Not loaded is fine here (first install), so the exit code is not checked.
subprocess.run(['launchctl', 'bootout', domain, str(plist)], stderr=subprocess.DEVNULL)
with plist.open('wb') as f:
    plistlib.dump({
        'Label': LABEL,
        'ProgramArguments': [MACMON, 'serve', '--port', str(PORT), '--interval', str(INTERVAL_MS)],
        'RunAtLoad': True,
        'KeepAlive': True,
        'StandardOutPath': str(log),
        'StandardErrorPath': str(log),
    }, f)
# Started fresh each install; launchd never rotates it.
log.write_text('')
subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True)

# The first sample lands one interval after start, so allow a few.
for _ in range(40):
    try:
        body = urllib.request.urlopen('http://127.0.0.1:%d/metrics' % PORT, timeout=1).read().decode()
        if 'macmon_cpu_temp_celsius' in body:
            break
    except OSError:
        pass
    time.sleep(0.5)
else:
    raise SystemExit('macmon did not come up - see %s' % log)
print('macmon serving on :%d/metrics' % PORT)
