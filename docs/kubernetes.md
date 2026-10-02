# Kubernetes (K3s)

Roadmap step 10. A two-node K3s cluster runs every homelab service except
DNS and the phone bridge: the Raspberry Pi 5 is the control plane, and a
Linux VM on an M1 MacBook is the worker. The Windows desktop is not in the
cluster; it only runs Ollama on its GPU.

## Current state

| Node | Role | Hardware | Address |
|---|---|---|---|
| `joe` | control plane, and where state lives | Raspberry Pi 5, Debian 13, arm64 | 192.168.1.253 |
| `m1-node` | worker | Ubuntu 24.04 VM (Multipass, bridged) on an M1 MacBook, arm64 | 192.168.1.63 |

The worker was a WSL2 VM on the desktop until 2026-09-10; why it moved is
in [node-migration.md](node-migration.md). When the Mac is off, `m1-node`
goes `NotReady` and anything unpinned runs on the Pi alone.

### Workloads

| Namespace | Workload | Placement | Reached at |
|---|---|---|---|
| `data` | Postgres + pgvector | Pi (5Gi `local-path`) | backend only (NetworkPolicy) |
| `backend` | Redis | Pi (1Gi) | backend only (NetworkPolicy) |
| `backend` | FastAPI `api`, 2 replicas | either node | `api.home`, `ai.home` |
| `backend` | `worker` (Redis job queue) | either node | - |
| `ai` | `inference` | Service + hand-written Endpoints to Ollama on the desktop, `192.168.1.131:11434` | in-cluster |
| `monitoring` | Prometheus, Grafana, Alertmanager, adguard-exporter | Pi | `prometheus.home`, `grafana.home`, `alerts.home` |
| `argocd` | Argo CD | - | `argocd.home` |
| `chat`, `kiwix`, `dashboard` | chat assistant, offline Wikipedia, status page | chat either; others Pi | `chat.home`, `wikipedia.home`, `dashboard.home` |

Stateful workloads are pinned to the Pi: `local-path` volumes live on
the node that created them, and the Pi is the machine that's always on.

Not in the cluster: CoreDNS and AdGuard (whole-LAN DNS, too critical for
a first Kubernetes pass - [router-migration.md](router-migration.md)) and
the phone bridge (it needs the Bluetooth radio - [phone.md](phone.md)).

### Networking and security

- **Pod network:** flannel with the `wireguard-native` backend
  (`--flannel-backend` on the K3s server). It was chosen when the worker
  was WSL2, where `vxlan` and `host-gw` both failed (below). The move back
  to `vxlan` planned in node-migration.md hasn't been done.
- **Ingress:** Traefik (bundled with K3s), HTTPS with the homelab CA
  ([https.md](https.md)).
- **NetworkPolicies:** `traefik-lan-only` (Traefik reachable from the LAN,
  plus Prometheus for its metrics); Postgres and Redis reachable from
  `backend` only; adguard-exporter reachable from Prometheus only; Argo
  CD's own policies.
- **Pod DNS goes through the Pi's resolver** (CoreDNS + AdGuard,
  `192.168.1.253`), like every other device on the LAN. Until 2026-10-01 it
  didn't: K3s builds the upstream for the cluster's CoreDNS from the node's
  `/etc/resolv.conf`, and when that lists only loopback - the Pi's is
  `127.0.0.1`, m1-node's systemd-resolved stub too - it quietly substitutes
  `8.8.8.8`. Cluster lookups skipped AdGuard's filtering and log, and pods
  couldn't resolve `*.home`. Now both nodes have
  `/etc/rancher/k3s/resolv.conf` (`nameserver 192.168.1.253`) and
  `config.yaml` (`resolv-conf:` that file). **It has to be on every node**:
  CoreDNS isn't pinned, and it reads the upstream of whichever node it
  lands on - setting only the Pi looked done until CoreDNS restarted onto
  m1-node. The Pi's half is in Ansible (`roles/k3s`, `--tags k3s`); m1-node
  isn't in the inventory, so its half was written by hand, through a
  `kubectl debug node` pod, followed by `systemctl restart k3s-agent`. Check:
  `dig @10.43.0.10 phone.home` answers `192.168.1.253`, and
  `dig @10.43.0.10 doubleclick.net` comes back blocked.
