"""Minimal client for BlueZ's OBEX daemon (org.bluez.obex, session bus).

Used for the iPhone's phonebook over PBAP (apps/phone/app/contacts.py).
obexd comes from the bluez-obexd package and is D-Bus-activated on first
use; obex-override.conf limits it to its client side. The iPhone reports
an empty phonebook until "Sync Contacts" is on for this device in its
Bluetooth settings.
"""
import asyncio

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

SERVICE = "org.bluez.obex"
CLIENT_PATH = "/org/bluez/obex"


class ObexError(Exception):
    pass


class Obex:
    def __init__(self):
        self.bus: MessageBus | None = None

    async def connect(self) -> None:
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

    async def call(self, path, iface, member, signature="", body=()):
        await self.connect()
        reply = await self.bus.call(
            Message(destination=SERVICE, path=path, interface=iface, member=member, signature=signature, body=list(body))
        )
        if reply.message_type == MessageType.ERROR:
            raise ObexError(f"{member}: {reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply.body

    async def open_session(self, address: str, target: str) -> str:
        [session] = await self.call(
            CLIENT_PATH, "org.bluez.obex.Client1", "CreateSession", "sa{sv}", [address, {"Target": Variant("s", target)}]
        )
        return session

    async def close_session(self, session: str) -> None:
        try:
            await self.call(CLIENT_PATH, "org.bluez.obex.Client1", "RemoveSession", "o", [session])
        except ObexError:
            pass  # already gone, e.g. the phone disconnected

    async def wait_transfer(self, transfer: str, timeout: float = 60) -> None:
        """Poll a Transfer1 until it completes. A transfer object that vanishes has finished."""
        for _ in range(int(timeout / 0.25)):
            try:
                [status] = await self.call(
                    transfer, "org.freedesktop.DBus.Properties", "Get", "ss", ["org.bluez.obex.Transfer1", "Status"]
                )
            except ObexError:
                return
            if status.value == "complete":
                return
            if status.value == "error":
                raise ObexError(f"transfer {transfer} failed")
            await asyncio.sleep(0.25)
        raise ObexError(f"transfer {transfer} timed out")
