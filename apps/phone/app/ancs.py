"""The iPhone's notifications, read over Bluetooth LE (ANCS) for the PC.

iPhones share their notifications with trusted Bluetooth accessories through
ANCS, the Apple Notification Center Service - how a watch gets them. The Pi
is already the iPhone's hands-free unit, and the same pairing covers this
(proved by a prototype, 2026-10-17): the iPhone links to the Pi over LE as
well, and offers ANCS on that link.

This keeps an LE advertisement up that asks for ANCS, so the iPhone links
back after Bluetooth drops; whenever the iPhone's ANCS service is there it
subscribes, and fetches each new notification's app, title and message. The
backlog sent on subscribing is skipped (on every reconnect it's the whole
Notification Center again), and so are calls - the bridge already handles
those. What's left goes to `on_notification`, kept in memory only (hub.py).

The protocol: a Notification Source event (8 bytes) says one was added,
changed or removed; a GetNotificationAttributes command on the Control Point
asks for its details, and the reply comes on the Data Source - possibly in
several packets, so it's buffered until complete. App display names come the
same way (GetAppAttributes) and are cached.
"""
import asyncio
import logging
import struct
import time
from dataclasses import dataclass

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

log = logging.getLogger("phone.ancs")

BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
ADV_PATH = "/homelab/phone/ancs_advertisement"
PROPS = "org.freedesktop.DBus.Properties"
CHAR = "org.bluez.GattCharacteristic1"

ANCS = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
NOTIFICATION_SOURCE = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"

EVENT_ADDED, EVENT_MODIFIED, EVENT_REMOVED = 0, 1, 2
FLAG_SILENT, FLAG_IMPORTANT, FLAG_PRE_EXISTING = 1 << 0, 1 << 1, 1 << 2
CATEGORIES = ["other", "incoming call", "missed call", "voicemail", "social", "schedule", "email",
              "news", "health", "finance", "location", "entertainment"]
SKIPPED_CATEGORIES = {1, 2}  # incoming and missed calls: the bridge and the PC app handle calls

CMD_NOTIFICATION_ATTRS, CMD_APP_ATTRS = 0, 1
ATTR_APP_ID, ATTR_TITLE, ATTR_MESSAGE = 0, 1, 3
NOTIFICATION_ATTRS = [(ATTR_APP_ID, None), (ATTR_TITLE, 64), (ATTR_MESSAGE, 256)]
APP_DISPLAY_NAME = 0

CHECK_SECONDS = 5  # looking for the iPhone's ANCS service
REPLY_SECONDS = 5  # an unanswered request gives up after this


@dataclass
class Event:
    kind: int
    flags: int
    category: int
    uid: int


def parse_event(value: bytes) -> Event | None:
    """A Notification Source packet."""
    if len(value) < 8:
        return None
    kind, flags, category, _count, uid = struct.unpack("<BBBBI", value[:8])
    return Event(kind, flags, category, uid)


def notification_request(uid: int) -> bytes:
    cmd = bytes([CMD_NOTIFICATION_ATTRS]) + struct.pack("<I", uid)
    for attr, maxlen in NOTIFICATION_ATTRS:
        cmd += bytes([attr]) + (struct.pack("<H", maxlen) if maxlen else b"")
    return cmd


def app_request(app_id: str) -> bytes:
    return bytes([CMD_APP_ATTRS]) + app_id.encode() + b"\0" + bytes([APP_DISPLAY_NAME])


