from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


FAKE_NODES = [
    {"name": "joe", "ready": True, "schedulable": True, "roles": ["control-plane"]},
    {"name": "m1-node", "ready": True, "schedulable": True, "roles": ["worker"]},
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
FAKE_DESKTOP_METRICS = {
    "load1": 0.05,
    "load5": 0.04,
    "load15": 0.03,
    "net_rx_bytes_per_sec": 2048.0,
    "net_tx_bytes_per_sec": 1024.0,
    "disk_read_bytes_per_sec": 0.0,
    "disk_write_bytes_per_sec": 512.0,
    "oom_kills": 0.0,
}
FAKE_CROSS_NODE_STATUS = "up"
FAKE_GPU_MODELS = [{"name": "qwen2.5-coder:7b", "size_vram": 4748056984}]


TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def client(monkeypatch):
    from app import auth, gpu, k8s, main, prometheus

    # config values are read at import time, so patch the already-bound
    # module attribute rather than the environment.
    monkeypatch.setattr(auth, "DASHBOARD_PASSWORD", TEST_PASSWORD)
    monkeypatch.setattr(main, "DASHBOARD_PASSWORD", TEST_PASSWORD)

    monkeypatch.setattr(k8s, "make_client", lambda: MagicMock(aclose=AsyncMock()))
    monkeypatch.setattr(k8s, "get_nodes", AsyncMock(return_value=FAKE_NODES))
    monkeypatch.setattr(gpu, "loaded_models", AsyncMock(return_value=FAKE_GPU_MODELS))
    monkeypatch.setattr(k8s, "get_pods", AsyncMock(return_value=FAKE_PODS))
    monkeypatch.setattr(k8s, "get_argo_applications", AsyncMock(return_value=FAKE_ARGO_APPS))
    monkeypatch.setattr(prometheus, "get_pi_metrics", AsyncMock(return_value=FAKE_PI_METRICS))
    monkeypatch.setattr(prometheus, "get_desktop_metrics", AsyncMock(return_value=FAKE_DESKTOP_METRICS))
    monkeypatch.setattr(prometheus, "get_cross_node_status", AsyncMock(return_value=FAKE_CROSS_NODE_STATUS))

    fake_http = MagicMock()
    fake_http.get = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"status": "ok"})
    )
    fake_http.aclose = AsyncMock()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: fake_http)

    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def password():
    return TEST_PASSWORD


@pytest.fixture
def authed_client(client):
    """A client that has completed a real login, cookie and all."""
    res = client.post("/api/login", json={"password": TEST_PASSWORD})
    assert res.status_code == 200
    return client
