from openclips import constants as C
from openclips.framing import bqs_frame, request_payload, strip_bqs
from openclips.pb import first, parse_pb


def test_request_envelope():
    body = request_payload(C.RT_LIST_MOMENTS, b"\x09" + b"\x01" * 8, seq=7)
    f = parse_pb(body)
    assert C.RT_LIST_MOMENTS in f
    assert first(f, C.SEQ_FIELD) == 7


def test_frame_roundtrip_short_and_long():
    for body in (b"", b"x" * 5, b"y" * 200, b"z" * 400):
        frame = bqs_frame(body)
        assert strip_bqs(frame) == body


def test_strip_leaves_unprefixed_data_alone():
    raw = bytes.fromhex("6a06080118002000c00208")  # a bare Response
    assert strip_bqs(raw) == raw


def test_keepalive_frame_matches_live_shape():
    assert request_payload(C.RT_KEEP_ALIVE, b"", 1).hex() == "1a00" + "b00201"  # field 38 tag = 0xb0 0x02