def parse_reply(buf: bytes, attrs: int) -> tuple[dict[int, str], int] | None:
    """A Data Source reply: (attributes, bytes used), or None until all of it
    has arrived. `attrs` is how many attributes the request asked for."""
    if not buf:
        return None
    if buf[0] == CMD_NOTIFICATION_ATTRS:
        pos = 5  # command, notification UID
    elif buf[0] == CMD_APP_ATTRS:
        end = buf.find(b"\0", 1)
        if end < 0:
            return None
        pos = end + 1  # command, app identifier
    else:
        return {}, len(buf)  # not ours: drop it
    found: dict[int, str] = {}
    while len(found) < attrs:
        if len(buf) < pos + 3:
            return None
        attr, length = buf[pos], struct.unpack("<H", buf[pos + 1:pos + 3])[0]
        if len(buf) < pos + 3 + length:
            return None
        found[attr] = buf[pos + 3:pos + 3 + length].decode("utf-8", "replace")
        pos += 3 + length
    return found, pos


class Advertisement(ServiceInterface):
    """Asks for ANCS over LE: how the iPhone finds the Pi again after a drop."""

    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":  # noqa: F821 - dbus-fast reads the annotation as a D-Bus signature
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def SolicitUUIDs(self) -> "as":  # noqa: F821
        return [ANCS]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":  # noqa: F821
        return "Homelab"

    @method()
    def Release(self) -> None:
        log.info("LE advertisement released by BlueZ")