- **ufw doesn't filter pod traffic.** K3s's iptables chains run before
  ufw's, so ufw rules don't apply to anything reaching a pod. The real
  boundaries are the NetworkPolicies above - found by testing from outside
  the allowed range, see [argocd.md](argocd.md).
- **Images:** built for `linux/arm64` by CI and pulled from
  `ghcr.io/josephvelasquez48/...` ([cicd.md](cicd.md)). Both nodes are
  arm64 now; multi-arch builds were dropped with the WSL2 node.

## How it got here

- **2026-09-03:** K3s on the Pi (after enabling the memory cgroup in
  `cmdline.txt`), the desktop joined as a WSL2 worker, and the Docker
  Compose stack moved in. Each service was checked through its real
  Ingress hostname before the Compose version was torn down.
- **2026-09-03 to 09-06:** a run of WSL2-specific networking failures (table
  below), each root-caused with packet captures.
- **2026-09-10:** the worker moved to the M1 VM and the desktop left the
  cluster - [node-migration.md](node-migration.md).

## Incidents and lessons

| What broke | Why | Fix | Lesson |
|---|---|---|---|
| K3s wouldn't start | Raspberry Pi OS leaves the memory cgroup off | `cgroup_memory=1 cgroup_enable=memory` in `cmdline.txt` | Check prerequisites against the running kernel, not assumed |
| Worker couldn't join | ufw on the Pi had no rules for 6443, 8472, 10250 | LAN-scoped rules | The error was a timeout, not auth - read the error class |
| `kubectl` failed over SSH only | `KUBECONFIG` was in `.zshrc`, which non-interactive shells skip | Moved to `.zshenv` | Test the way it's actually used |
| Pods on the WSL2 node couldn't resolve DNS | Windows silently dropped inbound flannel VXLAN, after both firewall layers were opened | Switched flannel to `host-gw` | Packet captures on both ends located the drop |
| Pi couldn't reach pods on the WSL2 node | Windows' IP stack refuses to forward (`pktmon`: `Not locally destined`) | Switched to `wireguard-native` - traffic to a local socket, not forwarded | `host-gw` needs the host to route, which Windows won't |
| WireGuard still dead after the switch | Old `/24` routes from `host-gw` beat the new `/16` by longest-prefix match | `ip route del` the old routes | A backend switch doesn't clean up the previous backend's routes |
| WireGuard got no replies after reboots | WSL2 mirrored mode didn't register the kernel socket for inbound until a userspace bind was tried on that port | A logon task that does that bind | A convincing ufw gap looked like the cause and wasn't - logs and `tcpdump` said the Pi was answering |
| WSL2 node flapping `NotReady` | `vmIdleTimeout` covers only the shared VM; the distro cold-booted every minute, hidden by a keepalive task | `instanceIdleTimeout=-1` | "Stayed Ready" wasn't proof - `dmesg` showed 100+ reboots |
| Prometheus couldn't scrape Traefik | `traefik-lan-only` blocked in-cluster traffic | A narrow rule for the Prometheus pod on port 9100 | The policy was working as written |
| adguard-exporter reachable from any pod on the Pi | A Compose container has no NetworkPolicy boundary | Moved into the cluster with a Prometheus-only policy | Verified from four pods: allowed, refused, timed out - one policy, two paths |
| Changed Endpoints never applied, while Argo said `Synced` | Argo CD excludes `Endpoints` by default | Applied by hand; warning in the manifest | `Synced` means "what Argo looks at matches" |
| Grafana `OOMKilled` | 512Mi limit, hit during restart churn | 1Gi | The node had headroom; the container limit was the ceiling |
| Pods on the WSL2 node couldn't reach Ollama on the same machine | A Windows listener is invisible to WSL-side traffic to the same IP | Ollama moved into WSL (later back to Windows when the desktop left) | Test from every node, not just one |

## Everyday commands

```bash
kubectl get nodes -o wide                        # is the Mac up?
kubectl get pods -A -o wide | grep -v Running    # anything unhappy
kubectl -n argocd get applications               # GitOps state
kubectl -n argocd get application ai -o jsonpath='{.status.conditions}'   # excluded-resource warnings
```
