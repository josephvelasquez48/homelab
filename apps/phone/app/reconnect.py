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

Only while the PC is around. With the desktop off the Pi is a hands-free
unit with no speaker or mic, and the iPhone still connected to it and
could send a call's audio there. So when nothing on the PC has checked
in for PC_GONE_SECONDS (the agent polls every second while it runs), the
phone is disconnected and *blocked*: BlueZ refuses its connection
attempts straight away but keeps the pairing. When the PC is back it's
unblocked and reconnected on the next check, a few seconds later.
"""
import asyncio
import logging
import time

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

log = logging.getLogger("phone.reconnect")

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
HFP_AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"  # the phone's media side
INTERVAL_SECONDS = 30  # between connect attempts
CHECK_SECONDS = 5  # between looks at the PC and the phone
CONNECT_TIMEOUT = 30
# Long enough for the agent to restart after a crash (the supervisor waits
# 5 s and more) or the PC to reboot quickly, without dropping the phone.
PC_GONE_SECONDS = 120


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


def paired_phones(objects: dict) -> dict[str, bool]:
    """Every paired device offering the hands-free gateway -> whether it's blocked."""
    found = {}
    for path, ifaces in sorted(objects.items()):
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            continue
        value = lambda k, d=None: getattr(dev.get(k), "value", d)  # noqa: E731
        if value("Paired") and HFP_AG_UUID in (value("UUIDs") or []):
            found[path] = bool(value("Blocked"))
    return found


class Reconnector:
    def __init__(self, is_connected, pc_present=lambda: True):
        self.is_connected = is_connected  # () -> bool, from the telephony state
        self.pc_present = pc_present  # () -> bool: has the PC checked in lately
        self._next_attempt = 0.0
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

    async def _set_blocked(self, path: str, blocked: bool) -> None:
        await self._call(
            path, "org.freedesktop.DBus.Properties", "Set", "ssv", [DEVICE_IFACE, "Blocked", Variant("b", blocked)]
        )
        log.info("%s %s", "blocked (PC is off)" if blocked else "unblocked (PC is back)", path)

    async def check(self, now: float) -> None:
        """One pass: block or unblock for the PC, then maybe try to connect."""
        [objects] = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        phones = paired_phones(objects)
        if not self.pc_present():
            for path, blocked in phones.items():
                if not blocked:
                    await self._set_blocked(path, True)  # disconnects it too
            return
        if any(phones.values()):
            for path, blocked in phones.items():
                if blocked:
                    await self._set_blocked(path, False)
            self._next_attempt = 0.0  # connect now, not in up to 30 s
            return  # the next check sees the device unblocked
        if self.is_connected() or now < self._next_attempt:
            return
        self._next_attempt = now + INTERVAL_SECONDS
        for path in reconnect_candidates(objects):
            await self.attempt(path)

    async def reconnect_profiles(self, address: str, media: bool) -> None:
        """After WirePlumber restarts: calls back now, and media in "all" mode."""
        path = "/org/bluez/hci0/dev_" + address.replace(":", "_")
        for uuid in (HFP_AG_UUID, A2DP_SOURCE_UUID) if media else (HFP_AG_UUID,):
            try:
                await asyncio.wait_for(self._call(path, DEVICE_IFACE, "ConnectProfile", "s", [uuid]), CONNECT_TIMEOUT)
            except (asyncio.TimeoutError, RuntimeError) as e:
                log.info("reconnecting %s on %s: %s", uuid[4:8], path, e)

    async def run(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            try:
                await self.check(time.monotonic())
            except Exception:
                log.exception("reconnect check failed")
