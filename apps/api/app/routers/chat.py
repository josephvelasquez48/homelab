from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.config import OLLAMA_MODEL
from app.rate_limit import rate_limit

router = APIRouter(dependencies=[Depends(rate_limit)])


class ChatRequest(BaseModel):
    message: str
    model: str = OLLAMA_MODEL


class ChatResponse(BaseModel):
    response: str
    model: str
    tokens_per_sec: float


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    r = await request.app.state.ollama.post(
        "/api/generate",
        json={"model": req.model, "prompt": req.message, "stream": False},
    )
    r.raise_for_status()
    data = r.json()
    tps = data["eval_count"] / (data["eval_duration"] / 1e9) if data.get("eval_duration") else 0.0
    return ChatResponse(response=data["response"], model=req.model, tokens_per_sec=round(tps, 1))
