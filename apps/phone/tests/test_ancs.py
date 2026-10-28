import asyncio
import struct

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import ancs
from app.ancs import Ancs, Event, parse_event, parse_reply


def event_bytes(kind=ancs.EVENT_ADDED, flags=0, category=4, uid=7):
    return struct.pack("<BBBBI", kind, flags, category, 1, uid)


def notification_reply(uid, app_id, title, message):
    out = bytes([ancs.CMD_NOTIFICATION_ATTRS]) + struct.pack("<I", uid)
    for attr, value in ((0, app_id), (1, title), (3, message)):
        v = value.encode()
        out += bytes([attr]) + struct.pack("<H", len(v)) + v
    return out


def app_reply(app_id, name):
    v = name.encode()
    return bytes([ancs.CMD_APP_ATTRS]) + app_id.encode() + b"\0" + bytes([0]) + struct.pack("<H", len(v)) + v


# ---- the protocol ------------------------------------------------------------------


def test_events_parse():
    e = parse_event(event_bytes(flags=ancs.FLAG_PRE_EXISTING, uid=1234))
    assert e == Event(ancs.EVENT_ADDED, ancs.FLAG_PRE_EXISTING, 4, 1234)
    assert parse_event(b"\0\0") is None


def test_requests_are_what_ancs_expects():
    assert ancs.notification_request(5) == bytes([0, 5, 0, 0, 0, 0, 1, 64, 0, 3, 0, 1])
    assert ancs.app_request("com.apple.MobileSMS") == b"\x01com.apple.MobileSMS\x00\x00"


def test_replies_parse_whole_or_wait_for_the_rest():
    whole = notification_reply(9, "com.apple.MobileSMS", "Sam", "See you at 6")
    attrs, used = parse_reply(whole, 3)
    assert attrs == {0: "com.apple.MobileSMS", 1: "Sam", 3: "See you at 6"} and used == len(whole)
    for cut in (1, 5, 8, len(whole) - 1):
        assert parse_reply(whole[:cut], 3) is None  # split across packets: not yet
    assert parse_reply(app_reply("com.apple.MobileSMS", "Messages"), 1)[0] == {0: "Messages"}
    assert parse_reply(b"\x07junk", 1) == ({}, 5)  # not ours: dropped


# ---- the flow -------------------------------------------------------------------------


def make_ancs(enabled=True):
    got, written = [], []
    a = Ancs(got.append, enabled=lambda: enabled)
    a.chars = {ancs.NOTIFICATION_SOURCE: "/ns", ancs.CONTROL_POINT: "/cp", ancs.DATA_SOURCE: "/ds"}

    async def call(path, iface, member, signature="", body=()):
        written.append(bytes(body[0]))
        return []

    a._call = call
    return a, got, written


@pytest.mark.asyncio
async def test_a_new_notification_is_fetched_with_its_apps_name():
    a, got, written = make_ancs()
    a.on_event(parse_event(event_bytes(uid=9)))
    await asyncio.sleep(0)
    assert written == [ancs.notification_request(9)]
    reply = notification_reply(9, "com.apple.MobileSMS", "Sam", "See you at 6")
    a.on_data(reply[:6])  # in two packets
    a.on_data(reply[6:])
    await asyncio.sleep(0)
    assert written[-1] == ancs.app_request("com.apple.MobileSMS") and got == []  # its name first
    a.on_data(app_reply("com.apple.MobileSMS", "Messages"))
    await asyncio.sleep(0)
    assert len(got) == 1 and got[0]["app"] == "Messages" and got[0]["title"] == "Sam" and got[0]["message"] == "See you at 6"
    # The name is cached: the next one from Messages needs no second request.
    a.on_event(parse_event(event_bytes(uid=10)))
    await asyncio.sleep(0)
    a.on_data(notification_reply(10, "com.apple.MobileSMS", "Sam", "Running late"))
    await asyncio.sleep(0)
    assert len(got) == 2 and written.count(ancs.app_request("com.apple.MobileSMS")) == 1


