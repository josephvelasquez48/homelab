# Kubernetes / K3s

Roadmap step 10. Multi-node cluster: Pi as control plane, desktop as a worker
node via WSL2. Bigger commitment than the single-node-on-Pi alternative, but
gets closer to the full namespace layout (ai/backend/data/monitoring all in
one cluster) from the original plan.

## Log

- 2026-09-03: **K3s control plane on the Pi.** First install attempt failed
  outright - Raspberry Pi OS doesn't enable the memory cgroup controller by
  default, which every container runtime needs. Fixed by appending
  `cgroup_memory=1 cgroup_enable=memory` to `/boot/firmware/cmdline.txt`
  (backed up first) and rebooting; confirmed the memory cgroup controller
  was live afterward, not just assumed. Second install succeeded - node
  `joe` came up `Ready` as `control-plane` immediately.

  Set up passwordless `kubectl` for the `joe` user by copying
  `/etc/rancher/k3s/k3s.yaml` to `~/.kube/config`. Note: `KUBECONFIG` needs
  to go in `~/.zshenv`, not just `~/.zshrc` - `.zshrc` only loads for
  interactive shells, and SSH-executed one-off commands (`ssh host "cmd"`,
  which is how this session runs everything) are non-interactive. Discovered
  this by testing a fresh `ssh ... "kubectl get nodes"` and watching it fail
  even though the interactive session moments earlier had it working.

- 2026-09-03: **Desktop as a worker node via WSL2 - the hard part.** WSL2
  defaults to NAT networking, where the VM's IP is only reachable from the
  Windows host itself - the Pi's control plane can't reach back into a NAT'd
  node for scheduling, logs, or exec. Fixed by enabling WSL2's **mirrored**
  networking mode (`networkingMode=mirrored` in `.wslconfig`), which makes
  the WSL2 VM share the host's real LAN IP directly - confirmed by checking
  the new distro's own `ip addr` and seeing `192.168.1.131` (the desktop's
  actual IP), not a NAT range.

  **This caused three separate Docker Desktop outages** while getting there,
  since mirrored mode changes networking for every WSL2 distro on the
  machine, including Docker Desktop's own:
  1. A `wsl --shutdown` mid-restart left stale Docker Desktop processes
     holding old port bindings, so the fresh instance failed to rebind
     `5432`/`8000` ("address already in use"). Fixed by force-killing every
     Docker-related process and doing a clean `down`/`up` instead of relying
     on `restart: unless-stopped` to recover stale state on its own.
  2. A stuck WSL2 reparse-point socket file
     (`sailor-ingest.sock`) blocked Docker Desktop's ingest server from
     starting, twice - not a process holding a lock, but a Windows-side
     filesystem object even `fsutil` (admin-only) initially couldn't clear
     cleanly. Needed the user to force-remove it from an elevated
     PowerShell.
  3. General lesson: **interrupting a Docker Desktop restart mid-boot
     (e.g. another `wsl --shutdown`) compounds the corruption** rather than
     fixing it. The recovery that actually stuck was: kill everything,
     wait several seconds for full tear-down, restart once, then wait it
     out fully without touching anything.

  This instability was significant enough to explicitly flag and confirm
  with the user before continuing, rather than assuming the chosen
  architecture (multi-node via WSL2 mirrored networking) was worth the risk
  - they chose to continue.

  Installed Ubuntu 24.04 as the WSL2 distro itself hit a snag too: `wsl
  --install -d Ubuntu` hung indefinitely with near-zero CPU usage - it
  depends on the Microsoft Store app, which doesn't complete its interactive
  flow in a non-interactive/automated context. Killed the stuck process and
  installed via `winget install Canonical.Ubuntu.2404` instead (bypasses the
  Store), then `ubuntu2404.exe install --root` for non-interactive first-run
  setup (root-only, no interactive user/password prompt - fine for a
  dedicated infra node, not a general dev environment).

  Once the agent was installed (`K3S_URL`/`K3S_TOKEN` env vars pointing at
  the Pi), it still couldn't join - `journalctl -u k3s-agent` showed
  consistent timeouts reaching `192.168.1.253:6443`, not a certificate or
  auth error. Root cause: the Pi's `ufw` never had rules for K3s's own
  cluster networking ports - only SSH/DNS/Grafana had been opened
  previously. Added `6443/tcp` (API server), `8472/udp` (Flannel VXLAN,
  pod-to-pod networking), and `10250/tcp` (kubelet), all scoped to
  `192.168.1.0/24` like every other rule on this Pi. Agent joined within
  seconds of the firewall fix - confirming the firewall, not WSL2 or K3s
  itself, was the actual blocker.

  **Verified the full cluster, not just that install commands succeeded**:
  `kubectl get nodes` from the Pi shows both `joe` (control-plane,
  `192.168.1.253`, Debian 13/arm64) and `desktop-j1grrmu` (worker,
  `192.168.1.131`, Ubuntu 24.04/amd64 via WSL2) as `Ready`. Confirmed
  Docker Desktop's existing stack (FastAPI/Postgres/Redis/worker, DNS,
  metrics) still fully functional after all of the above.

