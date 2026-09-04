"""Minimal Prometheus HTTP API client for the Pi's live hardware metrics.

Queries the same in-cluster Prometheus the Grafana "Homelab Overview"
dashboard uses (kubernetes/monitoring/grafana.yaml) - same job label
(node-pi), same expressions, just read as instant values here instead of
graphed over time.
"""
import asyncio

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


async def _query_one(client: httpx.AsyncClient, expr: str) -> float | None:
    try:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr})
        r.raise_for_status()
        result = r.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else None
    except Exception:
        return None


async def get_pi_metrics(client: httpx.AsyncClient) -> dict[str, float | None]:
    values = await asyncio.gather(*(_query_one(client, expr) for expr in QUERIES.values()))
    return dict(zip(QUERIES.keys(), values))
