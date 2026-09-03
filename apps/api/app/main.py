import os
from contextlib import asynccontextmanager

import asyncpg
import httpx
import redis.asyncio as redis
from fastapi import FastAPI
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    app.state.redis = redis.from_url(REDIS_URL)
    app.state.ollama = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=120.0)
    yield
    await app.state.pg_pool.close()
    await app.state.redis.aclose()
    await app.state.ollama.aclose()


app = FastAPI(title="Homelab API", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    async with app.state.pg_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await app.state.redis.ping()
    return {"status": "ok", "postgres": "ok", "redis": "ok"}


class ChatRequest(BaseModel):
    message: str
    model: str = OLLAMA_MODEL


class ChatResponse(BaseModel):
    response: str
    model: str
    tokens_per_sec: float


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    r = await app.state.ollama.post(
        "/api/generate",
        json={"model": req.model, "prompt": req.message, "stream": False},
    )
    r.raise_for_status()
    data = r.json()
    tps = data["eval_count"] / (data["eval_duration"] / 1e9) if data.get("eval_duration") else 0.0
    return ChatResponse(response=data["response"], model=req.model, tokens_per_sec=round(tps, 1))
