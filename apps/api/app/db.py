import asyncpg
import redis.asyncio as redis

from app.config import DATABASE_URL, REDIS_URL


async def create_pg_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)


def create_redis_client() -> redis.Redis:
    # socket_timeout must exceed the longest blocking command timeout used
    # against this client (the worker's BLPOP), or the client aborts the
    # read as a timeout before Redis's own blocking wait ever completes.
    return redis.from_url(REDIS_URL, socket_timeout=10)
