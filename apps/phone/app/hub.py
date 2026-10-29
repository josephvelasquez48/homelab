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
import collections
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
from app.media import MediaBridge, write_roles
from app.reconnect import PC_GONE_SECONDS, Reconnector
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
    # Music and videos play on the PC (the Pi is the phone's Bluetooth
    # speaker) - or, off, on the iPhone. Calls come to the PC either way.
    "mediaOnPc": True,
    # Drop and send links (Settings tab): see LIMITS for what's allowed.
    "dropKeepDays": 7,
    "dropMaxFileMb": 50,
    "linkSendsPerDay": 30,
    "linkMbPerDay": 300,
    "linkMbPerSend": 100,
    # The iPhone's notifications on the PC (ancs.py), and apps kept off it.
    "phoneNotifications": True,
    "mutedApps": [],
    # Clearing one on the PC clears it on the iPhone too.
    "clearOnPhone": True,
}
# The limits the Settings tab can change, and how far: (lowest, highest).
# Wide enough to be useful, narrow enough that a slip can't fill the Pi.
LIMITS = {
    "dropKeepDays": (1, 90),
    "dropMaxFileMb": (1, 500),
    "linkSendsPerDay": (1, 500),
    "linkMbPerDay": (10, 5000),
    "linkMbPerSend": (1, 500),
}
# Page actions that mean "this call belongs on the PC".
PC_ACTIONS = ("answer", "dial", "audio-to-pc", "answer-on-pc")
# How long "answer on the PC" (from the Pi's touchscreen) waits for the PC's
# window to take the audio before answering anyway, audio then on the iPhone.
HANDOFF_WAIT_SECONDS = 4
# The iPhone's notifications kept for the pages and the PC app.
NOTIFICATIONS_KEPT = 50
# How long after the iPhone connects a call already in progress, or its
# audio arriving, counts as "already going" - kept on the iPhone (_tick).
MID_CALL_GRACE_SECONDS = 10
# How long the phone's audio link may sit on the Pi with no call before the
# hands-free link is reset to make the phone announce its calls again.
ORPHAN_AUDIO_SECONDS = 8


