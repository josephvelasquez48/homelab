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


@pytest.fixture
def client(monkeypatch):
    from app import k8s, main

    monkeypatch.setattr(k8s, "make_client", lambda: MagicMock(aclose=AsyncMock()))
    monkeypatch.setattr(k8s, "get_nodes", AsyncMock(return_value=FAKE_NODES))
    monkeypatch.setattr(k8s, "get_pods", AsyncMock(return_value=FAKE_PODS))
    monkeypatch.setattr(k8s, "get_argo_applications", AsyncMock(return_value=FAKE_ARGO_APPS))

    fake_http = MagicMock()
    fake_http.get = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"status": "ok"})
    )
    fake_http.aclose = AsyncMock()
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: fake_http)

    with TestClient(main.app) as c:
        yield c
