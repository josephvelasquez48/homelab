# Dashboard

Not part of the original roadmap - a follow-on to
[docs/gaming-mode.md](gaming-mode.md). A small FastAPI app + single HTML
page, running in the cluster (pinned to the Pi), showing live cluster
status and letting `gaming-mode/pregame.ps1`/`postgame.ps1` be triggered
from a browser instead of a desktop shortcut.

## Why SSH, not a custom agent

Triggering the scripts from a pod on the Pi means reaching across to the
desktop somehow - the pod can't run PowerShell or touch WSL2 directly.
Considered a small custom HTTP agent on the desktop instead, but that's
strictly more to build and secure (new code, new auth scheme, another
autostarting Windows service) for something narrower than what SSH
already does well. Went with SSH: Windows' built-in OpenSSH Server,
key-only auth, a dedicated keypair scoped to nothing but this, LAN-only
- the same trust model already used for GitHub deploy keys elsewhere in
this project, just pointed at a different destination.

**The dashboard doesn't reimplement cordon/drain/uncordon logic** - it
SSHes in and runs the exact same `pregame.ps1`/`postgame.ps1` already
tested manually and via the desktop shortcuts (`docs/gaming-mode.md`).
One implementation, three ways to trigger it (terminal, shortcut,
browser), not three copies quietly drifting apart.

## SSH setup

- OpenSSH Server was already present on the desktop (Windows optional
  feature) but disabled. Enabled, set to start automatically.
- A **dedicated** ed25519 keypair, generated on the Pi
  (`~/.ssh/dashboard-desktop-key/`) - not the existing GitHub deploy
  key. Different purpose, different blast radius if it ever leaked, so
  a different key.
- `josep` is a member of the desktop's Administrators group, which
  means Windows' OpenSSH ignores the normal per-user
  `~/.ssh/authorized_keys` for that login entirely - admin accounts
  require `C:\ProgramData\ssh\administrators_authorized_keys` with
  restricted ACLs (SYSTEM + Administrators only) instead. Easy to miss;
  the symptom if missed is `Permission denied (publickey)` even with a
  correctly-installed key.
- Key-only auth: `PasswordAuthentication no` in `sshd_config`. Verified
  this is actually enforced, not just configured, by attempting a
  connection with `PreferredAuthentications=password` and confirming
  the server offers only `publickey,keyboard-interactive` - same
  "verify, don't assume" standard as everything else in this project.
- **Same class of bug as the earlier Hyper-V firewall issue** (see
  [docs/kubernetes.md](kubernetes.md)), a different specific instance:
  the `sshd` firewall rule was scoped to the `Private` Windows network
  profile by default, but this machine's active network is categorized
  `Public` (`Get-NetConnectionProfile`) - so the rule was silently
  inactive despite looking correctly configured. Same lesson both
  times: check the *active* network category before trusting that a
  profile-scoped firewall rule actually applies.
- Firewall rule additionally scoped to `RemoteAddress 192.168.1.0/24` -
  LAN-only, matching every other exposed service in this project (ufw
  on the Pi, the Ollama LAN rule, the flannel VXLAN rule).

## Non-interactive script execution

`pregame.ps1`/`postgame.ps1` originally always paused on
`Read-Host "Press Enter to close"` at the end - fine for a double-click,
fatal for a non-TTY SSH exec (no stdin to read from). Added a
`-NonInteractive` switch parameter rather than writing separate
SSH-only copies of the scripts - one implementation, a flag for the one
behavioral difference the calling context actually requires. Also added
explicit exit codes (0/1) on both success and failure paths, previously
missing - `return` alone always exits 0, which would have made
automated failure detection silently impossible.

Verified over a real SSH connection, not assumed to work the same as
local execution: both scripts run to completion non-interactively with
correct exit codes, and a full pregame -> postgame round trip over SSH
was confirmed against the live cluster (node cordon/drain/uncordon, all
6 Argo CD Applications `Synced`/`Healthy` afterward) before the app
itself was even built.

## What the app does

- `GET /api/status` - node Ready/cordoned state, pod health per watched
  namespace, Argo CD Application sync/health, `api.home/health`
  reachability, and gaming-mode state (derived from whether the desktop
  node is cordoned - no separate state to keep in sync or get stale).
- `POST /api/gaming/on` / `/off` - shells out to `ssh` and runs the
  corresponding script with `-NonInteractive`, returns its real stdout/
  stderr and exit code to the browser.
- In-cluster K8s API access via the ServiceAccount token/CA every pod
  gets mounted automatically - no kubeconfig, no extra client library
  (plain `httpx` against the API server, consistent with the rest of
  this project's stack).

## Design decisions

- **Pinned to the Pi** (`nodeSelector: kubernetes.io/hostname: joe`),
  same reasoning as Postgres/Redis/Grafana - the whole point of gaming
  mode is removing the desktop from the cluster, so a dashboard that
  could itself land there would be unavailable exactly when it's most
  likely to be needed (checking status, bringing the desktop back).
- RBAC is read-only and scoped as narrowly as the app's actual queries
  need: a `ClusterRole` for `nodes`/`pods` (`get`/`list` only, no
  `watch`/`create`/`delete`), plus a separate `Role` scoped to just the
  `argocd` namespace for reading `Application` status - not a blanket
  cluster-admin binding for convenience.
- SSH private key: SOPS-encrypted (`kubernetes/secrets/dashboard-ssh-key.enc.yaml`),
  applied out-of-band like every other secret in this project - see
  [docs/secrets.md](secrets.md). The plaintext key was never written to
  a path inside the git repo at any point, including transiently -
  built in the system scratch directory, encrypted straight from there
  into its final `kubernetes/secrets/` path, then deleted.
- The desktop's SSH host key is a `ConfigMap`, not a `Secret` - it's a
  public key, there's nothing to protect, and putting it in a Secret
  would misrepresent what actually needs protecting in this setup.

## Known gaps

- No auth on the dashboard itself - it's reachable to anything on the
  LAN, same trust boundary as Grafana/Argo CD's own UIs in this
  project. Acceptable for a single-operator homelab; would need real
  auth before this pattern scaled to more users.
- The gaming-mode buttons block on the full script duration (up to
  ~2-3 minutes for `postgame.ps1`'s Ready-wait) rather than streaming
  progress - the browser shows a static "running..." message the whole
  time instead of live output. Works, but a websocket or SSE stream
  would be a nicer follow-up.
