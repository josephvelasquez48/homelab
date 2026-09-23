"""Reconnect the iPhone when it drops, instead of waiting for it to try.

A paired, trusted phone that walks out of range and back, or toggles
Bluetooth, doesn't always come back to the hands-free unit on its own.
While no phone is connected, this asks BlueZ (system bus) every 30 s to
connect each paired device that offers the hands-free gateway profile.
Out of range, Connect just fails after BlueZ's page timeout; that's the
normal case while the phone is away, so it's counted, not logged loudly.
"""
import asyncio
import logging

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

log = logging.getLogger("phone.reconnect")

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
INTERVAL_SECONDS = 30


def reconnect_candidates(objects: dict) -> list[str]:
    """Paired, trusted, disconnected devices that can be our phone."""
    found = []
    for path, ifaces in sorted(objects.items()):
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            continue
        value = lambda k, d=None: getattr(dev.get(k), "value", d)  # noqa: E731
        if value("Paired") and value("Trusted") and not value("Connected") and HFP_AG_UUID in (value("UUIDs") or []):
            found.append(path)
    return found


class Reconnector:
    def __init__(self, is_connected):
        self.is_connected = is_connected  # () -> bool, from the telephony state
        self.attempts = 0
        self.successes = 0
        self.bus: MessageBus | None = None

    async def _call(self, path, iface, member, signature="", body=()):
        reply = await self.bus.call(
            Message(destination=BLUEZ, path=path, interface=iface, member=member, signature=signature, body=list(body))
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply.body

    async def run(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            if self.is_connected():
                continue
            try:
                [objects] = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
                for path in reconnect_candidates(objects):
                    self.attempts += 1
                    try:
                        await self._call(path, DEVICE_IFACE, "Connect")
                        self.successes += 1
                        log.info("reconnected %s", path)
                    except RuntimeError as e:
                        log.debug("reconnect %s failed: %s", path, e)
            except Exception:
                log.exception("reconnect check failed")
