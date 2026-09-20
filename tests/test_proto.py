"""Builders and parsers against live-captured vectors (firmware 1.8)."""

from openclips import constants as C
from openclips import proto as P
from openclips.pb import first, first_bytes, parse_packed_varints, parse_pb

SID = 1789825799491786000

# Decrypted LIST_MOMENTS response after COMPLETE: six moments, is_final=1.
LIVE_LIST_MOMENTS = bytes.fromhex(
    "6a4e08011206020301090405180120002a24"
    "b4bcbacf8b34a5bfbbcf8b34fdd2b9cf8b34b3cec6cf8b34"
    "f9f5bccf8b34e0a3becf8b3432180e22a93d74619e3d"
    "3629933d3438913ded03613d87f9593dc00209"
)
# Decrypted LIST_MOMENTS response for session_id=0: SUCCESS, no ids.
LIVE_LIST_EMPTY = bytes.fromhex("6a06080118002000c00208")
# Decrypted LIST_SESSIONS response: two ids packed as 8-byte LE.
LIVE_LIST_SESSIONS = bytes.fromhex("621408011210f874ff01eda0d618109589ec14bdd618c00207")
# Keep-alive answer.
LIVE_KEEPALIVE = bytes.fromhex("22020801c00203")


def test_list_moments_field1_is_fixed64_session_id():
    body = P.build_list_moments(SID)
    assert body[0] == 0x09
    assert body[1:9] != b"\x00" * 8
    assert first(parse_pb(body), 1) == SID


def test_parse_live_list_moments():
    got = P.parse_list_moments(LIVE_LIST_MOMENTS)
    assert got["ok"] and got["moment_ids"] == [2, 3, 1, 9, 4, 5]
    assert got["is_final"] == 1 and got["seq"] == 9
    assert "session_id" not in got


def test_parse_live_list_moments_empty():
    got = P.parse_list_moments(LIVE_LIST_EMPTY)
    assert got["ok"] and got["moment_ids"] == [] and got["is_final"] == 0


def test_parse_live_list_sessions():
    s = P.parse_list_sessions(LIVE_LIST_SESSIONS)
    assert s["status"] == C.STATUS_SUCCESS
    assert SID in s["session_ids"] and len(s["session_ids"]) == 2
    assert s["seq"] == 7


def test_keepalive_detection():
    assert P.is_keepalive_response(LIVE_KEEPALIVE)
    assert not P.is_keepalive_response(LIVE_LIST_EMPTY)


def test_fetch_moment_full_shape():
    mid = P.build_moment_id(1, 7)
    assert mid.hex() == "08011007"
    full = P.build_fetch_moment(1, 7, C.RESOLUTION_FULL)
    assert full.hex() == "0a" + "04" + mid.hex() + "1001"
    ranged = P.build_fetch_moment(1, 7, C.RESOLUTION_THUMB, offset=0, length=4096)
    assert ranged.endswith(bytes.fromhex("10021800208020"))


def test_fetch_metadata_and_frame_wrap_moment_id():
    for body in (P.build_fetch_moment_metadata(SID, 2), P.build_fetch_frame(SID, 2)):
        inner = parse_pb(first_bytes(parse_pb(body), 1))
        assert first(inner, 1) == SID and first(inner, 2) == 2


def test_wifi_paired_encoding_and_parse():
    assert P.build_initiate_wifi_paired().hex() == "08001001"
    pt = (
        bytes([0x52])
        + bytes([0x2E])
        + bytes.fromhex("0801")
        + bytes.fromhex("1209")
        + b"Clips6013"
        + bytes.fromhex("1a10")
        + b"0123456789abcdef"
        + bytes.fromhex("220f")
        + b"http://1.2.3.4:8"
    )
    # rebuild precisely with the codec rather than hand-count lengths
    from openclips.pb import pb_bytes, pb_uint

    inner = pb_uint(1, 1) + pb_bytes(2, b"Clips6013") + pb_bytes(3, b"0123456789abcdef") + pb_bytes(4, b"http://x:8080")
    pt = pb_bytes(10, inner) + pb_uint(40, 5)
    w = P.parse_initiate_wifi(pt)
    assert w == {
        "status": 1,
        "ssid": "Clips6013",
        "passphrase": "0123456789abcdef",
        "url": "http://x:8080",
        "mode": None,
    }


def test_delete_trash_restore_share_shape():
    ids = [2, 3, 1]
    d = P.build_delete_moments(SID, ids)
    assert d == P.build_move_moments_to_trash(SID, ids) == P.build_restore_moments_from_trash(SID, ids)
    packed = parse_pb(first_bytes(parse_pb(d), 1))
    assert first(packed, 1) == SID
    assert parse_packed_varints(first_bytes(packed, 2)) == ids


def test_handshake_builders():
    assert P.build_isc(b"\x01" * 16).hex() == "0a10" + "01" * 16
    csc = P.build_csc(b"\x02" * 32, device_id=1)
    assert csc.hex() == "0a20" + "02" * 32 + "1001"
    q = parse_pb(P.build_public_query(request_id=1234))
    assert first(q, 1) == 1234 and first(q, 2) == C.PROTO_VER
    pairing = parse_pb(P.build_initiate_pairing(b"\x03" * 64, "host"))
    assert first(pairing, 1) == 1 and len(first_bytes(pairing, 2)) == 64 and first_bytes(pairing, 3) == b"host"


def test_settings_builders_encode_explicit_false():
    assert P.build_set_update_required(False).hex() == "0800"
    assert P.build_set_cover(False).hex() == "0800"
    assert P.build_shutter(C.SHUTTER_PRESSED).hex() == "0801"
    au = parse_pb(P.build_active_user(1))
    assert first(au, 3) == 1 and first(au, 1) == 1


def test_status_helpers():
    from openclips.pb import pb_bytes, pb_uint

    ok = pb_bytes(C.response_field(C.RT_COMPLETE_SESSION), pb_uint(1, 1)) + pb_uint(40, 3)
    bad = pb_bytes(C.response_field(C.RT_COMPLETE_SESSION), pb_uint(1, 2)) + pb_uint(40, 4)
    assert P.is_success(ok, C.RT_COMPLETE_SESSION)
    assert not P.is_success(bad, C.RT_COMPLETE_SESSION)
    assert P.parse_status(None, C.RT_COMPLETE_SESSION) is None
    assert P.response_fields(ok) == [22]


def test_state_bundle_parsing():
    from openclips.pb import pb_bytes, pb_uint

    entries = [
        pb_bytes(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE)),
        pb_bytes(9, pb_uint(1, 1)),
        pb_bytes(3, pb_uint(1, SID)),
        pb_bytes(4, pb_uint(1, 6) + pb_uint(2, 1)),
        pb_bytes(1, pb_uint(1, 54) + pb_uint(2, 2)),
        pb_bytes(13, pb_uint(1, 3)),
    ]
    pt = b"".join(pb_bytes(1, e) for e in entries) + pb_bytes(8, pb_uint(1, 1))
    st = P.parse_state(pt)
    assert st.system_state_name == "CAPTURE" and st.cover_open is True
    assert st.session_uuid == SID and st.storage_level == 6 and st.battery_pct == 54
    assert st.as_dict()["charge_state_name"] == "CHARGING"
    assert st.as_dict()["wifi_state_name"] == "READY"
    assert len(st.raw) == 6
