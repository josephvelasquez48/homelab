"""cam: the webcam viewer, behind per-person accounts signed in with passkeys.

The video itself never passes through here. A browser sends its WebRTC
offer to /api/whep; this checks the session, forwards the offer to MediaMTX
on the Mac with the server-side viewer login, and returns the answer. The
stream then flows directly between the Mac and the browser over UDP. So
this app is the gate - accounts, sessions, the viewing log - and costs
nothing while people watch.

Accounts come from invite links: an admin names a person, gets a link, and
that person makes a passkey on their phone (Face ID, fingerprint, or the
device PIN). There are no passwords - nothing to guess, reuse or phish, and
the database holds only public keys. The same link for an existing name
resets their access: their old passkeys and sessions stop working.

Passkeys belong to one site, so this app has one address: PUBLIC_URL, the
Tailscale Funnel name. cam.home redirects there.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import re
import secrets
import time
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app import config
from app.store import Store, User

STATIC = Path(__file__).parent / "static"
COOKIE = "cam_session"
CEREMONY_COOKIE = "cam_ceremony"
CEREMONY_SECONDS = 300
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


@dataclass
class Ceremony:
    """One passkey sign-in or creation in progress: the challenge the
    authenticator must sign, and what it's for. Kept in memory (one replica)
    for five minutes; a lost one just means pressing the button again."""
    kind: str                      # "login", "invite" or "add"
    challenge: bytes
    expires: float = field(default_factory=lambda: time.monotonic() + CEREMONY_SECONDS)
    invite_token: str | None = None
    user_id: int | None = None
    webauthn_id: bytes | None = None


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
    # when), so only that viewer can end it. In memory: one replica, and a
    # lost entry only means MediaMTX times the session out on its own.
    app.state.whep_sessions = {}
    app.state.ceremonies = {}
    yield
    await app.state.mediamtx.aclose()
    await app.state.store.close()


app = FastAPI(title="Homelab Camera", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def redirect_old_hosts(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if host in config.REDIRECT_HOSTS:
        target = config.PUBLIC_URL + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=308)
    return await call_next(request)


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


# Sign-in throttling, per address only. With passkeys there's no secret to
# guess, so this just caps abuse - and with no per-name limit, nobody can
# lock someone else out by failing in their name (the password version could).
FAILURE_WINDOW = 15 * 60
MAX_FAILURES = 20
_failures: dict[str, list[float]] = {}


def _recent_failures(key: str) -> int:
    cutoff = time.monotonic() - FAILURE_WINDOW
    stamps = [t for t in _failures.get(key, []) if t > cutoff]
    _failures[key] = stamps
    return len(stamps)


def _record_failure(key: str) -> None:
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


def device_name(request: Request) -> str:
    """A label for a new passkey, from the browser's description of itself."""
    ua = request.headers.get("user-agent", "")
    for needle, name in [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                         ("Macintosh", "Mac"), ("Windows", "Windows"), ("CrOS", "Chromebook"),
                         ("Linux", "Linux")]:
        if needle in ua:
            return name
    return "Passkey"


# Passkey ceremonies: options out (a challenge), then the signed answer back.

def start_ceremony(request: Request, response: Response, ceremony: Ceremony) -> None:
    ceremonies = request.app.state.ceremonies
    clock = time.monotonic()
    for stale in [k for k, c in ceremonies.items() if c.expires < clock]:
        del ceremonies[stale]
    state = secrets.token_urlsafe(24)
    ceremonies[state] = ceremony
    response.set_cookie(
        CEREMONY_COOKIE, state, max_age=CEREMONY_SECONDS, httponly=True,
        secure=config.COOKIE_SECURE, samesite="strict", path="/api")


def finish_ceremony(request: Request, kind: str) -> Ceremony:
    """The ceremony this browser started - usable once, then gone."""
    state = request.cookies.get(CEREMONY_COOKIE, "")
    ceremony = request.app.state.ceremonies.pop(state, None)
    if not ceremony or ceremony.kind != kind or ceremony.expires < time.monotonic():
        raise HTTPException(400, "That took too long - try again")
    return ceremony


