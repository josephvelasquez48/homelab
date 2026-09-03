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
            DNS["CoreDNS (Docker Compose)<br/>*.home resolution, whole-LAN"]
            Traefik["Traefik Ingress<br/>(K3s-bundled, LAN-only NetworkPolicy)"]
            subgraph PiWorkloads["K3s workloads pinned here (local-path PVCs)"]
                PG[("Postgres<br/>+ pgvector")]
                Redis[("Redis")]
                ArgoCD["Argo CD"]
                Prom["Prometheus"]
                Graf["Grafana"]
            end
        end

        subgraph Desktop["Desktop — node 'desktop-j1grrmu' (K3s worker via WSL2, mirrored networking)"]
            Ollama["Ollama (native process)<br/>RTX 3070 Ti, qwen2.5-coder + nomic-embed"]
            subgraph DesktopWorkloads["K3s workloads (either node, unpinned)"]
                API["FastAPI api<br/>2 replicas, HPA-free"]
                Worker["Job worker<br/>(Redis queue consumer)"]
            end
        end
    end

    subgraph GitOps["GitOps"]
        Dev["git push"] --> CI["GitHub Actions CI<br/>test -> build multi-arch -> push ghcr.io<br/>-> commit new image tag [skip ci]"]
        Repo[("git: kubernetes/**")]
        CI --> Repo
    end

    Client -->|DNS lookup| DNS
    Client -->|HTTPS| Traefik
    Traefik --> API
    API --> PG
    API --> Redis
    API -->|HTTP, LAN, split connect/read timeout| Ollama
    Worker --> Redis
    Worker -->|HTTP, LAN| Ollama
    Prom -.->|scrape /metrics| API
    Prom -.->|scrape /metrics| Worker
    Graf -->|query| Prom

    ArgoCD -->|poll ~3min: new commits<br/>real-time: live drift| Repo
    ArgoCD -->|apply + selfHeal| PiWorkloads
    ArgoCD -->|apply + selfHeal| DesktopWorkloads
```

## Notes on what this diagram intentionally omits

- **Secrets** (`docs/secrets.md`) - SOPS-encrypted, applied out-of-band,
  deliberately outside Argo CD's sync path. Not drawn as a GitOps-managed
  resource because it isn't one.
- **The flannel VXLAN -> host-gw networking layer** underneath the two
  K3s nodes (`docs/kubernetes.md`) - this diagram shows the workload
  topology, not the pod-network internals that made cross-node traffic
  actually work.
- **ufw** on the Pi, scoping every inbound port to the LAN - see
  `docs/kubernetes.md` and `docs/argocd.md` for the real gap found and
  fixed there (kube-router's own iptables chains processed before ufw's).
