#!/usr/bin/env python3
"""Pull the Pi's live AdGuard config into the repo, with the admin hash redacted.

AdGuard owns this file at runtime - it rewrites it on every settings change
in the web UI - so the copy in this repo is a snapshot, not a source of
truth. Run this after changing anything in the UI, and the diff shows what
moved. An empty diff means the repo matches the box.

The admin password hash never lands in the working tree: it is replaced by
PLACEHOLDER here, and the real value lives in
docker/dns/secrets/adguard-password-hash.enc.yaml.
"""
import pathlib
import re
import subprocess
import sys

PI = "joe@192.168.1.253"
REMOTE = "/home/joe/apps/homelab/docker/dns/adguard/conf/AdGuardHome.yaml"
PLACEHOLDER = "SOPS_ADGUARD_ADMIN_PASSWORD_HASH"
LOCAL = pathlib.Path(__file__).parent / "adguard-config" / "AdGuardHome.yaml"

live = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", PI, "sudo cat " + REMOTE],
    capture_output=True, text=True, check=True,
).stdout.replace("\r\n", "\n")

hashes = re.findall(r"^    password: (\S+)$", live, re.M)
if len(hashes) != 1:
    # More than one admin, or none. Either way the redaction below is no
    # longer known-correct, so refuse rather than risk committing a hash.
    sys.exit("expected exactly 1 password hash, found %d - redact by hand" % len(hashes))

redacted = re.sub(r"^    password: \S+$", "    password: " + PLACEHOLDER, live, flags=re.M)
if hashes[0] in redacted:
    sys.exit("redaction failed, refusing to write")

LOCAL.write_text(redacted, encoding="utf-8", newline="\n")
print("captured %d lines to %s" % (redacted.count("\n"), LOCAL))
print("the admin hash was redacted; run `git diff` to see what changed on the Pi")
