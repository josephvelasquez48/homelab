# Backups and recovery

Encrypted nightly backups of the Pi, kept on the M1 Mac with
[restic](https://restic.net/). No cloud service. A rehearsal restores the
control plane into a throwaway VM and proves it's the same cluster.

## How it works

```
Pi: pi-snapshot.py (root) ──tar over SSH──► Mac: mac-backup.py ──► restic repository
                                            (03:00 nightly, LaunchAgent)   ~/Backups/homelab/repository
```

**Missed nights catch up.** The LaunchAgent runs `mac-backup.py --if-due`
at 03:00, at login and every 15 minutes. It backs up only when nothing has
succeeded since the most recent 03:00 (`~/Backups/homelab/last-success`)
and the Pi answers on SSH, so a Mac that was off, asleep or logged out at
03:00 backs up within 15 minutes of being back. A due backup that can't
reach the Pi yet (the network often isn't up right at login) logs "waiting
for the Pi" and exits 0 rather than tripping `HomelabBackupAgentFailed`;
a Pi that stays unreachable shows up as `HomelabBackupStale`.

**Captured, each consistent on its own** (there's no single atomic moment
across all of them):

| What | How |
|---|---|
| K3s datastore and server token | SQLite online backup API |
| Postgres (pgvector) | `pg_dump` custom format, plus roles |
| Redis (includes the job queue) | its RDB snapshot |
| Grafana | SQLite online backup |
| Host config: K3s service, NetworkManager, firewall, CoreDNS, AdGuard | files |

**Left out on purpose:** Prometheus history, AdGuard's query log and
caches, Ollama models, Grafana plugins, Terraform state.

After each run, restic checks every byte of the repository
(`check --read-data`). The SOPS age key is backed up as its own separate
encrypted snapshot (tag `recovery`).

## Where the keys are

| Secret | Where |
|---|---|
| restic password | Mac: `~/.config/homelab-backup/password` (0600); a second copy on Windows in the restricted age-key folder |
| SOPS age key | Windows `%USERPROFILE%\.config\sops\age\keys.txt`; a copy on the Mac, inside the recovery snapshot |

Never committed. Anyone who controls the Mac account has the password too,
so the encryption protects the repository's disk, not the account.

## Verified

- **Restores:** Postgres into a throwaway PostgreSQL 17 + pgvector
  container (documents and jobs present); Redis loaded its RDB and answered;
  Grafana started on the restored database; AdGuard's config validated. All
  in containers with no network, removed afterwards; production untouched.
- **Control-plane recovery rehearsal** (`rehearse-recovery.py`) - below.

## The recovery rehearsal

Integrity checks prove the archive is *intact*; this proves it's
*restorable*. It restores the newest snapshot into a throwaway Multipass
VM, boots K3s from it, checks, and destroys the VM.

| Check (first run, 2026-09-11) | Result |
|---|---|
| API server ready | 10 s |
| TLS files rebuilt from the datastore | 35 |
| CA fingerprint vs the live Pi | **identical** |
| Nodes, Deployments, Secrets, ConfigMaps, PVCs | `joe` + `m1-node`, 18, 24, 29, 4 |
| Pods Running in the restored cluster | 23 |

**Why the CA fingerprint matters:** a restore with the wrong token still
starts and looks healthy, but has a different CA and would refuse every
real node. Matching fingerprints separate "a working cluster" from "*your*
cluster". The script also checks the TLS folder is empty beforehand, since
the backup relies on K3s rebuilding it from the datastore.

**Not covered:** volume contents (restored from the separate dumps above),
and rebuilding the Pi's OS (that's [ansible.md](ansible.md)).

**Safety:** the VM is NAT-only, never bridged - it holds this cluster's
full identity and must never meet the real LAN. It's destroyed in a
`finally` block, and the archive is staged to a file because piping it
into the VM truncated silently.

## Monitoring

`publish-backup-metrics.py` runs hourly on the Mac as its own LaunchAgent
and pushes gauges to the Pi's node_exporter, which Prometheus scrapes.
Five alerts in `kubernetes/monitoring/alertmanager.yaml` email via
Alertmanager: `HomelabBackupStale` (no snapshot for 26 h),
`HomelabBackupAgentFailed`, `HomelabBackupRepositoryUnreadable`,
`HomelabBackupReporterStale`, `HomelabBackupMetricsMissing`.

**Design choices:**

- **The reporter is separate from the backup** - a backup that never runs
  can't report that it never ran.
- **It reports state (snapshot age), not events**, so it stays true
  whatever went wrong.
- **The Mac pushes; the Pi doesn't pull** - no listener on the Mac, and the
  Pi never gets the restic password. A dead Mac shows up as stale metrics.

**It has caught two real failures.** Twice an Xcode update revoked its
licence acceptance, which disables Apple's `python3`, so both agents
exited 69 while `backup.log` still ended with the last success. Fixed by
running both on Homebrew's Python, and by pointing `xcode-select` at the
Command Line Tools.

## Running it (on the Mac)

```sh
/opt/homebrew/bin/python3 ~/.config/homelab-backup/mac-backup.py     # back up now (without --if-due, always)
export RESTIC_REPOSITORY="$HOME/Backups/homelab/repository"
export RESTIC_PASSWORD_FILE="$HOME/.config/homelab-backup/password"
/opt/homebrew/bin/restic snapshots                                   # pick the pi or recovery tag
/opt/homebrew/bin/python3 ~/.config/homelab-backup/verify-mac.py     # restore checks in throwaway containers
/opt/homebrew/bin/python3 install-schedule.py --replace              # reinstall the agents
```

Restore into a new private folder, never over a live database. The
installed scripts don't update when the repo does; check them with
`git show origin/main:backup/<file> | diff - ~/.config/homelab-backup/<file>`.

## Limits

- **Only runs while the Mac is on and logged in.** FileVault stops a
  rebooted Mac at the unlock screen, which also stops the second K3s node
  ([node-migration.md](node-migration.md)). While the Mac is off, the
  stale-backup alert fires.
- **No off-site copy.** The next step, now that restores are proven.
- **No pruning yet.** About 22 MiB so far, so there's no pressure.
