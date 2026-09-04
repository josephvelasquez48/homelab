import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import k8s, prometheus
from app.config import API_HEALTH_URL, GAMING_NODE_NAME, WATCHED_NAMESPACES
from app.ssh_runner import run_gaming_script

import httpx


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.k8s = k8s.make_client()
    app.state.http = httpx.AsyncClient(timeout=5.0)
    yield
    await app.state.k8s.aclose()
    await app.state.http.aclose()


app = FastAPI(title="Homelab Dashboard", lifespan=lifespan)

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
    }


@app.post("/api/gaming/on")
async def gaming_on():
    # 120s drain timeout (kubectl drain --timeout=120s in pregame.ps1)
    # plus real headroom for cordon/SSH/agent-stop overhead.
    return await run_gaming_script("pregame.ps1", timeout=150)


@app.post("/api/gaming/off")
async def gaming_off():
    # 180s Ready-wait (a cold WSL2 start) plus headroom.
    return await run_gaming_script("postgame.ps1", timeout=210)
