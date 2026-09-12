# Architecture

Current state as of the Kubernetes migration ([docs/kubernetes.md](../docs/kubernetes.md))
onward - not the original target sketch in the top-level README, which
predates the K3s decision. See [docs/milestone-1.md](../docs/milestone-1.md)
for the pre-Kubernetes Docker Compose architecture this replaced.

```mermaid
flowchart TB
    Client["Client / browser"]

    subgraph LAN["Home network (192.168.1.0/24)"]
        subgraph Pi["Raspberry Pi 5 — node 'joe' (K3s control-plane)"]
            DNS["CoreDNS (Docker Compose)<br/>*.home resolution, LAN-facing"]
            AdGuard["AdGuard Home (Docker Compose)<br/>ad/tracker filtering, loopback-only"]
            Traefik["Traefik Ingress<br/>(K3s-bundled, LAN-only NetworkPolicy)"]
            subgraph PiWorkloads["K3s workloads pinned here (local-path PVCs)"]
                PG[("Postgres<br/>+ pgvector")]
                Redis[("Redis")]
                ArgoCD["Argo CD"]
                Prom["Prometheus"]
                Graf["Grafana"]
                AdGuardExporter["adguard-exporter<br/>NetworkPolicy: ingress from Prometheus only"]
                Alert["Alertmanager<br/>emails on backup failure"]
            end
            NodeExp["node_exporter (Docker Compose)<br/>+ textfile collector"]
        end

        subgraph M1["M1 MacBook — node 'm1-node' (K3s worker, Linux VM, bridged)"]
            subgraph M1Workloads["K3s workloads (either node, unpinned)"]
                API["FastAPI api<br/>2 replicas, HPA-free"]
                Worker["Job worker<br/>(Redis queue consumer)"]
            end
            Backup["restic backup collector<br/>nightly pull + restore rehearsal"]
        end

        subgraph Desktop["Windows desktop — GPU host, not a cluster member"]
            Ollama["Ollama (native Windows process)<br/>RTX 3070 Ti, qwen2.5-coder + nomic-embed"]
        end
    end

    subgraph GitOps["GitOps"]
        Dev["git push"] --> CI["GitHub Actions CI<br/>test -> build multi-arch -> push ghcr.io<br/>-> commit new image tag [skip ci]"]
        Repo[("git: kubernetes/**")]
        CI --> Repo
    end

    Client -->|DNS lookup| DNS
    DNS -->|forward, non-.home| AdGuard
    Client -->|HTTPS| Traefik
    Traefik --> API
    API --> PG
    API --> Redis
    API -->|HTTP, LAN, split connect/read timeout| Ollama
    Worker --> Redis
    Worker -->|HTTP, LAN| Ollama
    Prom -.->|scrape /metrics| API
    Prom -.->|scrape /metrics, only pod NetworkPolicy allows in| AdGuardExporter
    AdGuardExporter -->|/control/status, /control/stats| AdGuard
    Graf -->|query| Prom
    Prom -.->|fires rules| Alert
    Alert -->|SMTP| Email["Email"]
    Prom -.->|scrape| NodeExp
    Backup -->|tar over SSH: K3s state.db, Postgres, Redis, Grafana| Pi
    Backup -.->|snapshot age, last exit code| NodeExp

    ArgoCD -->|poll ~3min: new commits<br/>real-time: live drift| Repo
    ArgoCD -->|apply + selfHeal| PiWorkloads
    ArgoCD -->|apply + selfHeal| M1Workloads
```

## Notes on what this diagram intentionally omits

- **Secrets** (`docs/secrets.md`) - SOPS-encrypted, applied out-of-band,
  deliberately outside Argo CD's sync path. Not drawn as a GitOps-managed
  resource because it isn't one.
- **The flannel pod-network layer** underneath the two K3s nodes
  (`docs/kubernetes.md`) - currently the `wireguard-native` backend,
  after `vxlan` and then `host-gw` each turned out to be incompatible
  with WSL2 mirrored networking in a different way. This diagram shows
  the workload topology, not the pod-network internals that made
  cross-node traffic actually work.
- **ufw** on the Pi, scoping every inbound port to the LAN - see
  `docs/kubernetes.md` and `docs/argocd.md` for the real gap found and
  fixed there (kube-router's own iptables chains processed before ufw's).
