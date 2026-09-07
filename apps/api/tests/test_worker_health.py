"""Tests for the worker readiness check.

The point of this check is to return non-zero when a dependency is
unreachable - that is the case that previously went undetected and let a
broken worker replace a working one. So the failure paths matter more
here than the happy path.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import worker_health


@pytest.fixture
def deps(monkeypatch):
    """Patches both dependencies to succeed; tests break what they need."""
    redis_client = MagicMock(ping=AsyncMock(return_value=True), aclose=AsyncMock())
    monkeypatch.setattr(worker_health.redis, "from_url", lambda *a, **k: redis_client)

    conn = MagicMock(execute=AsyncMock(return_value="SELECT 1"), close=AsyncMock())
    monkeypatch.setattr(worker_health.asyncpg, "connect", AsyncMock(return_value=conn))

    return {"redis": redis_client, "conn": conn}


def test_healthy_exits_zero(deps):
    assert worker_health.main() == 0
    deps["redis"].ping.assert_awaited_once()
    deps["conn"].execute.assert_awaited_once()


def test_unreachable_redis_exits_nonzero(deps, capsys):
    """The exact failure that went undetected: DNS resolution failing."""
    deps["redis"].ping.side_effect = OSError(
        "failed to resolve host 'redis.backend.svc.cluster.local'"
    )

    assert worker_health.main() == 1
    assert "failed to resolve host" in capsys.readouterr().err


def test_unreachable_postgres_exits_nonzero(deps, capsys):
    worker_health.asyncpg.connect.side_effect = OSError(
        "failed to resolve host 'postgres.data.svc.cluster.local'"
    )

    assert worker_health.main() == 1
    assert "failed to resolve host" in capsys.readouterr().err


def test_redis_client_is_closed_when_postgres_fails(deps):
    """A probe running every 30s must not leak a connection per failure."""
    worker_health.asyncpg.connect.side_effect = OSError("nope")

    assert worker_health.main() == 1
    deps["redis"].aclose.assert_awaited_once()


def test_hanging_dependency_exits_nonzero(deps):
    """A dependency that never answers must fail, not hang the probe.

    Without the wait_for, kubelet would eventually time the probe out
    anyway - but as an opaque timeout with nothing in the pod events.
    """
    async def never_returns(*args, **kwargs):
        import asyncio

        await asyncio.sleep(worker_health.CHECK_TIMEOUT + 5)

    deps["redis"].ping.side_effect = never_returns

    assert worker_health.main() == 1
