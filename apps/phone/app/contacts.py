"""Caller names from the iPhone's phonebook (Bluetooth PBAP).

Pulled when the phone connects, every six hours after, and on demand
from the page, into ~/.config/phone-bridge/contacts.json - so names
still show after a restart, before the next pull. Only names and phone
numbers are requested and kept.

Numbers are matched on their last ten digits, which covers the same US
number written as +1 (760) 555-0123, 7605550123 or 1-760-555-0123.
"""
import json
import logging
import os
import quopri
import re
import tempfile
import time
from pathlib import Path

from dbus_fast import Variant

from app.obex import Obex, ObexError

log = logging.getLogger("phone.contacts")

REFRESH_SECONDS = 6 * 3600


def number_key(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    return digits[-10:] if len(digits) >= 10 else digits


def parse_vcards(text: str) -> dict[str, str]:
    """number key -> display name, from a vCard 2.1/3.0 dump."""
    # Unfold continuation lines (RFC 6350 3.2), then walk card by card.
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    out: dict[str, str] = {}
    name, numbers = "", []
    for line in lines:
        head, _, value = line.partition(":")
        field = head.split(";")[0].upper()
        if "QUOTED-PRINTABLE" in head.upper():
            value = quopri.decodestring(value).decode("utf-8", "replace")
        if field == "BEGIN":
            name, numbers = "", []
        elif field == "FN":
            name = value.strip()
        elif field == "N" and not name:
            parts = [p for p in value.split(";")[:2] if p]
            name = " ".join(reversed(parts)).strip()
        elif field == "TEL":
            numbers.append(value)
        elif field == "END" and name:
            for n in numbers:
                key = number_key(n)
                if key:
                    out.setdefault(key, name)
    return out


class Contacts:
    def __init__(self, path: Path, obex: Obex | None = None):
        self.path = path
        self.obex = obex or Obex()
        self.names: dict[str, str] = {}
        self.updated = 0.0
        self.error: str | None = None
        try:
            saved = json.loads(path.read_text())
            self.names, self.updated = saved["names"], saved["updated"]
        except (OSError, ValueError, KeyError):
            pass

    def lookup(self, number: str) -> str:
        return self.names.get(number_key(number), "")

    def due(self) -> bool:
        return time.time() - self.updated > REFRESH_SECONDS

    async def refresh(self, address: str) -> int:
        """Pull the phonebook. Returns how many numbers are known afterwards."""
        session = await self.obex.open_session(address, "pbap")
        fd, tmp = tempfile.mkstemp(prefix="phone-bridge-pb-", suffix=".vcf")
        os.close(fd)
        try:
            await self.obex.call(session, "org.bluez.obex.PhonebookAccess1", "Select", "ss", ["int", "pb"])
            [size] = await self.obex.call(session, "org.bluez.obex.PhonebookAccess1", "GetSize")
            if not size:
                # iOS reports an empty phonebook until "Sync Contacts" is on
                # for this device - keep whatever was pulled before.
                self.error = "The iPhone shared no contacts - turn on Sync Contacts for joe in its Bluetooth settings"
                self.updated = time.time() - REFRESH_SECONDS + 600  # ask again in 10 min
                return len(self.names)
            transfer, _ = await self.obex.call(
                session,
                "org.bluez.obex.PhonebookAccess1",
                "PullAll",
                "sa{sv}",
                [tmp, {"Format": Variant("s", "vcard30"), "Fields": Variant("as", ["FN", "N", "TEL"])}],
            )
            await self.obex.wait_transfer(transfer)
            names = parse_vcards(Path(tmp).read_text(encoding="utf-8", errors="replace"))
        finally:
            await self.obex.close_session(session)
            Path(tmp).unlink(missing_ok=True)
        self.names, self.updated, self.error = names, time.time(), None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_json = self.path.with_suffix(".tmp")
        tmp_json.write_text(json.dumps({"names": names, "updated": self.updated}))
        os.chmod(tmp_json, 0o600)
        tmp_json.replace(self.path)
        log.info("contacts refreshed: %d numbers", len(names))
        return len(names)

    async def try_refresh(self, address: str) -> None:
        try:
            await self.refresh(address)
        except ObexError as e:
            self.error = str(e)
            self.updated = time.time() - REFRESH_SECONDS + 600  # retry in 10 min, not every poll
            log.warning("contacts refresh failed: %s", e)
