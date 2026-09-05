"""Minimal Prometheus HTTP API client for live hardware metrics.

Queries the same in-cluster Prometheus the Grafana "Homelab Overview"
dashboard uses (kubernetes/monitoring/grafana.yaml), read as instant
values here instead of graphed over time.

The Pi's node_exporter runs directly on its host OS (job "node-pi"), so
its metrics are selected by that job label. The desktop's node_exporter
runs as a regular pod instead (kubernetes/monitoring/node-exporter-
desktop.yaml), auto-discovered by the existing kubernetes-pods scrape
job, so its metrics are selected by pod name. No hwmon/temp query for
the desktop - WSL2 exposes no hardware temperature sensors (checked via
/sys/class/hwmon, empty), so there's nothing for node_exporter to read.
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

DESKTOP_SELECTOR = 'pod=~"node-exporter-desktop.*"'
DESKTOP_QUERIES = {
    "load1": f'node_load1{{{DESKTOP_SELECTOR}}}',
    "load5": f'node_load5{{{DESKTOP_SELECTOR}}}',
    "load15": f'node_load15{{{DESKTOP_SELECTOR}}}',
    "net_rx_bytes_per_sec": f'sum(rate(node_network_receive_bytes_total{{{DESKTOP_SELECTOR},device=~"eth[0-9]+"}}[5m]))',
    "net_tx_bytes_per_sec": f'sum(rate(node_network_transmit_bytes_total{{{DESKTOP_SELECTOR},device=~"eth[0-9]+"}}[5m]))',
    "disk_read_bytes_per_sec": f'sum(rate(node_disk_read_bytes_total{{{DESKTOP_SELECTOR}}}[5m]))',
    "disk_write_bytes_per_sec": f'sum(rate(node_disk_written_bytes_total{{{DESKTOP_SELECTOR}}}[5m]))',
    "oom_kills": f'node_vmstat_oom_kill{{{DESKTOP_SELECTOR}}}',
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


async def get_desktop_metrics(client: httpx.AsyncClient) -> dict[str, float | None]:
    return await _gather_metrics(client, DESKTOP_QUERIES)
