#!/usr/bin/env python3
"""Push the repo's AdGuard config back to the Pi, re-inserting the admin hash.

This is the rebuild path. The repo snapshot carries every setting that is
not a secret - blocking mode, filter lists, user_rules, upstreams - and the
admin password hash is decrypted from SOPS and substituted back in on the
way.

Overwrites the running config and restarts the container, so it is the
right tool for rebuilding the box and the wrong one for routine edits.
Make routine changes in the web UI and run adguard-capture.py instead.
"""
import pathlib
import re
import shlex
import subprocess
import sys

PI = "joe@192.168.1.253"
REMOTE = "/home/joe/apps/homelab/docker/dns/adguard/conf/AdGuardHome.yaml"
PLACEHOLDER = "SOPS_ADGUARD_ADMIN_PASSWORD_HASH"
HERE = pathlib.Path(__file__).parent
LOCAL = HERE / "adguard-config" / "AdGuardHome.yaml"
SECRET = HERE / "secrets" / "adguard-password-hash.enc.yaml"

config = LOCAL.read_text(encoding="utf-8").replace("\r\n", "\n")
if PLACEHOLDER not in config:
    sys.exit("no placeholder in %s - was it captured with adguard-capture.py?" % LOCAL)

plain = subprocess.run(
    ["sops", "--decrypt", str(SECRET)], capture_output=True, text=True, check=True
).stdout
match = re.search(r"^password_hash:\s*(\S+)\s*$", plain, re.M)
if not match:
    sys.exit("no password_hash in the decrypted secret")

rendered = config.replace(PLACEHOLDER, match.group(1))

# Stage the complete file before stopping DNS. AdGuard must be stopped
# before its config is replaced, including before taking the final backup.
# Compose also handles a fresh rebuild where no container exists yet.
remote_script = f"""set -eu
umask 077
config={shlex.quote(REMOTE)}
cd /home/joe/apps/homelab/docker/dns
mkdir -p "$(dirname "$config")"
staged=$(mktemp "$config.restore.XXXXXX")
trap 'rm -f "$staged"' EXIT
cat > "$staged"
docker compose stop adguardhome
if [ -f "$config" ]; then
    backup=$(mktemp "$config.bak-$(date +%s).XXXXXX")
    cp -p "$config" "$backup"
fi
mv "$staged" "$config"
docker compose up -d adguardhome coredns
"""
subprocess.run(
    ["ssh", "-o", "BatchMode=yes", PI,
     "sudo sh -c " + shlex.quote(remote_script)],
    input=rendered, text=True, check=True)

check = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", PI, "dig @192.168.1.253 apple.com +short +time=5"],
    capture_output=True, text=True).stdout.strip()
print("resolution after restart: %s" % (check or "NOTHING - check the Pi"))
