import tempfile
from pathlib import Path

import pytest

from app.hub import Hub, load_settings
from app.telephony import Call, PhoneState, TelephonyError


class FakeTelephony:
    def __init__(self, state: PhoneState):
        self.state = state
        self.reject_sco = []
        self.answered = []

    async def refresh(self):
        return self.state

    async def set_reject_sco(self, reject):
        self.reject_sco.append(reject)

    async def answer(self, path):
        if path not in {c.path for c in self.state.calls}:
            raise TelephonyError("no such call")
        # Record RejectSCO as it stood when the phone was told to answer.
        self.answered.append((path, self.reject_sco[-1] if self.reject_sco else None))

    async def activate_audio(self):
        self.activated = True

    async def dial(self, number):
        self.dialed_with_reject = self.reject_sco[-1] if self.reject_sco else None
        if number == "bad":
            raise TelephonyError("invalid number")


class FakeBridge:
    def __init__(self):
        self.running = False
        self.starts = 0

    async def start(self):
        self.starts += 1
        self.running = True

    async def stop(self):
        self.running = False

    async def read(self):
        return b""

    def write(self, pcm):
        pass


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


def make(transport="idle", calls=(), settings_path=None):
    tel = FakeTelephony(PhoneState(True, "/ag1", "A0", transport, list(calls)))
    # Never the real ~/.config/phone-bridge/settings.json.
    path = settings_path or Path(tempfile.mkdtemp()) / "settings.json"
    return Hub(tel, FakeBridge(), settings_path=path), tel


@pytest.mark.asyncio
async def test_rejects_call_audio_until_a_page_enables_audio():
    hub, tel = make()
    viewer = FakeSocket()
    await hub.add(viewer)
    await hub.tick()
    assert tel.reject_sco == [True]  # a page that only watches doesn't count

    await hub.command(viewer, {"action": "audio-ready"})
    assert tel.reject_sco == [True, False]

    await hub.remove(viewer)
    await hub.tick()
    assert tel.reject_sco == [True, False, True]


@pytest.mark.asyncio
async def test_bridge_follows_the_audio_link():
    hub, tel = make(transport="active")
    page = FakeSocket()
    await hub.add(page)
    await hub.tick()
    assert not hub.bridge.running  # no audio-enabled page yet

    await hub.command(page, {"action": "audio-ready"})
    assert hub.bridge.running

    await hub.tick()
    assert hub.bridge.starts == 1

    tel.state.transport = "idle"
    await hub.tick()
    assert not hub.bridge.running


@pytest.mark.asyncio
async def test_unknown_call_path_is_refused():
    call = Call("/ag1/call1", "incoming", "+15555550123", "")
    hub, tel = make(calls=[call])
    page = FakeSocket()
    assert await hub.command(page, {"action": "answer", "call": "/org/freedesktop/Anything"}) == "no such call"
    assert await hub.command(page, {"action": "answer", "call": "/ag1/call1"}) is None
    assert [path for path, _ in tel.answered] == ["/ag1/call1"]


@pytest.mark.asyncio
async def test_unknown_action():
    hub, _ = make()
    assert await hub.command(FakeSocket(), {"action": "format-disk"}) == "unknown action"


@pytest.mark.asyncio
async def test_bridge_starts_on_pending_link():
    # "pending" = the phone opened SCO and is sending; the link only turns
    # "active" once the bridge consumes it. Waiting for "active" deadlocked
    # on a live call.
    hub, tel = make(transport="pending")
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "audio-ready"})
    assert hub.bridge.running
    assert hub.snapshot()["audioOnPi"]


RINGING = Call("/ag1/call1", "incoming", "+15555550123", "")


async def audio_page(hub):
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "audio-ready"})
    return page


@pytest.mark.asyncio
async def test_keep_phone_rejects_audio_even_with_a_page_open(tmp_path):
    hub, tel = make(calls=[RINGING], settings_path=tmp_path / "s.json")
    page = await audio_page(hub)
    assert tel.reject_sco[-1] is False  # default: an audio page takes calls

    await hub.command(page, {"action": "set-keep-phone", "value": True})
    assert tel.reject_sco[-1] is True
    assert hub.snapshot()["settings"]["keepPhoneAnswered"] is True
    assert load_settings(tmp_path / "s.json")["keepPhoneAnswered"] is True  # survives restart


