"""Minimal Prometheus HTTP API client for live hardware metrics.

Queries the same in-cluster Prometheus the Grafana "Homelab Overview"
dashboard uses (kubernetes/monitoring/grafana.yaml), read as instant
values here instead of graphed over time.

Pi hardware metrics come from the host exporter. Cross-node scrape health
uses worker pod CIDRs reported by Kubernetes, excluding host-network targets.
"""
import asyncio
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

import httpx

from app.config import PROMETHEUS_URL

QUERIES = {
    "cpu_temp_c": 'node_hwmon_temp_celsius{job="node-pi",chip="thermal_thermal_zone0",sensor="temp0"}',
    "load1": 'node_load1{job="node-pi"}',
    "load5": 'node_load5{job="node-pi"}',
    "load15": 'node_load15{job="node-pi"}',
    "net_rx_bytes_per_sec": 'sum(rate(node_network_receive_bytes_total{job="node-pi",device!="lo"}[5m]))',
    "net_tx_bytes_per_sec": 'sum(rate(node_network_transmit_bytes_total{job="node-pi",device!="lo"}[5m]))',
    "disk_read_bytes_per_sec": 'sum(rate(node_disk_read_bytes_total{job="node-pi"}[5m]))',
    "disk_write_bytes_per_sec": 'sum(rate(node_disk_written_bytes_total{job="node-pi"}[5m]))',
    "oom_kills": 'node_vmstat_oom_kill{job="node-pi"}',
}

# Backup health and inference reachability. Both are published by things
# outside the cluster - the Mac writes the backup gauges into the Pi's
# node_exporter textfile collector, and the api sets the inference gauge
# on every readiness probe - so Prometheus is the only place they meet.
#
# Ages are computed here rather than in the page, so a stale value is
# obvious as a number instead of a timestamp someone has to subtract.
BACKUP_QUERIES = {
    "backup_age_hours": "(time() - homelab_backup_last_snapshot_timestamp_seconds) / 3600",
    "backup_last_exit_code": "homelab_backup_last_exit_code",
    "backup_repository_readable": "homelab_backup_repository_readable",
    "backup_snapshot_count": "homelab_backup_snapshot_count",
    "backup_report_age_hours": "(time() - homelab_backup_report_timestamp_seconds) / 3600",
    # min() across replicas: if any replica cannot reach Ollama, say so,
    # rather than letting a healthy one mask it.
    "inference_reachable": "min(homelab_inference_reachable)",
}

async def _query_one(client: httpx.AsyncClient, expr: str) -> float | None:
    try:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr})
        r.raise_for_status()
        result = r.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else None
    except Exception:
        return None


async def _gather_metrics(client: httpx.AsyncClient, queries: dict[str, str]) -> dict[str, float | None]:
    values = await asyncio.gather(*(_query_one(client, expr) for expr in queries.values()))
    return dict(zip(queries.keys(), values))


async def get_pi_metrics(client: httpx.AsyncClient) -> dict[str, float | None]:
    return await _gather_metrics(client, QUERIES)


async def get_cross_node_status(client: httpx.AsyncClient, nodes: list[dict]) -> str | None:
    """Scrape health for worker pod networks; not a bidirectional network test."""
    try:
        networks = [
            ip_network(cidr)
            for node in nodes
            if "control-plane" not in node["roles"]
            for cidr in node.get("pod_cidrs", [])
        ]
        if not networks:
            return None
        r = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'up{job="kubernetes-pods"}'},
        )
        r.raise_for_status()
        values = []
        for item in r.json()["data"]["result"]:
            host = urlsplit("//" + item["metric"].get("instance", "")).hostname
            try:
                address = ip_address(host)
            except ValueError:
                continue
            if any(address in network for network in networks):
                values.append(float(item["value"][1]))
        if not values:
            return None
        return "down" if any(value == 0 for value in values) else "up"
    except Exception:
        return None


async def get_backup_health(client: httpx.AsyncClient) -> dict[str, float | None]:
    """Backup and inference gauges.

    Every value can legitimately be None, and None means something
    different from zero here: "Prometheus has no series for this" rather
    than "the value is 0". A backup age of None is the dangerous case -
    nothing is reporting - so the page must not render it as healthy.
    """
    return await _gather_metrics(client, BACKUP_QUERIES)
