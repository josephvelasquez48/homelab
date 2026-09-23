"""A minimal Bluetooth MAP Message Notification Server (MNS).

Why this exists: iOS refuses a device's connection to its messages (MAP,
"Connection refused") unless that device advertises an MNS - the service
the phone pushes new-message events to. obexd has one, but on this Pi it
never gets registered with BlueZ: its Bluetooth plugin registers server
profiles when it sees org.bluez appear, which happens during plugin
init, before the server list exists - so nothing is registered unless
bluetoothd starts *after* obexd. Restarting bluetoothd proved it (MNS
appeared and the iPhone listed its inbox), but needs root on every boot.

So the phone bridge registers MNS itself, as the logged-in user, through
BlueZ's ProfileManager1 (BlueZ builds the SDP record for the MNS UUID).
When the phone connects, a tiny OBEX server answers CONNECT/PUT/
DISCONNECT and reports NewMessage events, which just trigger an early
inbox poll - messages.py still reads them through obexd's MAP client.
Registration is redone whenever bluetoothd restarts, since BlueZ drops
profiles then.
"""
import asyncio
import logging
import os
import re
import socket
import struct
import threading

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger("phone.mns")

MNS_UUID = "00001133-0000-1000-8000-00805f9b34fb"
# OBEX Target/Who value for MNS (MAP spec).
MNS_TARGET = bytes.fromhex("bb582b41420c11dbb0de0800200c9a66")
PROFILE_PATH = "/homelab/phone/mns"

# OBEX opcodes and response codes.
CONNECT, DISCONNECT, PUT, PUT_FINAL, ABORT = 0x80, 0x81, 0x02, 0x82, 0xFF
CONTINUE, SUCCESS, NOT_IMPLEMENTED = 0x90, 0xA0, 0xD1
BODY, END_OF_BODY = 0x48, 0x49


def headers(data: bytes) -> list[tuple[int, bytes]]:
    """Split OBEX headers. The top two bits of the id give the encoding."""
    out, i = [], 0
    while i < len(data):
        hid = data[i]
        kind = hid >> 6
        if kind in (0, 1):  # unicode text / byte sequence, 2-byte length incl. the 3-byte prefix
            n = struct.unpack(">H", data[i + 1 : i + 3])[0]
            out.append((hid, data[i + 3 : i + n]))
            i += max(n, 3)
        elif kind == 2:
            out.append((hid, data[i + 1 : i + 2]))
            i += 2
        else:
            out.append((hid, data[i + 1 : i + 5]))
            i += 5
    return out


def response(code: int, extra: bytes = b"") -> bytes:
    return bytes([code]) + struct.pack(">H", 3 + len(extra)) + extra


def connect_response() -> bytes:
    # version 1.0, no flags, max packet 8 KiB, ConnectionID 1, Who = MNS.
    fields = b"\x10\x00" + struct.pack(">H", 0x2000)
    conn_id = b"\xcb" + struct.pack(">I", 1)
    who = b"\x4a" + struct.pack(">H", 3 + len(MNS_TARGET)) + MNS_TARGET
    return response(SUCCESS, fields + conn_id + who)


def events(report: str) -> list[dict]:
    """The <event .../> elements of a MAP-event-report, as attribute dicts."""
    return [dict(re.findall(r'(\w+)="([^"]*)"', attrs)) for attrs in re.findall(r"<event\b([^>]*)/?>", report)]


class ObexSession:
    """One phone connection: answer packets until DISCONNECT or EOF."""

    def __init__(self, sock: socket.socket, on_event):
        self.sock = sock
        self.on_event = on_event
        self.body = b""

    def handle(self, packet: bytes) -> tuple[bytes, bool]:
        op = packet[0]
        if op == CONNECT:
            return connect_response(), False
        if op in (PUT, PUT_FINAL):
            for hid, value in headers(packet[3:]):
                if hid in (BODY, END_OF_BODY):
                    self.body += value
            if op == PUT:
                return response(CONTINUE), False
            report, self.body = self.body.decode("utf-8", "replace"), b""
            for event in events(report):
                self.on_event(event)
            return response(SUCCESS), False
        if op == DISCONNECT:
            return response(SUCCESS), True
        if op == ABORT:
            self.body = b""
            return response(SUCCESS), False
        return response(NOT_IMPLEMENTED), False

    def serve(self) -> None:
        buf = b""
        with self.sock:
            while True:
                try:
                    data = self.sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while len(buf) >= 3:
                    length = struct.unpack(">H", buf[1:3])[0]
                    if length < 3 or len(buf) < length:
                        break
                    packet, buf = buf[:length], buf[length:]
                    reply, done = self.handle(packet)
                    self.sock.sendall(reply)
                    if done:
                        return


class _Profile(ServiceInterface):
    def __init__(self, server: "MnsServer"):
        super().__init__("org.bluez.Profile1")
        self.server = server

    @method()
    def Release(self):  # noqa: N802 - D-Bus method name
        pass

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):  # noqa: F821,N802
        self.server.accept(device, fd)

    @method()
    def RequestDisconnection(self, device: "o"):  # noqa: F821,N802
        pass


class MnsServer:
    def __init__(self, on_new_message):
        self.on_new_message = on_new_message  # called (from a worker thread) for each NewMessage event
        self.bus: MessageBus | None = None
        self.registered = False
        self.connections = 0
        self.loop: asyncio.AbstractEventLoop | None = None

    def accept(self, device: str, fd: int) -> None:
        self.connections += 1
        log.info("MNS connection from %s", device)
        sock = socket.socket(fileno=os.dup(fd))
        session = ObexSession(sock, self._event)
        threading.Thread(target=session.serve, name="mns", daemon=True).start()

    def _event(self, event: dict) -> None:
        log.info("MAP event %s in %s", event.get("type"), event.get("folder"))
        if event.get("type") == "NewMessage" and self.loop:
            self.loop.call_soon_threadsafe(self.on_new_message)

    async def _register(self) -> None:
        reply = await self.bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.ProfileManager1",
                member="RegisterProfile",
                signature="osa{sv}",
                body=[
                    PROFILE_PATH,
                    MNS_UUID,
                    {
                        "Name": Variant("s", "Message Notification Server"),
                        "Role": Variant("s", "server"),
                        "RequireAuthentication": Variant("b", True),
                        "RequireAuthorization": Variant("b", False),
                    },
                ],
            )
        )
        if reply.message_type == MessageType.ERROR:
            # AlreadyExists: obexd's own MNS got registered (bluetoothd
            # restarted after obexd) - the phone is covered either way.
            self.registered = False
            log.warning("MNS not registered: %s %s", reply.error_name, reply.body)
        else:
            self.registered = True
            log.info("MNS registered")

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        self.bus.export(PROFILE_PATH, _Profile(self))
        await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=["type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"],
            )
        )

        def on_signal(msg: Message) -> None:
            if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == "org.bluez" and msg.body[2]:
                log.info("bluetoothd restarted; registering MNS again")
                asyncio.ensure_future(self._register())

        self.bus.add_message_handler(on_signal)
        await self._register()
