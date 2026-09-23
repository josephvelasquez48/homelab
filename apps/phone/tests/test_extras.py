import pytest
from dbus_fast import Variant

from app.contacts import number_key, parse_vcards
from app.history import CallLog
from app.metrics import render
from app.reconnect import HFP_AG_UUID, reconnect_candidates
from app.telephony import Call

# -- call history ---------------------------------------------------------------


def c(path, state, number="+15555550123", name=""):
    return Call(path, state, number, name)


def test_answered_incoming_call(tmp_path):
    log = CallLog(tmp_path / "calls.db")
    log.observe([c("/ag1/call1", "incoming")], bridged=False, now=100)
    log.observe([c("/ag1/call1", "active")], bridged=True, now=105)
    assert log.observe([], bridged=False, now=165) == []  # not missed
    [row] = log.recent()
    assert row["direction"] == "in" and row["seconds"] == 60 and row["on_pc"] == 1 and not row["missed"]


def test_missed_call_is_reported_once(tmp_path):
    log = CallLog(tmp_path / "calls.db")
    log.observe([c("/ag1/call1", "incoming", name="Mom")], bridged=False, now=100)
    [missed] = log.observe([], bridged=False, now=120)
    assert missed["missed"] and missed["name"] == "Mom"
    assert log.observe([], bridged=False, now=130) == []
    assert log.last_missed()["id"] == missed["id"]


def test_reused_call_path_is_a_new_call(tmp_path):
    # PipeWire reuses /ag1/call1 for the next call.
    log = CallLog(tmp_path / "calls.db")
    log.observe([c("/ag1/call1", "dialing")], bridged=False, now=100)
    log.observe([], bridged=False, now=110)
    log.observe([c("/ag1/call1", "incoming")], bridged=False, now=200)
    log.observe([], bridged=False, now=210)
    rows = log.recent()
    assert [r["direction"] for r in rows] == ["in", "out"]
    assert log.counts() == {("in", "missed"): 1, ("out", "unanswered"): 1}


def test_history_survives_a_restart(tmp_path):
    CallLog(tmp_path / "calls.db").observe([c("/ag1/call1", "dialing")], bridged=False, now=1)
    assert len(CallLog(tmp_path / "calls.db").recent()) == 1


# -- contacts -----------------------------------------------------------------------

VCARDS = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Mom\r\nN:;Mom;;;\r\nTEL;TYPE=CELL:+1 (760) 555-0123\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Doe;Jane;;;\r\nTEL:760-555-0199\r\nTEL:+44 20 7946 0000\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:2.1\r\nFN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Jos=C3=A9\r\nTEL:5550100\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No Number\r\nEND:VCARD\r\n"
)


def test_parse_vcards():
    names = parse_vcards(VCARDS)
    assert names[number_key("7605550123")] == "Mom"
    assert names[number_key("+17605550199")] == "Jane Doe"  # from N when there's no FN
    assert names[number_key("+44 20 7946 0000")] == "Jane Doe"
    assert names[number_key("555-0100")] == "José"
    assert "No Number" not in names.values()


def test_number_key_matches_formats():
    assert number_key("+1 (760) 555-0123") == number_key("7605550123") == number_key("1-760-555-0123")
    assert number_key("") == ""


# -- reconnect ---------------------------------------------------------------------


def dev(paired=True, trusted=True, connected=False, uuids=(HFP_AG_UUID,)):
    return {
        "org.bluez.Device1": {
            "Paired": Variant("b", paired),
            "Trusted": Variant("b", trusted),
            "Connected": Variant("b", connected),
            "UUIDs": Variant("as", list(uuids)),
        }
    }


def test_reconnect_candidates():
    objects = {
        "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
        "/org/bluez/hci0/dev_phone": dev(),
        "/org/bluez/hci0/dev_connected": dev(connected=True),
        "/org/bluez/hci0/dev_untrusted": dev(trusted=False),
        "/org/bluez/hci0/dev_headphones": dev(uuids=["0000110b-0000-1000-8000-00805f9b34fb"]),
    }
    assert reconnect_candidates(objects) == ["/org/bluez/hci0/dev_phone"]


# -- metrics ------------------------------------------------------------------------


def test_metrics_render():
    text = render({"phone_connected": 1, "phone_reconnect_attempts_total": 3}, {("in", "missed"): 2}, {})
    assert "# TYPE phone_connected gauge\nphone_connected 1\n" in text
    assert "# TYPE phone_reconnect_attempts_total counter" in text
    assert 'phone_calls_total{direction="in",outcome="missed"} 2' in text
    assert text.endswith("\n")


# -- send audio back to the phone -------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_to_phone_holds_the_link_off_until_pc_asks(tmp_path, monkeypatch):
    from app import hub as hub_module
    from test_hub import FakeSocket, make

    hub, tel = make(transport="active", calls=[c("/ag1/call1", "active")], settings_path=tmp_path / "s.json")
    page = FakeSocket()
    await hub.add(page)
    await hub.command(page, {"action": "audio-ready"})
    assert tel.reject_sco[-1] is False

    ran = []

    async def fake_exec(*args, **kwargs):
        ran.append(args)

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return P()

    monkeypatch.setattr(hub_module.asyncio, "create_subprocess_exec", fake_exec)
    assert await hub.command(page, {"action": "audio-to-phone"}) is None
    assert ran and ran[0][-1].endswith("phone-bridge-release-sco")
    assert tel.reject_sco[-1] is True  # the phone can't bounce it straight back

    await hub.command(page, {"action": "audio-to-pc"})
    assert tel.reject_sco[-1] is False

    tel.state.calls = []
    await hub.tick()
    assert not hub._phone_held


def test_metrics_keep_full_timestamp_precision():
    text = render({"phone_bridge_last_update_timestamp_seconds": 1790133259.25}, {}, {})
    assert "phone_bridge_last_update_timestamp_seconds 1790133259.25\n" in text
