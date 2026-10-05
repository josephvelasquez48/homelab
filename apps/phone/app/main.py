"""Phone bridge web app: https://phone.home:8443.

Runs on the Pi as a systemd *user* service, not in K3s: it needs the
Pi's Bluetooth radio and the logged-in user's PipeWire session, neither
of which a pod can reach without giving it the host. See docs/phone.md.
"""
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from app.contacts import Contacts
from app.drops import MAX_FILE, MAX_TEXT, DropError, Drops
from app.guests import LinkError, Links
from app.history import CallLog
from app.hub import Hub
from app.media import CHANNELS, RATE, MediaBridge
from app.reconnect import Reconnector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("phone")

STATIC = Path(__file__).parent / "static"
# RNNoise, vendored from @sapphi-red/web-noise-suppressor (MIT, see
# static/rnnoise-LICENSE.txt): the mic's noise filter in the page.
NOISE_FILTER_FILES = {"rnnoise-worklet.js", "rnnoise.wasm", "rnnoise_simd.wasm"}
# Add to Home Screen: the manifest and its icons. Served without a login -
# Safari fetches them before anyone has signed in, and they say nothing.
APP_FILES = {"manifest.webmanifest", "icon-180.png", "icon-512.png"}
PASSWORD = os.environ.get("PHONE_PASSWORD", "")
# A per-start random key is fine: a restart only means logging in again.
SESSION_SECRET = os.environ.get("PHONE_SESSION_SECRET") or secrets.token_hex(32)
# For the desktop ring agent (agent/phone_agent.pyw): polling whether a
# call is ringing, and signing its embedded window in (/api/agent/session)
# so the popup has no login step. That makes it equivalent to the
# password - it lives only in the desktop user's %APPDATA%.
AGENT_TOKEN = os.environ.get("PHONE_AGENT_TOKEN", "")
# For the iPhone's "Send to PC" Shortcut (docs/phone.md): it can only make
# drops, so a copy on the phone gives away nothing else.
DROP_TOKEN = os.environ.get("PHONE_DROP_TOKEN", "")
# Send links (app/guests.py) are served on their own listener: plain HTTP,
# home network only (ufw, and on_home_network below), at the Pi's IP - a
# guest's phone neither trusts the homelab CA nor always uses the Pi's DNS.
GUEST_PORT = int(os.environ.get("PHONE_GUEST_PORT", "8081"))
GUEST_URL = os.environ.get("PHONE_GUEST_URL", f"http://192.168.1.253:{GUEST_PORT}").rstrip("/")
HOME_NETWORK = ipaddress.ip_network(os.environ.get("PHONE_HOME_NETWORK", "192.168.1.0/24"))
# One guest send, whole request: a few photos, not a film.
MAX_GUEST_REQUEST = 100 * 1024 * 1024

DATA = Path(os.environ.get("PHONE_DATA", Path.home() / ".local/share/phone-bridge"))
CONFIG = Path.home() / ".config/phone-bridge"

hub = Hub()
if os.environ.get("PHONE_EXTRAS", "1") == "1":
    # Off in tests (conftest) - these reach for Bluetooth, disk and D-Bus.
    hub.contacts = Contacts(CONFIG / "contacts.json")
    hub.history = CallLog(DATA / "calls.db")
    hub.reconnector = Reconnector(lambda: hub.tel.state.connected, hub.pc_present, lambda: hub.settings["mediaOnPc"])
    hub.media = MediaBridge()
    hub.write_metrics = True
    hub.drops = Drops(DATA / "drops")
    hub.links = Links(DATA / "send-links.json")
    hub.links_base = GUEST_URL


class _GuestServer(uvicorn.Server):
    """The send-link listener, inside this process so guests' drops land in
    the same store. It leaves signal handling to the main server."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not PASSWORD:
        log.warning("PHONE_PASSWORD is not set - nobody can log in")
    task = asyncio.create_task(hub.run())
    guests = None
    if hub.links and GUEST_PORT:  # 0: no guest listener (tests)
        guests = _GuestServer(uvicorn.Config(guest_app, host="0.0.0.0", port=GUEST_PORT,
                                             log_level="warning", access_log=False, server_header=False))
        guests_task = asyncio.create_task(guests.serve())
    yield
    if guests:
        guests.should_exit = True
        await guests_task
    task.cancel()
    await hub.bridge.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Send links only: /send/<token>, and nothing else - no session, no static
# files, no API. Served by _GuestServer on GUEST_PORT.
guest_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="phone_session",
    max_age=30 * 24 * 3600,
    same_site="strict",
    https_only=True,
)


def on_home_network(client) -> bool:
    """A send link's guest is on the home Wi-Fi. ufw already allows only the
    LAN to the guest port; this is the same rule, held by the app too."""
    try:
        return bool(client) and ipaddress.ip_address(client.host) in HOME_NETWORK
    except ValueError:
        return False


def check_password(candidate: str) -> bool:
    # Fail closed: no configured password means no login, not an open page.
    return bool(PASSWORD) and secrets.compare_digest(candidate.encode(), PASSWORD.encode())


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def is_local(client) -> bool:
    """The Pi's own touchscreen. Chromium there maps phone.home to 127.0.0.1
    (phone-screen.service), so its connections arrive on loopback, which no
    other machine can reach; the certificate still matches phone.home."""
    return bool(client) and client.host in ("127.0.0.1", "::1")


@app.get("/")
@app.get("/popup")
async def index(request: Request):
    # /popup is the same page; app.js switches to the compact layout the
    # ring agent opens as a small window (and ?touch=1 for the Pi's screen).
    signed_in = request.session.get("authenticated") or is_local(request.client)
    page = "index.html" if signed_in else "login.html"
    return FileResponse(STATIC / page, headers={"Cache-Control": "no-store"})


def require_agent(authorization: str = Header("")) -> None:
    expected = f"Bearer {AGENT_TOKEN}"
    if not AGENT_TOKEN or not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401)


def require_drop_token(authorization: str = Header("")) -> None:
    expected = f"Bearer {DROP_TOKEN}"
    if not DROP_TOKEN or not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401)


def require_page(request: Request) -> None:
    """Signed in, or the Pi's own screen - what the page itself needs."""
    if not (request.session.get("authenticated") or is_local(request.client)):
        raise HTTPException(status_code=401)


