from unittest.mock import AsyncMock, MagicMock

import pytest

from app import display, main

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
                       "ready": False, "restarts": 4, "reason": "CrashLoopBackOff", "mem_limit": None}
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
    assert data["page_version"] == main.PAGE_VERSION  # a display on an older page reloads
    assert set(data["rates"]) == {"-".join(e) for e in [
        ("lan", "coredns"), ("coredns", "adguard"), ("adguard", "internet"), ("lan", "traefik"),
        ("traefik", "api"), ("traefik", "apps"), ("api", "postgres"), ("api", "redis"), ("api", "ollama"),
        ("iphone", "phone"), ("phone", "phone_pc")]}


def test_display_page(client):
    res = client.get("/display")
    assert res.status_code == 200
    assert '<script src="/static/tank.js">' in res.text  # the aquarium, before display.js uses it
    assert '<script src="/static/display.js">' in res.text
    assert res.text.index("/static/tank.js") < res.text.index("/static/display.js")
    assert 'id="to-desktop"' in res.text and 'id="desk-confirm" hidden' in res.text  # the Desktop button, confirm first


def test_pages_and_scripts_are_rechecked_on_every_load(client):
    # Without this the Pi's Chromium kept running old files after a deploy.
    for path in ("/display", "/static/display.js", "/static/tank.js"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_an_unchanged_script_comes_back_as_not_modified(client):
    # Rechecking is cheap: the browser sends back the ETag and gets a 304.
    etag = client.get("/static/tank.js").headers["etag"]
    res = client.get("/static/tank.js", headers={"If-None-Match": etag})
    assert res.status_code == 304
    assert res.headers["cache-control"] == "no-cache"


def test_tank_script_is_served(client):
    res = client.get("/static/tank.js")
    assert res.status_code == 200
    assert "window.Tank" in res.text


def test_quantities():
    assert display.memory_bytes("177664Ki") == 177664 * 1024
    assert display.memory_bytes("256Mi") == 256 * 2**20
    assert display.memory_bytes("1Gi") == 2**30
    assert display.memory_bytes("32G") == 32e9
    assert display.memory_bytes("1024") == 1024
    assert display.cpu_millicores("3284811n") == pytest.approx(3.284811)
    assert display.cpu_millicores("250m") == 250
    assert display.cpu_millicores("500u") == pytest.approx(0.5)
    assert display.cpu_millicores("2") == 2000


def test_memory_limit_needs_every_container_limited():
    spec = lambda *lims: {"containers": [{"resources": {"limits": {"memory": q}}} if q else {} for q in lims]}  # noqa: E731
    assert display.memory_limit(spec("1Gi", "1Gi")) == 2 * 2**30
    assert display.memory_limit(spec("256Mi", None)) is None  # one unlimited container: no ceiling to warn about
    assert display.memory_limit({"containers": []}) is None


@pytest.mark.asyncio
async def test_pod_usage_sums_containers_and_tolerates_no_metrics_server():
    items = [{"metadata": {"namespace": "kiwix", "name": "kiwix-1"},
              "containers": [{"usage": {"cpu": "5m", "memory": "100Mi"}}, {"usage": {"cpu": "1500000n", "memory": "20Mi"}}]}]
    k8s = MagicMock()
    k8s.get = AsyncMock(return_value=MagicMock(raise_for_status=lambda: None, json=lambda: {"items": items}))
    usage = await display.pod_usage(k8s)
    assert usage[("kiwix", "kiwix-1")]["cpu_m"] == pytest.approx(6.5)
    assert usage[("kiwix", "kiwix-1")]["mem_bytes"] == 120 * 2**20
    k8s.get = AsyncMock(side_effect=Exception("503: metrics API not available"))
    assert await display.pod_usage(k8s) == {}


def test_display_endpoint_attaches_usage(client, monkeypatch):
    monkeypatch.setattr(display, "list_pods", AsyncMock(return_value=[{**POD, "mem_limit": 256 * 2**20}]))
    monkeypatch.setattr(display, "pod_usage", AsyncMock(return_value={("backend", "api-1"): {"cpu_m": 12.0, "mem_bytes": 2**27}}))
    monkeypatch.setattr(display, "probe_dns", AsyncMock(return_value="up"))
    monkeypatch.setattr(display, "probe_tcp", AsyncMock(return_value="up"))
    pod = client.get("/api/display").json()["pods"][0]
    assert (pod["cpu_m"], pod["mem_bytes"], pod["mem_limit"]) == (12.0, 2**27, 256 * 2**20)


@pytest.mark.asyncio
async def test_weather_carries_what_the_aquarium_sky_needs(monkeypatch):
    monkeypatch.setattr(display, "WEATHER_LAT", "33.92")
    monkeypatch.setattr(display, "WEATHER_LON", "-117.49")
    monkeypatch.setitem(display._weather, "at", None)
    # Fetches on a machine booted moments ago, too (the empty cache isn't "fresh").
    monkeypatch.setattr(display.time, "monotonic", lambda: 5.0)
    body = {"current": {"temperature_2m": 71.8, "weather_code": 61, "is_day": 0, "cloud_cover": 88,
                        "precipitation": 1.2, "wind_speed_10m": 9.5},
            "daily": {"sunrise": [1790689389], "sunset": [1790732203]}}
    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock(raise_for_status=lambda: None, json=lambda: body))
    wx = await display.weather(http)
    assert wx == {"temp_f": 72, "code": 61, "is_day": False, "cloud_cover": 88, "precip_mm": 1.2,
                  "wind_mph": 9.5, "sunrise": 1790689389, "sunset": 1790732203}
    params = http.get.call_args.kwargs["params"]
    assert params["timeformat"] == "unixtime" and "sunrise" in params["daily"]