def options_response(options) -> Response:
    return Response(options_to_json(options), media_type="application/json")


def registration_options(user_name: str, webauthn_id: bytes, exclude: list[bytes] = ()):
    return generate_registration_options(
        rp_id=config.RP_ID,
        rp_name=config.RP_NAME,
        user_name=user_name,
        user_id=webauthn_id,
        user_display_name=user_name,
        # Discoverable, so sign-in needs no name typed; verified, so it
        # takes Face ID / fingerprint / PIN, not just a tap.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=cid) for cid in exclude],
    )


def verify_registration(body: dict, ceremony: Ceremony):
    try:
        return verify_registration_response(
            credential=body,
            expected_challenge=ceremony.challenge,
            expected_rp_id=config.RP_ID,
            expected_origin=config.PUBLIC_URL,
            require_user_verification=True,
        )
    except (InvalidRegistrationResponse, ValueError, KeyError, TypeError):
        raise HTTPException(400, "Your device's passkey didn't check out - try again")


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

@app.post("/api/login/options", dependencies=[Depends(require_same_origin)])
async def login_options(request: Request, response: Response) -> Response:
    if _recent_failures(f"ip:{client_ip(request)}") >= MAX_FAILURES:
        raise HTTPException(429, "Too many attempts - try again in a few minutes")
    options = generate_authentication_options(
        rp_id=config.RP_ID, user_verification=UserVerificationRequirement.REQUIRED)
    reply = options_response(options)
    start_ceremony(request, reply, Ceremony("login", options.challenge))
    return reply


@app.post("/api/login", dependencies=[Depends(require_same_origin)])
async def login(request: Request) -> JSONResponse:
    ip_key = f"ip:{client_ip(request)}"
    if _recent_failures(ip_key) >= MAX_FAILURES:
        raise HTTPException(429, "Too many attempts - try again in a few minutes")
    body = await json_body(request)
    ceremony = finish_ceremony(request, "login")
    try:
        credential_id = base64url_to_bytes(str(body.get("rawId", "")))
    except ValueError:
        credential_id = b""
    found = await store(request).passkey_with_user(credential_id) if credential_id else None
    if not found:
        _record_failure(ip_key)
        raise HTTPException(401, "That passkey doesn't belong to an account here")
    passkey, user = found
    try:
        verified = verify_authentication_response(
            credential=body,
            expected_challenge=ceremony.challenge,
            expected_rp_id=config.RP_ID,
            expected_origin=config.PUBLIC_URL,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError):
        _record_failure(ip_key)
        raise HTTPException(401, "Your passkey didn't check out - try again")
    await store(request).passkey_used(passkey.id, verified.new_sign_count)
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


# Invites: the first passkey for a new account, or a reset for an old one

@app.get("/api/invite/{token}")
async def invite_info(token: str, request: Request) -> dict:
    invite = await store(request).invite_by_token(token)
    if not invite:
        raise HTTPException(404, "This link has expired or was already used")
    existing = await store(request).user_by_name(invite.username)
    return {"username": invite.username, "reset": bool(existing)}


@app.post("/api/invite/{token}/options", dependencies=[Depends(require_same_origin)])
async def invite_options(token: str, request: Request) -> Response:
    invite = await store(request).invite_by_token(token)
    if not invite:
        raise HTTPException(404, "This link has expired or was already used")
    existing = await store(request).user_by_name(invite.username)
    webauthn_id = (existing.webauthn_id if existing else None) or secrets.token_bytes(32)
    options = registration_options(invite.username, webauthn_id)
    reply = options_response(options)
    start_ceremony(request, reply, Ceremony("invite", options.challenge, invite_token=token,
                                            webauthn_id=webauthn_id))
    return reply


