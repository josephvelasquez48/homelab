# Monitoring (Prometheus + Grafana)

> **Retired** - superseded by `kubernetes/monitoring/`. Kept as-is for
> reference; see [../README.md](../README.md) for status of everything
> under `docker/`. The rest of this file describes how it worked when it
> was the live deployment.

Runs on the Raspberry Pi, `network_mode: host` (same pattern as `docker/dns`)
so Prometheus/Grafana/node_exporter can reach each other over `localhost`
without bridge-network plumbing, and node_exporter can read the host's own
`/proc`/`/sys` instead of the container's.

- **Prometheus** (`:9090`) scrapes itself, `node-exporter` on `localhost:9100`
  (Pi hardware: CPU/memory/disk/network), and the FastAPI `/metrics` on the
  desktop (`192.168.1.131:8000`).
- **node_exporter** (`:9100`) - Pi-only for now; nothing polls Windows host
  metrics yet (that needs `windows_exporter` running natively on the
  desktop, not containerized - not done yet).
- **Grafana** (`:3000`) - datasource and one dashboard
  (`Homelab Overview`: target health, Pi CPU/memory/disk, FastAPI request
  rate and p95 latency) are provisioned automatically from files in this
  repo, not clicked together by hand, so a fresh deploy comes up
  pre-configured. Admin password lives in `docker/monitoring/.env`
  (gitignored).

## Deploy (Pi)

```bash
cd ~/apps/homelab/docker/monitoring
docker compose up -d
```

Firewall: `ufw allow from 192.168.1.0/24 to any port 3000` for Grafana.
Prometheus (`9090`) and node_exporter (`9100`) are intentionally **not**
opened to the LAN - only used locally (Grafana -> Prometheus -> node's own
localhost:9100), nothing outside the Pi needs to reach them directly.

The desktop side needs Windows Firewall to allow inbound on `8000` from the
Pi, or Prometheus's scrape of `homelab-api` will show as `down`.

## Verify

```bash
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'   # all "up"
curl -s -u admin:$GRAFANA_ADMIN_PASSWORD localhost:3000/api/health     # Grafana healthy
```

Then `http://grafana.home:3000` (once DNS is pointed at the Pi on your
client) - login `admin` / the password in `.env` - "Homelab Overview"
dashboard should already be there with live data, not empty panels.
