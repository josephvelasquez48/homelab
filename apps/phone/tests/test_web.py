import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "correct horse")

    async def no_hub():
        pass

    # Keep the lifespan from reaching for a real session bus.
    monkeypatch.setattr(main.hub, "run", no_hub)
    with TestClient(main.app, base_url="https://phone.home:8443") as c:
        yield c


def test_logged_out_gets_login_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/login"' in r.text


def test_wrong_password(client):
    r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=1"
    assert "phone_session" not in r.cookies


def test_no_configured_password_means_no_login(client, monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "")
    r = client.post("/login", data={"password": ""}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=1"


def test_login_sets_strict_secure_cookie(client):
    r = client.post("/login", data={"password": "correct horse"}, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert 'id="call-card"' in client.get("/").text


def test_websocket_requires_login(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers={"origin": "https://phone.home:8443"}) as ws:
            ws.receive_text()
    assert e.value.code == 4401


def test_websocket_rejects_foreign_origin(client):
    client.post("/login", data={"password": "correct horse"})
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    assert e.value.code == 4401


def test_static_allowlist(client):
    assert client.get("/static/app.js").status_code == 200
    r = client.get("/static/../main.py", follow_redirects=False)
    assert r.status_code in (303, 404)
