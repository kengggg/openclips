"""Structural JPEG checks used before committing a downloaded file.

These checks do not prove every pixel is decodable. They confirm SOI, bounded
segment lengths, a Start-Of-Frame, a Start-Of-Scan, and EOI. They do not
detect arbitrary corruption inside entropy-coded data.

``structural_jpeg()`` builds a tiny synthetic 1×1 baseline JPEG with no EXIF
and no camera content, for tests and as a known-valid fixture.
"""

from __future__ import annotations

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_STANDALONE = frozenset({0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7})


def validate_jpeg(data: bytes) -> bool:
    """True if ``data`` is a structurally complete JPEG starting at SOI."""
    if len(data) < 4 or data[:2] != _SOI:
        return False
    i = 2
    seen_sof = False
    seen_sos = False
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            return False
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return False
        marker = data[i]
        i += 1
        if marker == 0xD9:
            return seen_sof and seen_sos
        if marker == 0xD8:
            return False
        if marker in _STANDALONE:
            continue
        if i + 2 > n:
            return False
        seglen = int.from_bytes(data[i : i + 2], "big")
        if seglen < 2 or i + seglen > n:
            return False
        if marker in _SOF:
            seen_sof = True
        if marker == 0xDA:
            seen_sos = True
            i += seglen
            while i < n - 1:
                if data[i] != 0xFF:
                    i += 1
                    continue
                nxt = data[i + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    i += 2
                    continue
                if nxt == 0xD9:
                    return seen_sof
                return False
            return False
        i += seglen
    return False


def structural_jpeg() -> bytes:
    """Tiny synthetic baseline JPEG (1×1, JFIF, no EXIF). Generated, not camera output."""
    app0 = bytes.fromhex("ffe000104a46494600010100000100010000")
    dqt = b"\xff\xdb\x00\x43\x00" + bytes([1] * 64)
    sof0 = bytes.fromhex("ffc0000b080001000101011100")
    dht = b"\xff\xc4\x00\x14\x00" + bytes(16) + b"\x00"
    sos = bytes.fromhex("ffda0008010100003f00")
    scan = b"\x00"
    return _SOI + app0 + dqt + sof0 + dht + sos + scan + _EOI
