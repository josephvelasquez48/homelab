import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import component, display, k8s
from app.config import ALERTMANAGER_URL
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


# The pages and their scripts are rechecked on every load. Sent without a
# Cache-Control, a browser picks its own freshness from Last-Modified (about
# a tenth of the file's age), so after a deploy the Pi's display kept running
# the old aquarium for hours - and restarting Chromium didn't help, the copies
# live on disk. "no-cache" still lets it keep a copy; it only has to ask
# first, and an unchanged static file comes back as a small 304.
@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/display" or path.startswith(("/static/", "/component/")):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# Which version of the display page this build serves: a hash of its files.
# /api/display carries it, and a display still running an older page reloads
# itself - so a deploy shows up within a poll, not at the 4 a.m. reload.
PAGE_VERSION = hashlib.sha256(
    b"".join((STATIC_DIR / name).read_bytes() for name in ("display.html", "display.js", "tank.js"))
).hexdigest()[:12]


# The status page that used to live here is gone - the Pi's display is the
# only page now, so the bare address goes to it.
@app.get("/")
async def index():
    return RedirectResponse("/display")


@app.get("/display")
async def display_page():
    """The Pi's always-on screen (apps/pi-display opens it full screen)."""
    return FileResponse(STATIC_DIR / "display.html")


@app.get("/component/{cid}")
async def component_page(cid: str):
    """A box's debug page (tap it on the map)."""
    if cid not in component.COMPONENTS:
        raise HTTPException(404, "No such component")
    return FileResponse(STATIC_DIR / "component.html")


@app.get("/component/pod/{namespace}/{name}")
async def pod_page(namespace: str, name: str):
    """A fish's debug page (tap it in the aquarium)."""
    return FileResponse(STATIC_DIR / "component.html")


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


@app.get("/api/display")
async def display_state():
    k8s_client = app.state.k8s
    state = await display.gather(
        k8s_client,
        app.state.http,
        k8s.get_argo_applications(k8s_client),
        _active_alerts(app.state.http),
        k8s.get_nodes(k8s_client),
    )
    state["page_version"] = PAGE_VERSION
    return state


def _status(services: dict, cid: str) -> str:
    """A box's status, the way the map colours it (static/display.js serviceStatus)."""
    if cid == "lan":
        return "up"
    if cid == "apps":
        apps = [services.get(a, "unknown") for a in ("grafana", "argocd", "chat", "kiwix")]
        return "down" if "down" in apps else "up" if all(a == "up" for a in apps) else "unknown"
    return services.get(cid, "unknown")


@app.get("/api/component/{cid}")
async def component_state(cid: str):
    if cid not in component.COMPONENTS:
        raise HTTPException(404, "No such component")
    k8s_client, http = app.state.k8s, app.state.http
    # The map's own reading, so the page says what the box said.
    state = await display.gather(k8s_client, http, k8s.get_argo_applications(k8s_client),
                                 _active_alerts(http), k8s.get_nodes(k8s_client))
    out = await component.component(
        k8s_client, http, cid, state["services"],
        k8s.get_argo_applications(k8s_client), asyncio.sleep(0, state["alerts"]), k8s.get_nodes(k8s_client))
    out["status"] = _status(state["services"], cid)
    out["connections"] = component.connections(cid, state["links"], state["rates"], state["services"], _status)
    return out


@app.get("/api/pod/{namespace}/{name}")
async def pod_state(namespace: str, name: str):
    k8s_client, http = app.state.k8s, app.state.http
    out = await component.single_pod(k8s_client, namespace, name, _active_alerts(http))
    if out is None:
        raise HTTPException(404, "That pod is gone - it was probably replaced. Go back and tap its fish again.")
    # Its box's connections, when it belongs to one (an api pod: FastAPI's).
    cid = component.component_of_pod(namespace, name)
    if cid:
        state = await display.gather(k8s_client, http, k8s.get_argo_applications(k8s_client),
                                     asyncio.sleep(0, out["alerts"]), k8s.get_nodes(k8s_client))
        out["box"] = {"id": cid, "title": component.COMPONENTS[cid]["title"]}
        out["guide"] = component.GUIDES.get(cid)
        out["connections"] = component.connections(cid, state["links"], state["rates"], state["services"], _status)
    return out