@pytest.mark.asyncio
async def test_the_backlog_calls_updates_and_switched_off_are_skipped():
    a, got, written = make_ancs()
    a.on_event(parse_event(event_bytes(flags=ancs.FLAG_PRE_EXISTING)))  # what was there on subscribing
    a.on_event(parse_event(event_bytes(category=1)))  # an incoming call
    a.on_event(parse_event(event_bytes(category=2)))  # a missed call
    a.on_event(parse_event(event_bytes(kind=ancs.EVENT_MODIFIED)))
    await asyncio.sleep(0)
    assert written == []
    off, _, off_written = make_ancs(enabled=False)
    off.on_event(parse_event(event_bytes()))
    await asyncio.sleep(0)
    assert off_written == []


def test_removed_reaches_the_hub():
    removed = []
    a = Ancs(lambda n: None, on_removed=removed.append)
    a.chars = {ancs.NOTIFICATION_SOURCE: "/ns"}
    a.on_event(parse_event(event_bytes(kind=ancs.EVENT_REMOVED, uid=42)))
    assert removed == [42]


# ---- the hub and the PC app ---------------------------------------------------------------


def note(uid=1, app_id="com.apple.MobileSMS", app="Messages", t=100.0):
    return {"uid": uid, "appId": app_id, "app": app, "title": "Sam", "message": "hi", "category": "social", "time": t}


@pytest.mark.asyncio
async def test_hub_keeps_them_in_memory_and_honours_mutes(tmp_path):
    from app.hub import Hub

    hub = Hub(telephony=object(), bridge=object(), settings_path=tmp_path / "s.json")

    async def nothing(*a, **k):
        pass

    hub.broadcast_extras = nothing
    hub.tick = nothing
    hub.add_notification(note(uid=1))
    hub.add_notification(note(uid=2, t=50.0))  # an earlier clock still sorts after
    assert [n["uid"] for n in hub.notifications_since(0)] == [1, 2] and hub.notify_latest() > 100
    hub.remove_notification(1)
    assert [n["uid"] for n in hub.notifications] == [2]
    await hub.command(object(), {"action": "set-app-muted", "app": "com.apple.MobileSMS", "value": True})
    assert list(hub.notifications) == []  # muting clears it
    hub.add_notification(note(uid=3))
    assert list(hub.notifications) == [] and hub.extras()["notifyApps"][0] == {"id": "com.apple.MobileSMS", "name": "Messages", "muted": True}
    await hub.command(object(), {"action": "set-phone-notifications", "value": False})
    hub.add_notification(note(uid=4, app_id="com.burbn.instagram", app="Instagram"))
    assert list(hub.notifications) == []
    assert not (tmp_path / "s.json").read_text().count("hi")  # the text never reaches the disk


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "AGENT_TOKEN", "agent-token")

    async def no_hub():
        pass

    monkeypatch.setattr(main.hub, "run", no_hub)
    with TestClient(main.app, base_url="https://phone.home:8443") as c:
        yield c


def test_the_pc_app_gets_new_ones_by_time(client, monkeypatch):
    import collections

    monkeypatch.setattr(main.hub, "notifications", collections.deque([note(uid=1, t=10.0), note(uid=2, t=20.0)], maxlen=50))
    agent = {"Authorization": "Bearer agent-token"}
    assert client.get("/api/agent/ringing", headers=agent).json()["notifyLatest"] == 20.0
    got = client.get("/api/agent/notifications?since=10", headers=agent).json()["notifications"]
    assert [n["uid"] for n in got] == [2]
    assert client.get("/api/agent/notifications?since=0").status_code == 401


def test_the_pis_notifications_screen_is_for_the_pi_only(client, monkeypatch):
    import collections

    monkeypatch.setattr(main.hub, "notifications", collections.deque([note(uid=1)], maxlen=50))
    assert client.get("/screen/notifications").status_code == 404  # anything but the Pi itself
    assert client.get("/api/notifications").status_code == 401
    monkeypatch.setattr(main, "is_local", lambda c: True)  # the display's Chromium, on 127.0.0.1
    page = client.get("/screen/notifications")
    assert page.status_code == 200 and "/api/notifications" in page.text and page.headers["cache-control"] == "no-store"
    assert client.get("/api/notifications").json()["notifications"][0]["title"] == "Sam"


