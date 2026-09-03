import secrets

from fastapi import Header, HTTPException

from app.config import API_KEY


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    if x_api_key is None or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return x_api_key
