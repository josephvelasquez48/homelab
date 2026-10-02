"""Make an invite link from the command line - for the first admin.

The admin page makes every other invite, but someone has to be an admin
first. Run inside the pod, so it uses the pod's database login:

    kubectl -n cam exec deploy/cam -- python -m app.cli invite <name> --admin

It prints a link; open it and choose your own password. The link works
once, for INVITE_DAYS days.
"""
import argparse
import asyncio

from app import config
from app.main import USERNAME
from app.store import Store


async def invite(username: str, is_admin: bool) -> None:
    store = await Store.connect(config.DATABASE_URL)
    try:
        _, token = await store.create_invite(username, is_admin, None, config.INVITE_DAYS)
    finally:
        await store.close()
    kind = "Admin invite" if is_admin else "Invite"
    print(f"{kind} for {username}, valid {config.INVITE_DAYS} days:")
    print(f"{config.PUBLIC_URL}/invite/{token}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("invite", help="print a one-time invite link")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    username = args.username.strip().lower()
    if not USERNAME.match(username):
        parser.error("names are 2-32 characters: letters, numbers, dot, dash, underscore")
    asyncio.run(invite(username, args.admin))


if __name__ == "__main__":
    main()