## Next

Namespaces (`ai`, `backend`, `data`, `monitoring`) and migrating the
existing Docker Compose workloads onto the cluster: Deployments, Services,
ConfigMaps, Secrets, PersistentVolumes, RBAC, health probes, resource
limits.

## Log (continued) - workload migration

- 2026-09-03: Migrated everything onto the cluster: `data/postgres`,
  `backend/{redis,api,worker}`, `ai/inference`, `monitoring/{prometheus,
  grafana}`. Manifests in `kubernetes/`, one file per component.

  **Getting images into the cluster at all was the first real problem.**
  K3s's containerd is a separate image store from Docker Desktop - an
  image built with `docker build` isn't visible to it. Pushed
  `apps/api`'s image to `ghcr.io/josephvelasquez48/homelab-api` instead
  (made the package public after confirming with the user, since pull
  secrets add real complexity for a personal project with no sensitive
  code). The cluster is also mixed-architecture (Pi = arm64, desktop
  worker = amd64), so a single-platform build wouldn't run on both nodes -
  built with `docker buildx build --platform linux/amd64,linux/arm64
  --push`, which needed a `docker-container` driver builder first (the
  default `docker` driver can't do multi-platform pushes at all - found
  this out from the build simply refusing to run, not a vague failure).

  **Design decisions, not just mechanical migration:**
  - `postgres` and `redis` are pinned to the Pi (`nodeSelector:
    kubernetes.io/hostname: joe`) with `local-path` PVCs. local-path PVs
    are tied to whichever node first hosts them - without pinning, a
    reschedule to the other node would leave the pod unable to find its
    own data. The Pi is also just the more "always-on" node; the desktop
    gets rebooted for normal use in a way a dedicated Pi doesn't.
  - `ai/inference` is a Service+Endpoints pair (no selector, manually
    specified IP) rather than an ExternalName Service - it points at
    native Ollama on the desktop (still not containerized, same reasoning
    as Milestone 1) by IP directly instead of depending on a pod's own
    external DNS resolution working for a hostname.
  - Prometheus uses `kubernetes_sd_configs` (pods annotated
    `prometheus.io/scrape: "true"`) instead of a static target list, which
    is what gives the RBAC ClusterRole/ClusterRoleBinding an actual
    purpose - Prometheus needs to query the API server to discover pods,
    it's not a token unused example.
  - The `ai`/`embeddings`/`rag-api` split from the original plan's example
    namespace layout isn't implemented as separate services - the FastAPI
    app already serves `/v1/embed` and `/v1/rag/query` from the same
    codebase as everything else in `backend/api`, and deploying the exact
    same image three times under different names would be namespace
    theater, not a real architectural split. It would take an actual code
    split to be honest, which wasn't done here.
  - CoreDNS (whole-LAN DNS, `hostNetwork`, port 53) was **not** migrated -
    it wasn't in the plan's own namespace example, and a botched rollout
    of something that critical is a worse failure mode than the modest
    benefit of having it in the cluster too. Stays on Docker Compose.

  **Blocked on Ollama being `127.0.0.1`-only.** Docker containers could
  always reach it via `host.docker.internal`'s special host-loopback
  routing, but a real pod on a different machine (the Pi) needs a genuine
  network path - `127.0.0.1` inside Ollama's own bind address isn't
  reachable from anywhere else no matter what the cluster networking
  looks like. This has a real security trade-off (opening Ollama to the
  LAN means any device on the network can call it directly, bypassing the
  gateway's auth/rate-limiting - explicitly against the "apps talk to the
  gateway, never straight to Ollama" design goal), so it was surfaced and
  confirmed rather than just done. Fixed with `OLLAMA_HOST=0.0.0.0:11434`
  plus a Windows Firewall rule scoping inbound `11434` to
  `192.168.1.0/24` - narrows the trade-off to "reachable within the LAN"
  rather than "reachable from anywhere."

  **The WSL2 idle-timeout gotcha came back.** The worker node went
  `NotReady` on its own, ~15 minutes after joining, with nothing having
  touched it - turned out WSL2 stops idle VMs after a period of no
  foreground activity, killing `k3s-agent` along with it, even though it
  was a live systemd service. Fixed with `vmIdleTimeout=-1` in
  `.wslconfig`. This needed another `wsl --shutdown` to take effect,
  applied deliberately while the cluster had zero real workloads on it
  yet - the lowest-risk moment available, given how disruptive that
  command had already proven to be.

  **`vmIdleTimeout=-1` didn't fully fix it** - the worker node went
  `NotReady` again roughly 45 minutes into the session, after real
  workloads existed. Waking the WSL2 distro with any command
  (`wsl -d Ubuntu-24.04 -- ...`) reliably brought `k3s-agent` back within
  15-30s and the node rejoined as `Ready` on its own, so this isn't
  data-loss-risky, just an open annoyance - worth a proper fix (a
  scheduled task pinging the distro, or investigating why the config
  isn't sticking) before this cluster is depended on for anything that
  needs to survive an unattended stretch of idle time.

  **Verified real functionality throughout, not just `kubectl apply`
  succeeding:** a rolling update on `api` (`maxUnavailable: 0`) replaced
  both pods with zero dropped requests; `/health`, `/v1/chat`, `/v1/embed`,
  `/v1/documents` + `/v1/rag/query`, and `/jobs` all tested through the
  real `api.home` Ingress hostname (not `kubectl exec` shortcuts) with
  fresh, never-cached responses; a rag query correctly retrieved a
  freshly-ingested document and answered from it; Prometheus showed every
  target - including dynamically-discovered pods - `up`; Grafana's
  dashboard and datasource were confirmed actually provisioned via its
  API, not just that the pod was healthy. Only after all of that did the
  old Docker Compose backend stack (api/worker/postgres/redis) and the old
  Docker Compose Prometheus/Grafana get torn down.

## Current state

| Namespace | Workload | Node | Notes |
|---|---|---|---|
| `data` | `postgres` | Pi | pgvector, `local-path` PVC |
| `backend` | `redis` | Pi | `local-path` PVC |
| `backend` | `api` (2 replicas) | either | Ingress: `api.home`, `ai.home` |
| `backend` | `worker` | either | processes the Redis job queue |
| `ai` | `inference` | - | Endpoints -> native Ollama, `192.168.1.131:11434` |
| `monitoring` | `prometheus` | Pi | K8s SD + RBAC for pod discovery |
| `monitoring` | `grafana` | Pi | Ingress: `grafana.home` |

Not migrated, staying on Docker Compose: CoreDNS (whole-LAN DNS, too
critical to risk on a first K8s pass) and node-exporter (needs host
`/proc`/`/sys`, simpler to leave where it already works).

## Next

CI/CD (roadmap step 11) is the natural next step now that there's an
image registry and a real deployment target - `git push` -> tests -> build
-> push to ghcr.io -> `kubectl apply`/rollout, replacing the manual
build-and-push done by hand in this phase.
