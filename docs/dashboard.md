# Dashboard

A small status page at `dashboard.home`: a FastAPI app plus one HTML page,
running in the cluster and pinned to the Pi. Not on the original roadmap.

## What it shows and does

- **Status** (`GET /api/status`, open on the LAN): nodes, pods per
  namespace, Argo CD sync and health, `api.home` reachability, the Pi's
  hardware metrics, and which models Ollama has loaded on the GPU.
- **Cross-node health:** it looks at Prometheus scrapes of pods on the
  worker node. Host-network and control-plane targets are excluded,
  because they answer even when the pod network is broken. No data shows
  as *Unknown*, not as healthy.
- **Free the GPU** (`POST /api/gpu/release`, login required): unloads the
  models from the desktop's GPU for gaming - see
  [gaming-mode.md](gaming-mode.md).

## Design

- **Pinned to the Pi**, so it stays up when the worker is off - exactly
  when you'd want to check status.
- **Read-only, narrow RBAC:** `get`/`list` on nodes and pods, and read
  access to Argo CD Applications in the `argocd` namespace only. It uses
  the pod's own ServiceAccount token, no kubeconfig.
- **No reach into the desktop.** Freeing the GPU is one HTTP call to the
  existing `inference` Service. It used to SSH to the desktop and run
  PowerShell to drain a node; that machinery (a keypair, host-key
  handling, `ssh_runner.py`) was deleted when the desktop left the
  cluster ([node-migration.md](node-migration.md)).

## Auth on the one action

The status is open on the LAN, like Grafana. The action needs a login.

**Why:** a bodyless `POST` is a "simple" cross-site request, so any web
page a LAN user visited could make their browser fire it - and the
NetworkPolicy would see a legitimate LAN address. The NetworkPolicy
decides who can *reach* the endpoint, not who can *cause* the request.

Two layers:

1. **A session cookie** - signed, `HttpOnly`, `SameSite=Strict`, `Secure`
   (the site is HTTPS). Not an API key: in a browser app, any key the page
   sends is readable by anyone who loads the page.
2. **`Content-Type: application/json` required**, which forces a CORS
   preflight this app never approves. Defence in depth behind the cookie.

**It fails closed.** The `dashboard-auth` Secret (`DASHBOARD_PASSWORD`,
`SESSION_SECRET`) is optional, so a missing Secret doesn't take the status
page down. Without a password, login always fails and the action is
unreachable; without a session secret, a random one is generated, so
cookies can't be forged. The pod logs a warning, `/api/session` reports
`configured: false`, and the page explains why the button is dead.

Check the deployed site (every request is unauthenticated and must be
rejected):

```bash
./scripts/verify-dashboard-auth.sh https://dashboard.home
```

It treats a missing password as a failure: every check would "pass" for
the wrong reason.

## Worth knowing from the SSH era

Still true of Windows OpenSSH, and each cost real time:

- **Admin accounts ignore `~/.ssh/authorized_keys`**; Windows reads
  `C:\ProgramData\ssh\administrators_authorized_keys` (SYSTEM and
  Administrators only). Symptom: `Permission denied (publickey)` with a
  correct key.
- **A firewall rule scoped to the Private profile does nothing** when the
  active network is categorised Public. Check `Get-NetConnectionProfile`.
- **A new ghcr.io package is private by default**, so K3s's anonymous
  pull fails with 401 until it's made public.

## Known gaps

- The read-only status (cluster layout, pod names, node metrics) is open
  to anything on the LAN.
- One shared password: no accounts, lockout or audit log.
