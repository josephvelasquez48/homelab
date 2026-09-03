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
