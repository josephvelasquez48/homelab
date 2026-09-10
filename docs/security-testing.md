# Security Testing

Layered security validation for the homelab. Scoped to the actual assets in
this repo — the manifests under `kubernetes/`, the images those manifests
pin, and the five hostnames CoreDNS serves.

Run the scanners from a trusted machine or an isolated VM **on the LAN**, so
you test the same attack surface a LAN client actually sees. Scanning from
the Pi or the desktop tests a loopback path that no real client uses.

## Targets

| What | Address | Notes |
|---|---|---|
| Pi (control plane, node `joe`) | `192.168.1.253` | K3s server, CoreDNS, AdGuard |
| Desktop (worker, `desktop-j1grrmu`) | `192.168.1.131` | Ollama :11434, sshd, node-exporter :9100 |
| LAN | `192.168.1.0/24` | The CIDR `traefik-lan-only` allows |
| `api.home` / `ai.home` | → `192.168.1.253` | FastAPI behind Traefik |
| `grafana.home` | → `192.168.1.253` | Grafana 13.2.1 |
| `argocd.home` | → `192.168.1.253` | Argo CD, `--insecure` |
| `dashboard.home` | → `192.168.1.253` | Status page **+ gaming-mode trigger** |

Images currently pinned by the manifests:

```
ghcr.io/josephvelasquez48/homelab-api:5d06bdd8d884
ghcr.io/josephvelasquez48/homelab-dashboard:69f979a559a7
redis:7-alpine
pgvector/pgvector:pg17
grafana/grafana:13.2.1
prom/node-exporter:v1.9.1
ghcr.io/josephvelasquez48/homelab-adguard-exporter:cc809aa77468
```

---

## Before you scan: read this

**Do not run an active ZAP scan or an unrestricted nuclei run against
`dashboard.home`.**

`apps/dashboard/app/main.py` exposes two POST endpoints that reach the host:

```python
@app.post("/api/gaming/on")   # -> ssh -> powershell pregame.ps1  (cordon + drain the desktop)
@app.post("/api/gaming/off")  # -> ssh -> powershell postgame.ps1 (uncordon, wait for Ready)
```

FastAPI publishes both in `/openapi.json`. Any scanner that imports the spec,
or that fuzzes discovered POST routes, **will drain a node in the middle of
your scan** and then leave you blaming the resulting pod churn on something
else. Use a passive baseline scan for that host, or exclude the two paths
explicitly.

**Status: fixed in the code, gated on the deployed check.** Both endpoints now
require a session (see finding 1). The exclusions below stay in place until
this passes against the live host:

```bash
./scripts/verify-dashboard-auth.sh http://dashboard.home
```

Unit tests cannot clear this gate. They never exercise Traefik, the Ingress,
the real Secret, or the cookie attributes a browser enforces — and the
failure being guarded against is a node drain, so "it passed in CI" is not
evidence that the deployed endpoint is closed.

The same caution applies to `argocd.home` — Argo CD has cluster-wide write
access and its API is reachable over plain HTTP.

---

## Ground truth (found by reading the manifests, before any scanner ran)

Use this list to check that your tooling is actually configured correctly. A
Trivy config run that comes back clean on `kubernetes/` is misconfigured, not
reassuring.

### 1. `dashboard.home` triggers remote command execution with no auth

**Fixed — verify with `scripts/verify-dashboard-auth.sh` before trusting it.**

The highest-impact finding, and the one no config scanner will catch — it is
application logic, not a manifest problem.

- As shipped, the endpoints had no `Depends(...)` guard of any kind. Compare
  `apps/api/`, where every router carries `dependencies=[Depends(rate_limit)]`
  and `rate_limit` chains `require_api_key`. The dashboard had no equivalent.
- The pod mounts an SSH private key (`dashboard-ssh-key`, mode `0400`) and
  shells out to `powershell.exe -ExecutionPolicy Bypass -File ...` on
  `192.168.1.131` as user `josep`.
