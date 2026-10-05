"""Drop: text, photos and files between the iPhone and the PC.

Kept on the Pi under PHONE_DATA/drops: one file per drop plus index.json.
Whatever one side drops, the other picks up - the PC's agent (it already
polls /api/agent/ringing every second) puts text on the clipboard and saves
files to a folder; the iPhone sees the list on the phone page (or in the
home-screen app) with Copy and Save. A drop lasts KEEP_SECONDS, and only
the newest KEEP_COUNT are kept, so the Pi's disk can't fill up from here.
"""
import json
import mimetypes
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_TEXT = 100_000  # characters
MAX_FILE = 50 * 1024 * 1024  # bytes: a long iPhone video won't fit, a photo will
KEEP_SECONDS = 7 * 24 * 3600
KEEP_COUNT = 50
SOURCES = ("phone", "pc", "guest")  # guest: through a send link (app/guests.py)


class DropError(ValueError):
    """Something the sender should be told: too big, empty, unknown."""


@dataclass
class Drop:
    id: str
    kind: str  # "text" or "file"
    source: str  # "phone", "pc" or "guest": phone and guest drops go to the PC, pc ones to the phone
    created: float
    text: str | None = None
    name: str | None = None  # file name as sent, made safe
    mime: str | None = None
    size: int = 0
    sender: str | None = None  # a guest's name, as they gave it
    via: str | None = None  # the send link it came through, by the name you gave the link


def safe_name(name: str) -> str:
    """A file name that's safe on the Pi and on Windows: no paths, no
    reserved characters, not empty."""
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:120] or "file"


class Drops:
    def __init__(self, folder: Path):
        self.folder = folder
        self.index = folder / "index.json"
        self.items: list[Drop] = []
        if self.index.exists():
            try:
                self.items = [Drop(**d) for d in json.loads(self.index.read_text())]
            except (ValueError, TypeError):
                self.items = []  # a damaged index only loses the list, not the service
        self.prune()

    def _path(self, drop: Drop) -> Path:
        return self.folder / drop.id

    def _save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(d) for d in self.items]))
        tmp.replace(self.index)

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        keep = [d for d in self.items if now - d.created < KEEP_SECONDS][-KEEP_COUNT:]
        for d in self.items:
            if d not in keep and d.kind == "file":
                self._path(d).unlink(missing_ok=True)
        if len(keep) != len(self.items):
            self.items = keep
            self._save()

    def _new(self, **fields) -> Drop:
        if fields["source"] not in SOURCES:
            raise DropError("unknown source")
        drop = Drop(id=secrets.token_hex(8), created=time.time(), **fields)
        self.items.append(drop)
        self._save()
        self.prune()
        return drop

    def add_text(self, text: str, source: str, sender: str | None = None, via: str | None = None) -> Drop:
        if not text.strip():
            raise DropError("Nothing to send")
        if len(text) > MAX_TEXT:
            raise DropError(f"Text is over {MAX_TEXT:,} characters")
        return self._new(kind="text", source=source, text=text, size=len(text.encode()), sender=sender, via=via)

    def add_file(self, name: str, mime: str | None, data: bytes, source: str,
                 sender: str | None = None, via: str | None = None) -> Drop:
        if source not in SOURCES:
            raise DropError("unknown source")
        if not data:
            raise DropError("The file is empty")
        if len(data) > MAX_FILE:
            raise DropError(f"Files over {MAX_FILE // 1024 // 1024} MB don't fit")
        name = safe_name(name)
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        drop = Drop(id=secrets.token_hex(8), kind="file", source=source, created=time.time(),
                    name=name, mime=mime, size=len(data), sender=sender, via=via)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(drop).write_bytes(data)
        self.items.append(drop)
        self._save()
        self.prune()
        return drop

    def get(self, drop_id: str) -> Drop | None:
        return next((d for d in self.items if d.id == drop_id), None)

    def file_path(self, drop: Drop) -> Path:
        return self._path(drop)

    def delete(self, drop_id: str) -> bool:
        drop = self.get(drop_id)
        if not drop:
            return False
        self.items.remove(drop)
        if drop.kind == "file":
            self._path(drop).unlink(missing_ok=True)
        self._save()
        return True

    def recent(self) -> list[dict]:
        """Newest first, for the page."""
        return [asdict(d) for d in reversed(self.items)]

    def latest_created(self) -> float:
        return self.items[-1].created if self.items else 0.0

    def for_pc_since(self, since: float) -> list[dict]:
        """Drops for the PC (from the phone or a guest) made after the newest
        one the PC's agent has handled. By time, not id, so a pruned drop
        can't make old ones new."""
        return [asdict(d) for d in self.items if d.source != "pc" and d.created > since]
