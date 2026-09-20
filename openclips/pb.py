"""Minimal protobuf (proto2 wire format) encoder and decoder.

The camera protocol is small enough that a dependency-free hand-rolled codec
is simpler than generated classes. ``parse_pb`` returns a mapping of field
number to the list of values seen for that field, in wire order. Length
delimited values are returned as ``bytes``; nested messages are parsed by the
caller when the schema says the field is a message.
"""

from __future__ import annotations

import struct

_U64 = (1 << 64) - 1

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_BYTES = 2
WIRE_FIXED32 = 5


def pb_varint(n: int) -> bytes:
    """Encode an unsigned varint (negative ints are two's-complement 64-bit)."""
    n = int(n) & _U64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_tag(field: int, wire: int) -> bytes:
    return pb_varint((field << 3) | wire)


def pb_uint(field: int, value: int) -> bytes:
    """Varint field. Zero is encoded explicitly (proto2 presence matters here)."""
    return pb_tag(field, WIRE_VARINT) + pb_varint(value)


def pb_bool(field: int, value: bool) -> bytes:
    return pb_uint(field, 1 if value else 0)


def pb_fixed64(field: int, value: int) -> bytes:
    return pb_tag(field, WIRE_FIXED64) + struct.pack("<Q", int(value) & _U64)


def pb_bytes(field: int, data: bytes | bytearray | str | None) -> bytes:
    """Length-delimited field (bytes, string or embedded message)."""
    if data is None:
        return b""
    if isinstance(data, str):
        data = data.encode()
    data = bytes(data)
    return pb_tag(field, WIRE_BYTES) + pb_varint(len(data)) + data


def pb_packed_varints(field: int, values) -> bytes:
    return pb_bytes(field, b"".join(pb_varint(v) for v in values))


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint at ``pos``; returns ``(value, new_pos)``."""
    value = 0
    shift = 0
    n = len(data)
    while True:
        if pos >= n:
            raise ValueError("truncated varint")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return value, pos
        if shift > 70:
            raise ValueError("varint too long")


def parse_packed_varints(data: bytes) -> list[int]:
    out: list[int] = []
    pos = 0
    while pos < len(data):
        v, pos = read_varint(data, pos)
        out.append(v)
    return out


def parse_pb(data: bytes) -> dict[int, list]:
    """Parse one message into ``{field: [values...]}``.

    Raises ``ValueError`` on truncated or unsupported input.
    """
    fields: dict[int, list] = {}
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            raise ValueError("field number 0")
        if wire == WIRE_VARINT:
            v, pos = read_varint(data, pos)
        elif wire == WIRE_BYTES:
            ln, pos = read_varint(data, pos)
            if pos + ln > n:
                raise ValueError(f"truncated bytes field {field}")
            v = bytes(data[pos : pos + ln])
            pos += ln
        elif wire == WIRE_FIXED32:
            if pos + 4 > n:
                raise ValueError(f"truncated fixed32 field {field}")
            v = struct.unpack("<I", data[pos : pos + 4])[0]
            pos += 4
        elif wire == WIRE_FIXED64:
            if pos + 8 > n:
                raise ValueError(f"truncated fixed64 field {field}")
            v = struct.unpack("<Q", data[pos : pos + 8])[0]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire} for field {field}")
        fields.setdefault(field, []).append(v)
    return fields


def first(fields: dict[int, list], field: int, default=None):
    """First value of ``field`` or ``default``."""
    vals = fields.get(field)
    return vals[0] if vals else default


def first_bytes(fields: dict[int, list], field: int) -> bytes:
    """First length-delimited value of ``field`` or ``b""``."""
    for v in fields.get(field, []):
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
    return b""
