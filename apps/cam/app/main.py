"""cam.home: the webcam viewer, behind per-person accounts.

The video itself never passes through here. A browser sends its WebRTC
offer to /api/whep; this checks the session, forwards the offer to MediaMTX
on the Mac with the server-side viewer login, and returns the answer. The
stream then flows directly between the Mac and the browser over UDP. So
this app is the gate - accounts, sessions, the viewing log - and costs
nothing while people watch.

Accounts come from invite links: an admin names a person, gets a link, and
that person sets their own password. The same link for an existing name is
a password reset. No password is ever chosen for someone else or sent to
them.
"""
from contextlib import asynccontextmanager
import re
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx

from app import config
from app.store import Store, User

STATIC = Path(__file__).parent / "static"
COOKIE = "cam_session"
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 10

hasher = PasswordHasher()
# Verifying against this when the name doesn't exist keeps a failed login
# for an unknown name as slow as one for a real name, so timing doesn't
# reveal who has an account.
DUMMY_HASH = hasher.hash("not a real password")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "store"):
        app.state.store = await Store.connect(config.DATABASE_URL)
    if not hasattr(app.state, "mediamtx"):
        app.state.mediamtx = httpx.AsyncClient(
            base_url=config.MEDIAMTX_URL,
            auth=(config.MEDIAMTX_USER, config.MEDIAMTX_PASSWORD),
            # The Mac answers in milliseconds when it's up. Connect fails
            # fast so a sleeping or unplugged Mac shows as "camera offline"
            # instead of a spinner; read allows for MediaMTX starting the
            # camera, which it does on the first viewer (up to ~2 s).
            timeout=httpx.Timeout(15.0, connect=3.0),
        )
    # WHEP sessions this app created: MediaMTX's session id -> (who owns it,
    # when), so only that viewer can end it. In memory: one replica, and a lost
    # entry only means MediaMTX times the session out on its own.
    app.state.whep_sessions = {}
    yield
    await app.state.mediamtx.aclose()
    await app.state.store.close()


app = FastAPI(title="Homelab Camera", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # Nothing inline anywhere, so the policy can be strict: a script that
    # isn't one of our files can't run, whatever ends up in the page.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Invite links carry their token in the path; never hand it to anyone.
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# Login throttling: per address and per name, in memory (one replica).
# Five failures in fifteen minutes locks that address or name out for the
# rest of the window.
FAILURE_WINDOW = 15 * 60
MAX_FAILURES = 5
_failures: dict[str, list[float]] = {}


def _recent_failures(key: str) -> int:
    cutoff = time.monotonic() - FAILURE_WINDOW
    stamps = [t for t in _failures.get(key, []) if t > cutoff]
    _failures[key] = stamps
    return len(stamps)


def _record_failure(*keys: str) -> None:
    for key in keys:
        _failures.setdefault(key, []).append(time.monotonic())


def store(request: Request) -> Store:
    return request.app.state.store


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def current_user(request: Request) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    return await store(request).session_user(token, config.SESSION_DAYS)


async def require_user(request: Request) -> User:
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Sign in first")
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admins only")
    return user


def require_same_origin(request: Request) -> None:
    """Every state change needs this header.

    The session cookie is SameSite=Strict, which already keeps it off
    requests started by other sites. The header is the second lock: a
    cross-origin page can't add a custom header without a CORS preflight,
    and this app answers no preflights.
    """
    if request.headers.get("x-requested-with") != "cam":
        raise HTTPException(403, "Missing X-Requested-With")


async def json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True,
        secure=config.COOKIE_SECURE, samesite="strict", path="/")


def check_password_rules(password: object) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Use at least {MIN_PASSWORD} characters")
    if len(password) > 256:
        raise HTTPException(400, "That password is too long")
    return password


# Pages. Each is a static file; the scripts they load ask /api/me who's
# signed in. The server-side redirects only save a flash of the wrong page.

def page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health(request: Request) -> dict:
    await store(request).ping()
    return {"status": "ok"}


@app.get("/")
async def viewer_page(request: Request):
    if not await current_user(request):
        return RedirectResponse("/login", status_code=303)
    return page("viewer.html")


@app.get("/login")
async def login_page(request: Request):
    if await current_user(request):
        return RedirectResponse("/", status_code=303)
    return page("login.html")


@app.get("/invite/{token}")
async def invite_page(token: str):
    return page("invite.html")


