from fastapi import APIRouter, HTTPException, Request
from prometheus_client import Gauge

router = APIRouter()

# Reachability of Ollama, reported rather than enforced. Set on every
# readiness probe, so it updates at the kubelet's cadence without any
# extra polling. Exposed on /metrics by the instrumentator in main.py.
INFERENCE_REACHABLE = Gauge(
    "homelab_inference_reachable",
    "1 if Ollama answered /api/tags on the last readiness check, else 0",
)


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await request.app.state.redis.ping()
    return {"status": "ok", "postgres": "ok", "redis": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    """Readiness: can this replica serve the routes it is routed traffic for?

    Deliberately separate from /health, which backs liveness and checks
    only Postgres and Redis - restarting a pod cannot fix an unreachable
    dependency, and would crash-loop every replica for the duration.

    Ollama is checked but does NOT fail readiness, which is a deliberate
    reversal. It used to: a replica that could not reach Ollama was taken
    out of the Service so requests routed elsewhere. That reasoning
    assumed there was an elsewhere. Ollama runs on a single desktop, so
    every replica fails the check simultaneously, the whole Deployment
    goes unready, and Argo CD reports the backend app Degraded - because
    a PC went to sleep. Everything the API serves that is not inference
    stops with it, for no benefit.

    So the result moves from enforcement to reporting: it sets
    homelab_inference_reachable and appears in the response body, and
    inference requests fail on their own terms instead. The alert rule
    HomelabInferenceDown is what now tells you, rather than inferring it
    from an unhealthy app.

    The previous rationale here described Ollama running as a systemd
    service inside WSL. WSL was retired in the node migration
    (docs/node-migration.md) and Ollama runs natively on Windows now.
    """
    checks: dict[str, str] = {}
    failed: list[str] = []

    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"{type(exc).__name__}: {exc}"
        failed.append("postgres")

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"{type(exc).__name__}: {exc}"
        failed.append("redis")

    try:
        # /api/tags is the cheapest endpoint that proves a usable Ollama:
        # it neither loads a model nor runs inference, so this stays a
        # reachability check rather than a periodic GPU wake-up.
        resp = await request.app.state.ollama.get("/api/tags", timeout=5.0)
        resp.raise_for_status()
        checks["ollama"] = "ok"
        INFERENCE_REACHABLE.set(1)
    except Exception as exc:
        # Reported, not appended to `failed`. See the docstring.
        checks["ollama"] = f"{type(exc).__name__}: {exc}"
        INFERENCE_REACHABLE.set(0)

    if failed:
        # Every check runs before returning, so a probe failure names all
        # of them at once rather than only the first - the difference
        # between "ollama is unreachable" and "this node reaches nothing".
        raise HTTPException(status_code=503, detail={"unready": failed, **checks})

    return {"status": "ok", **checks}
