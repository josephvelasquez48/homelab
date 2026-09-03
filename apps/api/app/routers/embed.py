import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import OLLAMA_EMBED_MODEL
from app.ollama import embed as ollama_embed
from app.rate_limit import rate_limit

router = APIRouter(dependencies=[Depends(rate_limit)])


class EmbedRequest(BaseModel):
    input: str | list[str]
    model: str = OLLAMA_EMBED_MODEL


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    model: str
    dimensions: int


@router.post("/v1/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest, request: Request) -> EmbedResponse:
    inputs = [req.input] if isinstance(req.input, str) else req.input
    if not inputs:
        raise HTTPException(status_code=422, detail="input must not be empty")

    try:
        embeddings = await ollama_embed(request.app.state.ollama, req.model, inputs)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise HTTPException(status_code=502, detail="AI backend unavailable") from exc

    return EmbedResponse(
        embeddings=embeddings,
        model=req.model,
        dimensions=len(embeddings[0]) if embeddings else 0,
    )