@app.get("/account")
async def account_page(request: Request):
    if not await current_user(request):
        return RedirectResponse("/login", status_code=303)
    return page("account.html")


@app.get("/admin")
async def admin_page(request: Request):
    user = await current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return RedirectResponse("/", status_code=303)
    return page("admin.html")


# Signing in and out

@app.post("/api/login", dependencies=[Depends(require_same_origin)])
async def login(request: Request) -> JSONResponse:
    body = await json_body(request)
    username = str(body.get("username", "")).strip().lower()
    password = body.get("password")
    ip_key, name_key = f"ip:{client_ip(request)}", f"name:{username}"
    if _recent_failures(ip_key) >= MAX_FAILURES or _recent_failures(name_key) >= MAX_FAILURES:
        raise HTTPException(429, "Too many attempts - try again in a few minutes")

    user = await store(request).user_by_name(username) if USERNAME.match(username) else None
    try:
        hasher.verify(user.password_hash if user and user.password_hash else DUMMY_HASH, str(password or ""))
        ok = bool(user and user.password_hash and not user.disabled)
    except (VerifyMismatchError, InvalidHashError):
        ok = False
    if not ok:
        _record_failure(ip_key, name_key)
        raise HTTPException(401, "Wrong name or password")

    if hasher.check_needs_rehash(user.password_hash):
        await store(request).set_password(user.id, hasher.hash(password))
    token = await store(request).create_session(user.id, config.SESSION_DAYS)
    response = JSONResponse({"user": user.public()})
    set_session_cookie(response, token)
    return response


@app.post("/api/logout", dependencies=[Depends(require_same_origin)])
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(COOKIE)
    if token:
        await store(request).delete_session(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/api/me")
async def me(user: User = Depends(require_user)) -> dict:
    return {"user": user.public()}


@app.post("/api/account/password", dependencies=[Depends(require_same_origin)])
async def change_password(request: Request, user: User = Depends(require_user)) -> dict:
    body = await json_body(request)
    full = await store(request).user_by_id(user.id)
    try:
        hasher.verify(full.password_hash or DUMMY_HASH, str(body.get("current") or ""))
    except (VerifyMismatchError, InvalidHashError):
        raise HTTPException(400, "Your current password isn't right")
    new = check_password_rules(body.get("new"))
    await store(request).set_password(user.id, hasher.hash(new))
    # Everywhere else is signed out; this browser stays in.
    await store(request).delete_other_sessions(user.id, request.cookies.get(COOKIE))
    return {"ok": True}


# Invites

@app.get("/api/invite/{token}")
async def invite_info(token: str, request: Request) -> dict:
    invite = await store(request).invite_by_token(token)
    if not invite:
        raise HTTPException(404, "This link has expired or was already used")
    existing = await store(request).user_by_name(invite.username)
    return {"username": invite.username, "reset": bool(existing and existing.password_hash)}


@app.post("/api/invite/{token}", dependencies=[Depends(require_same_origin)])
async def accept_invite(token: str, request: Request) -> JSONResponse:
    body = await json_body(request)
    password = check_password_rules(body.get("password"))
    user = await store(request).accept_invite(token, hasher.hash(password))
    if not user:
        raise HTTPException(404, "This link has expired or was already used")
    if user.disabled:
        raise HTTPException(403, "This account is turned off")
    session = await store(request).create_session(user.id, config.SESSION_DAYS)
    response = JSONResponse({"user": user.public()})
    set_session_cookie(response, session)
    return response


# Admin

@app.get("/api/admin/users")
async def admin_users(request: Request, admin: User = Depends(require_admin)) -> dict:
    users = await store(request).list_users()
    invites = await store(request).list_invites()
    return {"users": [u.public() for u in users], "invites": [i.public() for i in invites]}


@app.post("/api/admin/invites", dependencies=[Depends(require_same_origin)])
async def admin_invite(request: Request, admin: User = Depends(require_admin)) -> dict:
    body = await json_body(request)
    username = str(body.get("username", "")).strip().lower()
    if not USERNAME.match(username):
        raise HTTPException(400, "Names are 2-32 characters: letters, numbers, dot, dash, underscore")
    if await store(request).user_by_name(username):
        raise HTTPException(409, f"{username} already has an account - use Reset password instead")
    invite, token = await store(request).create_invite(
        username, bool(body.get("is_admin")), admin.id, config.INVITE_DAYS)
    return {"invite": invite.public(), "url": f"{config.PUBLIC_URL}/invite/{token}"}


@app.delete("/api/admin/invites/{invite_id}", dependencies=[Depends(require_same_origin)])
async def admin_revoke_invite(invite_id: int, request: Request, admin: User = Depends(require_admin)) -> dict:
    await store(request).delete_invite(invite_id)
    return {"ok": True}


async def _target(request: Request, user_id: int, admin: User, action: str) -> User:
    target = await store(request).user_by_id(user_id)
    if not target:
        raise HTTPException(404, "No such account")
    if target.id == admin.id and action != "reset":
        raise HTTPException(400, f"You can't {action} your own account")
    return target


@app.post("/api/admin/users/{user_id}/reset", dependencies=[Depends(require_same_origin)])
async def admin_reset(user_id: int, request: Request, admin: User = Depends(require_admin)) -> dict:
    target = await _target(request, user_id, admin, "reset")
    invite, token = await store(request).create_invite(
        target.username, target.is_admin, admin.id, config.INVITE_DAYS)
    return {"invite": invite.public(), "url": f"{config.PUBLIC_URL}/invite/{token}"}


@app.post("/api/admin/users/{user_id}/disable", dependencies=[Depends(require_same_origin)])
async def admin_disable(user_id: int, request: Request, admin: User = Depends(require_admin)) -> dict:
    await _target(request, user_id, admin, "turn off")
    await store(request).set_disabled(user_id, True)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/enable", dependencies=[Depends(require_same_origin)])
async def admin_enable(user_id: int, request: Request, admin: User = Depends(require_admin)) -> dict:
    await _target(request, user_id, admin, "turn on")
    await store(request).set_disabled(user_id, False)
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_same_origin)])
async def admin_delete(user_id: int, request: Request, admin: User = Depends(require_admin)) -> dict:
    await _target(request, user_id, admin, "delete")
    await store(request).delete_user(user_id)
    return {"ok": True}


