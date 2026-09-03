from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await request.app.state.redis.ping()
    return {"status": "ok", "postgres": "ok", "redis": "ok"}
