# Backup and recovery

## Status (2026-09-10)

Local encrypted backups are installed on the Mac. No cloud service is used.

- Repository: `/Users/josephvelasquez/Backups/homelab/repository`.
- Restic password: `~/.config/homelab-backup/password` on the Mac, mode 0600
  inside a mode 0700 directory. Keep this outside the repository.
- Independent password copy: `C:\Users\josep\.config\sops\age\mac-backup-restic-password.txt`
  inside the restricted Windows key directory. Passwords are never committed.
- SOPS age key copied to the Mac's protected `~/.config/homelab-backup/recovery/`
  directory and included in its own encrypted recovery snapshot.
- LaunchAgent: `~/Library/LaunchAgents/local.homelab.backup.plist`.
  Runs at 03:00 Mac local time and at agent load/login. Requires the Mac user
  session and network availability; it does not provide backups while the Mac
  is shut down or logged out. The collector uses the existing Mac-to-Pi SSH key.
- FileVault is on and automatic login is off, so "logged out" includes every
  reboot until someone types the password at the pre-boot unlock screen. The
  same lock keeps `multipassd` from restoring the `m1-node` VM, so a power cut
  takes the second K3s node down with the backups. See
  [node-migration.md](node-migration.md) for the node half and the options.
  `sudo fdesetup authrestart` unlocks the volume once for a planned reboot,
  which restores both; it cannot help after an unplanned power cut.
- As of 2026-09-10 the 03:00 trigger has never fired. Every snapshot so far
  came from a `RunAtLoad` or a manual `launchctl kickstart`; `launchctl print`
  is the check, not the presence of snapshots.
- The deployed copies under `~/.config/homelab-backup` should stay
  byte-identical to `backup/`. The check is
  `git show origin/main:backup/<file> | diff - ~/.config/homelab-backup/<file>`.
  They drifted once, by carriage returns picked up in a Windows-side transfer,
  and were redeployed from the committed copies on 2026-09-10.
- Logs: `~/.config/homelab-backup/backup.log` and `backup-error.log`.
  Nonzero exit means failure. External failure notifications are not installed.
- First snapshot `9d282746`: 19.7 MiB input, approximately 5.1 MiB stored.
  Recovery-key snapshot `a79a1396` is separate.
- No automatic pruning yet; retained snapshots accumulate until a reviewed
  retention policy is enabled. Monitor free space and rotate logs as needed.

### What is captured

`backup/pi-snapshot.py` runs as root on the Pi and streams a tar over SSH.
It uses SQLite's online backup API for K3s and Grafana, a PostgreSQL custom-format
dump plus role definitions, and Redis's replication RDB snapshot. It includes
the K3s token, service configuration, NetworkManager profiles, firewall files,
CoreDNS files and AdGuard YAML. Each database is independently consistent;
there is no atomic transaction spanning all these services.

`backup/mac-backup.py` stages that stream in a private temporary directory,
stores it in Restic, backs up the recovery key, then runs `restic check --read-data`.
A file lock prevents concurrent collectors. Temporary plaintext is removed on
normal completion or handled errors; an abrupt power loss can leave staging
files and they must be handled as secrets. No live service is stopped.

Excluded: Prometheus history, AdGuard query history/statistics/filter cache,
Ollama models, Grafana plugin binaries, and local Terraform state. AdGuard
configuration is checked for changes during capture; its caches can rebuild.
Encrypted repository storage does not protect against someone controlling the
Mac account, which also has the password and recovery key.

### Verification completed

- First run and full encrypted repository data check passed.
- Retrieved the archive from Restic and integrity-checked restored K3s/Grafana
  SQLite databases.
- Restored the Postgres dump into a disposable PostgreSQL 17/pgvector Docker
  container on the Pi with `--network none`, no published ports, a tmpfs data
  directory and resource limits. Found 2 documents, 2 jobs and pgvector 0.8.6.
  The container was removed; production database was not modified.
- Expanded restore rehearsal passed: Redis loaded the saved RDB and answered
  PONG; Grafana 13.2.1 started on the restored database and reported database
  health OK; AdGuard v0.107.79 passed configuration validation and served its
  login page. These ran in disposable containers with `--network none`, no
  published ports and only temporary restored files mounted. Containers were
  removed afterward. AdGuard upstream resolution and live filtering were not
  tested, since network isolation intentionally prevents upstream access.
- A full K3s boot/recovery rehearsal **has now been done** and is codified in
  `rehearse-recovery.py`. SQLite integrity alone does not establish
  control-plane recovery, which is why it was worth doing separately - see
  "The recovery rehearsal" below.

### Run and inspect (on the Mac)

```sh
/usr/bin/python3 ~/.config/homelab-backup/mac-backup.py
export RESTIC_REPOSITORY="$HOME/Backups/homelab/repository"
export RESTIC_PASSWORD_FILE="$HOME/.config/homelab-backup/password"
/opt/homebrew/bin/restic snapshots
/usr/bin/python3 ~/.config/homelab-backup/verify-mac.py
```