class Ancs:
    def __init__(self, on_notification, on_removed=None, enabled=lambda: True):
        self.on_notification = on_notification  # (dict) -> None
        self.on_removed = on_removed  # (uid) -> None
        self.enabled = enabled  # () -> bool: the Settings switch
        self.bus: MessageBus | None = None
        self.service: str | None = None  # the ANCS service's object path while subscribed
        self.chars: dict[str, str] = {}
        self.app_names: dict[str, str] = {}
        self.queue: list[tuple[str, object]] = []  # ("notification", Event) or ("app", app id)
        self.waiting: tuple[str, object] | None = None
        self.waiting_since = 0.0
        self.waiting_for: list[dict] = []  # notifications held until their app's name arrives
        self.buffer = b""
        self.received = 0

    async def _call(self, path, iface, member, signature="", body=()):
        reply = await self.bus.call(Message(destination=BLUEZ, path=path, interface=iface, member=member,
                                            signature=signature, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply.body

    async def _add_match(self, rule: str) -> None:
        """Ask the bus daemon (not BlueZ) to send us these signals."""
        reply = await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                            interface="org.freedesktop.DBus", member="AddMatch",
                                            signature="s", body=[rule]))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")

    async def run(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.export(ADV_PATH, Advertisement())
        try:
            await self._call(ADAPTER, "org.bluez.LEAdvertisingManager1", "RegisterAdvertisement",
                             "oa{sv}", [ADV_PATH, {}])
        except RuntimeError as e:
            log.warning("no LE advertisement (the iPhone may not link back after a drop): %s", e)
        self.bus.add_message_handler(self.on_signal)  # the GATT values, matched per path in subscribe()
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("ANCS check failed")
            await asyncio.sleep(CHECK_SECONDS)

    async def check(self) -> None:
        """Subscribe when the iPhone's ANCS service is there and we aren't."""
        if self.waiting and time.monotonic() - self.waiting_since > REPLY_SECONDS:
            log.info("no reply to an ANCS request; moving on")
            self.waiting, self.buffer = None, b""
            await self.next_request()
        if not self.enabled():
            return
        [objects] = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        service = next((p for p, i in objects.items()
                        if str(i.get("org.bluez.GattService1", {}).get("UUID", Variant("s", "")).value).lower() == ANCS), None)
        if service == self.service:
            return
        self.service, self.chars = None, {}
        if not service:
            return
        chars = {}
        for path, ifaces in objects.items():
            ch = ifaces.get(CHAR)
            if ch and ch["Service"].value == service:
                chars[ch["UUID"].value.lower()] = path
        if not all(u in chars for u in (NOTIFICATION_SOURCE, CONTROL_POINT, DATA_SOURCE)):
            return  # still being discovered: the next check
        await self.subscribe(service, chars)

    async def subscribe(self, service: str, chars: dict[str, str]) -> None:
        for uuid in (NOTIFICATION_SOURCE, DATA_SOURCE):
            await self._add_match(
                f"type='signal',sender='{BLUEZ}',path='{chars[uuid]}',interface='{PROPS}',member='PropertiesChanged'")
        # Data first: the backlog of events comes as soon as Notification
        # Source is subscribed, and their replies arrive on Data Source.
        await self._call(chars[DATA_SOURCE], CHAR, "StartNotify")
        await self._call(chars[NOTIFICATION_SOURCE], CHAR, "StartNotify")
        self.service, self.chars = service, chars
        self.queue, self.waiting, self.buffer = [], None, b""
        log.info("subscribed to the iPhone's notifications (%s)", service)

    def on_signal(self, msg: Message) -> None:
        if (msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged"
                or not self.chars or len(msg.body) < 2 or "Value" not in msg.body[1]):
            return
        value = bytes(msg.body[1]["Value"].value)
        if msg.path == self.chars.get(NOTIFICATION_SOURCE):
            self.on_event(parse_event(value))
        elif msg.path == self.chars.get(DATA_SOURCE):
            self.on_data(value)

    def on_event(self, event: Event | None) -> None:
        if not event:
            return
        if event.kind == EVENT_REMOVED:
            if self.on_removed:
                self.on_removed(event.uid)
            return
        if (event.kind != EVENT_ADDED or event.flags & FLAG_PRE_EXISTING
                or event.category in SKIPPED_CATEGORIES or not self.enabled()):
            return  # the backlog, an update, a call, or switched off
        self.queue.append(("notification", event))
        asyncio.get_running_loop().create_task(self.next_request())

    async def next_request(self) -> None:
        if self.waiting or not self.queue or not self.chars:
            return
        kind, item = self.waiting = self.queue.pop(0)
        self.waiting_since = time.monotonic()
        self.buffer = b""
        command = notification_request(item.uid) if kind == "notification" else app_request(item)
        try:
            await self._call(self.chars[CONTROL_POINT], CHAR, "WriteValue", "aya{sv}",
                             [command, {"type": Variant("s", "request")}])
        except RuntimeError as e:
            # Gone already (a notification removed before we asked), or the
            # link dropped: skip it.
            log.info("ANCS request failed: %s", e)
            self.waiting = None
            await self.next_request()

    def on_data(self, value: bytes) -> None:
        if not self.waiting:
            return
        kind, item = self.waiting
        self.buffer += value
        parsed = parse_reply(self.buffer, len(NOTIFICATION_ATTRS) if kind == "notification" else 1)
        if parsed is None:
            return  # more packets to come
        attrs, _ = parsed
        self.waiting, self.buffer = None, b""
        if kind == "notification":
            self.notification_done(item, attrs)
        else:
            self.app_names[item] = attrs.get(APP_DISPLAY_NAME) or item
            held, self.waiting_for = self.waiting_for, []
            for n in held:
                self.deliver(n)
        asyncio.get_running_loop().create_task(self.next_request())

    def notification_done(self, event: Event, attrs: dict[int, str]) -> None:
        app_id = attrs.get(ATTR_APP_ID, "")
        n = {
            "uid": event.uid,
            "appId": app_id,
            "title": attrs.get(ATTR_TITLE, ""),
            "message": attrs.get(ATTR_MESSAGE, ""),
            "category": CATEGORIES[event.category] if event.category < len(CATEGORIES) else "other",
            "time": time.time(),
        }
        self.received += 1
        if app_id and app_id not in self.app_names:
            self.waiting_for.append(n)
            if not any(k == "app" and i == app_id for k, i in self.queue):
                self.queue.insert(0, ("app", app_id))
            return
        self.deliver(n)

    def deliver(self, n: dict) -> None:
        n["app"] = self.app_names.get(n["appId"]) or n["appId"]
        try:
            self.on_notification(n)
        except Exception:
            log.exception("handling a notification failed")
