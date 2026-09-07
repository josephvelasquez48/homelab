"""Readiness check for the worker.

The worker is a blocking queue consumer, not an HTTP server, so there is
no endpoint for httpGet to probe - and with no probe at all, Kubernetes
counts the container Ready the instant the process starts. That is what
let a worker on a node with broken pod networking be declared Ready,
which in turn let the Deployment terminate the healthy worker to make
room for it. `kubectl get deploy worker` read 1/1 while nothing was
processing the queue.

So the check has to be what "Ready" actually means for this process: it
can reach the queue it blocks on, and the database it writes results to.
Reaching neither is precisely the state that went undetected.

Run as `python -m app.worker_health`; exits 0 when healthy, 1 otherwise,
printing the reason to stderr for `kubectl describe` to surface.
"""
import asyncio
import sys

import asyncpg
import redis.asyncio as redis

from app.config import DATABASE_URL, REDIS_URL

# Comfortably under the probe's own timeoutSeconds, so a hung dependency
# surfaces as this check's own error rather than as an opaque probe
# timeout with nothing in the events.
CHECK_TIMEOUT = 4.0


async def check() -> None:
    # A dedicated client rather than db.create_redis_client(): that one
    # carries socket_timeout=10 sized for the worker's BLPOP, which is
    # longer than this whole check is allowed to take.
    client = redis.from_url(REDIS_URL, socket_timeout=CHECK_TIMEOUT)
    try:
        await client.ping()
    finally:
        await client.aclose()

    # A single connection, not create_pg_pool() - a probe running every
    # 30s has no business building a 5-connection pool each time.
    conn = await asyncpg.connect(DATABASE_URL, timeout=CHECK_TIMEOUT)
    try:
        await conn.execute("SELECT 1")
    finally:
        await conn.close()


def main() -> int:
    try:
        asyncio.run(asyncio.wait_for(check(), timeout=CHECK_TIMEOUT))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