def ringing_call():
    return next((c for c in hub.tel.state.calls if c.state in ("incoming", "waiting")), None)


@app.get("/api/agent/ringing", dependencies=[Depends(require_agent)])
async def agent_ringing(screen: bool = False):
    # The Pi's own screen watcher polls here too (screen=1); only the PC's
    # agent means the PC is on.
    if not screen:
        hub.pc_seen()
    call = ringing_call()
    live = next((c for c in hub.tel.state.calls if c.state != "disconnected"), None)
    return {
        "ringing": call.__dict__ if call else None,
        # The call the agent's window is for: it shows for every call -
        # ringing, answered on the iPhone, or dialed from it - so its audio
        # can be moved to the PC at any point.
        "call": live.__dict__ if live else None,
        "inCall": live is not None,
        # Home was tapped on the Pi's call screen for the calls up now: its
        # watcher keeps the screen closed (screen/phone_screen.py).
        "screenHidden": hub.screen_hidden(),
        "connected": hub.tel.state.connected,  # for the tray icon
        # A page with PC audio on already rings by itself; the agent stays quiet.
        "audioPages": len(hub.audio_clients),
        # For the missed-call notification: the agent remembers which it has shown.
        "missed": hub.history.last_missed() if hub.history else None,
        # Newest drop: the agent fetches /api/agent/drops when it moves.
        "dropLatest": hub.drops.latest_created() if hub.drops else 0,
    }


@app.post("/api/agent/screen-show", dependencies=[Depends(require_agent)])
async def agent_screen_show():
    # The display's Call button (apps/pi-display/show-call.sh): bring the
    # Pi's call screen back after Home sent it away.
    hub.show_screen()
    return {"screenHidden": hub.screen_hidden()}


@app.get("/api/agent/media", dependencies=[Depends(require_agent)])
async def agent_media():
    """The phone's music and videos as raw PCM, for the agent's player.

    204 while media is set to play on the iPhone (the agent asks again in
    a few seconds). Otherwise an endless response: s16le chunks while the
    phone plays, nothing while it doesn't; it ends when the switch goes off.
    """
    if not hub.media or not hub.settings["mediaOnPc"]:
        return Response(status_code=204)
    q = hub.media.subscribe()

    async def chunks():
        try:
            while (chunk := await q.get()) is not None:
                yield chunk
        finally:
            hub.media.unsubscribe(q)

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"X-Audio-Format": f"s16le; rate={RATE}; channels={CHANNELS}", "Cache-Control": "no-store"},
    )


# ---- Drop (app/drops.py) ----------------------------------------------------


def _drops() -> Drops:
    if not hub.drops:
        raise HTTPException(status_code=404)
    return hub.drops


async def _add_drops(text: str, files: list[UploadFile], source: str, text_files_are_text: bool = False) -> list[dict]:
    drops, made = _drops(), []
    try:
        for f in files:
            data = await f.read(MAX_FILE + 1)
            if text_files_are_text and (f.content_type or "").startswith("text/plain") and len(data) <= MAX_TEXT:
                made.append(drops.add_text(data.decode("utf-8", "replace"), source))
                continue
            made.append(drops.add_file(f.filename or "file", f.content_type, data, source))
        if text.strip() or not files:
            made.append(drops.add_text(text, source))
    except DropError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if made:
            await hub.broadcast_extras()
    return [d.__dict__ for d in made]