def load_settings(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    settings = {k: type(v)(saved.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}
    for key, (low, high) in LIMITS.items():  # a hand-edited file can't go past them either
        settings[key] = min(max(settings[key], low), high)
    return settings


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
        self.media: MediaBridge | None = None  # set in main.py with the extras
        self.drops = None  # app.drops.Drops, set in main.py with the extras
        self.links = None  # app.guests.Links (send links), likewise
        self.links_base = ""  # their public address, from main.py
        self.ancs = None  # app.ancs.Ancs, set in main.py with the extras
        # The iPhone's notifications (ancs.py): the latest few, in memory
        # only - their text is private and never written to disk.
        self.notifications: collections.deque = collections.deque(maxlen=NOTIFICATIONS_KEPT)
        self.notify_apps: dict[str, str] = {}  # app id -> name, every app seen: Settings' mute list
        self.write_metrics = write_metrics
        self._was_connected = False
        self._link_written = None  # (iPhone connected, Bluetooth on) as last written to the metrics
        # The iPhone's link as the last tick saw it (None: not looked yet -
        # a service start mid-call isn't the phone connecting), and when it
        # last came up.
        self._link_seen: bool | None = None
        self._connected_at: float | None = None
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
        # When the PC last showed it's on (the agent's poll). Starts at
        # "now", so a restarted service gives the PC time to check in.
        self._pc_seen = time.monotonic()
        self.pc_checkins = 0  # agent polls since the service started, for the display's dots
        self._orphan_reset = False
        self.clients: list[WebSocket] = []
        # Pages on the Pi's own touchscreen (loopback). They can answer and
        # hang up, but have no speakers or mic, and don't mean the PC is on.
        self.local_clients: set[WebSocket] = set()
        # The call the Pi's touchscreen asked the PC's window to take.
        self._handoff: str | None = None
        # The calls up when Home was tapped on the Pi's call screen: the
        # screen stays closed while only those are around, and comes back
        # for any new one (see screen_hidden).
        self._screen_hidden_for: frozenset[str] | None = None
        # Mute, shared by every page: the PC's window and the Pi's screen
        # show and set the same one. Held here rather than in the page with
        # the mic, so the Pi's screen can mute a call whose mic is on the PC.
        # There's no Bluetooth command to mute the iPhone itself.
        self.muted = False
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

    # ---- the iPhone's notifications (ancs.py) ----------------------------------

    def add_notification(self, n: dict) -> None:
        """A new one from the iPhone: kept (unless switched off or its app
        muted) for the pages and the PC app, which shows it as a Windows
        notification."""
        self.notify_apps[n["appId"]] = n["app"]
        if not self.settings["phoneNotifications"] or n["appId"] in self.settings["mutedApps"]:
            asyncio.get_running_loop().create_task(self.broadcast_extras())  # a new app for the mute list
            return
        n = {**n, "time": max(n["time"], self.notify_latest() + 1e-6)}  # strictly increasing: the PC app goes by it
        self.notifications.append(n)
        asyncio.get_running_loop().create_task(self.broadcast_extras())

    def remove_notification(self, uid: int) -> None:
        """Dismissed or read on the iPhone: off the list too."""
        kept = [n for n in self.notifications if n["uid"] != uid]
        if len(kept) != len(self.notifications):
            self.notifications = collections.deque(kept, maxlen=NOTIFICATIONS_KEPT)
            asyncio.get_running_loop().create_task(self.broadcast_extras())

    async def _clear_on_phone(self, listed: list[dict]) -> None:
        """Clear these on the iPhone too, if Settings says so and the iPhone
        said they can be (most can; ancs.py)."""
        if not (self.ancs and self.settings["clearOnPhone"]):
            return
        for n in listed:
            if n.get("canClear"):
                await self.ancs.clear(n["uid"])

    def notify_latest(self) -> float:
        return self.notifications[-1]["time"] if self.notifications else 0.0

    def notifications_since(self, since: float) -> list[dict]:
        return [n for n in self.notifications if n["time"] > since]

    def apply_limits(self) -> None:
        """Hand the Settings tab's limits to Drop and the send links."""
        mb, s = 1024 * 1024, self.settings
        if self.drops:
            self.drops.max_file = s["dropMaxFileMb"] * mb
            self.drops.keep_seconds = s["dropKeepDays"] * 24 * 3600
            self.drops.prune()
        if self.links:
            self.links.max_sends = s["linkSendsPerDay"]
            self.links.max_bytes = s["linkMbPerDay"] * mb

    def guest_request_limit(self) -> int:
        """The most one guest send may be, in bytes."""
        return self.settings["linkMbPerSend"] * 1024 * 1024

    def snapshot(self) -> dict:
        state = self.tel.state.to_json()
        for call in state["calls"]:
            call["name"] = call["name"] or self.name_for(call["number"])
            # For the call timer: the same for every page and across reloads,
            # and a new call gets a new time even when the phone reuses its
            # path (call1 again on a redial).
            call["connectedAt"] = self.history.connected_at(call["path"]) if self.history else None
        state["bridged"] = self.bridge.running
        state["audioError"] = self._audio_error
        state["settings"] = dict(self.settings)
        state["handoff"] = self._handoff
        state["muted"] = self.muted
        # The Pi's Bluetooth adapter, as last read (every few seconds), so the
        # switch follows the display's button too. None: not known yet.
        state["bluetooth"] = self.reconnector.powered if self.reconnector else None
        return state

    def extras(self) -> dict:
        """Call history and contact-sync status, sent to pages as their own message."""
        return {
            "type": "extras",
            "history": self.history.recent() if self.history else [],
            "contacts": len(self.contacts.names) if self.contacts else 0,
            "contactsError": self.contacts.error if self.contacts else None,
            "drops": self.drops.recent() if self.drops else [],
            "links": [{**x, "url": f"{self.links_base}/send/{x['token']}"} for x in self.links.active()] if self.links else [],
            "notifications": list(reversed(self.notifications)),  # newest first
            "notifyApps": sorted(({"id": k, "name": v, "muted": k in self.settings["mutedApps"]}
                                  for k, v in self.notify_apps.items()), key=lambda a: a["name"].lower()),
            "notifyActive": bool(self.ancs and self.ancs.active),
        }

    async def broadcast_extras(self, only: WebSocket | None = None) -> None:
        message = json.dumps(self.extras())
        for ws in [only] if only else list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                pass

    def radio_busy(self) -> bool:
        """A call or music on the Pi's Bluetooth: no LE scans (ancs.py) -
        the radio is shared, and a scan could break up the audio."""
        state = self.tel.state
        return bool(state.calls or state.audio_on_pi or (self.media and self.media.node))

    def call_on(self) -> bool:
        """A call going (ancs.py closes the LE link for it)."""
        state = self.tel.state
        return bool(state.calls or state.audio_on_pi)

    def _want_reject_sco(self) -> bool:
        if not self.audio_clients or self._phone_held:
            return True
        return self.settings["keepPhoneAnswered"] and not self._pc_claimed

    async def ensure_speaker_role(self) -> None:
        """Put the speaker-role drop-in in place (media.py). Only a first
        start (or a changed drop-in) restarts WirePlumber; after that the
        switch just moves the media link."""
        if not self.media or not write_roles():
            return
        log.info("speaker role added: restarting WirePlumber")
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", "restart", "wireplumber")
        await proc.wait()
        if self.reconnector and self.tel.state.address:
            await asyncio.sleep(3)
            asyncio.create_task(
                self.reconnector.reconnect_profiles(self.tel.state.address, media=self.settings["mediaOnPc"])
            )

    async def run(self) -> None:
        await self.tel.connect()
        try:
            await self.ensure_speaker_role()
        except Exception:
            log.exception("couldn't add the speaker role")
        if self.media:
            asyncio.create_task(self.media.run(lambda: self.settings["mediaOnPc"]))
        asyncio.create_task(self.extras_loop())
        if self.reconnector:
            asyncio.create_task(self.reconnector.run())
        if self.ancs:
            asyncio.create_task(self.ancs.run())
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
            self.muted = False  # the next call starts unmuted
        self._had_calls = bool(state.calls)
        self._keep_call_on_phone(state)
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
        elif self.bridge.running and not (state.audio_on_pi and self.audio_clients):
            # Also when the last audio page left mid-call: a bridge feeding
            # nobody still reports "bridged", which hides Take on PC from
            # the next page.
            await self.bridge.stop()

        # The iPhone or the Pi's Bluetooth just changed: write the metrics
        # now rather than at the next 15 s write, so the display follows
        # within a scrape.
        link = (state.connected, self.reconnector.powered if self.reconnector else None)
        if self.write_metrics and link != self._link_written:
            self._link_written = link
            self._write_metrics()

        await self.broadcast_state()

    def pc_seen(self) -> None:
        self._pc_seen = time.monotonic()
        self.pc_checkins += 1

    def screen_hidden(self) -> bool:
        """Should the Pi's call screen stay closed? Yes after Home was tapped
        on it, while only the calls up at the time are around - a new call
        (another ringing, say) brings it back, and so does the next call
        once these have ended."""
        live = frozenset(c.path for c in self.tel.state.calls if c.state != "disconnected")
        if not live:
            self._screen_hidden_for = None
        return self._screen_hidden_for is not None and live <= self._screen_hidden_for

    def screen_shown(self) -> bool:
        """Is the Pi's call screen up? A call is, and Home didn't send it away."""
        return any(c.state != "disconnected" for c in self.tel.state.calls) and not self.screen_hidden()

    def show_screen(self) -> None:
        """Undo Home: the Pi's call screen comes back for the calls up now
        (the display's Call button, via apps/pi-display/show-call.sh)."""
        self._screen_hidden_for = None

    def pc_present(self) -> bool:
        """The agent polled lately, or a page is open (a browser counts) -
        not the Pi's own touchscreen, which would keep the phone connected
        with the PC off."""
        remote = any(ws not in self.local_clients for ws in self.clients)
        return remote or time.monotonic() - self._pc_seen < PC_GONE_SECONDS

    def _keep_call_on_phone(self, state) -> None:
        """A call already going when the iPhone connects stays on the iPhone.

        Turning the PC on mid-call unblocks and reconnects the phone, and
        iOS then moves the call's audio to the newly connected hands-free
        unit - the Pi, where nothing was playing it: the call went silent
        (2026-10-15). So for MID_CALL_GRACE_SECONDS after the phone connects,
        an active or held call, or the call audio arriving, means a call
        that was already going: refuse its audio, and send it back if it
        came. Move call audio to this PC still takes it over (it clears
        _phone_held), and calls that ring after this aren't affected.
        """
        now = time.monotonic()
        if state.connected and self._link_seen is False:
            self._connected_at = now
        self._link_seen = state.connected
        if (self._connected_at is None or now - self._connected_at >= MID_CALL_GRACE_SECONDS
                or self._pc_claimed or self._phone_held):
            return
        if not (state.audio_on_pi or any(c.state in ("active", "held") for c in state.calls)):
            return
        log.info("a call was already going when the iPhone connected: keeping it on the iPhone")
        self._phone_held = True  # RejectSCO on, from this tick
        if state.audio_on_pi:
            asyncio.create_task(self._send_audio_back())

    async def _send_audio_back(self) -> None:
        try:
            await self.release_audio()
        except TelephonyError as e:
            log.warning("couldn't send the call's audio back to the iPhone: %s", e)

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
            "phone_pc_present": int(self.pc_present()),
            "phone_media_on_pc": int(self.settings["mediaOnPc"]),
            "phone_media_listeners": len(self.media.listeners) if self.media else 0,
            "phone_contacts": len(self.contacts.names) if self.contacts else 0,
            "phone_reconnect_attempts_total": self.reconnector.attempts if self.reconnector else 0,
            "phone_reconnect_successes_total": self.reconnector.successes if self.reconnector else 0,
            "phone_call_rx_bytes_total": self.bridge.rx_bytes,
            "phone_call_tx_bytes_total": self.bridge.tx_bytes,
            "phone_media_sent_bytes_total": self.media.sent_bytes if self.media else 0,
            "phone_pc_checkins_total": self.pc_checkins,
            # The Pi's call screen is up (a call, not sent home): the display
            # under it pauses its animation (apps/dashboard, display.js).
            "phone_screen_shown": int(self.screen_shown()),
        }
        # The Pi's Bluetooth adapter, for the display's Bluetooth button. Left
        # out until the reconnector has looked (no data, not "off").
        powered = self.reconnector.powered if self.reconnector else None
        if powered is not None:
            values["phone_bluetooth_powered"] = int(powered)
        try:
            metrics.write(values, self.history.counts() if self.history else {})
        except OSError as e:
            log.warning("metrics not written: %s", e)

    async def _answer_on_pc(self, path: str) -> str | None:
        """Answer from the Pi's touchscreen, with the audio on the PC.

        The PC's window must be an audio page *before* the phone answers:
        with none, RejectSCO is on and the phone keeps the audio. So the
        handoff is published first (the PC's window, already ringing with
        its mic open, takes it and announces audio-ready), then the answer.
        If the PC doesn't take it in time, answer anyway - better on the
        iPhone than unanswered.
        """
        self._handoff = path
        await self.broadcast_state()
        try:
            # No page on another machine at all: nothing can take the audio,
            # so don't make the caller wait for it.
            has_pc_page = any(c not in self.local_clients for c in self.clients)
            deadline = time.monotonic() + (HANDOFF_WAIT_SECONDS if has_pc_page else 0)
            while not any(c not in self.local_clients for c in self.audio_clients):
                if time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.1)
            took = any(c not in self.local_clients for c in self.audio_clients)
            await self.tick()  # RejectSCO off now that a PC page has audio
            await self.tel.answer(path)
        finally:
            self._handoff = None
        if not took:
            return "Answered - the PC didn't take the audio, so it's on the iPhone"
        return None

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

    async def add(self, ws: WebSocket, local: bool = False) -> None:
        self.clients.append(ws)
        if local:
            self.local_clients.add(ws)
        await self.broadcast_state(force=True)
        await self.broadcast_extras(only=ws)

    async def remove(self, ws: WebSocket) -> None:
        self.local_clients.discard(ws)
        if ws in self.clients:
            self.clients.remove(ws)
        if ws in self.audio_clients:
            self.audio_clients.remove(ws)

    def audio_in(self, ws: WebSocket, pcm: bytes) -> None:
        if ws is self.audio_owner and self.bridge.running:
            # Muted: silence of the same length, so the phone's audio link
            # keeps its steady stream rather than running dry.
            self.bridge.write(bytes(len(pcm)) if self.muted else pcm)

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
            elif action == "set-limit":
                key = msg.get("key")
                if key not in LIMITS:
                    return "unknown setting"
                try:
                    value = int(msg.get("value"))
                except (TypeError, ValueError):
                    return "Not a number"
                low, high = LIMITS[key]
                self.settings[key] = min(max(value, low), high)
                save_settings(self.settings_path, self.settings)
                self.apply_limits()
                await self.broadcast_state(force=True)
            elif action == "clear-notifications":
                # Off the PC's list and the Pi's display - and the iPhone's, if set.
                await self._clear_on_phone(list(self.notifications))
                self.notifications.clear()
                await self.broadcast_extras()
            elif action == "dismiss-notification":
                uid = int(msg.get("uid", -1))
                await self._clear_on_phone([n for n in self.notifications if n["uid"] == uid])
                self.remove_notification(uid)
            elif action == "set-clear-on-phone":
                self.settings["clearOnPhone"] = bool(msg.get("value"))
                save_settings(self.settings_path, self.settings)
            elif action == "set-phone-notifications":
                self.settings["phoneNotifications"] = bool(msg.get("value"))
                save_settings(self.settings_path, self.settings)
                await self.broadcast_extras()
            elif action == "set-app-muted":
                app = str(msg.get("app", ""))[:200]
                muted = [a for a in self.settings["mutedApps"] if a != app]
                if msg.get("value") and app:
                    muted.append(app)
                    # Off the list too: muting is for not seeing it.
                    self.notifications = collections.deque(
                        (n for n in self.notifications if n["appId"] != app), maxlen=NOTIFICATIONS_KEPT)
                self.settings["mutedApps"] = muted
                save_settings(self.settings_path, self.settings)
                await self.broadcast_extras()
            elif action == "set-media-on-pc":
                self.settings["mediaOnPc"] = bool(msg.get("value"))
                save_settings(self.settings_path, self.settings)
                if self.media and not self.settings["mediaOnPc"]:
                    self.media.end_streams()
                if self.reconnector and self.reconnector.bus:
                    self.reconnector.media_changed()
                    asyncio.create_task(self.reconnector.check(time.monotonic()))
            elif action == "set-mute":
                self.muted = bool(msg.get("value"))
                await self.broadcast_state()
            elif action == "set-mic":
                self.settings["micLabel"] = str(msg.get("value", ""))[:200]
                save_settings(self.settings_path, self.settings)
            elif action == "audio-ready":
                if ws in self.local_clients:
                    return "The Pi's screen has no speakers or mic"
                if ws in self.audio_clients:
                    self.audio_clients.remove(ws)
                self.audio_clients.append(ws)
            elif action == "audio-off":
                # The page closed its mic and speakers (the agent's window
                # does once the call is over): no longer somewhere to send
                # a call, so RejectSCO goes back on.
                if ws in self.audio_clients:
                    self.audio_clients.remove(ws)
            elif action == "answer":
                await self.tel.answer(str(msg.get("call", "")))
            elif action == "answer-on-pc":
                return await self._answer_on_pc(str(msg.get("call", "")))
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
            elif action == "screen-home":
                # Home on the Pi's call screen: close it, back to the display.
                # Only that screen asks; the PC's window has nothing to hide.
                if ws not in self.local_clients:
                    return "Only the Pi's screen can do that"
                self._screen_hidden_for = frozenset(
                    c.path for c in self.tel.state.calls if c.state != "disconnected")
                return None
            elif action == "set-bluetooth":
                if not (self.reconnector and self.reconnector.bus):
                    return "Bluetooth control isn't available"
                try:
                    await self.reconnector.set_powered(bool(msg.get("value")))
                except RuntimeError as e:
                    return f"Bluetooth: {e}"
                # The tick below sees the change and writes the metrics.
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
