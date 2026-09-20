"""An in-process fake camera for tests.

``FakeLens`` implements :class:`openclips.transport.Transport` and answers
requests the way firmware 1.8 does on the wire: plaintext in setup mode,
ISC/CSC handshake, then an AES-EAX channel where the response field number
is ``request type + 1`` and the sequence number echoes in field 40.
"""

from __future__ import annotations

import os
from collections import deque

from openclips import constants as C
from openclips.crypto import (
    ROLE_LENS,
    SecureSession,
    app_proof,
    derive_pairing_key,
    generate_host_key,
    host_public_bytes,
    lens_proof,
)
from openclips.framing import bqs_frame, strip_bqs
from openclips.pb import first, first_bytes, parse_pb, pb_bytes, pb_fixed64, pb_packed_varints, pb_uint
from openclips.transport import Transport


def _resp(rtype: int, inner: bytes, seq: int) -> bytes:
    return pb_bytes(C.response_field(rtype), inner) + pb_uint(C.SEQ_ECHO_FIELD, seq)


def _status(ok: bool = True) -> bytes:
    return pb_uint(1, C.STATUS_SUCCESS if ok else C.STATUS_FAILURE)


class FakeLens(Transport):
    def __init__(
        self,
        pairing_key: bytes | None = None,
        setup_mode: bool = False,
        system_state: int = C.SYSTEM_STATE_IDLE,
        sessions: dict[int, list[int]] | None = None,
        open_session: int | None = None,
        cover_open: bool = False,
        asleep: bool = False,
    ):
        self.lens_key = generate_host_key()
        self.pairing_key = pairing_key
        self.setup_mode = setup_mode
        self.system_state = system_state
        self.sessions = sessions if sessions is not None else {}
        self.open_session = open_session
        self.cover_open = cover_open
        self.asleep = asleep
        self.update_required = True
        self.active_device = None
        self.sess: SecureSession | None = None
        self.lens_nonce = None
        self.app_nonce = None
        self.out: deque[bytes] = deque()
        self.requests: list[tuple[int, bytes]] = []
        self.closed = False
        self.wifi_open = False

    # ------------------------------------------------------------ Transport

    def write(self, frame: bytes) -> None:
        assert len(frame) <= C.ATT_MTU - 3, "frame too big for a single write"
        if self.asleep:
            return
        body = strip_bqs(frame)
        if self.sess is None:
            self._handle_plain(body)
        else:
            pt = self.sess.decrypt(body)
            assert pt is not None, "host sent undecryptable frame"
            self._handle_enc(pt)

    def read_indication(self, timeout: float = 1.0) -> bytes | None:
        return self.out.popleft() if self.out else None

    def close(self) -> None:
        """Dropping the GATT connection resets the secure channel, like the real camera."""
        self.closed = True
        self.sess = None
        self.out.clear()

    # ------------------------------------------------------------ helpers

    def _emit_plain(self, pt: bytes) -> None:
        self.out.append(bqs_frame(pt))

    def _emit_enc(self, pt: bytes) -> None:
        self.out.append(bqs_frame(self.sess.encrypt(pt)))

    def push_notification(self, kind: int, inner: bytes) -> None:
        """Async state notification: Response { 1: { kind: inner } }."""
        self._emit_enc(pb_bytes(1, pb_bytes(kind, inner)))

    def state_bundle(self) -> bytes:
        entries = [
            pb_bytes(2, pb_uint(1, self.system_state)),
            pb_bytes(9, pb_uint(1, 1 if self.cover_open else 0)),
            pb_bytes(3, pb_uint(1, self.open_session or 0)),
            pb_bytes(4, pb_uint(1, 6) + pb_uint(2, 1)),
            pb_bytes(1, pb_uint(1, 77) + pb_uint(2, 2)),
            pb_bytes(5, pb_uint(1, 1)),
            pb_bytes(10, pb_uint(1, 1)),
        ]
        return b"".join(pb_bytes(1, e) for e in entries)

    # ------------------------------------------------------------ plaintext phase

    def _handle_plain(self, body: bytes) -> None:
        f = parse_pb(body)
        seq = first(f, C.SEQ_FIELD, 0)
        rtype = next(k for k in f if k != C.SEQ_FIELD)
        inner = first_bytes(f, rtype)
        self.requests.append((rtype, inner))
        if rtype == C.RT_PUBLIC_QUERY:
            assert self.setup_mode, "plaintext PUBLIC_QUERY after pairing drops the link"
            self._emit_plain(_resp(rtype, _status() + pb_bytes(13, b"00008109J06013"), seq))
        elif rtype == C.RT_INITIATE_PAIRING:
            assert self.setup_mode
            fi = parse_pb(inner)
            host_pub = first_bytes(fi, 2)
            assert len(host_pub) == 64
            self.pairing_key = derive_pairing_key(self.lens_key, host_pub)
            self._emit_plain(_resp(rtype, _status() + pb_bytes(3, host_public_bytes(self.lens_key)), seq))
        elif rtype == C.RT_ISC:
            assert self.pairing_key, "ISC before pairing"
            self.app_nonce = first_bytes(parse_pb(inner), 1)
            assert len(self.app_nonce) == 16
            self.lens_nonce = os.urandom(16)
            proof = lens_proof(self.pairing_key, self.app_nonce, self.lens_nonce)
            self._emit_plain(_resp(rtype, _status() + pb_bytes(2, self.lens_nonce) + pb_bytes(3, proof), seq))
        elif rtype == C.RT_CSC:
            fi = parse_pb(inner)
            proof = first_bytes(fi, 1)
            self.device_id = first(fi, 2)
            assert proof == app_proof(self.pairing_key, self.app_nonce, self.lens_nonce), "bad app proof"
            self.sess = SecureSession(self.pairing_key, self.app_nonce, self.lens_nonce, role=ROLE_LENS)
            self.setup_mode = False
            self._emit_enc(self.state_bundle() + _resp(rtype, _status(), seq))
        else:
            raise AssertionError(f"plaintext request type {rtype} is not allowed")

    # ------------------------------------------------------------ encrypted phase

    def _handle_enc(self, pt: bytes) -> None:
        f = parse_pb(pt)
        seq = first(f, C.SEQ_FIELD, 0)
        rtype = next(k for k in f if k != C.SEQ_FIELD)
        inner = first_bytes(f, rtype)
        self.requests.append((rtype, inner))
        assert rtype not in C.UNSAFE_REQUESTS, f"unsafe request {rtype}"
        h = getattr(self, f"_rt_{rtype}", None)
        if h is None:
            self._emit_enc(_resp(rtype, _status(), seq))
            return
        resp = h(inner)
        if resp is not None:
            self._emit_enc(_resp(rtype, resp, seq))

    def _rt_3(self, inner):  # KEEP_ALIVE
        return _status()

    def _rt_2048(self, inner):  # DISABLE_CONNECTION_TIMEOUTS
        return _status()

    def _rt_2(self, inner):  # PRIVATE_QUERY
        return pb_uint(1, 5) + pb_bytes(13, b"00008109J06013")

    def _rt_8(self, inner):  # SET_UPDATE_REQUIRED
        self.update_required = bool(first(parse_pb(inner), 1))
        return _status()

    def _rt_51(self, inner):  # ACTIVE_USER_SETTINGS
        fi = parse_pb(inner)
        self.active_device = first(fi, 3)
        self.push_notification(100, pb_uint(2, self.active_device))
        return pb_uint(1, 51) + pb_uint(2, 10)

    def _rt_42(self, inner):  # shutter
        ev = first(parse_pb(inner), 1)
        return _status(ev in (C.SHUTTER_PRESSED, C.SHUTTER_RELEASED))

    def _rt_29(self, inner):  # SET_COVER_STATE
        self.cover_open = bool(first(parse_pb(inner), 1))
        self.push_notification(9, pb_uint(1, 1 if self.cover_open else 0))
        return _status()

    def _rt_27(self, inner):  # INITIATE_CAPTURE_PREVIEW
        return _status(self.system_state == C.SYSTEM_STATE_CAPTURE)

    def _rt_21(self, inner):  # COMPLETE_CURRENT_SESSION
        if self.cover_open or self.open_session is None:
            return _status(False)
        self.sessions.setdefault(self.open_session, [])
        self.open_session = None
        self.system_state = C.SYSTEM_STATE_IDLE
        return _status()

    def _rt_11(self, inner):  # LIST_SESSIONS
        ids = sorted(set(self.sessions) | ({self.open_session} if self.open_session else set()))
        packed = b"".join(s.to_bytes(8, "little") for s in ids)
        body = _status() + pb_bytes(2, packed)
        if self.open_session:
            body += pb_uint(3, self.open_session)
        return body

    def _rt_12(self, inner):  # LIST_MOMENTS
        sid = first(parse_pb(inner), 1)
        if not sid or sid == self.open_session:
            return _status() + pb_uint(3, 0) + pb_uint(4, 0)  # empty, mirrors firmware
        ids = self.sessions.get(sid, [])
        return _status() + pb_packed_varints(2, ids) + pb_uint(3, 1) + pb_uint(4, 0)

    def _rt_14(self, inner):  # DELETE_MOMENTS
        packed = parse_pb(first_bytes(parse_pb(inner), 1))
        sid = first(packed, 1)
        from openclips.pb import parse_packed_varints

        ids = parse_packed_varints(first_bytes(packed, 2))
        if sid not in self.sessions:
            return _status(False)
        self.sessions[sid] = [m for m in self.sessions[sid] if m not in ids]
        return _status()

    def _rt_9(self, inner):  # INITIATE_WIFI
        fi = parse_pb(inner)
        assert first(fi, 1) == 0 and first(fi, 2) == 1, "expected {wifi_direct:0, capture_preview:1}"
        self.wifi_open = True
        self.push_notification(13, pb_uint(1, 2))
        return (
            _status()
            + pb_bytes(2, b"Clips6013")
            + pb_bytes(3, b"0123456789abcdef")
            + pb_bytes(4, b"http://192.168.49.10:8080")
            + pb_uint(5, 2)
        )

    def _rt_10(self, inner):  # CANCEL_WIFI
        self.wifi_open = False
        return _status()


def paired_pair() -> tuple[FakeLens, bytes]:
    """A fake lens already paired with a random key, plus that key."""
    key = os.urandom(32)
    return FakeLens(pairing_key=key), key


__all__ = ["FakeLens", "paired_pair", "pb_fixed64"]
