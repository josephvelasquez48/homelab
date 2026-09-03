# Monitoring (Prometheus + Grafana)

Roadmap step 9. See [docker/monitoring/README.md](../docker/monitoring/README.md)
for the deploy/verify commands - this doc is the decision/debugging log.

## Log

- 2026-09-03: Deployed Prometheus + Grafana + node_exporter on the Pi
  (`network_mode: host`, same pattern as `docker/dns`). Prometheus scrapes
  itself, node_exporter (Pi hardware), and the FastAPI `/metrics` on the
  desktop across the network. Grafana's datasource and a 6-panel
  "Homelab Overview" dashboard are provisioned from files in the repo
  (`docker/monitoring/grafana/provisioning/`), not clicked together by
  hand in the UI - a fresh deploy comes up pre-configured with the same
  dashboard, not empty.

  Renamed the internal DNS domain from `.joseph` to `.home` in the same
  pass (`api.home`, `ai.home`, `grafana.home`) - unrelated to monitoring
  itself, just landed at the same time.

  **Verified real data, not just that nothing errored:**
  - All three Prometheus targets (`prometheus`, `node-pi`, `homelab-api`)
    reached `up` - including the cross-machine scrape from the Pi to the
    desktop, which could plausibly have been blocked by Windows Firewall
    but wasn't.
  - Grafana's datasource and dashboard are actually provisioned
    (`/api/datasources`, `/api/search?query=Homelab` both return them),
    not just that the container started.
  - Ran every panel's exact PromQL query directly against Prometheus:
    CPU/memory/disk return real, sane numbers (e.g. disk usage 6.27% on
    a 469GB NVMe with ~30GB used, which matches). Request-rate and p95
    latency initially returned `0`/`NaN` - not a bug, just no traffic in
    the last 5 minutes at query time - then generated real requests
    (`/health`, `/v1/chat`) and confirmed the new handler labels appeared
    and the panels populate once there's enough scrape history for
    `rate()` to compute (a counter needs 2+ scrapes in-window; brand-new
    series read `0` until then).

## Known gaps (not done yet)

- **No Windows host metrics.** node_exporter only runs on the Pi.
  Desktop CPU/RAM/disk would need `windows_exporter` running natively on
  Windows (not containerized - Docker Desktop on Windows can't cleanly
  expose host-level Windows metrics from inside a Linux container).
- **No GPU metrics.** The RTX 3070 Ti isn't monitored yet - would need
  something like `nvidia_gpu_exporter` or DCGM, running natively
  alongside Ollama.
- **No Postgres/Redis exporters.** `postgres_exporter`/`redis_exporter`
  would surface connection counts, query latency, cache hit rates, etc.
- **No alerting.** Prometheus Alertmanager isn't deployed; the dashboard
  is look-at-it monitoring, not paged-when-something-breaks monitoring.