The last command repeats PostgreSQL, Redis, Grafana, AdGuard and SQLite
checks using temporary containers on the Pi, never production volumes.
To recover individual files, use `restic restore` to a new private directory.
Choose the `pi` or `recovery` tag explicitly: there are separate snapshots.
Never restore directly over live database directories.

### Installed code

- Pi: `/usr/local/lib/homelab-backup/pi-snapshot.py` , `verify-postgres.py` and `verify-services.py`.
- Mac: `~/.config/homelab-backup/mac-backup.py` and `verify-mac.py`.
- Initial setup helpers are under `backup/` in this repository. Changes to the
  repository do not automatically update these installed scripts.

## Inventory

- Pi `joe`, 192.168.1.253: K3s control plane with SQLite datastore.
  `/var/lib/rancher/k3s/server/db/state.db` has active WAL/SHM files.
- K3s server token: `/var/lib/rancher/k3s/server/token`. Keep with the
  datastore backup, encrypted. Recovery requires the original token.
- Postgres: namespace `data`, Deployment `postgres`, database/user `homelab`,
  pgvector on PostgreSQL 17. Database measured approximately 8 MB.
- Redis: namespace `backend`, PVC `redis-data`; includes the job queue.
- Grafana: namespace `monitoring`, PVC `grafana-data`; includes live settings.
- Prometheus: namespace `monitoring`, PVC `prometheus-data`; metrics history.
- DNS: `/home/joe/apps/homelab/docker/dns/`, including CoreDNS configuration,
  `adguard/conf` and `adguard/work` (approximately 106 MB of work data).
- Windows age identity: `C:\Users\josep\.config\sops\age\keys.txt`.
  This is a private key, not a repository file. Its local second copy is
  not protection against loss of the Windows disk.
- Terraform state is local and gitignored; include it in secure operator backups.

## Recovery requirements

1. Store encrypted backups outside the Pi. Retain a separate secure copy of
   the age private key and any backup encryption/repository credentials.
   Do not encrypt the only key copy solely to that same key.
2. Use a consistent PostgreSQL logical dump, not a copy of its running data
   directory. Capture roles as needed for a fresh restore; treat dumps as secrets.
3. Use a consistent SQLite snapshot or a controlled stopped-service copy for
   K3s. Do not independently copy live DB/WAL/SHM files and assume consistency.
   Include the server token, K3s version, service configuration and host setup.
4. Back up Redis and Grafana using application-consistent methods. Record
   whether queued jobs may be lost or replayed. Restore exercises must not
   start a worker against a copied production queue.
5. Capture AdGuard configuration and state consistently. Any service stop
   causes a DNS interruption and must be planned explicitly.
6. Decide whether Prometheus history is retained or deliberately disposable.
   K3s datastore recovery does not restore local-path application volumes.
7. Record timestamps, versions, sizes, checksums, completion status and retention.
   Fail loudly on missing inputs, failed encryption or failed remote transfer.
   Apply retention only after a new backup has been verified.

## Remaining full-recovery validation

- Verify the off-host files decrypt and pass integrity checks.
- Restore Postgres into a separate PostgreSQL 17/pgvector instance. Check schema,
  document/job counts and representative embedding queries. Do not replace the
  production database as a test.
- Verify the copied SQLite database's integrity, then test full K3s recovery in
  an isolated environment with the matching token and version. Isolation must
  prevent restored controllers from reaching production nodes or reconciling
  production resources. An integrity check alone is not a recovery test.
- Test AdGuard configuration in isolation without binding production DNS ports.
- Record the backup timestamp, test date, restore duration and any excluded data.
- Document host-network recovery (Pi static address, DNS, firewall), manual SOPS
  secret application and the manually managed inference Endpoints/Traefik config.

## Remaining decisions

- Local Mac destination chosen; an off-site copy is not configured.
- Secure independent storage for the age identity and backup credentials.
- Retention, schedule, and acceptable recovery point/recovery time.
- Whether short service interruptions are acceptable for consistent snapshots.

