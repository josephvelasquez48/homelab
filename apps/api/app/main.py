import time
import uuid
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import OLLAMA_URL
from app.db import create_pg_pool, create_redis_client
from app.logging import configure_logging, get_logger
from app.routers import chat, embed, health, jobs

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pg_pool = await create_pg_pool()
    app.state.redis = create_redis_client()
    app.state.ollama = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=120.0)
    log.info("startup_complete")
    yield
    await app.state.pg_pool.close()
    await app.state.redis.aclose()
    await app.state.ollama.aclose()
    log.info("shutdown_complete")


app = FastAPI(title="Homelab API", lifespan=lifespan)
app.include_router(health.router)
app.include_router(chat.router)
app.include_router(jobs.router)
app.include_router(embed.router)

# Unauthenticated like /health - Prometheus scrapes this directly, and the
# actual protection boundary is the LAN-only firewall, not app-level auth.
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    log.info(
        "request_completed",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    structlog.contextvars.clear_contextvars()
    response.headers["X-Request-ID"] = request_id
    return response
