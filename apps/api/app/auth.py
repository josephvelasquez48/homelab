import secrets

from fastapi import Header, HTTPException

from app.config import API_KEY


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    if (
        x_api_key is None
        # Guard on the configured value too, not just the submitted one:
        # compare_digest("", "") is True, so with API_KEY set to an empty
        # string a client sending an empty X-API-Key header would
        # authenticate. config.py requires the variable to exist, but not
        # that it holds anything.
        or not API_KEY
        # compare_digest raises TypeError on non-ASCII str. Header values
        # arrive latin-1 decoded, so any unauthenticated client could send
        # a high byte and turn this 401 into an unhandled 500. Bytes
        # compare cleanly and stay constant-time.
        or not secrets.compare_digest(
            x_api_key.encode("utf-8"), API_KEY.encode("utf-8")
        )
    ):
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return x_api_key
