from unittest.mock import AsyncMock

from tests.conftest import (
    FAKE_ARGO_APPS,
    FAKE_CROSS_NODE_STATUS,
    FAKE_NODES,
    FAKE_PI_METRICS,
    FAKE_PODS,
)


def test_status_shape(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["nodes"] == FAKE_NODES
    assert data["argo_apps"] == FAKE_ARGO_APPS
    assert len(data["pods"]) == len(FAKE_PODS) * 5  # one WATCHED_NAMESPACES entry per namespace
    assert data["api_health"] == {"reachable": True, "status_code": 200, "body": {"status": "ok"}}
    assert data["pi_metrics"] == FAKE_PI_METRICS
    assert "desktop_metrics" not in data
    assert data["cross_node_status"] == FAKE_CROSS_NODE_STATUS


def test_status_degrades_gracefully_when_prometheus_unreachable(client, monkeypatch):
    from app import prometheus

    monkeypatch.setattr(
        prometheus, "get_pi_metrics", AsyncMock(side_effect=Exception("prometheus unreachable"))
    )
    monkeypatch.setattr(
        prometheus, "get_cross_node_status", AsyncMock(side_effect=Exception("prometheus unreachable"))
    )

    res = client.get("/api/status")
    assert res.status_code == 200
    assert res.json()["pi_metrics"] == dict.fromkeys(prometheus.QUERIES)
    assert res.json()["cross_node_status"] is None


def test_status_reports_what_is_resident_on_the_gpu(client):
    """Replaces the old gaming_mode_active flag, which was derived from
    whether a node was cordoned. That node no longer exists, and GPU
    residency is the only contention left worth showing."""
    res = client.get("/api/status")
    assert res.status_code == 200
    assert res.json()["gpu_models"] == [
        {"name": "qwen2.5-coder:7b", "size_vram": 4748056984}
    ]


def test_status_survives_ollama_being_unreachable(client, monkeypatch):
    """The status page must still render when inference is down - that is
    precisely when someone is looking at it. null distinguishes "could not
    ask" from the empty list, which means "asked, GPU is idle"."""
    from unittest.mock import AsyncMock

    from app import gpu

    monkeypatch.setattr(gpu, "loaded_models", AsyncMock(side_effect=OSError("refused")))

    res = client.get("/api/status")
    assert res.status_code == 200
    assert res.json()["gpu_models"] is None


def test_login_cookie_requires_https(client, password):
    response = client.post("/api/login", json={"password": password})
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
