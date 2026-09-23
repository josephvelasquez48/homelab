import pytest

from app.hub import Hub
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
        self.answered.append(path)


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


def make(transport="idle", calls=()):
    tel = FakeTelephony(PhoneState(True, "/ag1", "A0", transport, list(calls)))
    return Hub(tel, FakeBridge()), tel


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
    assert tel.answered == ["/ag1/call1"]


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
