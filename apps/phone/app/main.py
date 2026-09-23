"""Phone bridge web app: https://phone.home:8443.

Runs on the Pi as a systemd *user* service, not in K3s: it needs the
Pi's Bluetooth radio and the logged-in user's PipeWire session, neither
of which a pod can reach without giving it the host. See docs/phone.md.
"""
import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.contacts import Contacts
from app.history import CallLog
from app.hub import Hub
from app.messages import Messages
from app.reconnect import Reconnector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("phone")

STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("PHONE_PASSWORD", "")
# A per-start random key is fine: a restart only means logging in again.
SESSION_SECRET = os.environ.get("PHONE_SESSION_SECRET") or secrets.token_hex(32)
# For the desktop ring agent (agent/phone_agent.pyw): polling whether a
# call is ringing, and signing its embedded window in (/api/agent/session)
# so the popup has no login step. That makes it equivalent to the
# password - it lives only in the desktop user's %APPDATA%.
AGENT_TOKEN = os.environ.get("PHONE_AGENT_TOKEN", "")

DATA = Path(os.environ.get("PHONE_DATA", Path.home() / ".local/share/phone-bridge"))
CONFIG = Path.home() / ".config/phone-bridge"

hub = Hub()
if os.environ.get("PHONE_EXTRAS", "1") == "1":
    # Off in tests (conftest) - these reach for Bluetooth, disk and D-Bus.
    hub.contacts = Contacts(CONFIG / "contacts.json")
    hub.messages = Messages()
    hub.history = CallLog(DATA / "calls.db")
    hub.reconnector = Reconnector(lambda: hub.tel.state.connected)
    hub.write_metrics = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not PASSWORD:
        log.warning("PHONE_PASSWORD is not set - nobody can log in")
    task = asyncio.create_task(hub.run())
    yield
    task.cancel()
    await hub.bridge.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="phone_session",
    max_age=30 * 24 * 3600,
    same_site="strict",
    https_only=True,
)


def check_password(candidate: str) -> bool:
    # Fail closed: no configured password means no login, not an open page.
    return bool(PASSWORD) and secrets.compare_digest(candidate.encode(), PASSWORD.encode())


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
@app.get("/popup")
async def index(request: Request):
    # /popup is the same page; app.js switches to the compact layout the
    # ring agent opens as a small window.
    page = "index.html" if request.session.get("authenticated") else "login.html"
    return FileResponse(STATIC / page, headers={"Cache-Control": "no-store"})


def require_agent(authorization: str = Header("")) -> None:
    expected = f"Bearer {AGENT_TOKEN}"
    if not AGENT_TOKEN or not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401)


def ringing_call():
    return next((c for c in hub.tel.state.calls if c.state in ("incoming", "waiting")), None)


@app.get("/api/agent/ringing", dependencies=[Depends(require_agent)])
async def agent_ringing():
    call = ringing_call()
    live = next((c for c in hub.tel.state.calls if c.state != "disconnected"), None)
    return {
        "ringing": call.__dict__ if call else None,
        # The call the agent's window is for: it shows for every call -
        # ringing, answered on the iPhone, or dialed from it - so its audio
        # can be moved to the PC at any point.
        "call": live.__dict__ if live else None,
        "inCall": live is not None,
        "connected": hub.tel.state.connected,  # for the tray icon
        # A page with PC audio on already rings by itself; the agent stays quiet.
        "audioPages": len(hub.audio_clients),
        # For desktop notifications: the agent remembers which it has shown.
        "missed": hub.history.last_missed() if hub.history else None,
        "texts": list(hub.messages.recent)[:5] if hub.messages else [],
    }


@app.post("/api/agent/session", dependencies=[Depends(require_agent)])
async def agent_session(request: Request):
    # The ring agent's embedded window signs itself in with its token, so
    # there's no login step in the popup. Called with fetch() from the page
    # the agent loaded, so the cookie lands in that window's own profile.
    request.session["authenticated"] = True
    return {"ok": True}


@app.get("/static/{name}")
async def static(name: str):
    path = STATIC / name
    if name not in {"app.js", "worklets.js", "style.css"} or not path.exists():
        return RedirectResponse("/", status_code=303)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    if not check_password(password):
        await asyncio.sleep(1)  # blunt brute-forcing from the LAN
        return RedirectResponse("/?failed=1", status_code=303)
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    # SameSite=Strict keeps the cookie off cross-site requests, but a
    # WebSocket handshake isn't subject to CORS, so check Origin as well.
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    if not websocket.session.get("authenticated") or origin != f"https://{host}":
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await hub.add(websocket)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                hub.audio_in(websocket, message["bytes"])
            elif message.get("text"):
                try:
                    msg = json.loads(message["text"])
                except ValueError:
                    continue
                error = await hub.command(websocket, msg)
                if error:
                    await websocket.send_text(json.dumps({"type": "error", "message": error}))
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(websocket)
