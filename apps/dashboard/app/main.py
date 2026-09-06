import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import k8s, prometheus
from app.auth import check_password, is_authenticated, require_json, require_session
from app.config import (
    API_HEALTH_URL,
    DASHBOARD_PASSWORD,
    GAMING_NODE_NAME,
    SESSION_MAX_AGE,
    SESSION_SECRET,
    WATCHED_NAMESPACES,
)
from app.ssh_runner import run_gaming_script

import httpx


log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A missing dashboard-auth Secret must never be a silent condition.
    # It cannot re-open the endpoints - require_session is unconditional
    # and check_password fails closed with no password - but it does
    # leave gaming mode unusable, and "the buttons stopped working" is
    # not a diagnosis anyone should have to derive from a 401.
    if not DASHBOARD_PASSWORD:
        log.warning(
            "DASHBOARD_PASSWORD is not set: /api/gaming/* is unreachable "
            "(nobody can authenticate). Apply the dashboard-auth Secret - "
            "see docs/dashboard.md."
        )
    else:
        log.info("dashboard-auth configured: /api/gaming/* requires a session")

    app.state.k8s = k8s.make_client()
    app.state.http = httpx.AsyncClient(timeout=5.0)
    yield
    await app.state.k8s.aclose()
    await app.state.http.aclose()


app = FastAPI(title="Homelab Dashboard", lifespan=lifespan)

# same_site="strict" is the real cross-site defence: a cookie set this way
# is simply not attached to any request a third-party page originates, so
# the drive-by POST that used to reach /api/gaming/* now arrives
# unauthenticated and gets a 401. https_only stays False only because the
# Ingress is still plain HTTP - flip it the moment TLS lands (see
# docs/security-testing.md, finding 5), or the cookie crosses the LAN in
# the clear.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dashboard_session",
    max_age=SESSION_MAX_AGE,
    same_site="strict",
    https_only=False,
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    k8s_client = app.state.k8s

    nodes, pods_by_ns, argo_apps, pi_metrics, desktop_metrics, cross_node_status = await asyncio.gather(
        k8s.get_nodes(k8s_client),
        asyncio.gather(*(k8s.get_pods(k8s_client, ns) for ns in WATCHED_NAMESPACES)),
        k8s.get_argo_applications(k8s_client),
        prometheus.get_pi_metrics(app.state.http),
        prometheus.get_desktop_metrics(app.state.http),
        prometheus.get_cross_node_status(app.state.http),
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
    if isinstance(desktop_metrics, Exception):
        desktop_metrics = dict.fromkeys(prometheus.DESKTOP_QUERIES)
    if isinstance(cross_node_status, Exception):
        cross_node_status = None

    gaming_node = next((n for n in nodes if n["name"] == GAMING_NODE_NAME), None)
    gaming_mode_active = bool(gaming_node) and not gaming_node["schedulable"]

    try:
        api_resp = await app.state.http.get(API_HEALTH_URL)
        api_health = {"reachable": True, "status_code": api_resp.status_code, "body": api_resp.json()}
    except Exception as exc:
        api_health = {"reachable": False, "error": str(exc)}

    return {
        "nodes": nodes,
        "pods": pods,
        "argo_apps": argo_apps,
        "gaming_mode_active": gaming_mode_active,
        "api_health": api_health,
        "pi_metrics": pi_metrics,
        "desktop_metrics": desktop_metrics,
        "cross_node_status": cross_node_status,
    }


class LoginRequest(BaseModel):
    password: str


@app.get("/api/session")
async def session_state(request: Request):
    # `configured` lets the page explain itself when the dashboard-auth
    # Secret has not been applied: gaming mode is unavailable to everyone,
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
    "/api/gaming/on",
    dependencies=[Depends(require_session), Depends(require_json)],
)
async def gaming_on():
    # 120s drain timeout (kubectl drain --timeout=120s in pregame.ps1)
    # plus real headroom for cordon/SSH/agent-stop overhead.
    return await run_gaming_script("pregame.ps1", timeout=150)


@app.post(
    "/api/gaming/off",
    dependencies=[Depends(require_session), Depends(require_json)],
)
async def gaming_off():
    # 180s Ready-wait (a cold WSL2 start) plus headroom.
    return await run_gaming_script("postgame.ps1", timeout=210)
