"""Call control through PipeWire's built-in hands-free telephony service.

With the Pi acting as the phone's Bluetooth hands-free unit, WirePlumber
publishes org.pipewire.Telephony on the *session* bus - one "audio
gateway" object per connected phone (/org/pipewire/Telephony/agN), each
with an oFono-compatible VoiceCallManager. No oFono daemon is involved;
the oFono interface names are just the API PipeWire chose to mirror.

The agN number is not stable across reconnects, so it is rediscovered on
every poll rather than cached.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

SERVICE = "org.pipewire.Telephony"
ROOT = "/org/pipewire/Telephony"
AG_IFACE = "org.pipewire.Telephony.AudioGateway1"
TRANSPORT_IFACE = "org.pipewire.Telephony.AudioGatewayTransport1"
MANAGER_IFACE = "org.ofono.VoiceCallManager"
# D-Bus has no deadline of its own short of the daemon's ~25 s. A call
# that lands while WirePlumber restarts (switching the audio mode does
# that) went unanswered that long, holding up every tick: pages got no
# state for up to a minute, and the agent's window, still showing the
# ended call as its own, wouldn't close. Anything that waits on the phone
# itself (answer, dial) gets longer.
CALL_TIMEOUT = 5
PHONE_TIMEOUT = 20
CALL_IFACE = "org.ofono.VoiceCall"

# Digits plus the characters a dial pad can produce. Anything else is
# rejected before it reaches the phone as an AT command.
_NUMBER = re.compile(r"^\+?[0-9*#]{1,32}$")
_TONES = re.compile(r"^[0-9*#]{1,32}$")


class TelephonyError(Exception):
    pass


@dataclass
class Call:
    path: str
    state: str  # incoming, waiting, dialing, alerting, active, held, disconnected
    number: str
    name: str


@dataclass
class PhoneState:
    connected: bool = False
    gateway: str | None = None
    address: str | None = None
    transport: str | None = None  # idle / pending / active - see audio_on_pi

    @property
    def audio_on_pi(self) -> bool:
        # "pending" already means the phone has opened the SCO link and is
        # sending audio: it only turns "active" once something consumes
        # the streams. With autoconnect off (51-phone-bridge.conf), that
        # something is the bridge - so waiting for "active" deadlocked, with
        # a live call's audio arriving at the Pi and going nowhere.
        return self.transport in ("pending", "active")
    calls: list[Call] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "connected": self.connected,
            "audioOnPi": self.audio_on_pi,
            "calls": [c.__dict__ for c in self.calls],
        }


def _unwrap(value):
    return value.value if isinstance(value, Variant) else value


def parse_gateways(objects: dict) -> list[tuple[str, str | None, str | None]]:
    """(path, address, transport state) for each phone in GetManagedObjects."""
    found = []
    for path, ifaces in sorted(objects.items()):
        if AG_IFACE not in ifaces:
            continue
        address = _unwrap(ifaces[AG_IFACE].get("Address"))
        transport = _unwrap(ifaces.get(TRANSPORT_IFACE, {}).get("State"))
        found.append((path, address, transport))
    return found


def parse_calls(calls: dict) -> list[Call]:
    """Keep the fields the page shows from a GetCalls reply.

    PipeWire's GetCalls is a{oa{sv}} - a dict keyed by call path - where
    real oFono's is a(oa{sv}). Only the live reply showed the difference.
    """
    parsed = []
    for path, props in sorted(calls.items()):
        parsed.append(
            Call(
                path=path,
                state=str(_unwrap(props.get("State", "")) or ""),
                number=str(_unwrap(props.get("LineIdentification", "")) or ""),
                name=str(_unwrap(props.get("Name", "")) or ""),
            )
        )
    return parsed


# A "waiting" call with no other call beside it can't really be call
# waiting - past this many seconds it's a leftover the phone never cleared.
STALE_WAITING_SECONDS = 45


def drop_phantom_calls(calls: list[Call], waiting_since: dict[str, float], now: float) -> list[Call]:
    """Remove the phone's duplicate and stuck "waiting" calls.

    Seen live: one incoming call from a contact showed up as two call
    objects at the same instant - call1 "incoming" and call2 "waiting",
    same number. call1 was answered and ended; call2 stayed "waiting"
    until Bluetooth reconnected, so the page kept offering an Answer that
    couldn't work (on its own a waiting call can't be answered) and the
    UI never left the call. So: a waiting call with the number of another
    live call is that call reported twice, and a waiting call alone for
    longer than STALE_WAITING_SECONDS is stale. `waiting_since` is the
    caller's memory of when each waiting call was first seen.
    """
    for path in list(waiting_since):
        if not any(c.path == path and c.state == "waiting" for c in calls):
            del waiting_since[path]
    kept = []
    for call in calls:
        if call.state == "waiting":
            first = waiting_since.setdefault(call.path, now)
            others = [c for c in calls if c.path != call.path and c.state != "disconnected"]
            if call.number and any(c.number == call.number for c in others):
                continue
            if not others and now - first > STALE_WAITING_SECONDS:
                continue
        kept.append(call)
    return kept


def valid_number(number: str) -> bool:
    return bool(_NUMBER.match(number))


def valid_tones(tones: str) -> bool:
    return bool(_TONES.match(tones))


class Telephony:
    def __init__(self):
        self.bus: MessageBus | None = None
        self.state = PhoneState()
        self._waiting_since: dict[str, float] = {}

    async def connect(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()

    async def _call(self, path, iface, member, signature="", body=(), timeout=None):
        message = Message(
            destination=SERVICE, path=path, interface=iface, member=member, signature=signature, body=list(body)
        )
        timeout = timeout or CALL_TIMEOUT
        try:
            reply = await asyncio.wait_for(self.bus.call(message), timeout)
        except asyncio.TimeoutError:
            raise TelephonyError(f"{member}: no answer from PipeWire in {timeout} s") from None
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise TelephonyError(f"{reply.error_name}: {detail}".rstrip(": "))
        return reply.body

    async def refresh(self) -> PhoneState:
        try:
            objects = (await self._call(ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        except TelephonyError:
            # WirePlumber restarting, or bluetooth off: no phone, not a crash.
            self.state = PhoneState()
            return self.state
        gateways = parse_gateways(objects)
        if not gateways:
            self.state = PhoneState()
            return self.state
        # One phone is paired; if more ever are, the first one wins.
        path, address, transport = gateways[0]
        calls = parse_calls((await self._call(path, MANAGER_IFACE, "GetCalls"))[0])
        calls = drop_phantom_calls(calls, self._waiting_since, time.time())
        self.state = PhoneState(True, path, address, transport, calls)
        return self.state

    def _gateway(self) -> str:
        if not self.state.gateway:
            raise TelephonyError("no phone connected")
        return self.state.gateway

    def _known_call(self, path: str) -> str:
        # Only act on calls this service has seen, so a client can't aim
        # Answer/Hangup at an arbitrary object path on the bus.
        if path not in {c.path for c in self.state.calls}:
            raise TelephonyError("no such call")
        return path

    async def answer(self, path: str) -> None:
        known = self._known_call(path)
        call = next(c for c in self.state.calls if c.path == known)
        if call.state == "waiting":
            # A waiting call can't take a plain Answer (ATA); "hold and
            # answer" (AT+CHLD=2) accepts it, holding any active call.
            await self._call(self._gateway(), MANAGER_IFACE, "HoldAndAnswer", timeout=PHONE_TIMEOUT)
        else:
            await self._call(path, CALL_IFACE, "Answer", timeout=PHONE_TIMEOUT)

    async def hangup(self, path: str) -> None:
        await self._call(self._known_call(path), CALL_IFACE, "Hangup")

    async def dial(self, number: str) -> None:
        if not valid_number(number):
            raise TelephonyError("invalid number")
        await self._call(self._gateway(), MANAGER_IFACE, "Dial", "s", [number], timeout=PHONE_TIMEOUT)

    async def send_tones(self, tones: str) -> None:
        if not valid_tones(tones):
            raise TelephonyError("invalid tones")
        await self._call(self._gateway(), MANAGER_IFACE, "SendTones", "s", [tones])

    async def activate_audio(self) -> None:
        """Pull an in-progress call's audio from the handset onto the Pi."""
        await self._call(self._gateway(), TRANSPORT_IFACE, "Activate")

    async def set_reject_sco(self, reject: bool) -> None:
        """While no browser is listening, refuse the phone's audio link.

        iOS routes call audio to a connected hands-free unit by default,
        like a car. With nobody on the page, that would silently send a
        call's audio to a Pi with no speakers. Rejecting SCO keeps it on
        the handset until a browser is actually connected.
        """
        if not self.state.gateway:
            return
        await self._call(
            self.state.gateway,
            "org.freedesktop.DBus.Properties",
            "Set",
            "ssv",
            [TRANSPORT_IFACE, "RejectSCO", Variant("b", reject)],
        )
