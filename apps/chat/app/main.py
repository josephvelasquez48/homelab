"""Chat UI for the homelab's local LLM.

A thin backend-for-frontend rather than a second implementation of
anything. All conversation state and model access live in the api service;
this exists to serve the page and to hold the API key server-side.

That key is the whole reason for the proxy. The page could call
api.backend directly and save a hop, but only by shipping the key to every
browser that loads it, which on a LAN means anyone who can reach the host.
"""
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.config import API_KEY, API_URL, CHAT_MODELS, STREAM_TIMEOUT

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        base_url=API_URL,
        headers={"X-API-Key": API_KEY} if API_KEY else {},
        timeout=httpx.Timeout(STREAM_TIMEOUT, connect=10.0),
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="Homelab Chat", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/models")
async def models() -> JSONResponse:
    """The models the picker offers, parsed from config rather than queried.

    Deliberately not a live read of Ollama: everything installed is not
    everything worth offering, and the embedding model would show up.
    """
    options = []
    for entry in CHAT_MODELS.split(","):
        entry = entry.strip()
        if not entry:
            continue
        model, _, label = entry.partition("|")
        options.append({"model": model.strip(), "label": (label or model).strip()})
    return JSONResponse(content=options)


@app.get("/api/conversations")
async def list_conversations(request: Request) -> JSONResponse:
    return await _forward(request, "GET", "/v1/conversations")


@app.post("/api/conversations")
async def create_conversation(request: Request) -> JSONResponse:
    return await _forward(request, "POST", "/v1/conversations", json=await _body(request))


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request) -> JSONResponse:
    return await _forward(request, "GET", f"/v1/conversations/{conversation_id}")


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request) -> JSONResponse:
    return await _forward(request, "DELETE", f"/v1/conversations/{conversation_id}")


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, request: Request) -> StreamingResponse:
    """Pass the reply through as it arrives, without collecting it first.

    Buffering here would defeat the point: the api streams so the page can
    render tokens as they are generated, and a proxy that waits for the
    final byte turns that back into a single delayed response.
    """
    body = await _body(request)

    async def relay():
        try:
            async with request.app.state.http.stream(
                "POST",
                f"/v1/conversations/{conversation_id}/messages",
                json=body,
            ) as upstream:
                if upstream.status_code >= 400:
                    detail = (await upstream.aread()).decode(errors="replace")[:500]
                    yield _frame({"error": f"api returned {upstream.status_code}: {detail}"})
                    return
                async for chunk in upstream.aiter_raw():
                    yield chunk
        except httpx.ConnectError as exc:
            yield _frame({"error": f"cannot reach the api service: {exc}"})
        except httpx.ReadTimeout:
            # Distinguished from a generic failure because it has a
            # specific cause worth telling the user about: the desktop
            # holding the GPU may be asleep.
            yield _frame({"error": "timed out waiting for the model - is the GPU host awake?"})

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _frame(payload: dict) -> bytes:
    import json

    return f"data: {json.dumps(payload)}\n\n".encode()


async def _forward(request: Request, method: str, path: str, json=None) -> JSONResponse:
    try:
        response = await request.app.state.http.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"api unreachable: {exc}") from exc
    if response.status_code == 204:
        return JSONResponse(status_code=204, content=None)
    return JSONResponse(status_code=response.status_code, content=response.json())
