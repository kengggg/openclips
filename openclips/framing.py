"""ATT payload framing and the outer Request envelope.

Every ATT value written to or indicated by the camera is::

    frame = varint(len(body)) || body

``body`` is a plaintext protobuf ``Request``/``Response`` in setup mode and
``ciphertext || tag`` (AES-EAX, see :mod:`openclips.crypto`) once the secure
channel is up. The length prefix is never part of the encrypted data.
"""

from __future__ import annotations

from .constants import SEQ_FIELD
from .pb import pb_bytes, pb_uint, read_varint


def request_payload(rtype: int, inner: bytes, seq: int) -> bytes:
    """``Request { <rtype>: inner, 38: seq }``."""
    return pb_bytes(rtype, inner) + pb_uint(SEQ_FIELD, seq)


def bqs_frame(body: bytes) -> bytes:
    """Prefix ``body`` with its varint length."""
    return _varint(len(body)) + body


def strip_bqs(frame: bytes) -> bytes:
    """Remove the varint length prefix when it matches the remaining length.

    Indications are always prefixed on this firmware, but be tolerant: if the
    prefix does not match, the frame is returned unchanged.
    """
    if not frame:
        return frame
    try:
        ln, pos = read_varint(frame, 0)
    except ValueError:
        return frame
    if ln == len(frame) - pos:
        return frame[pos:]
    return frame


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)