- `traefik-lan-only` limits *who can reach it* to `192.168.1.0/24`. It does
  not limit *who can cause a request*. A plain `fetch()` from any page on the
  public internet is a CORS-simple request — no preflight, opaque response,
  but the POST still executes:

  ```js
  fetch('http://dashboard.home/api/gaming/on', {method: 'POST', mode: 'no-cors'})
  ```

  Any browser on the LAN that loads a hostile page drains the node. The
  NetworkPolicy is satisfied because the request genuinely originates from a
  LAN host: the victim's own browser.

Before the fix this returned `200` from any LAN host, and drained the node.
It should now return `401`:

```bash
curl -si -X POST -H 'Content-Type: application/json' -d '{}' http://dashboard.home/api/gaming/off | head -1
```

`scripts/verify-dashboard-auth.sh` runs that plus the cases a single curl
misses — the bodyless simple POST, a forged cookie, and the preflight.

The fix, in order of what actually does the work:

1. **Authorization.** A signed, `HttpOnly`, `SameSite=Strict` session cookie
   guards both endpoints (`apps/dashboard/app/auth.py`). Deliberately not the
   API's `X-API-Key` pattern: the dashboard is a browser app, so any key the
   page could send would have to sit in JavaScript that anyone able to load
   the page can read — a public string, not a credential.
2. **Content type.** Both endpoints require `application/json`, which makes
   the request non-simple and forces a preflight this app answers no CORS
   for. This is defence in depth *behind* the session check, not a boundary
   of its own: `SameSite=Strict` is what actually withholds the cookie
   cross-site.

`optional: true` on the `dashboard-auth` secretRef means a missing Secret
leaves the endpoints unreachable rather than open, and keeps the status page
itself running. See docs/dashboard.md.

### 2. `securityContext` is set on only two workloads

`adguard-exporter` is the exception and the template: it sets
`runAsNonRoot`, `runAsUser`/`runAsGroup`, `allowPrivilegeEscalation: false`,
`readOnlyRootFilesystem: true`, and `capabilities.drop: ["ALL"]`. The
`inference-endpoint-sync` CronJob
(`kubernetes/ai/inference-endpoint-sync.yaml`) carries the same block.

Every other workload sets none of them, so `api`, `worker`, `dashboard`,
`redis`, `postgres`, `grafana`, and `node-exporter-desktop` all run as root
inside their namespace. Trivy config should emit findings in the
KSV001/003/012/014/020/021/030 family for those seven, and stay quiet about
the two that are hardened. That makes it a useful control: if the scan
flags all nine equally, the scan is misconfigured. The fix for the other seven is
already written, in `kubernetes/monitoring/adguard-exporter.yaml`.

### 3. `node-exporter-desktop` is the widest blast radius in the cluster

`kubernetes/monitoring/node-exporter-desktop.yaml` sets `hostNetwork: true`,
`hostPID: true`, and hostPath-mounts `/proc`, `/sys`, and `/` (as
`/host/root`). The mounts are `readOnly`, and the file documents why all three
are needed — this is the standard node_exporter trade-off, not an accident.
It is still true that compromising this container reads the desktop's entire
filesystem and sees every process on the host. Worth an explicit
accepted-risk note, so the scanner finding does not look unexamined.

### 4. East-west traffic is default-allow

Two NetworkPolicies exist: `traefik-lan-only` (kube-system) and
`adguard-exporter-prometheus-only` (monitoring). Both are ingress rules
scoped to a single pod; nothing guards `backend`, `data`, or `ai` at all,
and the rest of `monitoring` is unguarded. So:

- `redis.backend.svc:6379` has **no `requirepass`** and is reachable from any
  pod in the cluster. It holds the rate-limit counters and the job queue —
  flushing it resets every client's rate limit.
- `postgres.data.svc:5432` is reachable cluster-wide; only the password
  stands in the way.
- `inference.ai.svc:11434` forwards to Ollama on the desktop, unauthenticated.

