import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost/0")
os.environ.setdefault("API_KEY", "test-api-key")

from unittest.mock import AsyncMock, MagicMock

import datetime

import pytest
from fastapi.testclient import TestClient


_NOW = datetime.datetime(2026, 9, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.fail_ping = False

    async def ping(self):
        if self.fail_ping:
            raise ConnectionError("redis unreachable")
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


class FakeStreamCtx:
    """Stands in for httpx's streaming context manager for /api/chat."""

    def __init__(self, client, url, payload):
        self.client = client
        self.url = url
        self.payload = payload

    async def __aenter__(self):
        if self.client.fail_with is not None:
            raise self.client.fail_with
        if self.url != "/api/chat":
            raise ValueError("unexpected streaming URL in test: %s" % self.url)
        return FakeStreamResponse()

    async def __aexit__(self, *args):
        return False


class FakeStreamResponse:
    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        # Two content chunks then a done frame, matching Ollama's shape.
        yield '{"message":{"content":"Hello"},"done":false}'
        yield ""
        yield '{"message":{"content":" there"},"done":false}'
        yield ('{"message":{"content":""},"done":true,'
               '"eval_count":10,"eval_duration":100000000}')


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
        elif "INSERT INTO conversations" in q:
            cid, title, model = args
            self.db.setdefault("conversations", {})[cid] = {
                "id": cid, "title": title, "model": model,
                "created_at": _NOW, "updated_at": _NOW,
            }
        elif "INSERT INTO messages" in q:
            mid, cid, content = args
            role = "user" if "'user'" in q else "assistant"
            self.db.setdefault("messages", []).append(
                {"id": mid, "conversation_id": cid, "role": role,
                 "content": content, "created_at": _NOW}
            )
        elif "UPDATE conversations SET title" in q:
            cid, title = args
            self.db.get("conversations", {}).get(cid, {})["title"] = title
        elif "UPDATE conversations SET updated_at" in q:
            pass
        elif "DELETE FROM conversations" in q:
            existed = args[0] in self.db.get("conversations", {})
            self.db.get("conversations", {}).pop(args[0], None)
            self.db["messages"] = [
                m for m in self.db.get("messages", []) if m["conversation_id"] != args[0]
            ]
            return "DELETE 1" if existed else "DELETE 0"
        elif "INSERT INTO documents" in q:
            doc_id, content, embedding, metadata = args
            self.db.setdefault("documents", []).append(
                {"id": doc_id, "content": content, "embedding": embedding, "metadata": metadata}
            )

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM jobs WHERE id" in q:
            return self.db.get("jobs", {}).get(args[0])
        if "FROM conversations WHERE id" in q:
            return self.db.get("conversations", {}).get(args[0])
        if "INSERT INTO conversations" in q:
            cid, title, model = args
            row = {"id": cid, "title": title, "model": model,
                   "created_at": _NOW, "updated_at": _NOW}
            self.db.setdefault("conversations", {})[cid] = row
            return row
        return None

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "FROM conversations" in q:
            rows = list(self.db.get("conversations", {}).values())
            return sorted(rows, key=lambda r: r["updated_at"], reverse=True)
        if "FROM messages" in q:
            msgs = [m for m in self.db.get("messages", []) if m["conversation_id"] == args[0]]
            if "DESC" in q:
                # Mirrors the real query: newest-first with a LIMIT, which
                # the router reverses. Getting this backwards in the fake
                # would hide a real ordering bug.
                limit = args[1] if len(args) > 1 else len(msgs)
                return list(reversed(msgs))[:limit]
            return msgs
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
        # Set to an exception to simulate an unreachable Ollama - the
        # case /ready exists to catch on the desktop node.
        self.fail_with = None

    async def get(self, url, **kwargs):
        self.requests.append((url, None))
        if self.fail_with is not None:
            raise self.fail_with
        if url == "/api/tags":
            return FakeOllamaResponse({"models": [{"name": "qwen2.5-coder:7b"}]})
        raise ValueError(f"unexpected Ollama URL in test: {url}")

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

    def stream(self, method, url, json=None, **kwargs):
        self.requests.append((url, json))
        return FakeStreamCtx(self, url, json)

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
