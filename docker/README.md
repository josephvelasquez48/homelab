# docker/

Mixed status - not everything here is still in use. Rather than delete the
retired pieces, they're kept as real evidence of how this project evolved
(see [docs/milestone-1.md](../docs/milestone-1.md)), with their status
made explicit here instead of left to guesswork.

| Directory / file | Status |
|---|---|
| `dns/` | **Live.** CoreDNS, whole-LAN `*.home` resolution + ad-blocking. Deliberately never migrated to Kubernetes - see [docs/kubernetes.md](../docs/kubernetes.md) for why. |
| `docker-compose.yml` | **Retired.** The original FastAPI/worker/Postgres/Redis stack from Milestone 1. Torn down once the equivalent workloads were verified working on K3s - see the "workload migration" log entry in [docs/kubernetes.md](../docs/kubernetes.md). Superseded by `kubernetes/backend/` and `kubernetes/data/`. |
| `monitoring/` | **Retired.** The original Prometheus + Grafana stack. Same story - torn down after the K3s equivalent (`kubernetes/monitoring/`) was verified. See [docs/monitoring.md](../docs/monitoring.md) for the original design and [docs/kubernetes.md](../docs/kubernetes.md) for the migration. |