Verify from any pod:

```bash
kubectl -n monitoring exec deploy/grafana -- sh -c 'nc -zv redis.backend.svc.cluster.local 6379; nc -zv postgres.data.svc.cluster.local 5432'
```

### 5. No TLS anywhere

Every Ingress is HTTP-only and Argo CD runs `--insecure` (documented in
`kubernetes/argocd/ingress.yaml`). Grafana's admin login, the Argo CD session
token, and the API key in `X-API-Key` all cross the LAN in cleartext, where
anything on the wire — including the CoreDNS/AdGuard host — can read them.

### 6. Mutable image tags

`redis:7-alpine`, `pgvector/pgvector:pg17`, and `python:3.12-slim` (the base
in both Dockerfiles) are floating tags. The first-party images are pinned to a
git SHA, which is reproducible from your side but is still a mutable tag in
the registry, and CI also pushes `:latest` alongside it. Digest pinning is
what makes "the manifest says X" and "the cluster runs X" the same statement.

---

## Layer 1 — Container supply chain

Trivy, against the images the manifests actually pin. Runs anywhere with
Docker; needs no LAN access.

```bash
trivy image --severity HIGH,CRITICAL --ignore-unfixed ghcr.io/josephvelasquez48/homelab-api:5d06bdd8d884
```

```bash
trivy image --severity HIGH,CRITICAL --ignore-unfixed ghcr.io/josephvelasquez48/homelab-dashboard:69f979a559a7
```

```bash
for img in redis:7-alpine pgvector/pgvector:pg17 grafana/grafana:13.2.1 prom/node-exporter:v1.9.1; do trivy image --severity HIGH,CRITICAL --ignore-unfixed "$img"; done
```

Dependencies and secrets straight from the tree, which catches anything
committed that never reached an image:

```bash
trivy fs --scanners vuln,secret --severity HIGH,CRITICAL --skip-dirs apps/api/.venv /d/homelab
```

`apps/api/uv.lock` is the lockfile Trivy reads for the Python dependency
graph. `--skip-dirs apps/api/.venv` matters — the checked-out virtualenv is in
the working tree and would otherwise be scanned as a second, duplicate
inventory.

Expect the SOPS-encrypted files under `kubernetes/secrets/` to come back
clean. If the secret scanner flags one, something got committed decrypted.

## Layer 2 — Kubernetes configuration

Trivy config over the manifests:

```bash
trivy config --severity MEDIUM,HIGH,CRITICAL /d/homelab/kubernetes
```

Cross-check against findings 2–4 above. Note that Trivy sees only what is in
this repo — Argo CD, Traefik, and K3s's own bundled components are installed
from upstream manifests and are invisible to this scan. That gap is what
kube-bench and Layer 3 cover.

CIS benchmark on the Pi, using the K3s-specific profile:

```bash
kubectl run kube-bench --rm -it --restart=Never --image=aquasec/kube-bench:latest --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"joe"},"hostPID":true}}' -- run --targets master,node --benchmark k3s-cis-1.24
```

K3s deviates from stock Kubernetes on purpose — single binary, different file
paths, embedded datastore — so a handful of failures are expected and
correct. Read the remediation text before changing anything; several CIS
items would break K3s outright.

## Layer 3 — Network exposure

From the scanner VM, not from either node.

Full service sweep of both hosts:

```bash
sudo nmap -sS -sV -p- --reason 192.168.1.253 192.168.1.131
```

Confirm the Traefik NetworkPolicy behaves the way `traefik-security.yaml`
claims — 80/443 should answer from a LAN address:

```bash
nmap -Pn -p 80,443,6443,8080,8443 192.168.1.253
```

The interesting question is what is exposed *besides* Traefik:

- `6443` (K3s API) — should not be broadly reachable
- `11434` on `.131` — Ollama, unauthenticated, no rate limit
- `9100` on `.131` — node-exporter via `hostNetwork`
- `22` on `.131` — the sshd the dashboard's key authenticates to
- `5432` / `6379` — should **not** appear; they are ClusterIP-only, and a hit
  here means something is publishing them on the host