@pytest.mark.asyncio
async def test_answering_on_pc_lifts_reject_before_the_phone_answers(tmp_path):
    hub, tel = make(calls=[RINGING], settings_path=tmp_path / "s.json")
    page = await audio_page(hub)
    await hub.command(page, {"action": "set-keep-phone", "value": True})

    await hub.command(page, {"action": "answer", "call": "/ag1/call1"})
    assert tel.answered == [("/ag1/call1", False)]

    # Call over: the claim ends, and the next call answered on the phone stays there.
    tel.state.calls = []
    await hub.tick()
    assert tel.reject_sco[-1] is True


@pytest.mark.asyncio
async def test_dial_from_pc_claims_the_call(tmp_path):
    hub, tel = make(settings_path=tmp_path / "s.json")
    page = await audio_page(hub)
    await hub.command(page, {"action": "set-keep-phone", "value": True})
    await hub.command(page, {"action": "dial", "number": "5555550123"})
    assert tel.dialed_with_reject is False
    # The call appears after dialing and the claim holds for it.
    tel.state.calls = [Call("/ag1/call2", "dialing", "5555550123", "")]
    await hub.tick()
    assert tel.reject_sco[-1] is False


@pytest.mark.asyncio
async def test_failed_dial_drops_the_claim(tmp_path):
    hub, tel = make(settings_path=tmp_path / "s.json")
    page = await audio_page(hub)
    await hub.command(page, {"action": "set-keep-phone", "value": True})
    assert await hub.command(page, {"action": "dial", "number": "bad"}) == "invalid number"
    assert tel.reject_sco[-1] is True


@pytest.mark.asyncio
async def test_mic_choice_is_saved_and_shared(tmp_path):
    hub, _ = make(settings_path=tmp_path / "s.json")
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "set-mic", "value": "Mic/Inst (Samson G-Track Pro)"})
    assert hub.snapshot()["settings"]["micLabel"] == "Mic/Inst (Samson G-Track Pro)"
    assert load_settings(tmp_path / "s.json")["micLabel"] == "Mic/Inst (Samson G-Track Pro)"
    # An older settings file without the key still loads.
    (tmp_path / "old.json").write_text('{"keepPhoneAnswered": true}')
    assert load_settings(tmp_path / "old.json") == {"keepPhoneAnswered": True, "micLabel": "", "mediaOnPc": True}


class FakeReconnector:
    def __init__(self):
        self.resets = []
        self.powered = True

    async def reset_hands_free(self, address):
        self.resets.append(address)


@pytest.mark.asyncio
async def test_audio_with_no_call_resets_hands_free_once(monkeypatch):
    import asyncio

    import app.hub as hub_module

    monkeypatch.setattr(hub_module, "ORPHAN_AUDIO_SECONDS", 0)
    hub, tel = make(transport="pending")
    hub.reconnector = FakeReconnector()
    for _ in range(3):
        await hub.tick()
        await asyncio.sleep(0)
    assert hub.reconnector.resets == ["A0"]  # once, not every tick

    tel.state.transport = "idle"  # the audio link went away: a new stretch may reset again
    await hub.tick()
    tel.state.transport = "pending"
    await hub.tick()
    await asyncio.sleep(0)
    assert hub.reconnector.resets == ["A0", "A0"]


@pytest.mark.asyncio
async def test_audio_with_a_call_is_left_alone(monkeypatch):
    import asyncio

    import app.hub as hub_module

    monkeypatch.setattr(hub_module, "ORPHAN_AUDIO_SECONDS", 0)
    hub, tel = make(transport="pending", calls=[Call("/ag1/call1", "active", "+1555", "")])
    hub.reconnector = FakeReconnector()
    await hub.tick()
    await asyncio.sleep(0)
    assert hub.reconnector.resets == []


@pytest.mark.asyncio
async def test_audio_to_pc_when_audio_already_on_pi_just_bridges(tmp_path):
    hub, tel = make(transport="pending", calls=[Call("/ag1/call1", "active", "+1555", "")], settings_path=tmp_path / "s.json")
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "audio-ready"})
    assert await hub.command(page, {"action": "audio-to-pc"}) is None
    assert not getattr(tel, "activated", False)
    assert hub.bridge.running


@pytest.mark.asyncio
async def test_bridge_stops_when_its_page_leaves_mid_call():
    hub, tel = make(transport="active", calls=[Call("/ag1/call1", "active", "+1555", "")])
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "audio-ready"})
    assert hub.bridge.running

    await hub.remove(page)
    await hub.tick()
    assert not hub.bridge.running
    assert hub.snapshot()["bridged"] is False  # so the next page offers Take on PC


