import os

# Plain HTTP in tests, so the session cookie has to be allowed without TLS.
os.environ.setdefault("COOKIE_SECURE", "false")
# Passkeys are bound to this; set before app.config is imported.
os.environ.setdefault("PUBLIC_URL", "https://cam.example.test")

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import config, main
from tests.fakes import FakeFocus, FakeMediaMTX, FakeStore, SoftAuthenticator

HEADERS = {"X-Requested-With": "cam"}


class Client(TestClient):
    """A TestClient that sends the same-origin header the pages always send."""

    def request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", None) or {}
        if kwargs.pop("same_origin", True):
            headers = {**HEADERS, **headers}
        return super().request(method, url, headers=headers, **kwargs)


@pytest.fixture
def app():
    main._failures.clear()
    main.app.state.store = FakeStore()
    main.app.state.mediamtx = FakeMediaMTX()
    main.app.state.focus = FakeFocus()
    yield main.app
    del main.app.state.store
    del main.app.state.mediamtx
    del main.app.state.focus


@pytest.fixture
def client(app):
    with Client(app) as c:
        yield c


def new_authenticator() -> SoftAuthenticator:
    return SoftAuthenticator(config.PUBLIC_URL, config.RP_ID)


def accept(client, token, authenticator):
    """Open an invite link and make a passkey with `authenticator`."""
    options = client.post(f"/api/invite/{token}/options").json()
    return client.post(f"/api/invite/{token}", json=authenticator.create(options))


def sign_in(client, authenticator):
    options = client.post("/api/login/options").json()
    return client.post("/api/login", json=authenticator.get(options))


@pytest.fixture
def make_user(app, client):
    """Create an account through a real invite and passkey; returns (user, authenticator).

    Signed in on `client` afterwards unless sign_in=False.
    """
    def make(username="alice", is_admin=False, sign_in=True):
        _, token = asyncio.run(app.state.store.create_invite(username, is_admin, None, 24))
        authenticator = new_authenticator()
        r = accept(client, token, authenticator)
        assert r.status_code == 200, r.text
        if not sign_in:
            client.cookies.clear()
        return r.json()["user"], authenticator

    return make
