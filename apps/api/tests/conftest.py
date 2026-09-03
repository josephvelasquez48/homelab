import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost/0")
os.environ.setdefault("API_KEY", "test-api-key")

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def ping(self):
        return True

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        pass

    async def rpush(self, key, value):
        self.store.setdefault(key, []).append(value)

    async def aclose(self):
        pass


class FakeConnection:
    def __init__(self, db):
        self.db = db  # {"jobs": {id: {...}}, "documents": [{...}]}

    async def fetchval(self, query, *args):
        return 1

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if "INSERT INTO jobs" in q:
            job_id, jtype, payload = args
            self.db.setdefault("jobs", {})[job_id] = {
                "id": job_id,
                "type": jtype,
                "status": "pending",
                "payload": payload,
                "result": None,
                "error": None,
            }
        elif "INSERT INTO documents" in q:
            doc_id, content, embedding, metadata = args
            self.db.setdefault("documents", []).append(
                {"id": doc_id, "content": content, "embedding": embedding, "metadata": metadata}
            )

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM jobs WHERE id" in q:
            return self.db.get("jobs", {}).get(args[0])
        return None

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "FROM documents" in q:
            docs = self.db.get("documents", [])
            top_k = args[-1] if args else len(docs)
            return [
                {"id": d["id"], "content": d["content"], "distance": 0.1 * i}
                for i, d in enumerate(docs[:top_k])
            ]
        return []


class FakeAcquireCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return FakeConnection(self.db)

    async def __aexit__(self, *args):
        return False


class FakePgPool:
    def __init__(self):
        self.db = {}

    def acquire(self):
        return FakeAcquireCtx(self.db)

    async def close(self):
        pass


class FakeOllamaResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeOllamaClient:
    def __init__(self):
        self.requests = []

    async def post(self, url, json=None, **kwargs):
        self.requests.append((url, json))
        if url == "/api/generate":
            return FakeOllamaResponse(
                {"response": "fake response", "eval_count": 10, "eval_duration": 100_000_000}
            )
        if url == "/api/embed":
            n = len(json["input"])
            return FakeOllamaResponse({"embeddings": [[0.1, 0.2, 0.3]] * n})
        raise ValueError(f"unexpected Ollama URL in test: {url}")

    async def aclose(self):
        pass


@pytest.fixture
def client(monkeypatch):
    fake_pg = FakePgPool()
    fake_redis = FakeRedis()
    fake_ollama = FakeOllamaClient()

    # app.main does `from app.db import create_pg_pool, create_redis_client`,
    # which binds its own local names at import time - patching app.db's
    # attributes only affects the *first* test to trigger that import.
    # Patch the names as seen from inside app.main instead, so every test
    # gets its own fakes rather than leaking the first test's state.
    from app import main as app_main

    monkeypatch.setattr(app_main, "create_pg_pool", AsyncMock(return_value=fake_pg))
    monkeypatch.setattr(app_main, "create_redis_client", MagicMock(return_value=fake_redis))
    monkeypatch.setattr(app_main.httpx, "AsyncClient", MagicMock(return_value=fake_ollama))

    app = app_main.app

    with TestClient(app) as c:
        c.fake_pg = fake_pg
        c.fake_redis = fake_redis
        c.fake_ollama = fake_ollama
        yield c


@pytest.fixture
def auth_headers():
    return {"X-API-Key": "test-api-key"}
