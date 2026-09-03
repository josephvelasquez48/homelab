import json
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import OLLAMA_EMBED_MODEL, OLLAMA_MODEL
from app.ollama import embed as ollama_embed
from app.ollama import generate
from app.rate_limit import rate_limit
from app.vectors import to_vector_literal

router = APIRouter(dependencies=[Depends(rate_limit)])


class DocumentCreate(BaseModel):
    content: str
    metadata: dict[str, Any] = {}


class DocumentCreated(BaseModel):
    id: str
    dimensions: int


@router.post("/v1/documents", response_model=DocumentCreated, status_code=201)
async def create_document(req: DocumentCreate, request: Request) -> DocumentCreated:
    try:
        [embedding] = await ollama_embed(request.app.state.ollama, OLLAMA_EMBED_MODEL, [req.content])
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise HTTPException(status_code=502, detail="AI backend unavailable") from exc

    doc_id = str(uuid.uuid4())
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO documents (id, content, embedding, metadata)
            VALUES ($1, $2, $3::vector, $4::jsonb)
            """,
            doc_id,
            req.content,
            to_vector_literal(embedding),
            json.dumps(req.metadata),
        )
    return DocumentCreated(id=doc_id, dimensions=len(embedding))


class RagQuery(BaseModel):
    question: str
    top_k: int = 3
    model: str = OLLAMA_MODEL


class RagSource(BaseModel):
    id: str
    content: str
    distance: float


class RagAnswer(BaseModel):
    answer: str
    sources: list[RagSource]
    model: str


PROMPT_TEMPLATE = """Answer the question using only the context below. \
If the context doesn't contain the answer, say you don't know.

Context:
{context}

Question: {question}

Answer:"""


@router.post("/v1/rag/query", response_model=RagAnswer)
async def rag_query(req: RagQuery, request: Request) -> RagAnswer:
    try:
        [question_embedding] = await ollama_embed(
            request.app.state.ollama, OLLAMA_EMBED_MODEL, [req.question]
        )
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise HTTPException(status_code=502, detail="AI backend unavailable") from exc

    vector = to_vector_literal(question_embedding)
    async with request.app.state.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, embedding <=> $1::vector AS distance
            FROM documents
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            vector,
            req.top_k,
        )

    sources = [RagSource(id=str(r["id"]), content=r["content"], distance=r["distance"]) for r in rows]
    context = "\n\n".join(s.content for s in sources) if sources else "(no documents found)"
    prompt = PROMPT_TEMPLATE.format(context=context, question=req.question)

    try:
        result = await generate(request.app.state.ollama, req.model, prompt)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        raise HTTPException(status_code=502, detail="AI backend unavailable") from exc

    return RagAnswer(answer=result["response"], sources=sources, model=req.model)
