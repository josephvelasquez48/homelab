"""Run the app locally to look at the pages - no database, no camera.

    cd apps/cam && uv run python -m tests.devserver [port]

On the in-memory FakeStore, seeded with an admin and a viewer (the test
logins below), a pending invite and some viewing history. The camera
reads as offline, since the Mac only accepts the stream login from the
cluster nodes.
"""
import asyncio
import os
import sys

os.environ["COOKIE_SECURE"] = "false"
os.environ["PUBLIC_URL"] = "http://localhost:8767"

import httpx
import uvicorn

from app import main
from tests.fakes import FakeMediaMTX, FakeStore

# Test-only logins for this in-memory store. Never used anywhere real.
DEV_ADMIN = ("demo", "demo-password-123")
DEV_VIEWER = ("maria", "maria-password-123")


async def seed(store: FakeStore) -> None:
    for (name, password), admin in [(DEV_ADMIN, True), (DEV_VIEWER, False)]:
        _, token = await store.create_invite(name, admin, None, 7)
        await store.accept_invite(token, main.hasher.hash(password))
    await store.create_invite("sam", False, None, 7)
    maria = await store.user_by_name("maria")
    await store.log_view(maria, "192.168.1.42")


def run(port: int) -> None:
    store = FakeStore()
    asyncio.run(seed(store))
    mediamtx = FakeMediaMTX()
    mediamtx.fail_with = httpx.ConnectError("dev server: no camera")
    main.app.state.store = store
    main.app.state.mediamtx = mediamtx
    uvicorn.run(main.app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8767)