Reference: [K3s backup and restore](https://docs.k3s.io/datastore/backup-restore).
The server token is required to recover encrypted bootstrap data.

## Backup health is measured now, not just logged

On 2026-09-11 the backup agent exited 69 for fourteen minutes and nothing
noticed. `/usr/bin/python3` had stopped working because the Xcode license
had not been accepted, which takes the interpreter down with it on macOS.
The last good backup was only hours old so nothing was lost, but the
failure was invisible in the place anyone would look: `backup.log` still
ended with "Backup and full repository integrity check succeeded", because
a run that dies at the interpreter never writes anything. Only
`launchctl print` showed the real exit code.

`publish-backup-metrics.py` runs hourly on the Mac under its own
LaunchAgent, `local.homelab.backup-metrics`, and publishes six gauges into
the Pi's node_exporter textfile collector, which Prometheus already
scrapes as `node-pi`.

| Metric | Meaning |
| --- | --- |
| `homelab_backup_last_snapshot_timestamp_seconds` | newest restic snapshot |
| `homelab_backup_snapshot_count` | snapshots in the repository |
| `homelab_backup_repository_readable` | 1 if restic could list the repo |
| `homelab_backup_last_exit_code` | last exit of the backup agent, -1 unknown |
| `homelab_backup_agent_loaded` | 1 if the agent is loaded in launchd |
| `homelab_backup_report_timestamp_seconds` | when the report was generated |

The one to watch:

```promql
(time() - homelab_backup_last_snapshot_timestamp_seconds) / 3600
```

Under 26 is healthy. Above it means no successful backup since the last
03:00 run, whatever the reason.

### Three choices in here that are the actual design

**The reporter is a separate agent from the backup.** A reporter that runs
as part of the backup cannot report that the backup never ran, which is
exactly what happened. Separating them is the whole point, not tidiness.

**It reports state, not events.** Snapshot age stays true no matter what
went wrong - a crashed run, an unloaded agent, a Mac that never woke.
"Did the last run succeed" only describes runs that happened, so it is
carried alongside as a secondary signal rather than as the primary one.

**The Mac pushes; the Pi does not pull.** The Mac already holds an SSH key
to the Pi for the backup itself, so publishing reuses it. Pulling would
mean either a listener on the Mac or giving the Pi the restic password,
and neither is worth it. The cost is that a dead Mac stops publishing
rather than reporting its own death - covered because the metrics carry
their own generation time, so a stale report is visible as staleness.

### Still not done

Nothing pages anyone. Alertmanager is still not deployed
(`docs/monitoring.md`), so this is detection, not notification. It needs a
human to look at Grafana, or a query run on a schedule. That is a smaller
gap than before, but it is not zero.

## The recovery rehearsal

Everything else here verifies the archive is **intact**. This verifies it is
**restorable**, which is a different claim. A `state.db` can pass
`PRAGMA integrity_check` and still fail to boot a control plane, and those
two outcomes are indistinguishable in every other check in this document.

`rehearse-recovery.py` runs on the Mac, restores the newest `pi` snapshot
into a throwaway multipass VM, boots K3s against it, checks the result, and
destroys the VM in a `finally` block.

### What it actually proves

**`state.db` plus the token is sufficient.** K3s keeps its certificate
authority in the datastore, encrypted with the server token, and regenerates
`/var/lib/rancher/k3s/server/tls` on first start. That directory is
deliberately not in the backup, so whether the backup is complete rests
entirely on that mechanism working. The script asserts `tls/` is empty
before starting, or the check afterwards would prove nothing.

**The restored cluster is the same cluster.** This is the part worth the
effort. A restore using a token from a different cluster than the datastore
still starts, still serves the API, and still looks healthy - and would
still refuse every existing agent, because its CA is different. Comparing
the `server-ca.crt` SHA-256 fingerprint against the live Pi is what
separates "a working cluster" from "your cluster".

First run, 2026-09-11:

| Check | Result |
| --- | --- |
| API server ready | 10 seconds |
| TLS files regenerated from the datastore | 35 |
| CA fingerprint vs the live Pi | identical |
| Node records recovered | `joe`, `m1-node` |
| Deployments / secrets / configmaps / PVCs | 18 / 24 / 29 / 4 |
| Pods reaching Running in the restored cluster | 23 |

### What it does not prove

**PV contents are not in the archive.** The PVCs bind, but to empty local
volumes. Postgres, Redis and Grafana data come back from the separate
logical dumps (`postgres.dump`, `redis.rdb`, `grafana.db`), which the
existing container rehearsals cover. A real recovery restores the control
plane from here and the data from those.

**It does not test the Pi's own rebuild.** The host half - Raspberry Pi OS,
Docker, the DNS stack, ufw, NetworkManager - is Ansible's job, not this
archive's, though the archive does carry copies of those configs.

### Handling

The VM is NAT-only and never bridged. A bridged VM would put a second
control plane holding this cluster's exact identity onto the live LAN.
While it runs it holds the complete credential set and a CA identical to
production, so it is purged in a `finally` block rather than at the end of
the happy path, and the staged archive is wiped with it.

The archive is staged to a file rather than piped into the VM. Piping
through `multipass exec` truncates silently, which extracts a partial tree
and presents as a corrupt backup rather than a broken transfer - a
genuinely misleading failure to debug.

### What this unblocks

Retention was deliberately gated behind this rehearsal, both here and in
`mac-backup.py`. That gate is now satisfied. Worth noting the pressure is
low regardless: the whole repository is about 22 MiB of raw data against
280 GB free, so pruning is a tidiness decision rather than a deadline.

An off-site copy is the more valuable next step, and is now worth doing
precisely because what would be copied has been shown to work.
