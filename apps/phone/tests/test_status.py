import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.status as status

DASHBOARD = {
    "services": {"api": "up", "postgres": "down", "ollama": "unknown"},
    "stats": {"pi_temp_c": 58.4, "pi_cpu": 0.17, "pods_ready": 24, "pods_total": 24, "api_rps": 0.0},
    "alerts": [{"name": "PhoneBridgeDown", "summary": "Phone bridge down"}],
    "events": [{"level": "ok", "text": "Last backup 14 h ago"}],
    "nodes": [{"name": "joe", "ready": True}],
    "pods": [{"name": "lots of these"}],
    "rates": {"lan-coredns": 1.0},
}


def test_summary_keeps_what_a_phone_needs():
    s = status.summarize(DASHBOARD)
    assert s["down"] == ["postgres"] and s["unknown"] == ["ollama"]
    assert s["alerts"][0]["summary"] == "Phone bridge down"
    assert s["stats"]["pi_temp_c"] == 58.4 and "api_rps" not in s["stats"]
    assert "pods" not in s and "rates" not in s  # the map's data stays behind


@pytest.mark.asyncio
async def test_dashboard_is_cached_and_a_failure_keeps_the_last_good(monkeypatch):
    st = status.Status()
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("no route")
        return status.summarize(DASHBOARD)

    st._fetch = fetch
    first = await st.homelab()
    again = await st.homelab()
    assert len(calls) == 1 and first["down"] == again["down"] == ["postgres"]  # cached
    st._fetched -= status.CACHE_SECONDS
    stale = await st.homelab()
    assert stale["down"] == ["postgres"] and "no route" in stale["error"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "correct horse")

    async def no_hub():
        pass

    async def homelab():
        return {**status.summarize(DASHBOARD), "error": None, "age": 0}

    async def display_on():
        return True

    monkeypatch.setattr(main.hub, "run", no_hub)
    monkeypatch.setattr(main.homelab_status, "homelab", homelab)
    monkeypatch.setattr(status, "display_on", display_on)
    with TestClient(main.app, base_url="https://phone.home:8443") as c:
        yield c


def test_status_needs_a_login_and_adds_the_pis_own(client):
    assert client.get("/api/status").status_code == 401
    client.post("/login", data={"password": "correct horse"})
    st = client.get("/api/status").json()
    assert st["down"] == ["postgres"] and st["display"] is True and "bluetooth" in st and "iphone" in st


def test_display_control(client, monkeypatch):
    calls = []

    async def set_display(on):
        calls.append(on)
        return None if on else "nope"

    monkeypatch.setattr(status, "set_display", set_display)
    assert client.post("/api/display", data={"on": "true"}).status_code == 401
    client.post("/login", data={"password": "correct horse"})
    assert client.post("/api/display", data={"on": "true"}).json() == {"display": True}
    r = client.post("/api/display", data={"on": "false"})
    assert r.status_code == 409 and r.json()["detail"] == "nope" and calls == [True, False]


@pytest.mark.asyncio
async def test_opening_the_display_needs_the_desktop(monkeypatch, tmp_path):
    ran = []

    async def systemctl(*args):
        ran.append(args)
        return 0, ""

    monkeypatch.setattr(status, "_systemctl", systemctl)
    monkeypatch.setattr(status, "X11_SOCKET", tmp_path / "X0")  # no desktop
    assert "desktop isn't running" in await status.set_display(True)
    assert ran == []
    (tmp_path / "X0").touch()
    assert await status.set_display(True) is None
    assert ran[0][0] == "set-environment" and ran[1] == ("restart", "pi-display.service")
    assert await status.set_display(False) is None and ran[2] == ("stop", "pi-display.service")
