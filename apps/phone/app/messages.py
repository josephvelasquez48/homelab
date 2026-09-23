"""New text messages from the iPhone (Bluetooth MAP), for notifications.

iOS offers MAP the way it does to car kits: only while "Show
Notifications" is on for this device, and only for messages that arrive
while it's connected - it isn't a way to read message history. So this
is a notifier: poll the inbox listing every 20 s, treat everything in
the first listing as already seen, and report handles that appear after.
The listing's Subject is the start of the message text; nothing is
downloaded or stored beyond the last few notifications in memory.
"""
import collections
import logging
import time

from app.obex import Obex, ObexError

log = logging.getLogger("phone.messages")

POLL_SECONDS = 20


def new_messages(listing: dict, seen: set[str]) -> list[dict]:
    """Messages in a ListMessages result whose handles aren't in `seen`."""
    out = []
    for path, props in sorted(listing.items()):
        if path in seen:
            continue
        value = lambda k: getattr(props.get(k), "value", "")  # noqa: E731
        out.append(
            {
                "id": path.rsplit("/", 1)[-1],
                "from": value("SenderAddress") or value("Sender"),
                "sender": value("Sender"),
                "text": value("Subject"),
                "timestamp": value("Timestamp"),
            }
        )
    return out


class Messages:
    def __init__(self, obex: Obex | None = None):
        self.obex = obex or Obex()
        self.session: str | None = None
        self.address: str | None = None
        self.seen: set[str] | None = None  # None until the first listing
        self.recent: collections.deque[dict] = collections.deque(maxlen=20)
        self.error: str | None = None
        self.last_poll = 0.0

    async def _open(self, address: str) -> None:
        self.session = await self.obex.open_session(address, "map")
        self.address = address
        await self.obex.call(self.session, "org.bluez.obex.MessageAccess1", "SetFolder", "s", ["/telecom/msg"])

    async def close(self) -> None:
        if self.session:
            await self.obex.close_session(self.session)
        self.session = self.address = None

    async def poll(self, address: str, name_for) -> list[dict]:
        """Check the inbox. Returns messages new since the last poll, with a contact name added."""
        if time.time() - self.last_poll < POLL_SECONDS:
            return []
        self.last_poll = time.time()
        try:
            if self.session is None or self.address != address:
                await self.close()
                await self._open(address)
            [listing] = await self.obex.call(
                self.session, "org.bluez.obex.MessageAccess1", "ListMessages", "sa{sv}", ["inbox", {}]
            )
        except ObexError as e:
            # MAP refused (Show Notifications off) or the session died with a
            # disconnect: start over next time.
            self.error = str(e)
            self.last_poll = time.time() + 300 - POLL_SECONDS  # back off to 5 min
            await self.close()
            return []
        self.error = None
        if self.seen is None:
            self.seen = set(listing)
            return []
        fresh = new_messages(listing, self.seen)
        self.seen |= set(listing)
        for msg in fresh:
            msg["name"] = name_for(msg["from"]) or msg["sender"] or msg["from"]
            msg["received"] = time.time()
            self.recent.appendleft(msg)
        if fresh:
            log.info("%d new message(s)", len(fresh))
        return fresh
