from unittest.mock import AsyncMock, MagicMock

import pytest

from app import display

VALUES = dict.fromkeys(display.VALUE_QUERIES, None)
RATES = dict.fromkeys(display.RATE_QUERIES, None)

POD = {"namespace": "backend", "name": "api-1", "ready": True, "phase": "Running", "restarts": 0, "reason": None}


def values(**kw):
    return {**VALUES, **kw}


def test_dns_query_round_trip():
    q = display.dns_query("dashboard.home", qid=7)
    assert q[:2] == b"\x00\x07"
    assert b"\x09dashboard\x04home\x00" in q
    # A reply: same id, QR set, NOERROR, one answer.
    reply = b"\x00\x07" + b"\x81\x80" + b"\x00\x01\x00\x01" + b"\x00\x00\x00\x00"
    assert display.dns_answered(reply, qid=7)
    assert not display.dns_answered(reply, qid=8)
    nxdomain = b"\x00\x07\x81\x83\x00\x01\x00\x00\x00\x00\x00\x00"
    assert not display.dns_answered(nxdomain, qid=7)
    assert not display.dns_answered(b"\x00", qid=7)


def test_pod_status():
    pods = [POD, {**POD, "name": "api-2", "ready": False}]
    assert display.pod_status(pods, "backend", "api-") == "up"  # one ready is enough
    assert display.pod_status([{**POD, "ready": False}], "backend", "api-") == "down"
    assert display.pod_status(pods, "data", "postgres-") == "unknown"


def test_services_host_and_phone():
    v = values(adguard_up=1, adguard_protection=1, phone_age_s=4, phone_connected=1, phone_pc=0, inference=1,
               dns_today=100, blocked_today=20)
    s = display.services([], v, "up", "down")
    assert s["coredns"] == "up" and s["rustdesk"] == "down"
    assert s["adguard"] == "up" and s["internet"] == "up"
    assert s["phone"] == "up" and s["iphone"] == "up" and s["phone_pc"] == "down"
    assert s["traefik"] == "unknown"  # no pods seen is not healthy


def test_services_stale_or_missing_data():
    s = display.services([], values(phone_age_s=600), "down", "up")
    assert s["phone"] == "down"  # the phone service stopped writing its gauges
    s = display.services([], values(), "down", "up")
    assert s["phone"] == s["adguard"] == s["ollama"] == s["iphone"] == "unknown"
    # Filtering switched off: DNS still answers, but AdGuard isn't doing its job.
    assert display.services([], values(adguard_up=1, adguard_protection=0), "up", "up")["adguard"] == "down"


def test_edge_rates():
    r = {**RATES, "dns": 0.5, "web": 2, "web_api": 1.5, "web_apps": 0.5, "api": 1, "ollama": None}
    rates = display.edge_rates(r, values(phone_audio=0))
    assert rates["lan-coredns"] == rates["adguard-internet"] == 0.5
    assert rates["api-ollama"] == 0.0
    assert rates["iphone-phone"] == 0.0  # no call audio, no dots
    assert display.edge_rates(r, values(phone_audio=1))["phone-phone_pc"] > 0


def test_events_lead_with_problems():
    v = values(backup_age_h=250, backup_exit=0, dns_today=1000, blocked_today=200, phone_connected=1, phone_call=0,
               api_p95_s=0.04, targets=6, targets_up=5)
    ev = display.events(
        v,
        argo=[{"name": "backend", "sync_status": "Synced", "health_status": "Degraded"}],
        alerts=[{"name": "HomelabBackupStale", "summary": "No successful backup in 250 hours"}],
        nodes=[{"name": "m1-node", "ready": False}],
        top_blocked="ads.example",
    )
    texts = [e["text"] for e in ev]
    assert ev[0] == {"level": "bad", "text": "Alert: No successful backup in 250 hours"}
    assert "Node m1-node is not ready" in texts
    assert any("backend not synced" in t for t in texts)
    assert {"level": "bad", "text": "Last backup 250 h ago - check it"} in ev
    assert "AdGuard blocked 200 of 1,000 queries today (20%)" in texts
    assert "Most blocked: ads.example" in texts
    assert "API p95 40 ms over the last hour" in texts
    assert {"level": "warn", "text": "Prometheus: 5 of 6 targets up"} in ev


@pytest.mark.asyncio
async def test_list_pods_skips_finished_jobs_and_reports_why():
    items = [
        {"metadata": {"name": "api-1", "namespace": "backend"}, "spec": {"nodeName": "joe"},
         "status": {"phase": "Running", "containerStatuses": [
             {"ready": False, "restartCount": 4, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}},
        {"metadata": {"name": "migrate", "namespace": "backend"}, "spec": {}, "status": {"phase": "Succeeded"}},
        {"metadata": {"name": "api-2", "namespace": "backend"}, "spec": {}, "status": {"phase": "Pending"}},
    ]
    k8s = MagicMock()
    k8s.get = AsyncMock(return_value=MagicMock(raise_for_status=lambda: None, json=lambda: {"items": items}))
    pods = await display.list_pods(k8s)
    assert [p["name"] for p in pods] == ["api-1", "api-2"]
    assert pods[0] == {"name": "api-1", "namespace": "backend", "node": "joe", "phase": "Running",
                       "ready": False, "restarts": 4, "reason": "CrashLoopBackOff"}
    assert pods[1]["ready"] is False and pods[1]["node"] is None


def test_display_endpoint(client, monkeypatch):
    monkeypatch.setattr(display, "list_pods", AsyncMock(return_value=[POD]))
    monkeypatch.setattr(display, "probe_dns", AsyncMock(return_value="up"))
    monkeypatch.setattr(display, "probe_tcp", AsyncMock(return_value="up"))
    res = client.get("/api/display")
    assert res.status_code == 200
    data = res.json()
    assert data["services"]["api"] == "up" and data["services"]["coredns"] == "up"
    assert data["stats"]["pods_ready"] == data["stats"]["pods_total"] == 1
    assert data["weather"] is None  # no location configured
    assert set(data["rates"]) == {"-".join(e) for e in [
        ("lan", "coredns"), ("coredns", "adguard"), ("adguard", "internet"), ("lan", "traefik"),
        ("traefik", "api"), ("traefik", "apps"), ("api", "postgres"), ("api", "redis"), ("api", "ollama"),
        ("iphone", "phone"), ("phone", "phone_pc")]}


def test_display_page(client):
    res = client.get("/display")
    assert res.status_code == 200
    assert '<script src="/static/display.js">' in res.text