@app.get("/api/admin/views")
async def admin_views(request: Request, admin: User = Depends(require_admin)) -> dict:
    return {"views": await store(request).recent_views()}


# The stream: WHEP signaling, relayed to MediaMTX

@app.post("/api/whep", dependencies=[Depends(require_same_origin)])
async def whep_offer(request: Request, user: User = Depends(require_user)) -> Response:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/sdp":
        raise HTTPException(415, "Expected application/sdp")
    offer = await request.body()
    if not offer or len(offer) > 64_000:
        raise HTTPException(400, "Bad offer")
    try:
        upstream = await request.app.state.mediamtx.post(
            f"/{config.MEDIAMTX_PATH}/whep", content=offer, headers={"Content-Type": "application/sdp"})
    except httpx.HTTPError:
        raise HTTPException(503, "The camera is offline - the Mac may be asleep or off")
    if upstream.status_code == 404:
        raise HTTPException(503, "The camera isn't streaming - is it plugged in?")
    if upstream.status_code != 201:
        raise HTTPException(502, f"The camera server answered {upstream.status_code}")

    # MediaMTX's Location is /<path>/whep/<session id>; keep the id, and point
    # the browser at our own endpoint for ending it.
    session_id = upstream.headers.get("location", "").rstrip("/").rsplit("/", 1)[-1]
    if session_id:
        sessions = request.app.state.whep_sessions
        # A closed tab never sends its DELETE, so entries are dropped after
        # a day rather than kept forever.
        cutoff = time.monotonic() - 86400
        for stale in [k for k, (_, at) in sessions.items() if at < cutoff]:
            del sessions[stale]
        sessions[session_id] = (user.id, time.monotonic())
    await store(request).log_view(user, client_ip(request))
    return Response(
        content=upstream.content, status_code=201, media_type="application/sdp",
        headers={"Location": f"/api/whep/{session_id}"} if session_id else {})


@app.delete("/api/whep/{session_id}", dependencies=[Depends(require_same_origin)])
async def whep_end(session_id: str, request: Request, user: User = Depends(require_user)) -> Response:
    owner, _ = request.app.state.whep_sessions.get(session_id, (None, None))
    if owner is None:
        return Response(status_code=204)
    if owner != user.id:
        raise HTTPException(403, "Not your stream")
    del request.app.state.whep_sessions[session_id]
    try:
        await request.app.state.mediamtx.delete(f"/{config.MEDIAMTX_PATH}/whep/{session_id}")
    except httpx.HTTPError:
        pass  # MediaMTX ends it anyway once the browser stops sending
    return Response(status_code=204)
