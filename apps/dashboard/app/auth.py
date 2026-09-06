"""Session auth for the state-changing endpoints.

The dashboard is a browser app served from this same origin, so the API's
X-API-Key pattern (apps/api/app/auth.py) does not transfer here: any key
this page could send would have to be embedded in JavaScript that anyone
able to load the dashboard can read, which makes it a public string, not
a credential. A signed HttpOnly session cookie keeps the secret out of
the page entirely - the browser holds it, the JS never sees it.

Authorization (require_session) is the boundary. require_json is a second
layer behind it, not a substitute: it rejects the CORS-simple POST that
would otherwise let any page on the internet trigger gaming mode through
a LAN user's browser.
"""
import secrets

from fastapi import HTTPException, Request

from app.config import DASHBOARD_PASSWORD


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


async def require_session(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="authentication required")


async def require_json(request: Request) -> None:
    """Reject requests that qualify as CORS-simple.

    A bodyless cross-origin `fetch(url, {method: 'POST'})` is not
    preflighted, so SameSite is the only thing standing between a hostile
    page and this endpoint. Requiring application/json forces a preflight,
    and this app registers no CORS middleware, so that preflight has
    nothing to succeed against.
    """
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=415, detail="Content-Type: application/json required"
        )


def check_password(candidate: str) -> bool:
    # Fail closed. With no password configured the comparison never runs
    # and this returns False, so nobody can log in and the gaming
    # endpoints stay unreachable - a missing Secret must not mean
    # "no auth". Note the guard is on the configured value, not on the
    # candidate: an empty submitted password still has to match a
    # configured one, and cannot short-circuit its way to True.
    if not DASHBOARD_PASSWORD:
        return False
    # compare_digest raises TypeError on non-ASCII str, so a password
    # with an accented character would 500 rather than compare. Bytes
    # sidestep that while keeping the constant-time comparison.
    return secrets.compare_digest(
        candidate.encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8")
    )
