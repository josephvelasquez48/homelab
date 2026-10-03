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

With the Pi's Bluetooth turned off (the display's Bluetooth button,
apps/pi-display, or the switch on the phone page), there's nothing to do:
no connects, no blocking. The adapter's state, re-read every check, is what
both of those show, so each follows the other.
"""
import asyncio
import logging
import time

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

log = logging.getLogger("phone.reconnect")

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_PATH = "/org/bluez/hci0"
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


def adapter_powered(objects: dict) -> bool | None:
    """Whether the Pi's Bluetooth is on; None with no adapter at all."""
    adapter = objects.get(ADAPTER_PATH, {}).get("org.bluez.Adapter1")
    return None if adapter is None else bool(getattr(adapter.get("Powered"), "value", False))


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


def media_links(objects: dict) -> dict[str, bool]:
    """Connected, unblocked phones -> whether their A2DP (media) link is up."""
    links = {}
    for path, blocked in paired_phones(objects).items():
        dev = objects[path][DEVICE_IFACE]
        if blocked or not getattr(dev.get("Connected"), "value", False):
            continue
        links[path] = any(
            p.startswith(path + "/") and "org.bluez.MediaTransport1" in ifaces for p, ifaces in objects.items()
        )
    return links


class Reconnector:
    def __init__(self, is_connected, pc_present=lambda: True, media_wanted=lambda: False):
        self.is_connected = is_connected  # () -> bool, from the telephony state
        self.pc_present = pc_present  # () -> bool: has the PC checked in lately
        self.media_wanted = media_wanted  # () -> bool: the "Music and videos on this PC" switch
        self._next_attempt = 0.0
        self._next_media_attempt = 0.0
        self.attempts = 0
        self.successes = 0
        self.bus: MessageBus | None = None  # set by run()
        self._last_reason: str | None = None
        self.powered: bool | None = None  # the adapter, as of the last check

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

    async def set_powered(self, on: bool) -> None:
        """Turn the Pi's Bluetooth on or off (the phone page's switch)."""
        await self._call(
            ADAPTER_PATH, "org.freedesktop.DBus.Properties", "Set", "ssv",
            ["org.bluez.Adapter1", "Powered", Variant("b", on)],
        )
        self.powered = on
        if on:
            self._next_attempt = 0.0  # reconnect the phone now, not in up to 30 s
        log.info("Bluetooth turned %s", "on" if on else "off")

    async def check(self, now: float) -> None:
        """One pass: block or unblock for the PC, then maybe try to connect."""
        [objects] = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        self.powered = adapter_powered(objects)
        if self.powered is False:
            return  # Bluetooth turned off on purpose: nothing would connect
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
        if self.is_connected():
            await self._match_media(objects, now)
            return
        if now < self._next_attempt:
            return
        self._next_attempt = now + INTERVAL_SECONDS
        for path in reconnect_candidates(objects):
            await self.attempt(path)

    async def _match_media(self, objects: dict, now: float) -> None:
        """Connect or drop just the media link, to match the switch.

        Every check, so it also holds after the phone reconnects (iOS
        brings A2DP up with the calls link): media goes back to the iPhone
        within a check if the switch says so.
        """
        wanted = self.media_wanted()
        for path, up in media_links(objects).items():
            if up == wanted:
                continue
            if not wanted:
                member = "DisconnectProfile"
            elif now >= self._next_media_attempt:
                self._next_media_attempt = now + INTERVAL_SECONDS
                member = "ConnectProfile"
            else:
                continue
            try:
                await asyncio.wait_for(self._call(path, DEVICE_IFACE, member, "s", [A2DP_SOURCE_UUID]), 15)
                log.info("media %s %s", "to the PC:" if wanted else "back to the iPhone:", path)
            except (asyncio.TimeoutError, RuntimeError) as e:
                log.info("media %s on %s failed: %s", member, path, e)

    def media_changed(self) -> None:
        """The switch moved: act on the next check, not after a back-off."""
        self._next_media_attempt = 0.0

    async def reconnect_profiles(self, address: str, media: bool) -> None:
        """After WirePlumber restarts: calls back now, and media if it's wanted on the PC."""
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