@pytest.mark.asyncio
async def test_clearing_one_or_all(tmp_path):
    from app.hub import Hub

    hub = Hub(telephony=object(), bridge=object(), settings_path=tmp_path / "s.json")

    async def nothing(*a, **k):
        pass

    hub.broadcast_extras = nothing
    hub.tick = nothing
    for uid in (1, 2, 3):
        hub.add_notification(note(uid=uid, t=float(uid)))
    await hub.command(object(), {"action": "dismiss-notification", "uid": 2})
    assert [n["uid"] for n in hub.notifications] == [1, 3]
    await hub.command(object(), {"action": "clear-notifications"})
    assert list(hub.notifications) == [] and hub.notify_latest() == 0.0
    hub.add_notification(note(uid=4, t=10.0))  # new ones still come through afterwards
    assert hub.notifications_since(0)[0]["uid"] == 4


def test_clear_request_is_ancs_negative_action():
    assert ancs.clear_request(7) == bytes([2, 7, 0, 0, 0, 1])


@pytest.mark.asyncio
async def test_a_notification_says_whether_the_iphone_can_clear_it():
    a, got, written = make_ancs()
    a.app_names["com.apple.MobileSMS"] = "Messages"
    a.on_event(parse_event(event_bytes(uid=9, flags=ancs.FLAG_NEGATIVE_ACTION)))
    await asyncio.sleep(0)
    a.on_data(notification_reply(9, "com.apple.MobileSMS", "Sam", "hi"))
    await asyncio.sleep(0)
    assert got[0]["canClear"] is True
    assert await a.clear(9) is True and written[-1] == ancs.clear_request(9)


@pytest.mark.asyncio
async def test_clearing_on_the_pc_clears_on_the_iphone_too(tmp_path):
    from app.hub import Hub

    hub = Hub(telephony=object(), bridge=object(), settings_path=tmp_path / "s.json")

    async def nothing(*a, **k):
        pass

    hub.broadcast_extras = nothing
    hub.tick = nothing
    cleared = []

    class FakeAncs:
        service = "/svc"

        async def clear(self, uid):
            cleared.append(uid)
            return True

    hub.ancs = FakeAncs()
    hub.add_notification({**note(uid=1, t=1.0), "canClear": True})
    hub.add_notification({**note(uid=2, t=2.0), "canClear": False})  # no Clear action: left on the iPhone
    hub.add_notification({**note(uid=3, t=3.0), "canClear": True})
    await hub.command(object(), {"action": "dismiss-notification", "uid": 1})
    assert cleared == [1]
    await hub.command(object(), {"action": "clear-notifications"})
    assert cleared == [1, 3] and list(hub.notifications) == []
    await hub.command(object(), {"action": "set-clear-on-phone", "value": False})
    hub.add_notification({**note(uid=4, t=4.0), "canClear": True})
    await hub.command(object(), {"action": "clear-notifications"})
    assert cleared == [1, 3]  # switched off: the iPhone keeps them


@pytest.mark.asyncio
async def test_follows_the_le_link_and_resubscribes_afresh(monkeypatch):
    from dbus_fast import Variant

    link = {"up": True}
    monkeypatch.setattr(ancs, "le_linked", lambda: link["up"])
    svc = "/org/bluez/hci0/dev_X/service0023"
    objects = {svc: {"org.bluez.GattService1": {"UUID": Variant("s", ancs.ANCS)}}}
    for i, uuid in enumerate((ancs.NOTIFICATION_SOURCE, ancs.CONTROL_POINT, ancs.DATA_SOURCE)):
        objects[f"{svc}/char{i}"] = {ancs.CHAR: {"UUID": Variant("s", uuid), "Service": Variant("o", svc)}}
    calls = []
    a = Ancs(lambda n: None)

    async def call(path, iface, member, signature="", body=()):
        calls.append(member)
        return [objects] if member == "GetManagedObjects" else []

    async def add_match(rule):
        pass

    async def scan():
        pass  # while it's down: no real scan (that runs the sudo helper)

    a._call, a._add_match, a.scan = call, add_match, scan
    await a.check()
    assert a.active and calls.count("StartNotify") == 2 and calls.index("StopNotify") < calls.index("StartNotify")
    calls.clear()
    await a.check()
    assert "StartNotify" not in calls  # still linked: nothing to redo
    link["up"] = False
    await a.check()
    assert not a.active and a.service is None  # dropped, though BlueZ still lists the service
    link["up"] = True
    calls.clear()
    await a.check()
    assert a.active and calls.count("StartNotify") == 2  # back: subscribed again, same service path


