import pytest

from openclips.pb import (
    first,
    first_bytes,
    parse_packed_varints,
    parse_pb,
    pb_bool,
    pb_bytes,
    pb_fixed64,
    pb_packed_varints,
    pb_uint,
    pb_varint,
    read_varint,
)


@pytest.mark.parametrize(
    "n,enc",
    [(0, "00"), (1, "01"), (127, "7f"), (128, "8001"), (300, "ac02"), (1 << 32, "8080808010")],
)
def test_varint_vectors(n, enc):
    assert pb_varint(n).hex() == enc
    assert read_varint(bytes.fromhex(enc), 0) == (n, len(enc) // 2)


def test_negative_int_is_twos_complement_64():
    assert len(pb_varint(-1)) == 10
    assert read_varint(pb_varint(-1), 0)[0] == (1 << 64) - 1


def test_roundtrip_all_wire_types():
    msg = pb_uint(1, 26) + pb_bytes(2, b"abc") + pb_fixed64(3, 1789825799491786000) + pb_bool(4, False)
    f = parse_pb(msg)
    assert first(f, 1) == 26
    assert first_bytes(f, 2) == b"abc"
    assert first(f, 3) == 1789825799491786000
    assert first(f, 4) == 0  # explicit zero is preserved (proto2 presence)


def test_packed_varints():
    body = pb_packed_varints(2, [2, 3, 1, 9, 4, 5])
    assert parse_packed_varints(first_bytes(parse_pb(body), 2)) == [2, 3, 1, 9, 4, 5]


def test_repeated_fields_keep_order():
    f = parse_pb(pb_uint(1, 5) + pb_uint(1, 6) + pb_uint(1, 7))
    assert f[1] == [5, 6, 7]


@pytest.mark.parametrize("bad", ["0a05616263", "08", "80", "0d0102", "0904", "0000"])
def test_truncated_or_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_pb(bytes.fromhex(bad))


def test_unsupported_wire_type():
    with pytest.raises(ValueError):
        parse_pb(bytes([0x0B]))  # wire type 3 (group start)
