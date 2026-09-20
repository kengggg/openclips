import os

import pytest

from openclips.crypto import (
    ROLE_LENS,
    SecureSession,
    app_proof,
    derive_keys48,
    derive_pairing_key,
    generate_host_key,
    host_key_from_pem,
    host_key_to_pem,
    host_public_bytes,
    lens_proof,
    pairing_key_from_point,
)


def test_ecdh_both_sides_agree():
    host, lens = generate_host_key(), generate_host_key()
    k1 = derive_pairing_key(host, host_public_bytes(lens))
    k2 = derive_pairing_key(lens, host_public_bytes(host))
    assert k1 == k2 and len(k1) == 32


def test_pairing_key_uses_both_coordinates_little_endian():
    import hashlib

    x, y = 12345, 67890
    assert pairing_key_from_point(x, y) == hashlib.sha256(x.to_bytes(32, "little") + y.to_bytes(32, "little")).digest()
    assert pairing_key_from_point(x, y) != pairing_key_from_point(x, y + 1)


def test_rejects_point_off_curve():
    host = generate_host_key()
    with pytest.raises(ValueError):
        derive_pairing_key(host, b"\x01" * 64)
    with pytest.raises(ValueError):
        derive_pairing_key(host, b"\x01" * 63)


def test_pem_roundtrip():
    k = generate_host_key()
    k2 = host_key_from_pem(host_key_to_pem(k))
    assert host_public_bytes(k) == host_public_bytes(k2)


def test_proofs_are_role_specific_and_nonce_bound():
    pk, a, b = os.urandom(32), os.urandom(16), os.urandom(16)
    assert app_proof(pk, a, b) != lens_proof(pk, a, b)
    assert app_proof(pk, a, b) != app_proof(pk, b, a)
    assert len(app_proof(pk, a, b)) == 32


def test_key_schedule_layout():
    pk, a, b = os.urandom(32), os.urandom(16), os.urandom(16)
    k = derive_keys48(pk, a, b)
    assert len(k) == 48
    app = SecureSession(pk, a, b)
    assert app.rx_key == k[:16] and app.tx_key == k[16:32] and app.nonce_base == k[32:48]


def test_channel_is_symmetric_between_roles():
    pk, a, b = os.urandom(32), os.urandom(16), os.urandom(16)
    app, lens = SecureSession(pk, a, b), SecureSession(pk, a, b, role=ROLE_LENS)
    for i in range(5):
        msg = f"hello {i}".encode()
        assert lens.decrypt(app.encrypt(msg)) == msg
        assert app.decrypt(lens.encrypt(msg[::-1])) == msg[::-1]
    assert app.tx_counter == lens.rx_counter == 5


def test_app_cannot_decrypt_its_own_frames():
    pk, a, b = os.urandom(32), os.urandom(16), os.urandom(16)
    app = SecureSession(pk, a, b)
    assert app.decrypt(app.encrypt(b"x")) is None


def test_bad_tag_does_not_advance_counter():
    pk, a, b = os.urandom(32), os.urandom(16), os.urandom(16)
    app, lens = SecureSession(pk, a, b), SecureSession(pk, a, b, role=ROLE_LENS)
    frame = lens.encrypt(b"real")
    assert app.decrypt(b"junk" + os.urandom(16)) is None
    assert app.rx_counter == 0
    assert app.decrypt(frame) == b"real"
    assert app.rx_counter == 1


def test_ciphertext_length_is_plaintext_plus_tag():
    app = SecureSession(os.urandom(32), os.urandom(16), os.urandom(16))
    assert len(app.encrypt(b"")) == 12
    assert len(app.encrypt(b"a" * 33)) == 45
