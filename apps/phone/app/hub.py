"""Ties call state, the audio bridge and connected browsers together.

One loop polls the phone twice a second and reacts to what it sees. The
phone's state is small and local-bus-cheap to read, and polling means a
WirePlumber restart or a phone walking out of range is picked up the same
way as any other change - no signal subscription to lose.

Audio goes to one browser at a time: the page that most recently turned
on its mic and speakers ("audio-ready"). Every logged-in page sees call
state and can answer or hang up, but a page that hasn't enabled audio
doesn't count as somewhere to send a call - see set_reject_sco().

The slower work - phonebook pulls, the text-message inbox, metrics - runs
in a separate loop (extras_loop) so a seconds-long Bluetooth transfer
never holds up call state. Contacts, history and the
reconnector are optional, so tests build a Hub without any of them.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from fastapi import WebSocket

from app import metrics
from app.audio import AudioBridge
from app.contacts import Contacts
from app.history import CallLog
from app.reconnect import Reconnector
from app.telephony import Call, Telephony, TelephonyError

log = logging.getLogger("phone.hub")

POLL_SECONDS = 0.5
EXTRAS_SECONDS = 15
RELEASE_SCO = ["sudo", "-n", "/usr/local/sbin/phone-bridge-release-sco"]

SETTINGS_PATH = Path(os.environ.get("PHONE_SETTINGS", Path.home() / ".config/phone-bridge/settings.json"))
DEFAULT_SETTINGS = {
    # Calls answered on the iPhone keep their audio on the iPhone; the Pi
    # only takes a call's audio when the PC answered, dialed or pulled it.
    "keepPhoneAnswered": False,
    # Label of the mic pages should use; "" means the browser's default.
    # A label, not a device ID: IDs differ per browser profile.
    "micLabel": "",
}
# Page actions that mean "this call belongs on the PC".
PC_ACTIONS = ("answer", "dial", "audio-to-pc")
# How long the phone's audio link may sit on the Pi with no call before the
# hands-free link is reset to make the phone announce its calls again.
ORPHAN_AUDIO_SECONDS = 8


def load_settings(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    return {k: type(v)(saved.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}


def save_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings))
    tmp.replace(path)


class Hub:
    def __init__(
        self,
        telephony: Telephony | None = None,
        bridge: AudioBridge | None = None,
        settings_path: Path = SETTINGS_PATH,
        contacts: Contacts | None = None,
        history: CallLog | None = None,
        reconnector: Reconnector | None = None,
        write_metrics: bool = False,
    ):
        self.tel = telephony or Telephony()
        self.contacts = contacts
        self.history = history
        self.reconnector = reconnector
        self.write_metrics = write_metrics
        self._was_connected = False
        # Set by "send audio to the iPhone": keep refusing the audio link
        # until the call ends or the PC asks for it back.
        self._phone_held = False
        self.bridge = bridge or AudioBridge()
        self.settings_path = settings_path
        self.settings = load_settings(settings_path)
        # Set by a PC action (answer/dial/move audio), cleared when the
        # calls it covered are all over. Only matters with keepPhoneAnswered.
        self._pc_claimed = False
        self._had_calls = False
        # When the audio link was first seen on the Pi with no call, and
        # whether this stretch of it already got its one reset.
        self._orphan_since: float | None = None
        self._orphan_reset = False
        self.clients: list[WebSocket] = []
        self.audio_clients: list[WebSocket] = []
        self._last_state: dict | None = None
        self._reject_sco: tuple[str | None, bool] | None = None
        self._pump: asyncio.Task | None = None
        self._audio_error: str | None = None
        # The poll loop and a page command can both tick; without this they
        # race on bridge.start() and spawn two pairs of pw-cat processes.
        self._tick_lock = asyncio.Lock()

    @property
    def audio_owner(self) -> WebSocket | None:
        return self.audio_clients[-1] if self.audio_clients else None

    def name_for(self, number: str) -> str:
        return self.contacts.lookup(number) if self.contacts else ""

    def snapshot(self) -> dict:
        state = self.tel.state.to_json()
        for call in state["calls"]:
            call["name"] = call["name"] or self.name_for(call["number"])
        state["bridged"] = self.bridge.running
        state["audioError"] = self._audio_error
        state["settings"] = dict(self.settings)
        return state

    def extras(self) -> dict:
        """Call history and contact-sync status, sent to pages as their own message."""
        return {
            "type": "extras",
            "history": self.history.recent() if self.history else [],
            "contacts": len(self.contacts.names) if self.contacts else 0,
            "contactsError": self.contacts.error if self.contacts else None,
        }

    async def broadcast_extras(self, only: WebSocket | None = None) -> None:
        message = json.dumps(self.extras())
        for ws in [only] if only else list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                pass

    def _want_reject_sco(self) -> bool:
        if not self.audio_clients or self._phone_held:
            return True
        return self.settings["keepPhoneAnswered"] and not self._pc_claimed

    async def run(self) -> None:
        await self.tel.connect()
        asyncio.create_task(self.extras_loop())
        if self.reconnector:
            asyncio.create_task(self.reconnector.run())
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> None:
        async with self._tick_lock:
            await self._tick()

    async def _tick(self) -> None:
        state = await self.tel.refresh()

        if self._had_calls and not state.calls:
            self._pc_claimed = False
            self._phone_held = False
        self._had_calls = bool(state.calls)
        self._check_orphan_audio(state)

        if self.history:
            named = [Call(c.path, c.state, c.number, c.name or self.name_for(c.number)) for c in state.calls]
            self.history.observe(named, self.bridge.running)
            if self.history.changed:
                self.history.changed = False
                asyncio.create_task(self.broadcast_extras())

        want_reject = self._want_reject_sco()
        if state.gateway and self._reject_sco != (state.gateway, want_reject):
            await self.tel.set_reject_sco(want_reject)
            self._reject_sco = (state.gateway, want_reject)

        if state.audio_on_pi and self.audio_clients and not self.bridge.running:
            try:
                await self.bridge.start()
                self._audio_error = None
                self._pump = asyncio.create_task(self._pump_rx())
            except Exception as e:
                # Keep retrying next tick, but surface it on the page
                # instead of just a silent call.
                self._audio_error = str(e)
                log.warning("bridge start failed: %s", e)
        elif not state.audio_on_pi and self.bridge.running:
            await self.bridge.stop()

        await self.broadcast_state()

    def _check_orphan_audio(self, state) -> None:
        """Recover a call the Pi never heard about.

        When the hands-free link drops and comes back during a call, the
        phone reopens the audio link to the Pi but PipeWire's telephony
        doesn't pick up the call already in progress: seen live as a
        "pending" transport and an empty GetCalls for minutes, so the pages
        had nothing to show or answer. Dropping and reopening just the
        hands-free profile made the phone announce the call. Once per
        stretch of orphaned audio: an app call (FaceTime, WhatsApp) can also
        route audio here without an HFP call, and must not reset in a loop.
        """
        if not (state.connected and state.audio_on_pi):
            self._orphan_since = None
            self._orphan_reset = False
            return
        if state.calls:
            self._orphan_since = None
            return
        now = time.monotonic()
        self._orphan_since = self._orphan_since or now
        if self._orphan_reset or not self.reconnector or now - self._orphan_since < ORPHAN_AUDIO_SECONDS:
            return
        self._orphan_reset = True
        log.warning("audio link on the Pi with no call for %d s - resetting the hands-free link", ORPHAN_AUDIO_SECONDS)
        asyncio.create_task(self._reset_hands_free(state.address))

    async def _reset_hands_free(self, address: str) -> None:
        try:
            await self.reconnector.reset_hands_free(address)
        except Exception as e:
            log.warning("hands-free reset failed: %s", e)

    async def extras_loop(self) -> None:
        while True:
            try:
                await self._extras()
            except Exception:
                log.exception("extras failed")
            await asyncio.sleep(EXTRAS_SECONDS)

    async def _extras(self) -> None:
        state = self.tel.state
        just_connected = state.connected and not self._was_connected
        self._was_connected = state.connected
        changed = False
        if state.connected and self.contacts and (just_connected or self.contacts.due()):
            before = (len(self.contacts.names), self.contacts.error)
            await self.contacts.try_refresh(state.address)
            changed |= before != (len(self.contacts.names), self.contacts.error)
        if changed:
            await self.broadcast_extras()
        if self.write_metrics:
            self._write_metrics()

    def _write_metrics(self) -> None:
        rx, tx = self.bridge.take_peaks()
        state = self.tel.state
        values = {
            "phone_connected": int(state.connected),
            "phone_call_active": int(bool(state.calls)),
            "phone_audio_on_pi": int(state.audio_on_pi),
            "phone_bridge_running": int(self.bridge.running),
            "phone_pages": len(self.clients),
            "phone_audio_pages": len(self.audio_clients),
            "phone_audio_rx_peak": rx,
            "phone_audio_tx_peak": tx,
            "phone_contacts": len(self.contacts.names) if self.contacts else 0,
            "phone_reconnect_attempts_total": self.reconnector.attempts if self.reconnector else 0,
            "phone_reconnect_successes_total": self.reconnector.successes if self.reconnector else 0,
        }
        try:
            metrics.write(values, self.history.counts() if self.history else {})
        except OSError as e:
            log.warning("metrics not written: %s", e)

    async def release_audio(self) -> None:
        """Send a call's audio back to the iPhone (see release-sco.sh)."""
        self._phone_held = True
        self._pc_claimed = False
        await self.tick()  # RejectSCO on first, so the phone can't bounce it straight back
        proc = await asyncio.create_subprocess_exec(
            *RELEASE_SCO, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode:
            raise TelephonyError(err.decode().strip() or "couldn't release the audio link")

    async def _pump_rx(self) -> None:
        while chunk := await self.bridge.read():
            owner = self.audio_owner
            if owner is None:
                continue
            try:
                await owner.send_bytes(chunk)
            except Exception:
                pass  # disconnect is handled by the socket's own handler

    async def broadcast_state(self, force: bool = False) -> None:
        state = self.snapshot()
        if state == self._last_state and not force:
            return
        self._last_state = state
        message = json.dumps({"type": "state", **state})
        for ws in list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                pass

    async def add(self, ws: WebSocket) -> None:
        self.clients.append(ws)
        await self.broadcast_state(force=True)
        await self.broadcast_extras(only=ws)

    async def remove(self, ws: WebSocket) -> None:
        if ws in self.clients:
            self.clients.remove(ws)
        if ws in self.audio_clients:
            self.audio_clients.remove(ws)

    def audio_in(self, ws: WebSocket, pcm: bytes) -> None:
        if ws is self.audio_owner and self.bridge.running:
            self.bridge.write(pcm)

    async def command(self, ws: WebSocket, msg: dict) -> str | None:
        """Run one page action; returns an error message for the page, if any."""
        action = msg.get("action")
        if action in PC_ACTIONS and (not self._pc_claimed or self._phone_held):
            # Lift RejectSCO *before* telling the phone, or the audio link
            # it opens in response gets refused.
            self._pc_claimed = True
            self._phone_held = False
            await self.tick()
        try:
            if action == "set-keep-phone":
                self.settings["keepPhoneAnswered"] = bool(msg.get("value"))
                save_settings(self.settings_path, self.settings)
            elif action == "set-mic":
                self.settings["micLabel"] = str(msg.get("value", ""))[:200]
                save_settings(self.settings_path, self.settings)
            elif action == "audio-ready":
                if ws in self.audio_clients:
                    self.audio_clients.remove(ws)
                self.audio_clients.append(ws)
            elif action == "answer":
                await self.tel.answer(str(msg.get("call", "")))
            elif action == "hangup":
                await self.tel.hangup(str(msg.get("call", "")))
            elif action == "dial":
                await self.tel.dial(str(msg.get("number", "")).replace(" ", "").replace("-", ""))
            elif action == "tones":
                await self.tel.send_tones(str(msg.get("tones", "")))
            elif action == "audio-to-pc":
                # Already on the Pi (the phone kept it there with no page
                # taking it): the audio-ready before this starts the bridge.
                if not self.tel.state.audio_on_pi:
                    await self.tel.activate_audio()
            elif action == "audio-to-phone":
                await self.release_audio()
            elif action == "refresh-contacts":
                if self.contacts and self.tel.state.connected:
                    self.contacts.updated = 0  # due now; the extras loop picks it up
                    asyncio.create_task(self._extras())
            else:
                return "unknown action"
        except TelephonyError as e:
            if action in PC_ACTIONS and not self.tel.state.calls:
                # A dial that never became a call mustn't leave the claim
                # set, or the next call answered on the phone comes here.
                self._pc_claimed = False
                await self.tick()
            return str(e)
        await self.tick()
        return None
