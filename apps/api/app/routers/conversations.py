import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import CHAT_MODEL
from app.logging import get_logger
from app.ollama import chat_stream
from app.retrieval import as_context, as_stored, lookup
from app.rate_limit import rate_limit

router = APIRouter(dependencies=[Depends(rate_limit)])
log = get_logger(__name__)

# How many prior messages get replayed to the model. A 7B model at Q4 on an
# 8GB card has a finite context, and exceeding it fails at generation time
# rather than at request time - which reads as "the assistant broke" long
# after the conversation that caused it. Truncating from the front keeps the
# most recent turns, which is what coherence actually depends on.
HISTORY_LIMIT = 40

# Titles are derived, not asked for. Generating one with the model would mean
# a second inference round-trip before the first token of the actual reply.
TITLE_MAX = 60


class ConversationCreate(BaseModel):
    title: str | None = None
    model: str = CHAT_MODEL


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    model: str
    created_at: str
    updated_at: str


class Source(BaseModel):
    n: int
    title: str
    archive: str
    url: str
    excerpt: str


class Message(BaseModel):
    role: str
    content: str
    created_at: str
    # Absent on every turn that did not search, which is most of them. An
    # empty list would claim a search happened and returned nothing.
    sources: list[Source] | None = None


class ConversationDetail(ConversationSummary):
    messages: list[Message]


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=32000)
    # Per message, not per conversation. Within one conversation some turns
    # are lookups and some are "say that again shorter", and searching an
    # encyclopedia for the second wastes both the round trip and the context.
    retrieve: bool = False


@router.post("/v1/conversations", response_model=ConversationSummary, status_code=201)
async def create_conversation(body: ConversationCreate, request: Request) -> dict:
    conversation_id = uuid.uuid4()
    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO conversations (id, title, model) VALUES ($1, $2, $3) "
            "RETURNING id, title, model, created_at, updated_at",
            conversation_id,
            body.title,
            body.model,
        )
    return _conversation_row(row)


@router.get("/v1/conversations", response_model=list[ConversationSummary])
async def list_conversations(request: Request) -> list[dict]:
    async with request.app.state.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, model, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC LIMIT 100"
        )
    return [_conversation_row(r) for r in rows]


@router.get("/v1/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, request: Request) -> dict:
    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, model, created_at, updated_at FROM conversations WHERE id = $1",
            conversation_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        messages = await conn.fetch(
            "SELECT role, content, created_at, sources FROM messages "
            "WHERE conversation_id = $1 ORDER BY created_at",
            conversation_id,
        )
    detail = _conversation_row(row)
    detail["messages"] = [
        {
            "role": m["role"],
            "content": m["content"],
            "created_at": m["created_at"].isoformat(),
            # asyncpg hands back JSONB as text unless a codec is registered,
            # and registering one for a single column is more machinery than
            # a json.loads here.
            "sources": json.loads(m["sources"]) if isinstance(m["sources"], str) else m["sources"],
        }
        for m in messages
    ]
    return detail


@router.delete("/v1/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, request: Request) -> None:
    async with request.app.state.pg_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM conversations WHERE id = $1", conversation_id
        )
    if result.endswith("0"):
        raise HTTPException(status_code=404, detail="conversation not found")


@router.post("/v1/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID, body: MessageCreate, request: Request
) -> StreamingResponse:
    """Persist the user's turn, then stream the reply as server-sent events.

    The user message is written before generation starts, so a failure
    mid-reply leaves a conversation that is missing an answer rather than
    missing the question - the first is obvious and recoverable, the second
    silently loses what you typed.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        conversation = await conn.fetchrow(
            "SELECT id, model, title FROM conversations WHERE id = $1", conversation_id
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")

        await conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content) "
            "VALUES ($1, $2, 'user', $3)",
            uuid.uuid4(),
            conversation_id,
            body.content,
        )
        if conversation["title"] is None:
            await conn.execute(
                "UPDATE conversations SET title = $2 WHERE id = $1",
                conversation_id,
                body.content[:TITLE_MAX],
            )
        history = await conn.fetch(
            "SELECT role, content FROM messages WHERE conversation_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            conversation_id,
            HISTORY_LIMIT,
        )

    # Fetched newest-first to make LIMIT keep the recent end, then reversed
    # because the model needs them in the order they were said.
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(history)]
    model = conversation["model"]

    sources: list[dict] = []
    if body.retrieve:
        sources = await lookup(request.app.state.zimsearch, body.content)
        if sources:
            # Inserted just before the question rather than at the front of
            # the conversation. A 7B model weights the end of its context
            # most heavily, and passages sitting 40 turns back read as old
            # discussion rather than as material for the turn being asked.
            messages.insert(
                len(messages) - 1, {"role": "system", "content": as_context(sources)}
            )
    stored = as_stored(sources)

    async def event_stream():
        collected: list[str] = []
        try:
            async for chunk in chat_stream(request.app.state.ollama, model, messages):
                if "token" in chunk:
                    collected.append(chunk["token"])
                yield f"data: {json.dumps(chunk)}\n\n"
            if stored and collected:
                # Its own frame rather than trailing tokens, so the page
                # can render sources as a panel with links into the local
                # wiki instead of as more prose at the end of the answer.
                yield f"data: {json.dumps({'sources': stored})}\n\n"
        except Exception as exc:
            log.warning("chat_stream_failed", error=str(exc), conversation=str(conversation_id))
            yield f"data: {json.dumps({'error': f'{type(exc).__name__}: {exc}'})}\n\n"
        finally:
            # Persist whatever was generated even when the client hangs up or
            # the model errors halfway. A partial answer that is visible on
            # screen but absent from the database is the worst outcome: the
            # next turn would be sent without it and the model would
            # contradict itself.
            if collected:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO messages "
                        "(id, conversation_id, role, content, sources) "
                        "VALUES ($1, $2, 'assistant', $3, $4)",
                        uuid.uuid4(),
                        conversation_id,
                        "".join(collected),
                        # NULL, not an empty list, when nothing was searched:
                        # the column is the only place "did not look" and
                        # "looked and found nothing" stay distinguishable.
                        json.dumps(stored) if stored else None,
                    )
                    await conn.execute(
                        "UPDATE conversations SET updated_at = now() WHERE id = $1",
                        conversation_id,
                    )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # Without these, a proxy in front of this will buffer the whole
        # response and deliver it at once - which looks exactly like
        # streaming being broken.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _conversation_row(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "model": row["model"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
