"""Structural JPEG validation. Fixtures are synthetic, with no EXIF or camera bytes."""

from openclips.camera import extract_jpeg
from openclips.jpeg import structural_jpeg, validate_jpeg


def test_structural_jpeg_is_valid():
    jpeg = structural_jpeg()
    assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    assert b"Exif" not in jpeg
    assert validate_jpeg(jpeg)


def test_extract_then_validate_wrapper():
    jpeg = structural_jpeg()
    wrapped = b"resp-wrapper" + jpeg
    extracted = extract_jpeg(wrapped)
    assert extracted == jpeg
    assert validate_jpeg(extracted)


def test_damaged_jpegs_are_rejected():
    jpeg = structural_jpeg()
    assert not validate_jpeg(b"")
    assert not validate_jpeg(b"\xff\xd8\xff\xd9")  # SOI/EOI, no SOF/SOS
    assert not validate_jpeg(jpeg[:10])  # truncated
    assert not validate_jpeg(jpeg[:-2])  # missing EOI
    assert not validate_jpeg(b"\xff\xd8\xff\xe0\x00\x02")  # truncated segment
    assert not validate_jpeg(b"junk")
    sos = jpeg.find(b"\xff\xda")
    assert sos > 0 and not validate_jpeg(jpeg[:sos] + jpeg[-2:])  # SOF but no SOS
