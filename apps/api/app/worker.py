import asyncio
import json

import httpx

from app.config import JOB_QUEUE_KEY, OLLAMA_TIMEOUT, OLLAMA_URL
from app.db import create_pg_pool, create_redis_client
from app.logging import configure_logging, get_logger
from app.ollama import generate

configure_logging()
log = get_logger(__name__)


async def process_chat(payload: dict, ollama: httpx.AsyncClient) -> dict:
    return await generate(ollama, payload.get("model", "qwen2.5-coder:7b"), payload["message"])


HANDLERS = {"chat": process_chat}


async def run_job(job_id: str, pg_pool, ollama: httpx.AsyncClient) -> None:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT type, payload FROM jobs WHERE id = $1", job_id)
        if row is None:
            log.warning("job_not_found", job_id=job_id)
            return

        await conn.execute(
            "UPDATE jobs SET status = 'running', updated_at = now() WHERE id = $1", job_id
        )

    handler = HANDLERS.get(row["type"])
    payload = json.loads(row["payload"])

    try:
        result = await handler(payload, ollama)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jobs SET status = 'done', result = $2::jsonb, updated_at = now()
                WHERE id = $1
                """,
                job_id,
                json.dumps(result),
            )
        log.info("job_completed", job_id=job_id, type=row["type"])
    except Exception as exc:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'failed', error = $2, updated_at = now() WHERE id = $1",
                job_id,
                str(exc),
            )
        log.error("job_failed", job_id=job_id, type=row["type"], error=str(exc))


async def main() -> None:
    pg_pool = await create_pg_pool()
    redis_client = create_redis_client()
    ollama = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=OLLAMA_TIMEOUT)
    log.info("worker_started")

    try:
        while True:
            item = await redis_client.blpop(JOB_QUEUE_KEY, timeout=5)
            if item is None:
                continue
            _, job_id = item
            await run_job(job_id.decode(), pg_pool, ollama)
    finally:
        await pg_pool.close()
        await redis_client.aclose()
        await ollama.aclose()


if __name__ == "__main__":
    asyncio.run(main())
