import struct

from app.mns import (
    BODY,
    CONNECT,
    DISCONNECT,
    END_OF_BODY,
    MNS_TARGET,
    PUT,
    PUT_FINAL,
    ObexSession,
    connect_response,
    events,
    headers,
)


def packet(op: int, payload: bytes = b"") -> bytes:
    return bytes([op]) + struct.pack(">H", 3 + len(payload)) + payload


def byte_header(hid: int, value: bytes) -> bytes:
    return bytes([hid]) + struct.pack(">H", 3 + len(value)) + value


REPORT = (
    b'<MAP-event-report version="1.0">'
    b'<event type="NewMessage" handle="20000100001" folder="TELECOM/MSG/INBOX" msg_type="SMS_GSM" />'
    b"</MAP-event-report>"
)


def test_headers_all_four_encodings():
    data = (
        byte_header(0x42, b"x-bt/MAP-event-report\x00")  # byte sequence
        + b"\x01" + struct.pack(">H", 7) + b"\x00a\x00b"  # unicode text
        + b"\x97\x01"  # 1-byte
        + b"\xcb" + struct.pack(">I", 7)  # 4-byte (ConnectionID)
    )
    assert headers(data) == [(0x42, b"x-bt/MAP-event-report\x00"), (0x01, b"\x00a\x00b"), (0x97, b"\x01"), (0xCB, struct.pack(">I", 7))]


def test_connect_response_names_mns():
    resp = connect_response()
    assert resp[0] == 0xA0 and struct.unpack(">H", resp[1:3])[0] == len(resp)
    assert resp.endswith(MNS_TARGET)  # Who header, so the phone knows it reached MNS


def test_put_report_across_two_packets_fires_the_event():
    seen = []
    session = ObexSession(sock=None, on_event=seen.append)
    assert session.handle(packet(CONNECT, b"\x10\x00\x20\x00" + byte_header(0x46, MNS_TARGET)))[0][0] == 0xA0
    half = len(REPORT) // 2
    reply, done = session.handle(packet(PUT, byte_header(BODY, REPORT[:half])))
    assert reply[0] == 0x90 and not done and seen == []  # Continue
    reply, done = session.handle(packet(PUT_FINAL, byte_header(END_OF_BODY, REPORT[half:])))
    assert reply[0] == 0xA0 and not done
    assert seen == [{"type": "NewMessage", "handle": "20000100001", "folder": "TELECOM/MSG/INBOX", "msg_type": "SMS_GSM"}]
    assert session.handle(packet(DISCONNECT)) == (packet(0xA0), True)


def test_events_parses_several():
    report = '<MAP-event-report><event type="NewMessage" handle="1"/><event type="MessageDeleted" handle="2"/></MAP-event-report>'
    assert [e["type"] for e in events(report)] == ["NewMessage", "MessageDeleted"]