@pytest.mark.asyncio
async def test_media_switch_works_mid_call_and_ends_the_pc_stream(tmp_path):
    from app.media import MediaBridge

    hub, tel = make(calls=[Call("/ag1/call1", "active", "+1555", "")], settings_path=tmp_path / "s.json")
    hub.media = MediaBridge()
    listener = hub.media.subscribe()
    page = FakeSocket()
    assert await hub.command(page, {"action": "set-media-on-pc", "value": False}) is None  # no restart, so no refusal
    assert hub.settings["mediaOnPc"] is False
    assert load_settings(tmp_path / "s.json")["mediaOnPc"] is False
    assert listener.get_nowait() is None  # the agent's stream ends


@pytest.mark.asyncio
async def test_page_that_closes_its_audio_stops_counting():
    hub, tel = make()
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "audio-ready"})
    assert tel.reject_sco[-1] is False
    await hub.command(page, {"action": "audio-off"})
    assert hub.audio_clients == []
    assert tel.reject_sco[-1] is True  # a call answered on the iPhone stays there again


@pytest.mark.asyncio
async def test_pis_own_screen_does_not_count_as_the_pc(monkeypatch):
    import app.hub as hub_module

    clock = {"t": 1000.0}
    monkeypatch.setattr(hub_module.time, "monotonic", lambda: clock["t"])
    hub, tel = make()
    clock["t"] += hub_module.PC_GONE_SECONDS + 1  # the PC's agent stopped polling
    await hub.add(FakeSocket(), local=True)  # only the Pi's touchscreen is open
    assert not hub.pc_present()
    await hub.add(FakeSocket())  # a page on another machine does count
    assert hub.pc_present()


@pytest.mark.asyncio
async def test_touchscreen_answer_hands_the_audio_to_the_pc_first(tmp_path):
    import asyncio

    hub, tel = make(calls=[Call("/ag1/call1", "incoming", "+1555", "")], settings_path=tmp_path / "s.json")
    touch, pc = FakeSocket(), FakeSocket()
    await hub.add(touch, local=True)
    await hub.add(pc)

    async def pc_window():  # the PC's ringing window sees the handoff and takes the audio
        while hub.snapshot()["handoff"] != "/ag1/call1":
            await asyncio.sleep(0.01)
        await hub.command(pc, {"action": "audio-ready"})

    taker = asyncio.create_task(pc_window())
    assert await hub.command(touch, {"action": "answer-on-pc", "call": "/ag1/call1"}) is None
    await taker
    # Answered only once the PC had the audio, with RejectSCO off - so the phone sends it to the Pi.
    assert tel.answered == [("/ag1/call1", False)]
    assert hub.snapshot()["handoff"] is None


@pytest.mark.asyncio
async def test_touchscreen_answer_still_answers_if_the_pc_never_takes_it(monkeypatch, tmp_path):
    import app.hub as hub_module

    monkeypatch.setattr(hub_module, "HANDOFF_WAIT_SECONDS", 0.05)
    hub, tel = make(calls=[Call("/ag1/call1", "incoming", "+1555", "")], settings_path=tmp_path / "s.json")
    touch = FakeSocket()
    await hub.add(touch, local=True)
    error = await hub.command(touch, {"action": "answer-on-pc", "call": "/ag1/call1"})
    assert "on the iPhone" in error
    assert [path for path, _ in tel.answered] == ["/ag1/call1"]


@pytest.mark.asyncio
async def test_pis_screen_cannot_become_an_audio_page():
    hub, tel = make()
    touch = FakeSocket()
    await hub.add(touch, local=True)
    assert "no speakers" in await hub.command(touch, {"action": "audio-ready"})
    assert hub.audio_clients == []


@pytest.mark.asyncio
async def test_touchscreen_answer_does_not_wait_when_no_pc_page_is_open(tmp_path):
    import time as clock

    hub, tel = make(calls=[Call("/ag1/call1", "incoming", "+1555", "")], settings_path=tmp_path / "s.json")
    touch = FakeSocket()
    await hub.add(touch, local=True)  # only the Pi's screen - nothing to hand the audio to
    started = clock.monotonic()
    await hub.command(touch, {"action": "answer-on-pc", "call": "/ag1/call1"})
    assert clock.monotonic() - started < 1  # not the 4 s handoff wait
    assert [path for path, _ in tel.answered] == ["/ag1/call1"]