@pytest.mark.asyncio
async def test_the_advertisement_is_registered_again_after_bluetoothd_restarts():
    from types import SimpleNamespace

    from dbus_fast import MessageType

    owner = {"name": ":1.10"}
    registered = []
    a = Ancs(lambda n: None)

    class Bus:
        async def call(self, msg):
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[owner["name"]])

    async def call(path, iface, member, signature="", body=()):
        registered.append(member)
        return []

    a.bus, a._call = Bus(), call
    await a.keep_advertising()
    await a.keep_advertising()
    assert registered == ["RegisterAdvertisement"]  # once while bluetoothd stays
    owner["name"] = ":1.99"  # bluetoothd restarted: it has forgotten it
    await a.keep_advertising()
    a.forget_advertisement()  # or BlueZ released it
    await a.keep_advertising()
    assert registered == ["RegisterAdvertisement"] * 3


@pytest.mark.asyncio
async def test_a_bluez_call_that_never_answers_gives_up(monkeypatch):
    monkeypatch.setattr(ancs, "CALL_SECONDS", 0.05)
    a = Ancs(lambda n: None)

    class Bus:
        async def call(self, msg):
            await asyncio.sleep(3600)  # bluetoothd stalled

    a.bus = Bus()
    with pytest.raises(RuntimeError, match="no reply from BlueZ"):
        await a._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")


@pytest.mark.asyncio
async def test_while_unlinked_it_scans_every_minute_but_not_during_a_call(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(ancs.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(ancs, "le_linked", lambda: False)
    busy = {"on": False}
    a = Ancs(lambda n: None, busy=lambda: busy["on"])
    scans = []

    async def scan():
        scans.append(now["t"])

    a.scan = scan
    await a.check()
    await asyncio.sleep(0)
    assert scans == [1000.0]
    now["t"] += 30
    await a.check()
    await asyncio.sleep(0)
    assert len(scans) == 1  # not yet a minute
    now["t"] += 30
    busy["on"] = True
    await a.check()
    await asyncio.sleep(0)
    assert len(scans) == 1  # a call on the Pi: the radio's busy
    busy["on"] = False
    await a.check()
    await asyncio.sleep(0)
    assert len(scans) == 2


@pytest.mark.asyncio
async def test_a_scan_sets_auto_connect_and_stops_once_linked(monkeypatch):
    link = {"up": False}
    monkeypatch.setattr(ancs, "le_linked", lambda: link["up"])
    calls = []
    a = Ancs(lambda n: None)

    async def call(path, iface, member, signature="", body=()):
        calls.append(member)
        if member == "StartDiscovery":
            link["up"] = True  # the kernel connected it during the scan
        return []

    async def autoconnect():
        calls.append("autoconnect")

    async def sleep(_):
        pass

    monkeypatch.setattr(ancs.asyncio, "sleep", sleep)
    a._call, a.autoconnect = call, autoconnect
    await a.scan()
    assert calls == ["autoconnect", "SetDiscoveryFilter", "StartDiscovery", "StopDiscovery"]

    async def no_helper():
        raise RuntimeError("sudo: a password is required")

    calls.clear()
    a.autoconnect = no_helper
    await a.scan()
    assert calls == []  # no auto-connect entry: a scan alone wouldn't link it