Cluster-aware probing, from off-cluster:

```bash
docker run --rm --network host aquasec/kube-hunter --remote 192.168.1.253
```

And again as a pod, which tests what an attacker with code execution inside a
container can reach. Given finding 4, this is the more informative of the two:

```bash
kubectl run kube-hunter --rm -it --restart=Never --image=aquasec/kube-hunter -- --pod
```

## Layer 4 — Web and API

ZAP baseline is passive: it spiders and analyzes, it does not attack. Safe for
all four hosts.

```bash
docker run --rm -t -v "$(pwd):/zap/wrk:rw" ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t http://api.home -r zap-api.html
```

```bash
docker run --rm -t -v "$(pwd):/zap/wrk:rw" ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t http://grafana.home -r zap-grafana.html
```

For `dashboard.home`, stay passive and exclude the mutating endpoints anyway —
a spider that follows the UI's own buttons will reach them:

```bash
docker run --rm -t -v "$(pwd):/zap/wrk:rw" ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t http://dashboard.home -r zap-dashboard.html -z "-config globalexcludeurl.url_list.url\(0\).regex=.*/api/gaming/.*"
```

The API returns `401` without `X-API-Key`, so an unauthenticated run against
`api.home` only exercises `/health`, `/metrics`, `/docs`, and `/openapi.json`.
To reach the real surface, give ZAP the header — and point the API at a
scratch database first, because `/v1/documents` writes:

```bash
docker run --rm -t -v "$(pwd):/zap/wrk:rw" ghcr.io/zaproxy/zaproxy:stable zap-api-scan.py -t http://api.home/openapi.json -f openapi -r zap-api-full.html -z "-config replacer.full_list\(0\).description=apikey -config replacer.full_list\(0\).enabled=true -config replacer.full_list\(0\).matchtype=REQ_HEADER -config replacer.full_list\(0\).matchstr=X-API-Key -config replacer.full_list\(0\).replacement=YOUR_KEY"
```

Nuclei across all five hosts:

```bash
printf 'http://api.home\nhttp://ai.home\nhttp://grafana.home\nhttp://argocd.home\nhttp://dashboard.home\n' > targets.txt
```

```bash
nuclei -l targets.txt -severity medium,high,critical -exclude-tags dos,fuzz -o nuclei.txt
```

`-exclude-tags dos,fuzz` is not optional here. Several fuzzing templates issue
POSTs against discovered paths, which is the same node-draining problem as
above.

Grafana 13.2.1 and whichever Argo CD version is installed are the two most
likely sources of real CVE hits — both are internet-facing-grade software
running here with no TLS, and Argo CD with cluster-admin.

## Suggested order to fix

1. ~~**Authenticate `/api/gaming/*`.**~~ Done: session auth, plus a JSON
   content-type requirement behind it. Still needs the `dashboard-auth`
   Secret applied and `scripts/verify-dashboard-auth.sh` passing against the
   deployed host before gaming mode works again or the scan exclusions come
   off.
2. **NetworkPolicies for `backend` and `data`.** The pattern already exists in
   `traefik-security.yaml`; apply it so only the API and worker can reach
   Redis and Postgres. Set a Redis `requirepass` while you are there.
3. **Add `securityContext` blocks to the other seven workloads.** Mechanical,
   and it clears most of what Trivy config reports — copy the block
   already in `kubernetes/monitoring/adguard-exporter.yaml`.
4. **TLS on the Ingresses.** A local CA or cert-manager with a self-signed
   issuer; drop Argo CD's `--insecure` once it is in place.
5. **Digest-pin the third-party images.**
6. **Wire Layers 1–2 into CI** — `trivy image` after the build step and
   `trivy config` on `kubernetes/`, both gated on HIGH,CRITICAL. Layers 3–4
   stay manual; they need LAN position that GitHub-hosted runners do not have.
