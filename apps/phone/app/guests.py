"""Send links: let someone on the home Wi-Fi send you text, photos and files.

You make a link from the Drop card, named for who it's for; it works until
you turn it off. Whoever has it gets a bare page - a text box and a file
picker - at /send/<token>, on the guest listener (home network only, see
main.py); nothing else of this app is reachable through it. What they send
lands in Drop under the link's name.

Each link has its own daily caps, so a forwarded one can't fill the Pi:
MAX_SENDS sends and MAX_BYTES in any DAY_SECONDS (counted from the first
send of the day), and one send every MIN_GAP_SECONDS.
"""
import json
import secrets
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DAY_SECONDS = 24 * 3600
MAX_SENDS = 30  # a day
MAX_BYTES = 300 * 1024 * 1024  # a day
MIN_GAP_SECONDS = 2.0
KEEP_REVOKED_SECONDS = 7 * 24 * 3600  # a turned-off link is forgotten after this


class LinkError(ValueError):
    """Shown to the guest as is."""


@dataclass
class Link:
    token: str
    created: float
    label: str = ""  # who it's for, as you typed it
    revoked: bool = False
    revoked_at: float = 0.0
    sends: int = 0  # all time, for the Drop card
    day_start: float = 0.0  # the day's caps count from here
    day_sends: int = 0
    day_bytes: int = 0
    last_send: float = 0.0

    def live(self) -> bool:
        return not self.revoked


FIELDS = {f.name for f in fields(Link)}


class Links:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[Link] = []
        if path.exists():
            try:
                # Only known fields: links made before a change (the 24-hour
                # ones had "expires") still load, and now last until revoked.
                self.items = [Link(**{k: v for k, v in d.items() if k in FIELDS}) for d in json.loads(path.read_text())]
            except (ValueError, TypeError):
                self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self.items]))
        tmp.replace(self.path)

    def create(self, label: str) -> Link:
        now = time.time()
        self.items = [x for x in self.items if not x.revoked or now - x.revoked_at < KEEP_REVOKED_SECONDS]
        link = Link(token=secrets.token_urlsafe(18), created=now, label=label.strip()[:60])
        self.items.append(link)
        self._save()
        return link

    def get(self, token: str) -> Link | None:
        return next((x for x in self.items if secrets.compare_digest(x.token, token)), None)

    def live(self, token: str) -> Link | None:
        link = self.get(token)
        return link if link and link.live() else None

    def revoke(self, token: str) -> bool:
        link = self.get(token)
        if not link:
            return False
        link.revoked = True
        link.revoked_at = time.time()
        self._save()
        return True

    def check_send(self, link: Link, size: int, now: float | None = None) -> None:
        """Raise LinkError if this send would go over the link's caps."""
        now = time.time() if now is None else now
        if not link.live():
            raise LinkError("This link has been turned off.")
        if now - link.last_send < MIN_GAP_SECONDS:
            raise LinkError("Slow down a little and try again.")
        if now - link.day_start >= DAY_SECONDS:
            return  # a new day: the caps start again on this send
        if link.day_sends >= MAX_SENDS:
            raise LinkError("That's as much as this link takes in a day. Try again tomorrow.")
        if link.day_bytes + size > MAX_BYTES:
            raise LinkError("That's more than this link takes in a day. Try again tomorrow.")

    def record_send(self, link: Link, size: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - link.day_start >= DAY_SECONDS:
            link.day_start, link.day_sends, link.day_bytes = now, 0, 0
        link.sends += 1
        link.day_sends += 1
        link.day_bytes += size
        link.last_send = now
        self._save()

    def active(self) -> list[dict]:
        """For the Drop card: links that still work, newest first."""
        return [asdict(x) for x in reversed(self.items) if x.live()]
