import time

from fastapi import Depends, HTTPException, Request

from app.auth import require_api_key
from app.config import RATE_LIMIT_PER_MINUTE


async def rate_limit(request: Request, api_key: str = Depends(require_api_key)) -> None:
    window = int(time.time() // 60)
    key = f"ratelimit:{api_key}:{window}"

    count = await request.app.state.redis.incr(key)
    if count == 1:
        await request.app.state.redis.expire(key, 60)

    if count > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
