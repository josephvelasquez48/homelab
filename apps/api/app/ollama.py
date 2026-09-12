import json

import httpx
import tenacity

from app.logging import get_logger

log = get_logger(__name__)


def _log_retry(retry_state: tenacity.RetryCallState) -> None:
    log.warning(
        "ollama_retry",
        attempt=retry_state.attempt_number,
        error=str(retry_state.outcome.exception()),
    )


@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    retry=tenacity.retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    before_sleep=_log_retry,
    reraise=True,
)
async def generate(client: httpx.AsyncClient, model: str, prompt: str) -> dict:
    r = await client.post(
        "/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
    )
    r.raise_for_status()
    data = r.json()
    tps = data["eval_count"] / (data["eval_duration"] / 1e9) if data.get("eval_duration") else 0.0
    return {"response": data["response"], "tokens_per_sec": round(tps, 1)}


@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    retry=tenacity.retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    before_sleep=_log_retry,
    reraise=True,
)
async def embed(client: httpx.AsyncClient, model: str, inputs: list[str]) -> list[list[float]]:
    r = await client.post("/api/embed", json={"model": model, "input": inputs})
    r.raise_for_status()
    return r.json()["embeddings"]

async def chat_stream(client: httpx.AsyncClient, model: str, messages: list[dict]):
    """Stream a reply for a full message history.

    Uses /api/chat rather than /api/generate. The difference is not
    cosmetic: /api/generate takes one prompt string and has no notion of who
    said what, so a conversation has to be flattened into text and the model
    has to infer the turn structure. /api/chat takes role-tagged messages,
    which is what the instruct tuning was actually trained on.

    Deliberately not wrapped in the retry decorator the other calls use.
    A retry here would replay a half-delivered reply from the start, and the
    client has already rendered the first half - so a transient error has to
    surface rather than silently duplicate text.
    """
    async with client.stream(
        "POST",
        "/api/chat",
        json={"model": model, "messages": messages, "stream": True},
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            token = chunk.get("message", {}).get("content", "")
            if token:
                yield {"token": token}
            if chunk.get("done"):
                # eval_count/eval_duration only appear on the final chunk,
                # so throughput is reported at the end or not at all.
                duration = chunk.get("eval_duration") or 0
                count = chunk.get("eval_count") or 0
                tps = count / (duration / 1e9) if duration else 0.0
                yield {"done": True, "tokens_per_sec": round(tps, 1)}
