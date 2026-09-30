# Security testing

Roadmap step 15. What's exposed, what was found by reading the manifests,
what's fixed, and how to scan it.

Scan from a trusted machine **on the LAN**, so you see what a LAN client
sees. Scanning from the Pi tests a loopback path no real client uses.

## Targets

| What | Address |
|---|---|
| Pi - K3s control plane, CoreDNS, AdGuard | 192.168.1.253 |
| M1 VM - K3s worker | 192.168.1.63 |
| Windows desktop - Ollama `:11434`, sshd (not in the cluster) | 192.168.1.131 |
| Web: `api.home`, `ai.home`, `grafana.home`, `argocd.home`, `dashboard.home`, `chat.home`, `wikipedia.home`, `prometheus.home`, `alerts.home` | all via Traefik on the Pi, HTTPS |
| Phone bridge | `phone.home:8443` (its own TLS, [phone.md](phone.md)) |

## Findings and status

Found by reading the manifests first - so a scanner that comes back clean
on them is misconfigured, not reassuring.

| # | Finding | Status |
|---|---|---|
| 1 | **The dashboard's POST endpoints ran remote commands with no auth.** A bodyless `POST` is a simple cross-site request, so any web page could make a LAN browser trigger a node drain; the NetworkPolicy only checks who *reaches* it | **Gone**: session cookie and JSON-only POST until 2026-09-30, when the status page and its one action (Free the GPU) were removed - the dashboard now has no POST routes |
| 2 | **Most workloads run as root** - no `securityContext` | **Partly**: hardened - adguard-exporter, alertmanager, chat, kiwix. Not yet - api, worker, dashboard, redis, postgres, grafana, prometheus. The block to copy is in `kubernetes/monitoring/adguard-exporter.yaml` |
| 3 | A `hostNetwork`, host-PID node-exporter on the desktop could read its whole disk | **Gone** with the desktop node |
| 4 | **East-west traffic was allowed everywhere** - any pod could reach Redis and Postgres | **Partly**: Redis and Postgres now accept only the `backend` namespace. Redis still has no password; `inference` (Ollama) is open to any pod |
| 5 | **No TLS** - logins and API keys crossed the LAN in clear text | **Fixed** for every Ingress ([https.md](https.md)). Still plain: AdGuard's admin on `:3000`, Ollama on the desktop |
| 6 | **Floating image tags** (`redis:7-alpine`, `pgvector/pgvector:pg17`, the `python:3.12-slim` base) | **Open**. Our own images are pinned to a git SHA, not a digest |

## Before you scan

- **Don't fuzz `argocd.home`.** Argo CD has cluster-wide write access.
  Use passive scans.

## How to scan

**1. Images and dependencies** (Trivy, from anywhere with Docker):

```bash
trivy image --severity HIGH,CRITICAL --ignore-unfixed ghcr.io/josephvelasquez48/homelab-api:<tag>
trivy fs --scanners vuln,secret --severity HIGH,CRITICAL --skip-dirs apps/api/.venv .
```

The SOPS files should come back clean; a secret hit means something was
committed decrypted.

**2. Kubernetes config:**

```bash
trivy config --severity MEDIUM,HIGH,CRITICAL kubernetes/
kubectl run kube-bench --rm -it --restart=Never --image=aquasec/kube-bench:latest \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"joe"},"hostPID":true}}' \
  -- run --targets master,node --benchmark k3s-cis-1.24
```

Trivy should flag the seven unhardened workloads and *not* the four
hardened ones - a useful check that it's configured right. It can't see
Argo CD, Traefik or K3s's own components (installed from upstream);
kube-bench covers those. K3s fails some CIS checks by design - read the
remediation before changing anything.

**3. Network exposure** (from the LAN, not a node):

```bash
sudo nmap -sS -sV -p- --reason 192.168.1.253 192.168.1.131
```

Expect 80/443 on the Pi. Watch for `6443` (K3s API), `11434` (Ollama,
unauthenticated), `22` on the desktop, and `5432`/`6379`, which should
**not** appear. Then from inside a pod, which shows what a compromised
container can reach:

```bash
kubectl run kube-hunter --rm -it --restart=Never --image=aquasec/kube-hunter -- --pod
```

**4. Web and API** (ZAP baseline is passive - safe):

```bash
docker run --rm -t -v "$(pwd):/zap/wrk:rw" ghcr.io/zaproxy/zaproxy:stable zap-baseline.py -t https://api.home -r zap-api.html
nuclei -l targets.txt -severity medium,high,critical -exclude-tags dos,fuzz -o nuclei.txt
```

`-exclude-tags dos,fuzz` matters: fuzzing templates POST to discovered
routes. For a full API scan, give ZAP an `X-API-Key` and point the API at
a scratch database first - `/v1/documents` writes.

## What to fix next

1. Redis `requirepass`, and a NetworkPolicy for `inference`.
2. `securityContext` on the remaining seven workloads.
3. Pin third-party images by digest.
4. Trivy image and config scans in CI, failing on HIGH/CRITICAL.
   (Network and web scans stay manual - they need a LAN position.)
