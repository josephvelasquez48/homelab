import hashlib
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import OLLAMA_MODEL
from app.ollama import generate
from app.rate_limit import rate_limit

router = APIRouter(dependencies=[Depends(rate_limit)])

CACHE_TTL_SECONDS = 3600


class ChatRequest(BaseModel):
    message: str
    model: str = OLLAMA_MODEL


class ChatResponse(BaseModel):
    response: str
    model: str
    tokens_per_sec: float
    cached: bool


def _cache_key(model: str, message: str) -> str:
    digest = hashlib.sha256(f"{model}:{message}".encode()).hexdigest()
    return f"chat:cache:{digest}"


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    cache_key = _cache_key(req.model, req.message)

    cached = await request.app.state.redis.get(cache_key)
    if cached is not None:
        data = json.loads(cached)
        return ChatResponse(model=req.model, cached=True, **data)

    try:
        result = await generate(request.app.state.ollama, req.model, req.message)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise HTTPException(status_code=502, detail="AI backend unavailable") from exc

    await request.app.state.redis.set(cache_key, json.dumps(result), ex=CACHE_TTL_SECONDS)
    return ChatResponse(model=req.model, cached=False, **result)