@pytest.mark.asyncio
async def test_mute_is_shared_silences_the_uplink_and_resets_after_the_call():
    hub, tel = make(transport="active", calls=[Call("/c1", "active", "+1555", "")])
    written = []
    hub.bridge.write = written.append
    pc, pi = FakeSocket(), FakeSocket()
    await hub.add(pc)
    await hub.add(pi, local=True)
    await hub.command(pc, {"action": "audio-ready"})
    await hub.tick()
    assert hub.bridge.running

    # The Pi's screen mutes; every page is told, and the PC's mic is silenced.
    await hub.command(pi, {"action": "set-mute", "value": True})
    assert '"muted": true' in pc.sent[-1] and '"muted": true' in pi.sent[-1]
    hub.audio_in(pc, b"\x01\x02\x03\x04")
    assert written[-1] == b"\x00\x00\x00\x00"

    await hub.command(pc, {"action": "set-mute", "value": False})
    hub.audio_in(pc, b"\x01\x02\x03\x04")
    assert written[-1] == b"\x01\x02\x03\x04"

    await hub.command(pi, {"action": "set-mute", "value": True})
    tel.state.calls = []
    await hub.tick()
    assert hub.muted is False  # the next call starts unmuted


@pytest.mark.asyncio
async def test_home_on_the_pi_screen_hides_it_until_a_new_call():
    # Home on the Pi's call screen: closed for the call up now, back for a new one.
    active = Call("/ag1/call1", "active", "+15555550123", "")
    hub, tel = make(transport="active", calls=[active])
    touch, pc = FakeSocket(), FakeSocket()
    await hub.add(touch, local=True)
    await hub.add(pc)
    assert not hub.screen_hidden()

    assert await hub.command(pc, {"action": "screen-home"}) == "Only the Pi's screen can do that"
    assert not hub.screen_hidden()

    assert await hub.command(touch, {"action": "screen-home"}) is None
    assert hub.screen_hidden()  # same call: stays home

    tel.state.calls.append(Call("/ag1/call2", "waiting", "+15555550199", ""))
    assert not hub.screen_hidden()  # another call rings: the screen comes back

    tel.state.calls.clear()
    assert not hub.screen_hidden()  # all over: forgotten
    tel.state.calls.append(Call("/ag1/call1", "incoming", "+15555550123", ""))
    assert not hub.screen_hidden()  # even a reused path is a new call


@pytest.mark.asyncio
async def test_the_pi_call_screen_counts_as_up_unless_sent_home():
    # phone_screen_shown: the display under the call screen pauses while it's up.
    hub, tel = make(transport="active", calls=[Call("/ag1/call1", "active", "+15555550123", "")])
    assert hub.screen_shown()
    touch = FakeSocket()
    await hub.add(touch, local=True)
    await hub.command(touch, {"action": "screen-home"})
    assert not hub.screen_shown()  # sent home: the display runs
    tel.state.calls.clear()
    assert not hub.screen_shown()  # no call


class FakeAdapter:
    """A reconnector stand-in that owns the Pi's Bluetooth adapter."""

    def __init__(self, powered=True, error=None):
        self.powered = powered
        self.bus = object()
        self.error = error

    async def set_powered(self, on):
        if self.error:
            raise RuntimeError(self.error)
        self.powered = on


@pytest.mark.asyncio
async def test_bluetooth_switch_turns_the_adapter_off_and_on():
    hub, _ = make()
    hub.reconnector = FakeAdapter()
    page = FakeSocket()
    await hub.add(page)
    assert hub.snapshot()["bluetooth"] is True
    assert await hub.command(page, {"action": "set-bluetooth", "value": False}) is None
    assert hub.snapshot()["bluetooth"] is False
    assert await hub.command(page, {"action": "set-bluetooth", "value": True}) is None
    assert hub.snapshot()["bluetooth"] is True


@pytest.mark.asyncio
async def test_bluetooth_switch_reports_failures_and_missing_control():
    hub, _ = make()
    page = FakeSocket()
    await hub.add(page)
    assert hub.snapshot()["bluetooth"] is None  # no reconnector: the page hides the switch
    assert await hub.command(page, {"action": "set-bluetooth", "value": False}) == "Bluetooth control isn't available"
    hub.reconnector = FakeAdapter(error="org.bluez.Error.Busy")
    assert await hub.command(page, {"action": "set-bluetooth", "value": False}) == "Bluetooth: org.bluez.Error.Busy"


@pytest.mark.asyncio
async def test_metrics_written_when_the_phone_or_bluetooth_changes(monkeypatch):
    hub, tel = make()
    hub.reconnector = FakeAdapter()
    hub.write_metrics = True
    written = []
    monkeypatch.setattr(hub, "_write_metrics", lambda: written.append(1))
    await hub.tick()
    await hub.tick()
    assert len(written) == 1  # once for the first state, not every tick
    hub.reconnector.powered = False
    await hub.tick()
    tel.state.connected = False
    await hub.tick()
    assert len(written) == 3
