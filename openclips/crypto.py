"""Pairing key derivation, session proofs and the AES-EAX secure channel.

All formulas were validated byte-for-byte against live captures from a
Google Clips camera running firmware 1.8. See ``docs/PROTOCOL.md``.

Pairing (setup mode only)::

    host key   = random P-256 key (reused across factory resets is fine)
    S          = host_priv * lens_pub          # ECDH shared point
    pairing_key = SHA256(S.x_le || S.y_le)    # BOTH coordinates, little-endian

Session (every connection)::

    app_proof  = HMAC-SHA256(pairing_key, 0x01 || app_nonce || lens_nonce)
    lens_proof = HMAC-SHA256(pairing_key, 0x02 || app_nonce || lens_nonce)

    PRK    = HMAC-SHA256("links-session-key-hkdf-sha256", pairing_key)
    T1     = HMAC-SHA256(PRK, info || 0x01)         info = app_nonce || lens_nonce
    T2     = HMAC-SHA256(PRK, T1 || info || 0x02)
    keys48 = T1 || T2[:16]
    app rx key = keys48[0:16]     app tx key = keys48[16:32]
    nonce_base = keys48[32:48]

    frame body = AES-EAX(key, nonce_base || counter_le3, plaintext) || tag12

Counters are independent per direction and start at 1.
"""

from __future__ import annotations

import hashlib
import hmac

from Crypto.Cipher import AES
from Crypto.PublicKey import ECC

HKDF_LABEL = b"links-session-key-hkdf-sha256"
TAG_SIZE = 12
NONCE_SIZE = 16
COUNTER_SIZE = 3
CURVE = "P-256"

ROLE_APP = "app"
ROLE_LENS = "lens"


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


# --- Pairing -----------------------------------------------------------------


def generate_host_key() -> ECC.EccKey:
    """Fresh P-256 key for the host side of pairing."""
    return ECC.generate(curve=CURVE)


def host_key_from_pem(pem: str | bytes) -> ECC.EccKey:
    return ECC.import_key(pem)


def host_key_to_pem(key: ECC.EccKey) -> str:
    return key.export_key(format="PEM")


def host_public_bytes(key: ECC.EccKey) -> bytes:
    """Uncompressed ``X || Y`` (64 bytes) as sent in INITIATE_PAIRING."""
    q = key.pointQ
    return int(q.x).to_bytes(32, "big") + int(q.y).to_bytes(32, "big")


def pairing_key_from_point(x: int, y: int) -> bytes:
    """``SHA256(x_le || y_le)`` of the shared point. Not an X-only KDF."""
    return hashlib.sha256(x.to_bytes(32, "little") + y.to_bytes(32, "little")).digest()


def derive_pairing_key(host_key: ECC.EccKey, lens_pub64: bytes) -> bytes:
    """ECDH with the lens public key (64-byte ``X || Y``, no ``0x04`` prefix)."""
    if len(lens_pub64) != 64:
        raise ValueError("lens public key must be 64 bytes (X || Y)")
    lens = ECC.construct(
        curve=CURVE,
        point_x=int.from_bytes(lens_pub64[:32], "big"),
        point_y=int.from_bytes(lens_pub64[32:], "big"),
    )
    shared = lens.pointQ * int(host_key.d)
    return pairing_key_from_point(int(shared.x), int(shared.y))


# --- Proofs and key schedule -----------------------------------------------


def app_proof(pairing_key: bytes, app_nonce: bytes, lens_nonce: bytes) -> bytes:
    return _hmac(pairing_key, b"\x01" + app_nonce + lens_nonce)


def lens_proof(pairing_key: bytes, app_nonce: bytes, lens_nonce: bytes) -> bytes:
    return _hmac(pairing_key, b"\x02" + app_nonce + lens_nonce)


def derive_keys48(pairing_key: bytes, app_nonce: bytes, lens_nonce: bytes) -> bytes:
    info = app_nonce + lens_nonce
    prk = _hmac(HKDF_LABEL, pairing_key)
    t1 = _hmac(prk, info + b"\x01")
    t2 = _hmac(prk, t1 + info + b"\x02")
    return t1 + t2[:16]


# --- Channel -----------------------------------------------------------------


class SecureSession:
    """AES-EAX channel state for one connection.

    ``role`` selects which half of the key material is used for sending. The
    camera (``ROLE_LENS``) is the mirror image of the host (``ROLE_APP``);
    the lens role exists so tests can simulate a camera.
    """

    def __init__(
        self,
        pairing_key: bytes,
        app_nonce: bytes,
        lens_nonce: bytes,
        role: str = ROLE_APP,
    ):
        if role not in (ROLE_APP, ROLE_LENS):
            raise ValueError(f"unknown role {role!r}")
        self.app_nonce = app_nonce
        self.lens_nonce = lens_nonce
        self.role = role
        keys48 = derive_keys48(pairing_key, app_nonce, lens_nonce)
        app_rx, app_tx = keys48[:16], keys48[16:32]
        self.rx_key, self.tx_key = (app_rx, app_tx) if role == ROLE_APP else (app_tx, app_rx)
        self.nonce_base = keys48[32:48]
        self.rx_counter = 0
        self.tx_counter = 0

    def _nonce(self, counter: int) -> bytes:
        return self.nonce_base + counter.to_bytes(COUNTER_SIZE, "little")

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return ``ciphertext || tag`` and advance the send counter."""
        self.tx_counter += 1
        c = AES.new(self.tx_key, AES.MODE_EAX, nonce=self._nonce(self.tx_counter), mac_len=TAG_SIZE)
        ct, tag = c.encrypt_and_digest(plaintext)
        return ct + tag

    def decrypt(self, data: bytes) -> bytes | None:
        """Verify and decrypt ``ciphertext || tag``.

        Returns ``None`` (and leaves the receive counter untouched) when the
        tag does not verify, so a stray plaintext indication does not
        desynchronise the channel.
        """
        if len(data) < TAG_SIZE:
            return None
        ct, tag = data[:-TAG_SIZE], data[-TAG_SIZE:]
        counter = self.rx_counter + 1
        c = AES.new(self.rx_key, AES.MODE_EAX, nonce=self._nonce(counter), mac_len=TAG_SIZE)
        try:
            pt = c.decrypt_and_verify(ct, tag)
        except ValueError:
            return None
        self.rx_counter = counter
        return pt
