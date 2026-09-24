"""Reconnect the iPhone when it drops, instead of waiting for it to try.

A paired, trusted phone that walks out of range and back, or toggles
Bluetooth, doesn't always come back to the hands-free unit on its own.
While no phone is connected, this asks BlueZ (system bus) every 30 s to
connect each paired device that offers the hands-free gateway profile.
Out of range, the connect just fails after BlueZ's page timeout; that's the
normal case while the phone is away, so it's counted, not logged loudly.

It connects the hands-free profile (ConnectProfile), not the device
(Connect). The iPhone is dual-mode, and BlueZ ran every Device1.Connect
over Bluetooth LE: a 30 s passive scan for the phone's public address,
which an iPhone never advertises (it uses a rotating random one), so it
never connected - 168 attempts in a row, with the phone on the desk.
btmon showed no classic page at all. A classic-only profile makes BlueZ
page the phone over BR/EDR, which connected in 2 s.
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
CONNECT_TIMEOUT = 30


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
        self.bus: MessageBus | None = None  # set by run()
        self._last_reason: str | None = None

    async def _call(self, path, iface, member, signature="", body=()):
        reply = await self.bus.call(
            Message(destination=BLUEZ, path=path, interface=iface, member=member, signature=signature, body=list(body))
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply.body

    async def attempt(self, path: str) -> bool:
        """One connect, with a time limit; clears BlueZ's stuck state after a hang."""
        self.attempts += 1
        try:
            await asyncio.wait_for(
                self._call(path, DEVICE_IFACE, "ConnectProfile", "s", [HFP_AG_UUID]), CONNECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            reason = "timed out"
        except RuntimeError as e:
            reason = str(e)
        else:
            self.successes += 1
            self._last_reason = None
            log.info("reconnected %s", path)
            return True
        # Every 30 s while the phone is away, so only log a new reason, or
        # every 20th attempt as a heartbeat.
        if reason != self._last_reason or self.attempts % 20 == 0:
            log.info("reconnect %s failed (attempt %d): %s", path, self.attempts, reason)
        self._last_reason = reason
        if reason == "timed out" or "InProgress" in reason:
            # A Connect that never finishes leaves BlueZ answering every later
            # one with InProgress, without paging the phone at all: seen live
            # as 23 failed attempts with nothing on the air while the phone sat
            # in range. Disconnect clears it for the next attempt.
            try:
                await asyncio.wait_for(self._call(path, DEVICE_IFACE, "Disconnect"), 10)
            except (asyncio.TimeoutError, RuntimeError) as e:
                log.info("clearing stuck connect on %s failed: %s", path, e)
        return False

    async def reset_hands_free(self, address: str) -> None:
        """Drop and reopen only the hands-free link, which makes the phone
        announce its calls again (see Hub._check_orphan_audio)."""
        path = "/org/bluez/hci0/dev_" + address.replace(":", "_")
        await asyncio.wait_for(self._call(path, DEVICE_IFACE, "DisconnectProfile", "s", [HFP_AG_UUID]), 10)
        await asyncio.sleep(3)
        await asyncio.wait_for(self._call(path, DEVICE_IFACE, "ConnectProfile", "s", [HFP_AG_UUID]), CONNECT_TIMEOUT)

    async def run(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            if self.is_connected():
                continue
            try:
                [objects] = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
                for path in reconnect_candidates(objects):
                    await self.attempt(path)
            except Exception:
                log.exception("reconnect check failed")
