"""Send links: let someone without Tailscale send you text, photos and files.

You make a link from the Drop card; it lasts LINK_SECONDS (24 h, like the
camera's invites) or until revoked. Whoever has it gets a bare page - a
name, a text box, a file picker - at /send/<token>, published on its own
through Tailscale Funnel (docs/phone.md, Send links); nothing else of this
app is public. What they send lands in Drop marked as theirs. Each link has
its own caps, so a leaked one can't fill the Pi: MAX_SENDS sends, MAX_BYTES
in all, and one send every MIN_GAP_SECONDS.
"""
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

LINK_SECONDS = 24 * 3600
MAX_SENDS = 30
MAX_BYTES = 300 * 1024 * 1024
MIN_GAP_SECONDS = 2.0
KEEP_SECONDS = 7 * 24 * 3600  # expired links stay listed a while, then go


class LinkError(ValueError):
    """Shown to the guest as is."""


@dataclass
class Link:
    token: str
    created: float
    expires: float
    label: str = ""  # who it's for, as you typed it
    revoked: bool = False
    sends: int = 0
    bytes: int = 0
    last_send: float = 0.0

    def live(self, now: float) -> bool:
        return not self.revoked and now < self.expires


class Links:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[Link] = []
        if path.exists():
            try:
                self.items = [Link(**d) for d in json.loads(path.read_text())]
            except (ValueError, TypeError):
                self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self.items]))
        tmp.replace(self.path)

    def create(self, label: str = "") -> Link:
        now = time.time()
        self.items = [x for x in self.items if now - x.expires < KEEP_SECONDS]
        link = Link(token=secrets.token_urlsafe(18), created=now, expires=now + LINK_SECONDS, label=label.strip()[:60])
        self.items.append(link)
        self._save()
        return link

    def get(self, token: str) -> Link | None:
        return next((x for x in self.items if secrets.compare_digest(x.token, token)), None)

    def live(self, token: str) -> Link | None:
        link = self.get(token)
        return link if link and link.live(time.time()) else None

    def revoke(self, token: str) -> bool:
        link = self.get(token)
        if not link:
            return False
        link.revoked = True
        self._save()
        return True

    def check_send(self, link: Link, size: int, now: float | None = None) -> None:
        """Raise LinkError if this send would go over the link's caps."""
        now = time.time() if now is None else now
        if not link.live(now):
            raise LinkError("This link has expired. Ask for a new one.")
        if now - link.last_send < MIN_GAP_SECONDS:
            raise LinkError("Slow down a little and try again.")
        if link.sends >= MAX_SENDS:
            raise LinkError("This link has been used as much as it can be. Ask for a new one.")
        if link.bytes + size > MAX_BYTES:
            raise LinkError("That's more than this link can take. Ask for a new one.")

    def record_send(self, link: Link, size: int) -> None:
        link.sends += 1
        link.bytes += size
        link.last_send = time.time()
        self._save()

    def active(self) -> list[dict]:
        """For the Drop card: links that still work, newest first."""
        now = time.time()
        return [asdict(x) for x in reversed(self.items) if x.live(now)]
