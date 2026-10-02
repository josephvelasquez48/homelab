"""Run the app locally to look at the pages - no database, no camera.

    cd apps/cam && uv run python -m tests.devserver [port]

On the in-memory FakeStore, seeded with an admin and a viewer, a pending
invite and some viewing history. The camera reads as offline, since the Mac
only accepts the stream login from the cluster nodes.

Passkeys work on http://localhost (browsers treat it as secure), but a
preview pane may have no authenticator to make one with. So this dev server
- and only this file, never the app - adds /dev/signin/<name>, which signs
in as that seeded account directly.
"""
import asyncio
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
os.environ["COOKIE_SECURE"] = "false"
os.environ["PUBLIC_URL"] = f"http://localhost:{PORT}"

import httpx
from fastapi.responses import RedirectResponse
import uvicorn

from app import config, main
from tests.fakes import FakeMediaMTX, FakeStore, SoftAuthenticator


async def seed(store: FakeStore) -> None:
    for name, admin in [("demo", True), ("maria", False)]:
        _, token = await store.create_invite(name, admin, None, 24)
        key = SoftAuthenticator(config.PUBLIC_URL, config.RP_ID)
        await store.accept_invite(token, os.urandom(32), key.credential_id, b"dev", 0,
                                  "iPhone" if admin else "Android", admin)
    await store.create_invite("sam", False, None, 24)
    await store.log_view(await store.user_by_name("maria"), "192.168.1.42")


@main.app.get("/dev/signin/{name}")
async def dev_signin(name: str):
    user = await main.app.state.store.user_by_name(name)
    response = RedirectResponse("/", status_code=303)
    main.set_session_cookie(response, await main.app.state.store.create_session(user.id, 30))
    return response


def run() -> None:
    store = FakeStore()
    asyncio.run(seed(store))
    mediamtx = FakeMediaMTX()
    mediamtx.fail_with = httpx.ConnectError("dev server: no camera")
    main.app.state.store = store
    main.app.state.mediamtx = mediamtx
    uvicorn.run(main.app, host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    run()
