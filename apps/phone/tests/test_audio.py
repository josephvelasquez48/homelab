import pytest

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


def _node(id_, name, profile):
    return {"type": "PipeWire:Interface:Node", "id": id_, "info": {"props": {"node.name": name, "api.bluez5.profile": profile}}}


def test_call_bridge_ignores_the_media_stream():
    dump = [
        _node(80, "bluez_input.A0_EE.2", "a2dp-source"),  # music: comes first, must not be taken for the call
        _node(81, "bluez_input.A0_EE.0", "headset-audio-gateway"),
        _node(82, "bluez_output.A0_EE.1", "headset-audio-gateway"),
    ]
    assert find_bluez_nodes(dump) == {"bluez_input.A0_EE.0": 81, "bluez_output.A0_EE.1": 82}


def test_media_node_is_the_a2dp_input():
    from app.media import find_media_node

    dump = [_node(81, "bluez_input.A0_EE.0", "headset-audio-gateway"), _node(80, "bluez_input.A0_EE.2", "a2dp-source")]
    assert find_media_node(dump) == ("bluez_input.A0_EE.2", 80)
    assert find_media_node(dump[:1]) is None


def test_roles_drop_in_follows_the_mode(tmp_path):
    from app.media import ROLES_CONF, write_roles

    path = tmp_path / "52-phone-media.conf"
    assert write_roles(path) is True  # first start: WirePlumber restarts once
    assert "a2dp_sink" in path.read_text() and path.read_text() == ROLES_CONF
    assert write_roles(path) is False  # after that, never again


@pytest.mark.asyncio
async def test_slow_media_listener_loses_the_oldest_audio():
    from app.media import QUEUE_CHUNKS, MediaBridge

    media = MediaBridge()
    q = media.subscribe()
    for i in range(QUEUE_CHUNKS + 3):
        media._offer(q, bytes([i]))
    assert q.qsize() == QUEUE_CHUNKS
    assert q.get_nowait() == bytes([3])  # the first three were dropped


@pytest.mark.asyncio
async def test_media_counts_only_audible_bytes_per_listener():
    # The display's dots for music: a paused phone keeps the stream open and
    # sends silence, which mustn't count; each listener gets its own copy.
    import array
    import asyncio
    from types import SimpleNamespace

    from app.media import CHUNK_BYTES, MediaBridge

    loud = array.array("h", [1200] * (CHUNK_BYTES // 2)).tobytes()
    silent = bytes(CHUNK_BYTES)
    stdout = asyncio.StreamReader()
    stdout.feed_data(loud + silent + loud)
    stdout.feed_eof()

    media = MediaBridge()
    a, b = media.subscribe(), media.subscribe()
    media.proc = SimpleNamespace(stdout=stdout, returncode=0)
    await media._pump()
    assert media.sent_bytes == 2 * CHUNK_BYTES * 2  # two loud chunks, two listeners
    assert a.qsize() == b.qsize() == 3  # silence is still delivered, just not counted


def test_call_audio_bytes_count_what_is_written():
    from types import SimpleNamespace

    from app.audio import AudioBridge

    written = []
    bridge = AudioBridge()
    bridge.tx = SimpleNamespace(stdin=SimpleNamespace(
        is_closing=lambda: False, write=written.append,
        transport=SimpleNamespace(get_write_buffer_size=lambda: 0)))
    bridge.write(b"\x01\x00" * 160)
    assert bridge.tx_bytes == 320 and written
