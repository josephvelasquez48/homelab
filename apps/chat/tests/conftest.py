import os

os.environ.setdefault("API_KEY", "test-api-key")

import json

import pytest
from fastapi.testclient import TestClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    async def aread(self):
        return json.dumps(self._payload).encode()


class FakeStreamCtx:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        if self.client.raise_on_stream is not None:
            raise self.client.raise_on_stream
        return FakeUpstream(self.client.stream_status, self.client.stream_chunks)

    async def __aexit__(self, *args):
        return False


class FakeUpstream:
    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = chunks

    async def aread(self):
        return b'{"detail":"nope"}'

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk


class FakeApiClient:
    """Stands in for the api service, not for Ollama.

    The chat app never talks to a model; it only relays. So the useful
    fakes here are api-shaped: status codes, JSON bodies, and raw byte
    chunks on the streaming path.
    """

    def __init__(self):
        self.calls = []
        self.raise_on_stream = None
        self.stream_status = 200
        self.stream_chunks = [b'data: {"token":"Hi"}\n\n', b'data: {"done":true}\n\n']
        self.next_response = FakeResponse()

    async def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return self.next_response

    def stream(self, method, path, json=None):
        self.calls.append((method, path, json))
        return FakeStreamCtx(self)

    async def aclose(self):
        pass


@pytest.fixture
def client(monkeypatch):
    from app import main as app_main

    fake = FakeApiClient()
    monkeypatch.setattr(app_main.httpx, "AsyncClient", lambda **kwargs: fake)

    with TestClient(app_main.app) as c:
        c.fake_api = fake
        yield c
