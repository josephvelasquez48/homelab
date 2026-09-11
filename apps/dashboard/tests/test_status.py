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


def test_status_reports_pods_per_node(client):
    """A Ready node running nothing is invisible without this.

    m1-node sat Ready, untainted and empty for a day after joining, because
    nothing reschedules onto a new node on its own. The page said "Ready"
    and looked fine.
    """
    data = client.get("/api/status").json()
    per_node = data["pods_per_node"]

    assert set(per_node) == {n["name"] for n in FAKE_NODES}
    assert sum(per_node.values()) == sum(
        1 for p in data["pods"] if p.get("node") in per_node
    )


def test_backup_and_alerts_are_present(client):
    data = client.get("/api/status").json()
    assert "backup" in data
    assert "alerts" in data


def test_unreachable_alertmanager_is_not_an_empty_list(client, monkeypatch):
    """"Nothing is firing" and "I cannot tell" must not look the same.

    The page colours an empty list green. If a failed fetch returned [],
    an unreachable Alertmanager would render as all-clear - the worst
    possible failure for an alerting display.
    """
    from app import main

    monkeypatch.setattr(main, "_active_alerts", AsyncMock(return_value=None))

    data = client.get("/api/status").json()
    assert data["alerts"] is None


def test_backup_metrics_degrade_to_none_not_zero(client, monkeypatch):
    """A missing backup metric must not read as a fresh backup.

    Zero would render as "0.0 h ago", i.e. a backup seconds old, which is
    precisely backwards when the truth is that nothing is reporting.
    """
    from app import prometheus

    monkeypatch.setattr(
        prometheus, "get_backup_health", AsyncMock(side_effect=Exception("prometheus down"))
    )

    data = client.get("/api/status").json()
    assert data["backup"] == dict.fromkeys(prometheus.BACKUP_QUERIES)
    assert data["backup"]["backup_age_hours"] is None
