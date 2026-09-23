from dbus_fast import Variant

from app.telephony import PhoneState, parse_calls, parse_gateways, valid_number, valid_tones

# Shapes as returned live on 2026-09-22 by WirePlumber 0.5.8 / PipeWire 1.4.2.
OBJECTS = {
    "/org/pipewire/Telephony/ag1": {
        "org.pipewire.Telephony.AudioGateway1": {"Address": Variant("s", "A0:EE:1A:96:E6:9E")},
        "org.pipewire.Telephony.AudioGatewayTransport1": {
            "Codec": Variant("y", 1),
            "State": Variant("s", "active"),
            "RejectSCO": Variant("b", False),
        },
    },
}

# GetCalls as dbus-fast decodes PipeWire's reply: a{oa{sv}}, a dict.
CALLS = {
    "/org/pipewire/Telephony/ag1/call1": {
        "LineIdentification": Variant("s", "+15555550123"),
        "IncomingLine": Variant("s", ""),
        "Name": Variant("s", ""),
        "Multiparty": Variant("b", False),
        "State": Variant("s", "incoming"),
    },
}


def test_parse_gateways():
    assert parse_gateways(OBJECTS) == [("/org/pipewire/Telephony/ag1", "A0:EE:1A:96:E6:9E", "active")]


def test_parse_gateways_ignores_other_objects():
    assert parse_gateways({"/org/pipewire/Telephony": {"org.ofono.Manager": {}}}) == []


def test_parse_calls():
    [call] = parse_calls(CALLS)
    assert call.path == "/org/pipewire/Telephony/ag1/call1"
    assert call.state == "incoming"
    assert call.number == "+15555550123"


def test_state_json_hides_bus_paths_except_calls():
    state = PhoneState(True, "/org/pipewire/Telephony/ag1", "A0:EE", "active", parse_calls(CALLS))
    out = state.to_json()
    assert out["connected"] and out["audioOnPi"]
    assert "gateway" not in out and "address" not in out
    assert out["calls"][0]["state"] == "incoming"


def test_number_validation():
    assert valid_number("+15555550123")
    assert valid_number("*86")
    assert not valid_number("")
    assert not valid_number("555;ATH")  # would be an AT command injection
    assert not valid_number("1" * 33)


def test_tone_validation():
    assert valid_tones("1#")
    assert not valid_tones("+1")
