from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    await request.app.state.redis.ping()
    return {"status": "ok", "postgres": "ok", "redis": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    """Readiness: can this replica actually serve every route it exposes?

    Deliberately separate from /health, because the two questions have
    opposite correct answers during a dependency outage. /health backs the
    liveness probe, and restarting a pod cannot fix an unreachable Ollama -
    it would just crash-loop every replica for the duration. Readiness can
    safely say "not me, route elsewhere" and recover on its own.

    Ollama is checked here because it is a real dependency of /v1/chat and
    the RAG routes: a replica that cannot reach it should stop receiving
    traffic for them rather than failing every request.

    It was added for a sharper reason that no longer applies. Ollama used
    to run as a Windows-side process, unreachable from pods on the desktop
    node itself - inside WSL the host's LAN IP is WSL's own address, and
    only WSL-side listeners answer on it. Such a replica passed /health on
    Postgres and Redis alone, joined the Service, and then failed every
    inference request routed to it. Ollama now runs as a systemd service
    inside WSL and is reachable from both nodes, so that trap is gone -
    the check stays because "Ollama is down" is still a reason not to
    serve.
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
    except Exception as exc:
        checks["ollama"] = f"{type(exc).__name__}: {exc}"
        failed.append("ollama")

    if failed:
        # Every check runs before returning, so a probe failure names all
        # of them at once rather than only the first - the difference
        # between "ollama is unreachable" and "this node reaches nothing".
        raise HTTPException(status_code=503, detail={"unready": failed, **checks})

    return {"status": "ok", **checks}
