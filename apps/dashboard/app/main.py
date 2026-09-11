import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import gpu, k8s, prometheus
from app.auth import check_password, is_authenticated, require_json, require_session
from app.config import (
    ALERTMANAGER_URL,
    API_HEALTH_URL,
    DASHBOARD_PASSWORD,
    SESSION_MAX_AGE,
    SESSION_SECRET,
    WATCHED_NAMESPACES,
)
import httpx


log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A missing dashboard-auth Secret must never be a silent condition.
    # It cannot re-open the endpoints - require_session is unconditional
    # and check_password fails closed with no password - but it does
    # leave the GPU control unusable, and "the button stopped working" is
    # not a diagnosis anyone should have to derive from a 401.
    if not DASHBOARD_PASSWORD:
        log.warning(
            "DASHBOARD_PASSWORD is not set: /api/gpu/release is unreachable "
            "(nobody can authenticate). Apply the dashboard-auth Secret - "
            "see docs/dashboard.md."
        )
    else:
        log.info("dashboard-auth configured: /api/gpu/release requires a session")

    app.state.k8s = k8s.make_client()
    app.state.http = httpx.AsyncClient(timeout=5.0)
    yield
    await app.state.k8s.aclose()
    await app.state.http.aclose()


app = FastAPI(title="Homelab Dashboard", lifespan=lifespan)

# Strict SameSite blocks cross-site cookies; Secure restricts them to HTTPS.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dashboard_session",
    max_age=SESSION_MAX_AGE,
    same_site="strict",
    https_only=True,
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _active_alerts(client: httpx.AsyncClient) -> list[dict] | None:
    """What Alertmanager is currently holding, not what Prometheus is evaluating.

    Returns None on failure rather than [], because "no alerts" and
    "cannot tell" must not look the same on a page whose whole job is
    telling you something is wrong.
    """
    try:
        r = await client.get(f"{ALERTMANAGER_URL}/api/v2/alerts", timeout=5.0)
        r.raise_for_status()
        return [
            {
                "name": a.get("labels", {}).get("alertname", "unknown"),
                "severity": a.get("labels", {}).get("severity", ""),
                "summary": a.get("annotations", {}).get("summary", ""),
                "state": a.get("status", {}).get("state", ""),
            }
            for a in r.json()
        ]
    except Exception:
        return None

@app.get("/api/status")
async def status():
    k8s_client = app.state.k8s

    nodes, pods_by_ns, argo_apps, pi_metrics = await asyncio.gather(
        k8s.get_nodes(k8s_client),
        asyncio.gather(*(k8s.get_pods(k8s_client, ns) for ns in WATCHED_NAMESPACES)),
        k8s.get_argo_applications(k8s_client),
        prometheus.get_pi_metrics(app.state.http),
        return_exceptions=True,
    )

    if isinstance(nodes, Exception):
        nodes = []
    pods = []
    if not isinstance(pods_by_ns, Exception):
        for ns_pods in pods_by_ns:
            pods.extend(ns_pods)
    if isinstance(argo_apps, Exception):
        argo_apps = []
    if isinstance(pi_metrics, Exception):
        pi_metrics = dict.fromkeys(prometheus.QUERIES)
    try:
        cross_node_status = await prometheus.get_cross_node_status(app.state.http, nodes)
    except Exception:
        cross_node_status = None

    backup, alerts = await asyncio.gather(
        prometheus.get_backup_health(app.state.http),
        _active_alerts(app.state.http),
        return_exceptions=True,
    )
    if isinstance(backup, Exception):
        backup = dict.fromkeys(prometheus.BACKUP_QUERIES)
    if isinstance(alerts, Exception):
        alerts = None

    # What is resident on the GPU, which is the only contention left now
    # that no cluster workload runs on the machine hosting it.
    try:
        gpu_models = await gpu.loaded_models(app.state.http)
    except Exception:
        gpu_models = None

    try:
        api_resp = await app.state.http.get(API_HEALTH_URL)
        api_health = {"reachable": True, "status_code": api_resp.status_code, "body": api_resp.json()}
    except Exception as exc:
        api_health = {"reachable": False, "error": str(exc)}

    # Pod counts per node. Cheap to compute here, and it answers a question
    # the page could not previously answer at all: a node can be Ready,
    # untainted and running nothing, which is exactly what m1-node was
    # doing after it joined - nothing reschedules onto a new node on its own.
    pods_per_node: dict[str, int] = {n["name"]: 0 for n in nodes}
    for pod in pods:
        node_name = pod.get("node")
        if node_name in pods_per_node:
            pods_per_node[node_name] += 1

    return {
        "nodes": nodes,
        "pods_per_node": pods_per_node,
        "backup": backup,
        "alerts": alerts,
        "pods": pods,
        "argo_apps": argo_apps,
        "gpu_models": gpu_models,
        "api_health": api_health,
        "pi_metrics": pi_metrics,
        "cross_node_status": cross_node_status,
    }


class LoginRequest(BaseModel):
    password: str


@app.get("/api/session")
async def session_state(request: Request):
    # `configured` lets the page explain itself when the dashboard-auth
    # Secret has not been applied: the GPU control is unavailable to all,
    # rather than the login simply appearing to reject a correct password.
    return {
        "authenticated": is_authenticated(request),
        "configured": bool(DASHBOARD_PASSWORD),
    }


@app.post("/api/login", dependencies=[Depends(require_json)])
async def login(request: Request, body: LoginRequest):
    if not check_password(body.password):
        raise HTTPException(status_code=401, detail="invalid password")
    request.session["authenticated"] = True
    return {"authenticated": True}


@app.post("/api/logout", dependencies=[Depends(require_json)])
async def logout(request: Request):
    request.session.clear()
    return {"authenticated": False}


# require_session is declared first so an unauthenticated caller gets 401
# regardless of content type - authorization is the boundary, and
# require_json is defence in depth behind it, not a gate of its own.
@app.post(
    "/api/gpu/release",
    dependencies=[Depends(require_session), Depends(require_json)],
)
async def gpu_release():
    """Evict Ollama's resident models so a game gets the VRAM back.

    One endpoint, not the on/off pair this replaces. There is nothing to
    turn back on - the next inference request reloads the model on demand
    - so a second button would only ever have been a no-op with a
    reassuring label.
    """
    try:
        return await gpu.release(app.state.http)
    except Exception as exc:
        # Ollama unreachable is the realistic failure here, and it is worth
        # distinguishing from "evicted nothing": the GPU may well be busy.
        raise HTTPException(
            status_code=502,
            detail=f"could not reach Ollama: {type(exc).__name__}: {exc}",
        ) from exc
