"""Ties call state, the audio bridge and connected browsers together.

One loop polls the phone twice a second and reacts to what it sees. The
phone's state is small and local-bus-cheap to read, and polling means a
WirePlumber restart or a phone walking out of range is picked up the same
way as any other change - no signal subscription to lose.

Audio goes to one browser at a time: the page that most recently turned
on its mic and speakers ("audio-ready"). Every logged-in page sees call
state and can answer or hang up, but a page that hasn't enabled audio
doesn't count as somewhere to send a call - see set_reject_sco().
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import WebSocket

from app.audio import AudioBridge
from app.telephony import Telephony, TelephonyError

log = logging.getLogger("phone.hub")

POLL_SECONDS = 0.5

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
    ):
        self.tel = telephony or Telephony()
        self.bridge = bridge or AudioBridge()
        self.settings_path = settings_path
        self.settings = load_settings(settings_path)
        # Set by a PC action (answer/dial/move audio), cleared when the
        # calls it covered are all over. Only matters with keepPhoneAnswered.
        self._pc_claimed = False
        self._had_calls = False
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

    def snapshot(self) -> dict:
        state = self.tel.state.to_json()
        state["bridged"] = self.bridge.running
        state["audioError"] = self._audio_error
        state["settings"] = dict(self.settings)
        return state

    def _want_reject_sco(self) -> bool:
        if not self.audio_clients:
            return True
        return self.settings["keepPhoneAnswered"] and not self._pc_claimed

    async def run(self) -> None:
        await self.tel.connect()
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
        self._had_calls = bool(state.calls)

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
        if action in PC_ACTIONS and not self._pc_claimed:
            # Lift RejectSCO *before* telling the phone, or the audio link
            # it opens in response gets refused.
            self._pc_claimed = True
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
                await self.tel.activate_audio()
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
