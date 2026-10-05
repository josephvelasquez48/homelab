"""Drop on the PC: what the iPhone sends lands here by itself.

The agent's status poll carries the newest drop's time (dropLatest); when
it moves, this fetches the drops from the phone it hasn't handled yet.
Text goes on the clipboard; photos and files are saved to DROP_DIR. Either
way a notification says what arrived. Which drops are handled is kept in
DROP_SINCE, so drops sent while the PC was off arrive when it's back -
except on the very first run, which starts from now rather than replaying
the last week.
"""
import ctypes
import json
import logging
import os
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("phone-agent")

DROP_DIR = Path.home() / "Downloads" / "Phone Drop"
DROP_SINCE = Path(os.environ["APPDATA"]) / "phone-bridge" / "drop-since"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def set_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard (no extra packages)."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))  # the clipboard owns it now
    finally:
        user32.CloseClipboard()


def unused_path(folder: Path, name: str) -> Path:
    """name, or "name (2)" and so on if that's taken - never overwrite."""
    path, stem, n = folder / name, Path(name).stem, 2
    while path.exists():
        path = folder / f"{stem} ({n}){Path(name).suffix}"
        n += 1
    return path


class DropReceiver:
    def __init__(self, pi, notify):
        self.pi = pi
        self.notify = notify  # (title, message) -> None
        self.since: float | None = None
        try:
            self.since = float(DROP_SINCE.read_text())
        except (OSError, ValueError):
            pass  # first run: start from the first poll's newest drop

    def _get(self, path: str):
        req = urllib.request.Request(f"{self.pi.url}{path}", headers={"Authorization": f"Bearer {self.pi.token}"})
        return urllib.request.urlopen(req, context=self.pi.context, timeout=30)

    def _remember(self, since: float) -> None:
        self.since = since
        try:
            DROP_SINCE.write_text(repr(since))
        except OSError as e:
            log.warning("drop: can't save progress: %s", e)

    def check(self, latest: float) -> None:
        """Called with each status poll's dropLatest."""
        if self.since is None:
            self._remember(latest)
            return
        if latest <= self.since:
            return
        with self._get(f"/api/agent/drops?since={urllib.parse.quote(repr(self.since))}") as r:
            drops = json.load(r)["drops"]
        for d in drops:
            self.receive(d)
            self._remember(d["created"])
        if latest > self.since:  # only drops from the PC since: nothing to do for those
            self._remember(latest)

    def receive(self, d: dict) -> None:
        if d["kind"] == "text":
            ok = set_clipboard(d["text"])
            preview = " ".join(d["text"].split())[:120]
            self.notify("Copied from iPhone" if ok else "Text from iPhone (couldn't copy)", preview)
            log.info("drop: text, %d chars, %s", len(d["text"]), "copied" if ok else "clipboard busy")
            return
        DROP_DIR.mkdir(parents=True, exist_ok=True)
        path = unused_path(DROP_DIR, d["name"])
        with self._get(f"/api/agent/drops/{d['id']}/file") as r, open(path, "wb") as f:
            while chunk := r.read(65536):
                f.write(chunk)
        self.notify("Saved from iPhone", f"{path.name} in Downloads\\Phone Drop")
        log.info("drop: saved %s (%d bytes)", path, d["size"])
