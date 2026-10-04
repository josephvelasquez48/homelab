# Monitoring and alerting

Roadmap step 9. Prometheus, Grafana and Alertmanager run in the cluster's
`monitoring` namespace, pinned to the Pi. They started as Docker Compose
on the Pi and moved into K3s ([kubernetes.md](kubernetes.md)).

## What's collected

| Target | What | Where it runs |
|---|---|---|
| `node-pi` | Pi hardware: CPU, memory, disk, temperature, network | `node_exporter` in Docker on the Pi (`docker/monitoring`), since it needs the host's `/proc` and `/sys` |
| `kubernetes-pods` | Any pod annotated `prometheus.io/scrape` - the API, adguard-exporter, Traefik | Found through the Kubernetes API (RBAC lets Prometheus list pods) |
| `node-mac` | The MacBook's CPU temperature, CPU and memory | `macmon` as a LaunchAgent (`apps/mac-stats/install.py`), on `:9101` |
| `node-pc` | The Windows desktop's CPU temperature, CPU and memory (and every other sensor) | LibreHardwareMonitor's web server, on `:8085` - see below |
| Textfile metrics | Backups ([backups.md](backups.md)) and the phone bridge ([phone.md](phone.md)) | Files in node_exporter's textfile directory, written by those services |

**Grafana** is provisioned from the repo (`kubernetes/monitoring/grafana.yaml`),
so a fresh install comes up with its data source and the "Homelab
Overview" dashboard: targets up, Pi CPU / memory / disk / temperature /
load / network / disk I/O, OOM kills, API request rate and p95 latency,
and AdGuard (queries, blocked, block rate, top domains).

**Verified with data, not just "it started":** every panel's query was run
directly against Prometheus and returned sane numbers (disk 6.3% of the
469 GB NVMe matched `df`). Rate panels read 0 until a series has two
scrapes in the window.

## The Mac's and the PC's stats

The Pi's screen shows temp · CPU · memory for the Pi, the Mac and the PC.
Neither the Mac nor the PC can use `node_exporter` for this: it has no
temperatures on Apple Silicon, and an Intel CPU's temperature on Windows
needs a kernel driver. So each runs the tool that can read it:

- **Mac:** `macmon` (Homebrew), no sudo. `brew install macmon`, then
  `/opt/homebrew/bin/python3 apps/mac-stats/install.py`.
- **PC:** [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  0.9.6 or later, whose web server has a Prometheus `/metrics` page. It
  reads the CPU through PawnIO, a signed driver, so it has to run as
  administrator. Set up by hand, once:
  1. Unzip the release to `C:\Program Files\LibreHardwareMonitor` and run
     `LibreHardwareMonitor.exe` (it asks for administrator).
  2. **Options:** tick *Start Minimized*, *Minimize To Tray* and *Run On
     Windows Startup* (a scheduled task at logon, as administrator).
  3. **Options -> Remote Web Server:** port `8085`, then *Run*.
  4. Let only the Pi in, from an administrator PowerShell:
     `New-NetFirewallRule -DisplayName "LibreHardwareMonitor from the Pi" -Direction Inbound -Protocol TCP -LocalPort 8085 -RemoteAddress 192.168.1.253 -Action Allow`.
     The web server can also *change* things (reset min/max, set fan
     control), so it shouldn't be open to the whole LAN.

The PC sleeps at night, so its cell reads "-" and Prometheus counts one
target down until it wakes. Not alerted, for the reason Ollama isn't.

A new scrape job only takes effect when Prometheus restarts (its reload
API is off): `kubectl -n monitoring rollout restart deploy/prometheus`.
Its data is on a volume, so that costs a few seconds of scrapes.

## Alerting

Alertmanager emails through Gmail (`smtp.gmail.com:587`, an app password).
Rules are in `kubernetes/monitoring/alertmanager.yaml`:

| Alert | Fires when |
|---|---|
| `HomelabBackupStale` | No snapshot for 26 h (the daily 03:00 run plus slack) |
| `HomelabBackupMetricsMissing` | The backup metrics disappear altogether |
| `HomelabBackupAgentFailed`, `...RepositoryUnreadable`, `...ReporterStale` | The Mac's backup agent, repository or reporter fails |
| `PhoneBridgeDown`, `PhoneCallerSilent`, `PhonePcMicSilent` | The phone service stops, or a bridged call is silent one way |

**`absent()` is the rule that matters most.** A `value > threshold` rule
never fires on a series that doesn't exist - and if the Mac stops
publishing, the backup series just vanish. That's the failure that looks
like health.

**The config is a SOPS Secret, not a ConfigMap:** it holds the SMTP
password *and* the recipient address, and this repo is public. Apply it
(`kubernetes/secrets/apply.sh`) before Argo CD creates the Deployment, or
the pod can't mount its config.

## Deliberately not alerted

**Ollama being unreachable.** `homelab_inference_reachable` is set by the
API's `/ready` handler. It used to fail readiness, which took *every* API
replica out of service - and Argo CD reported `backend` Degraded - whenever
the desktop slept. It's a metric now; Postgres and Redis still fail
readiness, since those have somewhere else to route. An alert would fire
every night the desktop sleeps and teach you to ignore alerts.

## Known gaps

- **Only the PC's sensors, not its OS.** LibreHardwareMonitor covers the
  CPU, GPU and memory hardware; disk space, services and per-process use
  would need `windows_exporter`. The GPU sensors are scraped but not
  graphed or shown anywhere yet.
- **No Postgres or Redis exporters** (connections, query latency, hit rates).
- **Few alerts.** Nothing for a node down, a crash-looping pod or a full disk.
- **One receiver.** Everything goes to one mailbox, so if email breaks, the
  alert about it arrives by email.
- **`alerts.home` and `prometheus.home` have no login** - they exist so links
  in alert emails work. Anyone on the LAN can silence an alert or read
  every metric. Prometheus's admin and lifecycle APIs are off, so it's read
  only. The fix is Traefik basic-auth middleware, not built yet.
