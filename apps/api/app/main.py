import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as redis
from fastapi import FastAPI

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    app.state.redis = redis.from_url(REDIS_URL)
    yield
    await app.state.pg_pool.close()
    await app.state.redis.aclose()


app = FastAPI(title="Homelab API", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    async with app.state.pg_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok", "postgres": "ok", "redis": "ok"}
