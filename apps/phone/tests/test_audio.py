from app.audio import find_bluez_nodes, pair_ports, ports_of

# `pw-link -o` during the live call on 2026-09-22.
OUTPUTS = """\
auto_null:monitor_FL
auto_null:monitor_FR
bluez_input.A0_EE_1A_96_E6_9E.0:output_FL
bluez_input.A0_EE_1A_96_E6_9E.0:output_FR
phone-bridge-tx:output_MONO
"""


def test_ports_of_matches_whole_node_name():
    assert ports_of(OUTPUTS, "bluez_input.A0_EE_1A_96_E6_9E.0") == [
        "bluez_input.A0_EE_1A_96_E6_9E.0:output_FL",
        "bluez_input.A0_EE_1A_96_E6_9E.0:output_FR",
    ]
    assert ports_of(OUTPUTS, "bluez_input.A0_EE_1A_96_E6_9E") == []


def test_stereo_into_mono_takes_one_channel():
    # Both channels carry the same mono call; linking both would double it.
    assert pair_ports(["in:FL", "in:FR"], ["rx:MONO"]) == [("in:FL", "rx:MONO")]


def test_mono_out_fans_to_every_channel():
    assert pair_ports(["tx:MONO"], ["out:FL", "out:FR"]) == [("tx:MONO", "out:FL"), ("tx:MONO", "out:FR")]


def test_pair_ports_empty():
    assert pair_ports([], ["x"]) == []


def test_find_bluez_nodes():
    dump = [
        {"id": 39, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "auto_null"}}},
        {"id": 99, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "bluez_input.A0_EE.0"}}},
        {"id": 90, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "bluez_output.A0_EE.1"}}},
        {"id": 87, "type": "PipeWire:Interface:Device", "info": {"props": {"device.name": "bluez_card.A0_EE"}}},
    ]
    assert find_bluez_nodes(dump) == {"bluez_input.A0_EE.0": 99, "bluez_output.A0_EE.1": 90}
