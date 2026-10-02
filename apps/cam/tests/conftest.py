import os

# Plain HTTP in tests, so the session cookie has to be allowed without TLS.
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.fakes import FakeMediaMTX, FakeStore

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
    yield main.app
    del main.app.state.store
    del main.app.state.mediamtx


@pytest.fixture
def client(app):
    with Client(app) as c:
        yield c


@pytest.fixture
def make_user(app, client):
    """Create an account through a real invite, signed in on `client` unless told otherwise."""
    async def _create(username, is_admin):
        invite, token = await app.state.store.create_invite(username, is_admin, None, 7)
        return token

    def make(username="alice", password="correct horse battery", is_admin=False, sign_in=True):
        import asyncio
        token = asyncio.run(_create(username, is_admin))
        r = client.post(f"/api/invite/{token}", json={"password": password})
        assert r.status_code == 200, r.text
        if not sign_in:
            client.cookies.clear()
        return r.json()["user"]

    return make
