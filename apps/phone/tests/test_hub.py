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
    assert load_settings(tmp_path / "old.json") == {"keepPhoneAnswered": True, "micLabel": ""}


class FakeReconnector:
    def __init__(self):
        self.resets = []

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
