from unittest.mock import AsyncMock

from tests.conftest import FAKE_ARGO_APPS, FAKE_DESKTOP_METRICS, FAKE_NODES, FAKE_PI_METRICS, FAKE_PODS


def test_status_shape(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["nodes"] == FAKE_NODES
    assert data["argo_apps"] == FAKE_ARGO_APPS
    assert len(data["pods"]) == len(FAKE_PODS) * 5  # one WATCHED_NAMESPACES entry per namespace
    assert data["api_health"] == {"reachable": True, "status_code": 200, "body": {"status": "ok"}}
    assert data["pi_metrics"] == FAKE_PI_METRICS
    assert data["desktop_metrics"] == FAKE_DESKTOP_METRICS


def test_status_degrades_gracefully_when_prometheus_unreachable(client, monkeypatch):
    from app import prometheus

    monkeypatch.setattr(
        prometheus, "get_pi_metrics", AsyncMock(side_effect=Exception("prometheus unreachable"))
    )
    monkeypatch.setattr(
        prometheus, "get_desktop_metrics", AsyncMock(side_effect=Exception("prometheus unreachable"))
    )

    res = client.get("/api/status")
    assert res.status_code == 200
    assert res.json()["pi_metrics"] == dict.fromkeys(prometheus.QUERIES)
    assert res.json()["desktop_metrics"] == dict.fromkeys(prometheus.DESKTOP_QUERIES)


def test_gaming_mode_inactive_when_desktop_schedulable(client):
    res = client.get("/api/status")
    data = res.json()
    assert data["gaming_mode_active"] is False


def test_gaming_mode_active_when_desktop_cordoned(client, monkeypatch):
    from app import k8s
    from unittest.mock import AsyncMock

    cordoned_nodes = [
        {"name": "joe", "ready": True, "schedulable": True, "roles": ["control-plane"]},
        {"name": "desktop-j1grrmu", "ready": False, "schedulable": False, "roles": ["worker"]},
    ]
    monkeypatch.setattr(k8s, "get_nodes", AsyncMock(return_value=cordoned_nodes))

    res = client.get("/api/status")
    assert res.json()["gaming_mode_active"] is True
