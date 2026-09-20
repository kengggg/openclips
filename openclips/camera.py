"""High-level camera session: pairing, resume, control RPCs and media fetch.

A :class:`Camera` owns one connected :class:`~openclips.transport.Transport`.
Typical use::

    with BtgattTransport("AA:BB:CC:DD:EE:FF") as t:
        cam = Camera(t, pairing_key)
        state = cam.resume()
        sessions = cam.list_sessions()["session_ids"]
        moments = cam.list_moments(sessions[0])["moment_ids"]
        with cam.keepalive():
            creds = cam.initiate_wifi()
            ...join creds.ssid...
            jpeg = cam.fetch_moment_http(sessions[0], moments[0], url=creds.url)
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from . import constants as C
from . import proto as P
from .crypto import (
    SecureSession,
    app_proof,
    derive_pairing_key,
    host_public_bytes,
    lens_proof,
)
from .framing import bqs_frame, request_payload, strip_bqs
from .pb import parse_pb
from .transport import Transport
from .wifi import WifiCredentials


class CameraError(Exception):
    """Base class for camera protocol errors."""


class CameraAsleep(CameraError):
    """The camera advertises but Myriad is idle. Press the shutter once."""


class PairingKeyMismatch(CameraError):
    """The lens proof did not verify: the stored pairing key is stale."""


class NotPaired(CameraError):
    """Operation needs a pairing key / secure session."""


class RequestTimeout(CameraError):
    """No response to a request within the timeout."""


class Camera:
    def __init__(
        self,
        transport: Transport,
        pairing_key: bytes | None = None,
        device_id: int = 1,
        log=None,
    ):
        self.transport = transport
        self.pairing_key = pairing_key
        self.device_id = device_id
        self.log = log or (lambda msg: None)
        self.sess: SecureSession | None = None
        self.state = P.CameraState()
        self.app_nonce: bytes | None = None
        self.lens_nonce: bytes | None = None
        self.notifications: list[bytes] = []
        self._pending: list[bytes] = []
        self._seq = 0
        self._lock = threading.RLock()
        self._hb_stop: threading.Event | None = None

    # ------------------------------------------------------------------ plumbing

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _send_plain(self, rtype: int, inner: bytes = b"") -> int:
        seq = self._next_seq()
        self.transport.write(bqs_frame(request_payload(rtype, inner, seq)))
        return seq

    def _send_enc(self, rtype: int, inner: bytes = b"") -> int:
        if self.sess is None:
            raise NotPaired("secure session not established; call resume() first")
        if rtype in C.UNSAFE_REQUESTS:
            raise CameraError(f"{C.REQUEST_NAMES.get(rtype, rtype)} is known to wedge the camera")
        with self._lock:
            seq = self._next_seq()
            frame = bqs_frame(self.sess.encrypt(request_payload(rtype, inner, seq)))
            self.transport.write(frame)
        return seq

    def _read_plain(self, timeout: float, want_field: int | None = None) -> bytes | None:
        end = time.time() + timeout
        while time.time() < end:
            raw = self.transport.read_indication(min(1.0, max(0.05, end - time.time())))
            if not raw:
                continue
            body = strip_bqs(raw)
            if want_field is None:
                return body
            try:
                if want_field in parse_pb(body):
                    return body
            except ValueError:
                pass
        return None

    def _decrypt(self, raw: bytes) -> bytes | None:
        body = strip_bqs(raw)
        pt = self.sess.decrypt(body)
        if pt is None and body is not raw:
            pt = self.sess.decrypt(raw)
        return pt

    def _classify(self, pt: bytes) -> bool:
        """Absorb keep-alive answers and state notifications. True if absorbed."""
        if P.is_keepalive_response(pt):
            return True
        if self.state.update_from(pt) and len(P.response_fields(pt)) <= 1:
            self.notifications.append(pt)
            return True
        return False

    def _matches(self, pt: bytes, seq: int | None, field: int | None) -> bool:
        try:
            f = parse_pb(pt)
        except ValueError:
            return False
        if field is not None and field in f:
            return True
        if seq is not None and f.get(C.SEQ_ECHO_FIELD, [None])[0] == seq and field is None:
            return True
        return False

    def _read_enc(self, timeout: float, seq: int | None = None, field: int | None = None) -> bytes | None:
        for pt in list(self._pending):
            if self._matches(pt, seq, field):
                self._pending.remove(pt)
                return pt
        end = time.time() + timeout
        while time.time() < end:
            raw = self.transport.read_indication(min(0.5, max(0.05, end - time.time())))
            if not raw:
                continue
            pt = self._decrypt(raw)
            if pt is None:
                continue
            if self._classify(pt):
                continue
            if self._matches(pt, seq, field):
                return pt
            self._pending.append(pt)
        return None

    def request(self, rtype: int, inner: bytes = b"", timeout: float = 12.0) -> bytes | None:
        """Send an encrypted request and wait for its response (decrypted)."""
        seq = self._send_enc(rtype, inner)
        return self._read_enc(timeout, seq=seq, field=C.response_field(rtype))

    def heartbeat(self) -> None:
        """Encrypted KEEP_ALIVE. Send every few seconds during long waits."""
        self._send_enc(C.RT_KEEP_ALIVE)

    @contextmanager
    def keepalive(self, interval: float = 3.5):
        """Run heartbeats on a background thread for the duration of the block."""
        stop = threading.Event()

        def loop():
            while not stop.wait(interval):
                try:
                    self.heartbeat()
                except Exception:
                    return

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=2)

    # ------------------------------------------------------------------ handshake

    def pair(self, host_key, name: str = "openclips", timeout: float = 30.0) -> tuple[bytes, bytes]:
        """Setup-mode pairing. Returns ``(pairing_key, lens_public_key)``.

        Only works during the setup window after a factory reset (volleying
        white LEDs, ``setup_bit`` set in advertisements). Follow with
        :meth:`resume` on the same connection.
        """
        self._send_plain(C.RT_PUBLIC_QUERY, P.build_public_query())
        self._read_plain(6.0, want_field=C.response_field(C.RT_PUBLIC_QUERY))
        self._send_plain(C.RT_INITIATE_PAIRING, P.build_initiate_pairing(host_public_bytes(host_key), name))
        end = time.time() + timeout
        lens_pub = None
        while time.time() < end and lens_pub is None:
            body = self._read_plain(1.5)
            if not body:
                continue
            lens_pub = P.parse_initiate_pairing(body)
            if lens_pub is None:
                i = body.find(b"\x1a\x40")  # tolerant fallback: field 3, 64 bytes
                if i >= 0 and len(body) >= i + 66:
                    lens_pub = body[i + 2 : i + 66]
        if lens_pub is None:
            raise CameraAsleep("no pairing response: is the camera in setup mode?")
        self.pairing_key = derive_pairing_key(host_key, lens_pub)
        self.log(f"paired: lens_pub={lens_pub.hex()[:16]}…")
        return self.pairing_key, lens_pub

    def resume(self, app_nonce: bytes | None = None, timeout: float = 15.0) -> P.CameraState:
        """ISC + CSC with the stored pairing key. Returns the camera state."""
        if not self.pairing_key:
            raise NotPaired("no pairing key")
        self.app_nonce = app_nonce or os.urandom(16)
        self._send_plain(C.RT_ISC, P.build_isc(self.app_nonce))
        isc = None
        end = time.time() + timeout
        while time.time() < end and isc is None:
            body = self._read_plain(1.5)
            if not body:
                continue
            isc = P.parse_isc(body)
            if isc is None:
                i_n, i_p = body.find(b"\x12\x10"), body.find(b"\x1a\x20")
                if i_n >= 0 and i_p >= 0:
                    isc = P.IscResponse(None, body[i_n + 2 : i_n + 18], body[i_p + 2 : i_p + 34])
        if isc is None:
            raise CameraAsleep("no ISC response: press the shutter once to wake the camera")
        self.lens_nonce = isc.lens_nonce
        if isc.lens_proof != lens_proof(self.pairing_key, self.app_nonce, self.lens_nonce):
            raise PairingKeyMismatch("lens proof mismatch: factory reset happened, pair again")
        proof = app_proof(self.pairing_key, self.app_nonce, self.lens_nonce)
        self._send_plain(C.RT_CSC, P.build_csc(proof, self.device_id))
        self.sess = SecureSession(self.pairing_key, self.app_nonce, self.lens_nonce)
        end = time.time() + timeout
        pt = None
        while time.time() < end and pt is None:
            raw = self.transport.read_indication(1.0)
            if not raw:
                continue
            pt = self._decrypt(raw)
        if pt is None:
            self.sess = None
            raise RequestTimeout("no CSC response")
        self.state = P.parse_state(pt)
        self.log(f"session up: state={self.state.system_state_name}")
        self.disable_timeouts()
        return self.state

    # ------------------------------------------------------------------ control

    def disable_timeouts(self) -> bool:
        return self.request(C.RT_DISABLE_TIMEOUTS, timeout=8) is not None

    def time_sync(self) -> bytes | None:
        return self.request(C.RT_PRIVATE_QUERY, P.build_private_query(), timeout=8)

    def set_update_required(self, required: bool = False) -> bool:
        pt = self.request(C.RT_SET_UPDATE_REQUIRED, P.build_set_update_required(required))
        return P.is_success(pt, C.RT_SET_UPDATE_REQUIRED)

    def set_active_user(self, username: str = "") -> bool:
        pt = self.request(C.RT_ACTIVE_USER, P.build_active_user(self.device_id, username), timeout=8)
        return pt is not None

    def shutter(self) -> bool:
        """Send PRESSED then RELEASED. Sending only PRESSED wedges the camera."""
        a = self.request(C.RT_SHUTTER, P.build_shutter(C.SHUTTER_PRESSED))
        b = self.request(C.RT_SHUTTER, P.build_shutter(C.SHUTTER_RELEASED))
        return P.is_success(a, C.RT_SHUTTER) and P.is_success(b, C.RT_SHUTTER)

    def set_cover_state(self, open_: bool) -> bool:
        pt = self.request(C.RT_SET_COVER, P.build_set_cover(open_))
        return P.is_success(pt, C.RT_SET_COVER)

    def start_capture_preview(self) -> bool:
        return P.is_success(self.request(C.RT_CAPTURE_PREVIEW), C.RT_CAPTURE_PREVIEW)

    def cancel_capture_preview(self) -> bool:
        return P.is_success(self.request(C.RT_CANCEL_CAPTURE_PREVIEW), C.RT_CANCEL_CAPTURE_PREVIEW)

    def complete_session(self) -> bool:
        """Close the open capture session. Succeeds only with the cover closed."""
        return P.is_success(self.request(C.RT_COMPLETE_SESSION), C.RT_COMPLETE_SESSION)

    def flash_leds(self) -> bool:
        return self.request(C.RT_FLASH_IDENTIFY_LEDS, timeout=8) is not None

    # ------------------------------------------------------------------ media index

    def list_sessions(self, max_count: int = 20, timeout: float = 12.0) -> dict:
        pt = self.request(C.RT_LIST_SESSIONS, P.build_list_sessions(0, max_count), timeout)
        if pt is None:
            raise RequestTimeout("LIST_SESSIONS")
        return P.parse_list_sessions(pt)

    def list_moments(self, session_id: int, timeout: float = 20.0) -> dict:
        """Moments of a completed session. Blocks while the session is open."""
        pt = self.request(C.RT_LIST_MOMENTS, P.build_list_moments(session_id), timeout)
        if pt is None:
            raise RequestTimeout("LIST_MOMENTS")
        return P.parse_list_moments(pt)

    def delete_moments(self, session_id: int, moment_ids) -> bool:
        pt = self.request(C.RT_DELETE_MOMENTS, P.build_delete_moments(session_id, list(moment_ids)))
        return P.is_success(pt, C.RT_DELETE_MOMENTS)

    def move_to_trash(self, session_id: int, moment_ids) -> bool:
        pt = self.request(C.RT_MOVE_TO_TRASH, P.build_move_moments_to_trash(session_id, list(moment_ids)))
        return P.is_success(pt, C.RT_MOVE_TO_TRASH)

    def restore_from_trash(self, session_id: int, moment_ids) -> bool:
        pt = self.request(C.RT_RESTORE_FROM_TRASH, P.build_restore_moments_from_trash(session_id, list(moment_ids)))
        return P.is_success(pt, C.RT_RESTORE_FROM_TRASH)

    # ------------------------------------------------------------------ Wi-Fi / HTTP

    def initiate_wifi(self, timeout: float = 20.0) -> WifiCredentials | None:
        """Ask the camera to start its SoftAP. Keep heartbeats running afterwards."""
        pt = self.request(C.RT_INITIATE_WIFI, P.build_initiate_wifi_paired(), timeout)
        if pt is None:
            return None
        w = P.parse_initiate_wifi(pt)
        if not w["ssid"] or not w["passphrase"]:
            return None
        return WifiCredentials(w["ssid"], w["passphrase"], w["url"] or C.HTTP_DEFAULT, pt)

    def cancel_wifi(self) -> bool:
        return self.request(C.RT_CANCEL_WIFI, timeout=8) is not None

    @staticmethod
    def http_post(url: str, path: str, body: bytes, timeout: float = 30.0) -> bytes:
        req = urllib.request.Request(url.rstrip("/") + path, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()
            raise CameraError(f"HTTP {e.code} on {path}: {detail}") from None

    def fetch_moment_http(
        self,
        session_id: int,
        moment_id: int,
        url: str | None = None,
        resolution: int = C.RESOLUTION_FULL,
        timeout: float = 30.0,
    ) -> bytes:
        """``POST /fetch_moment``. The host must already be on the camera's SoftAP."""
        body = P.build_fetch_moment(session_id, moment_id, resolution)
        return self.http_post(url or C.HTTP_DEFAULT, "/fetch_moment", body, timeout)

    def fetch_moment_metadata_http(self, session_id: int, moment_id: int, url: str | None = None) -> bytes:
        body = P.build_fetch_moment_metadata(session_id, moment_id)
        return self.http_post(url or C.HTTP_DEFAULT, "/fetch_moment_metadata", body)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self.transport.close()


def extract_jpeg(data: bytes) -> bytes | None:
    """Return the JPEG starting at the first SOI marker, or ``None``."""
    i = data.find(b"\xff\xd8\xff")
    return data[i:] if i >= 0 else None
