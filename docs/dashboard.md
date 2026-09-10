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

## Deployed and verified live, not just "it builds"

Two more real things surfaced getting this actually running, on top of
the SSH setup above:

- `homelab-dashboard` is a brand-new `ghcr.io` package, and like
  `homelab-api` before it (`docs/kubernetes.md`), a freshly-pushed
  package defaults to private - K3s pulls images anonymously, so the
  pod sat in `ImagePullBackOff` with a `401 Unauthorized` until the
  package was made public (confirmed first, same reasoning as the
  earlier `homelab-api` case: no secrets baked into the image, personal
  project).
- `dashboard.home` needed adding to CoreDNS's `home.hosts` - easy to
  forget since every other service already had its entry from a prior
  phase.

End-to-end verification, through the deployed app itself rather than
by re-running the scripts directly: `POST /api/gaming/on` against the
live `dashboard.home` correctly cordoned, drained, and stopped
`k3s-agent` (`gaming_mode_active` flipped to `true` in `/api/status`
immediately after); `POST /api/gaming/off` correctly reversed it. All 7
Argo CD Applications (including `dashboard` itself) confirmed
`Synced`/`Healthy` afterward.

## The desktop's address is not pinned anywhere

**Superseded.** The router that could not do reservations was replaced
([router-migration.md](router-migration.md)), and DESKTOP-J1GRRMU is now
reserved at `192.168.1.131`. The section below records why the machinery
exists; it is no longer the reason it has to.

Originally: neither host had a DHCP reservation, the same limitation
already recorded for DNS override in docs/milestone-1.md. The desktop's
lease had already moved once (`.131 -> .133`), which broke in-cluster
inference and would have broken gaming mode too.

So nothing here stores that address:

- **The SSH target** is read from the node's `InternalIP` at call time
  (`k8s.get_node_internal_ip`). k3s updates that field within seconds of a
  lease change on its own, which makes it the one place in the cluster
  that is reliably right. `GAMING_SSH_HOST` still overrides it when set, for
  local runs or if the API is unavailable.
- **The host key** is verified under the alias `homelab-desktop` rather than
  under an address, via `ssh -o HostKeyAlias` and a matching `known_hosts`
  entry. Without this a lease change fails as *host key verification*,
  which points at the wrong problem entirely - the key is fine, the
  address it is filed under is not.

Verified against the live host: connecting to the current address with
the alias reaches authentication (`Permission denied (publickey)`, i.e. the
host key checked out), while the same connection without the alias fails
with `No ED25519 host key is known for 192.168.1.133` - the exact failure a
lease change would otherwise produce.

The equivalent fix for the `inference` Endpoints *was* a CronJob, because
an Endpoints object has no code of its own to do the lookup. It has been
deleted: the reservation removes the problem, and the desktop node it read
`InternalIP` from no longer exists.

That second half applies to the SSH target above too. `get_node_internal_ip`
resolves a node that is gone, so gaming mode now depends on the
`GAMING_SSH_HOST` override until the rewrite in
[node-migration.md](node-migration.md) lands.

## Auth on the gaming-mode endpoints

`/api/gaming/on` and `/api/gaming/off` require a session. Everything else
- the status page, `/api/status`, `/health` - stays open on the LAN, the
same trust boundary as Grafana's and Argo CD's own UIs here.

The split exists because the read-only parts and the state-changing parts
are not the same risk. This app's POST endpoints SSH to the desktop and
run PowerShell, so "reachable by anything on the LAN" meant an
unauthenticated remote-execution path. Worse, it was reachable from
*off* the LAN: a bodyless `fetch(url, {method: 'POST'})` is a CORS-simple
request, so any page on the internet could make a LAN user's browser send
it, and `traefik-lan-only` would see a legitimate LAN source IP. The
NetworkPolicy controls who can reach the endpoint, not who can cause the
request.

Two layers, in order of importance:

1. **A session cookie** (`apps/dashboard/app/auth.py`), signed by
   starlette's `SessionMiddleware`, `HttpOnly` and `SameSite=Strict`.
   Deliberately *not* the API's `X-API-Key` pattern: this is a browser
   app, so any key the page could send would have to be embedded in
   JavaScript that anyone able to load the dashboard can read - a public
   string, not a credential. The cookie keeps the secret out of the page.
2. **`Content-Type: application/json` required**, which makes the request
   non-simple and forces a preflight this app answers no CORS for. This
   is defence in depth behind the session check, not a boundary of its
   own - `SameSite=Strict` is what actually stops the cross-site cookie.

`https_only` on the cookie is still `False`, only because the Ingress is
plain HTTP. Flip it when TLS lands (docs/security-testing.md, finding 5).

### The dashboard-auth Secret

```bash
sops kubernetes/secrets/dashboard-auth.enc.yaml
```

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: dashboard-auth
  namespace: dashboard
type: Opaque
stringData:
  DASHBOARD_PASSWORD: <a long random password>
  SESSION_SECRET: <openssl rand -base64 32>
```

Then `kubernetes/secrets/apply.sh`, same out-of-band path as the others.

The Deployment's `secretRef` is `optional: true`. A required one would
park the pod in `CreateContainerConfigError` until the Secret existed,
taking down the status page - the thing you look at to find out whether
the cluster is healthy - because of a missing credential. Optional means
the pod starts and the app fails closed instead: with no
`DASHBOARD_PASSWORD`, `check_password` always returns false, nobody can
log in, and the gaming endpoints are unreachable rather than open.
`/api/session` reports `configured: false` so the page can say why the
buttons are dead instead of silently rejecting a correct password.

### What a missing Secret does, precisely

The one failure this design must never have is: Secret missing, auth
silently disabled, old unauthenticated behaviour returns. It cannot,
because nothing about the guard is conditional on the password existing:

- `require_session` is an unconditional dependency on both endpoints. It
  checks the session, and only the session.
- The only way to get a session is `/api/login`, and `check_password`
  returns False outright when no password is configured. A rejected login
  sets no cookie at all.
- `SESSION_SECRET` falls back to a freshly generated random value, never
  a fixed default, so a missing Secret cannot make cookies forgeable
  either. The cost is that sessions do not survive a pod restart.

So the states are "closed and usable" or "closed and unusable" - never
open. What a missing Secret costs is gaming mode, not the boundary.

It is also not silent:

- The pod logs `WARNING: DASHBOARD_PASSWORD is not set: /api/gaming/* is
  unreachable` at startup.
- `/api/session` returns `configured: false`, and the page renders an
  explicit note instead of leaving the buttons mysteriously dead.
- `scripts/verify-dashboard-auth.sh` treats it as a **failure**, not a
  warning. Every authorization check passes in that state, for the wrong
  reason, and a script exiting 0 there would report "secure" for a
  deployment that is merely broken.

Verify against the deployed host, not just in unit tests:

```bash
./scripts/verify-dashboard-auth.sh http://dashboard.home
```

Every request it sends is unauthenticated and expected to be rejected, so
a passing run never triggers a drain.

## Known gaps

- No auth on the read-only surface - `/`, `/api/status` and `/health` are
  reachable by anything on the LAN. That exposes cluster topology, pod
  names, and node metrics, which is acceptable for a single-operator
  homelab but would need to change before this scaled to more users.
- A single shared password with no user accounts, lockout, or audit of
  who triggered a drain. Fine for one operator; not a multi-user design.
- The gaming-mode buttons block on the full script duration (up to
  ~2-3 minutes for `postgame.ps1`'s Ready-wait) rather than streaming
  progress - the browser shows a static "running..." message the whole
  time instead of live output. Works, but a websocket or SSE stream
  would be a nicer follow-up.
