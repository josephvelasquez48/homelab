from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


FAKE_NODES = [
    {"name": "joe", "ready": True, "schedulable": True, "roles": ["control-plane"]},
    {"name": "desktop-j1grrmu", "ready": True, "schedulable": True, "roles": ["worker"]},
]
FAKE_ARGO_APPS = [
    {"name": "backend", "sync_status": "Synced", "health_status": "Healthy"},
]
FAKE_PODS = [
    {"name": "api-abc123", "namespace": "backend", "phase": "Running", "ready": 1, "total": 1, "restarts": 0, "node": "joe"},
]
FAKE_PI_METRICS = {
    "cpu_temp_c": 54.5,
    "load1": 0.3,
    "load5": 0.25,
    "load15": 0.2,
    "net_rx_bytes_per_sec": 1024.0,
    "net_tx_bytes_per_sec": 512.0,
    "disk_read_bytes_per_sec": 0.0,
    "disk_write_bytes_per_sec": 2048.0,
    "oom_kills": 0.0,
}


@pytest.fixture
def client(monkeypatch):
    from app import k8s, main, prometheus

    monkeypatch.setattr(k8s, "make_client", lambda: MagicMock(aclose=AsyncMock()))
    monkeypatch.setattr(k8s, "get_nodes", AsyncMock(return_value=FAKE_NODES))
    monkeypatch.setattr(k8s, "get_pods", AsyncMock(return_value=FAKE_PODS))
    monkeypatch.setattr(k8s, "get_argo_applications", AsyncMock(return_value=FAKE_ARGO_APPS))
    monkeypatch.setattr(prometheus, "get_pi_metrics", AsyncMock(return_value=FAKE_PI_METRICS))

    fake_http = MagicMock()
    fake_http.get = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"status": "ok"})
    )
    fake_http.aclose = AsyncMock()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: fake_http)

    with TestClient(main.app) as c:
        yield c
