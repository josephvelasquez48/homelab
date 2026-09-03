# Homelab Cloud + AI Platform

A self-hosted cloud/AI platform built across a Raspberry Pi 5 and a GPU-equipped
Windows desktop, demonstrating production-style backend, infrastructure, and
MLOps practices: Linux administration, Docker/Kubernetes, FastAPI, PostgreSQL +
pgvector, Redis, local LLM inference (RAG), CI/CD, GitOps, and observability.

## Status

Milestone 1 complete — see [docs/milestone-1.md](docs/milestone-1.md).
Backend feature list (auth, rate limiting, caching, retries, metrics,
jobs, embeddings) complete — see [docs/backend.md](docs/backend.md).
RAG pipeline complete — see [docs/rag.md](docs/rag.md).
Prometheus + Grafana deployed — see [docs/monitoring.md](docs/monitoring.md).
Migrated onto a multi-node K3s cluster (Pi + desktop via WSL2) — backend,
data, ai, and monitoring namespaces all live, old Docker Compose stacks
decommissioned — see [docs/kubernetes.md](docs/kubernetes.md).
CI/CD live — GitHub Actions tests, builds, and pushes to ghcr.io on push
— see [docs/cicd.md](docs/cicd.md).
Argo CD live — git is now the actual source of truth for the cluster,
CI commits new image tags instead of deploying directly, with a real
network security fix along the way (ufw's LAN-only rules never applied
to K3s) — see [docs/argocd.md](docs/argocd.md).
Pi host setup codified in Ansible — six roles, three real bugs found and
fixed by actually running it, verified idempotent (a second real run
reports zero changes) — see [docs/ansible.md](docs/ansible.md).
GitHub repo settings managed via Terraform (import, not create) — see
[docs/terraform.md](docs/terraform.md).
Secrets encrypted at rest with SOPS + age, out of Argo CD's sync path —
a real incident along the way (a rotation-ordering mistake that cascaded
into an unrelated flannel VXLAN bug on the WSL2 worker node) — see
[docs/secrets.md](docs/secrets.md) and
[docs/kubernetes.md](docs/kubernetes.md).
Load tested with k6 — 0% errors at ~480 req/s sustained over a 5.5-minute
soak, rate limiter verified to trigger at exactly the right request, no
memory growth under sustained load — see
[docs/load-testing.md](docs/load-testing.md).
Failure tested — zero-downtime pod kills, Postgres data confirmed to
survive a pod restart, Argo CD self-heals live drift in ~11s (much
faster than its ~3min git-polling interval), and a real gap found in
the Ollama retry/timeout logic (a dead backend can hang a request for
minutes instead of failing fast) — see
[docs/failure-testing.md](docs/failure-testing.md).

## Target architecture

```
Home Network
      |
Private DNS
      |
Reverse Proxy
      |
Kubernetes / K3s
      |
+-----+--------+----------+
|              |          |
FastAPI     AI/RAG     Monitoring
|              |          |
Redis       Ollama     Prometheus
|              |          |
PostgreSQL   RTX GPU     Grafana
|
pgvector
```

## Hardware

| Node | Role |
|---|---|
| Raspberry Pi 5 (NVMe) | Lightweight infra: Linux server, DNS, monitoring, Docker, eventually K3s |
| Desktop (i7-14700K, RTX 3070 Ti 8GB, 32GB RAM) | FastAPI, PostgreSQL, Redis, Ollama/GPU inference, Docker/Kubernetes workloads |

## Repo structure

```
homelab/
├── apps/
│   ├── api/        # FastAPI backend service
│   └── ai/         # RAG / inference gateway
├── docker/         # Compose files
├── kubernetes/      # K3s manifests (ai/backend/data/monitoring namespaces)
├── ansible/         # Server configuration automation
├── terraform/        # Infra-as-code where applicable
├── monitoring/       # Prometheus/Grafana/Loki config
├── scripts/          # Helper scripts
├── diagrams/          # Architecture diagrams
└── docs/              # Setup guides, decisions, benchmarks, incident reports
```

## Roadmap

1. Linux/server setup
2. Networking
3. Docker
4. Private DNS
5. FastAPI
6. PostgreSQL + Redis
7. Ollama/GPU inference
8. RAG
9. Prometheus + Grafana
10. Kubernetes/K3s
11. CI/CD
12. Argo CD
13. Ansible
14. Terraform
15. Security
16. Load testing
17. Failure testing
18. Documentation