@app.post("/api/invite/{token}", dependencies=[Depends(require_same_origin)])
async def accept_invite(token: str, request: Request) -> JSONResponse:
    body = await json_body(request)
    ceremony = finish_ceremony(request, "invite")
    if ceremony.invite_token != token:
        raise HTTPException(400, "That took too long - try again")
    verified = verify_registration(body, ceremony)
    try:
        user = await store(request).accept_invite(
            token, ceremony.webauthn_id, verified.credential_id, verified.credential_public_key,
            verified.sign_count, device_name(request), verified.credential_backed_up)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "That passkey is already registered")
    if not user:
        raise HTTPException(404, "This link has expired or was already used")
    if user.disabled:
        raise HTTPException(403, "This account is turned off")
    session = await store(request).create_session(user.id, config.SESSION_DAYS)
    response = JSONResponse({"user": user.public()})
    set_session_cookie(response, session)
    return response


# Your own passkeys

@app.get("/api/account/passkeys")
async def my_passkeys(request: Request, user: User = Depends(require_user)) -> dict:
    return {"passkeys": [p.public() for p in await store(request).passkeys_for(user.id)]}


@app.post("/api/account/passkeys/options", dependencies=[Depends(require_same_origin)])
async def add_passkey_options(request: Request, user: User = Depends(require_user)) -> Response:
    # Accounts from before passkeys have no user handle yet; their
    # still-valid session is enough to add their first passkey.
    webauthn_id = user.webauthn_id or await store(request).set_webauthn_id(user.id, secrets.token_bytes(32))
    existing = [p.id for p in await store(request).passkeys_for(user.id)]
    options = registration_options(user.username, webauthn_id, exclude=existing)
    reply = options_response(options)
    start_ceremony(request, reply, Ceremony("add", options.challenge, user_id=user.id))
    return reply


@app.post("/api/account/passkeys", dependencies=[Depends(require_same_origin)])
async def add_passkey(request: Request, user: User = Depends(require_user)) -> dict:
    body = await json_body(request)
    ceremony = finish_ceremony(request, "add")
    if ceremony.user_id != user.id:
        raise HTTPException(400, "That took too long - try again")
    verified = verify_registration(body, ceremony)
    try:
        await store(request).add_passkey(
            user.id, verified.credential_id, verified.credential_public_key, verified.sign_count,
            device_name(request), verified.credential_backed_up)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "That passkey is already registered")
    return {"ok": True}


@app.delete("/api/account/passkeys/{passkey_id}", dependencies=[Depends(require_same_origin)])
async def remove_passkey(passkey_id: str, request: Request, user: User = Depends(require_user)) -> dict:
    try:
        credential_id = base64url_to_bytes(passkey_id)
    except ValueError:
        raise HTTPException(404, "No such passkey")
    if not await store(request).delete_passkey(user.id, credential_id):
        raise HTTPException(400, "That's your only passkey - add another before removing it")
    return {"ok": True}


# Admin

@app.get("/api/admin/users")
async def admin_users(request: Request, admin: User = Depends(require_admin)) -> dict:
    users = await store(request).list_users()
    invites = await store(request).list_invites()
    return {"users": [u.public() for u in users], "invites": [i.public() for i in invites],
            "invite_hours": config.INVITE_HOURS}


@app.post("/api/admin/invites", dependencies=[Depends(require_same_origin)])
async def admin_invite(request: Request, admin: User = Depends(require_admin)) -> dict:
    body = await json_body(request)
    username = str(body.get("username", "")).strip().lower()
    if not USERNAME.match(username):
        raise HTTPException(400, "Names are 2-32 characters: letters, numbers, dot, dash, underscore")
    if await store(request).user_by_name(username):
        raise HTTPException(409, f"{username} already has an account - use Reset access instead")
    invite, token = await store(request).create_invite(
        username, bool(body.get("is_admin")), admin.id, config.INVITE_HOURS)
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
        target.username, target.is_admin, admin.id, config.INVITE_HOURS)
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