def _file_response(drop_id: str, download: bool) -> FileResponse:
    drop = _drops().get(drop_id)
    if not drop or drop.kind != "file":
        raise HTTPException(status_code=404)
    return FileResponse(
        _drops().file_path(drop), media_type=drop.mime, filename=drop.name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/api/drops", dependencies=[Depends(require_page)])
async def page_drop(text: str = Form(""), source: str = Form("pc"), files: list[UploadFile] = File(default=[])):
    """From the page: the iPhone (source=phone) or the PC (source=pc)."""
    return {"made": await _add_drops(text, files, source)}


@app.post("/api/shortcut/drop", dependencies=[Depends(require_drop_token)])
async def shortcut_drop(text: str = Form(""), files: list[UploadFile] = File(default=[])):
    """The iPhone's "Send to PC" Shortcut: text, photos or files from the
    share sheet, always from the phone. Shortcuts sends shared text as a
    plain-text file, so here that counts as text (onto the PC's clipboard)."""
    return {"made": await _add_drops(text, files, "phone", text_files_are_text=True)}


@app.get("/api/drops/{drop_id}/file", dependencies=[Depends(require_page)])
async def page_drop_file(drop_id: str, download: bool = False):
    return _file_response(drop_id, download)


@app.delete("/api/drops/{drop_id}", dependencies=[Depends(require_page)])
async def page_drop_delete(drop_id: str):
    if not _drops().delete(drop_id):
        raise HTTPException(status_code=404)
    await hub.broadcast_extras()
    return {"ok": True}


# ---- Send links (app/guests.py): the only public part ---------------------------


def _links() -> Links:
    if not hub.links:
        raise HTTPException(status_code=404)
    return hub.links


@app.post("/api/links", dependencies=[Depends(require_page)])
async def link_create(label: str = Form("")):
    link = _links().create(label)
    await hub.broadcast_extras()
    return {"url": f"{GUEST_URL}/send/{link.token}", "expires": link.expires}


@app.delete("/api/links/{token}", dependencies=[Depends(require_page)])
async def link_revoke(token: str):
    if not _links().revoke(token):
        raise HTTPException(status_code=404)
    await hub.broadcast_extras()
    return {"ok": True}


@app.get("/api/agent/drops", dependencies=[Depends(require_agent)])
async def agent_drops(since: float = 0):
    """Drops from the phone the PC's agent hasn't handled yet."""
    return {"drops": _drops().for_pc_since(since)}


@app.get("/api/agent/drops/{drop_id}/file", dependencies=[Depends(require_agent)])
async def agent_drop_file(drop_id: str):
    return _file_response(drop_id, download=True)


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
    if name not in {"app.js", "worklets.js", "style.css", *NOISE_FILTER_FILES, *APP_FILES} or not path.exists():
        return RedirectResponse("/", status_code=303)
    media = "application/manifest+json" if name.endswith(".webmanifest") else None
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache"})


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


def same_origin(headers) -> bool:
    """The page opening the socket is this app's own. Through Tailscale
    (docs/phone.md, Drop) the page's address may arrive as X-Forwarded-Host;
    a web page can't set that header on a WebSocket, so trusting it keeps
    the cross-site check intact."""
    hosts = {headers.get("host", ""), headers.get("x-forwarded-host", "")} - {""}
    return headers.get("origin", "") in {f"https://{h}" for h in hosts}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    # SameSite=Strict keeps the cookie off cross-site requests, but a
    # WebSocket handshake isn't subject to CORS, so check Origin as well.
    local = is_local(websocket.client)
    if not (websocket.session.get("authenticated") or local) or not same_origin(websocket.headers):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await hub.add(websocket, local=local)
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


# ---- Send links: the guest listener (app/guests.py) ----------------------------


GUEST_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",  # the link is the key: never send it on
    "X-Frame-Options": "DENY",
}


@guest_app.get("/send/{token}")
async def guest_page(token: str, request: Request):
    if not on_home_network(request.client):
        raise HTTPException(status_code=403)
    page = "send.html" if hub.links and hub.links.live(token) else "send-expired.html"
    return FileResponse(STATIC / page, headers=GUEST_HEADERS, status_code=200 if page == "send.html" else 404)


@guest_app.post("/send/{token}")
async def guest_send(token: str, request: Request):
    """A guest's send: name, text and files. Checked against the link's caps
    before the body is read, so an oversized upload never reaches the disk."""
    if not on_home_network(request.client):
        raise HTTPException(status_code=403)
    link = hub.links.live(token) if hub.links else None
    if not link:
        raise HTTPException(status_code=404, detail="This link has expired. Ask for a new one.")
    length = int(request.headers.get("content-length") or 0)
    if not length or length > MAX_GUEST_REQUEST:
        raise HTTPException(status_code=413, detail=f"That's too much at once - up to {MAX_GUEST_REQUEST // 1024 // 1024} MB a send.")
    try:
        hub.links.check_send(link, length)
    except LinkError as e:
        raise HTTPException(status_code=429, detail=str(e))
    form = await request.form(max_files=20, max_fields=10)
    sender = " ".join(str(form.get("name") or "").split())[:40] or "Guest"
    text = str(form.get("text") or "")
    files = [f for f in form.getlist("files") if hasattr(f, "read")]
    drops, made = _drops(), []
    try:
        for f in files:
            made.append(drops.add_file(f.filename or "file", f.content_type, await f.read(MAX_FILE + 1), "guest", sender))
        if text.strip() or not files:
            made.append(drops.add_text(text, "guest", sender))
    except DropError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await form.close()
        if made:
            hub.links.record_send(link, length)
            await hub.broadcast_extras()
    return {"sent": len(made)}
