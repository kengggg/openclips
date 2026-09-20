"""High-level camera session: pairing, resume, control RPCs and media fetch.

A :class:`Camera` owns one connected :class:`~openclips.transport.Transport`.
Applications usually get one from :class:`openclips.connection.ConnectionManager`
rather than building it by hand::

    with ConnectionManager(address, pairing_key=key) as cam:
        cam.on(EVENT_STATE, lambda state, changes: print(changes))
        sid = max(cam.list_sessions()["session_ids"])
        for m in cam.list_moments(sid)["moments"]:
            print(m.moment_id, m.datetime, m.score)

Threading model: every method blocks and must be called from one thread at
a time (a worker thread in a GUI). The keepalive thread only writes
heartbeats and is safe alongside that. Event callbacks run on whichever
thread called into the camera when the notification was read.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
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
from .errors import (
    CameraAsleep,
    CameraError,
    ConnectionLost,
    HttpError,
    NotPaired,
    PairingKeyMismatch,
    RequestTimeout,
    UnsafeRequest,
)
from .framing import bqs_frame, request_payload, strip_bqs
from .pb import first, parse_pb
from .transport import Transport
from .wifi import WifiCredentials

__all__ = [
    "Camera",
    "CameraAsleep",
    "CameraError",
    "ConnectionLost",
    "NotPaired",
    "PENDING_RESPONSE_LIMIT",
    "PairingKeyMismatch",
    "RequestTimeout",
    "UnsafeRequest",
    "EVENT_STATE",
    "EVENT_NOTIFICATION",
    "EVENT_DISCONNECTED",
    "extract_jpeg",
]

logger = logging.getLogger(__name__)

#: ``callback(state: CameraState, changes: dict)`` after any state notification.
EVENT_STATE = "state"
#: ``callback(kind: int, name: str, fields: dict)`` for every raw state entry.
EVENT_NOTIFICATION = "notification"
#: ``callback()`` once, when the transport reports the link is gone.
EVENT_DISCONNECTED = "disconnected"

_EVENTS = (EVENT_STATE, EVENT_NOTIFICATION, EVENT_DISCONNECTED)

# Per-read caps on indication waits. The remaining monotonic budget is passed
# through; a zero or already-expired budget does not start another read.
_ENC_READ_CAP = 0.5
_PLAIN_READ_CAP = 1.0
_HANDSHAKE_READ_CAP = 1.5
#: Independent budget for the plaintext PUBLIC_QUERY phase of :meth:`Camera.pair`.
PAIR_PUBLIC_QUERY_BUDGET = 6.0

#: Bound on decrypted stray RPC replies retained between reads.
PENDING_RESPONSE_LIMIT = 16
_RETIRED_SEQ_LIMIT = 64


class Camera:
    def __init__(self, transport: Transport, pairing_key: bytes | None = None, device_id: int = 1):
        self.transport = transport
        self.pairing_key = pairing_key
        self.device_id = device_id
        self.sess: SecureSession | None = None
        self.state = P.CameraState()
        self.app_nonce: bytes | None = None
        self.lens_nonce: bytes | None = None
        self.notifications: list[bytes] = []
        self._pending: list[bytes] = []
        self._retired_seqs: deque[int] = deque(maxlen=_RETIRED_SEQ_LIMIT)
        self._seq = 0
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Callable]] = {e: [] for e in _EVENTS}
        self._disconnected_emitted = False
        self._hb_thread: threading.Thread | None = None
        self._hb_stop: threading.Event | None = None
        self._last_hb = 0.0

    # ------------------------------------------------------------------ events

    def on(self, event: str, callback: Callable) -> Callable:
        """Register ``callback`` for ``event``; returns it for :meth:`off`."""
        if event not in self._listeners:
            raise ValueError(f"unknown event {event!r}")
        self._listeners[event].append(callback)
        return callback

    def off(self, event: str, callback: Callable) -> None:
        try:
            self._listeners[event].remove(callback)
        except (KeyError, ValueError):
            pass

    def _emit(self, event: str, *args) -> None:
        for cb in list(self._listeners.get(event, ())):
            try:
                cb(*args)
            except Exception:
                logger.exception("event handler for %s failed", event)

    # ------------------------------------------------------------------ plumbing

    @property
    def connected(self) -> bool:
        return self.sess is not None and getattr(self.transport, "alive", True)

    def _check_alive(self) -> None:
        if not getattr(self.transport, "alive", True):
            if not self._disconnected_emitted:
                self._disconnected_emitted = True
                self._emit(EVENT_DISCONNECTED)
            raise ConnectionLost("BLE link dropped")

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _write(self, frame: bytes) -> None:
        self._check_alive()
        try:
            self.transport.write(frame)
        except OSError as e:
            self.transport.alive = False
            self._check_alive()
            raise ConnectionLost(str(e)) from e

    def _send_plain(self, rtype: int, inner: bytes = b"") -> int:
        seq = self._next_seq()
        logger.debug("-> %s (plain, seq %d, %d B)", C.REQUEST_NAMES.get(rtype, rtype), seq, len(inner))
        self._write(bqs_frame(request_payload(rtype, inner, seq)))
        return seq

    def _send_enc(self, rtype: int, inner: bytes = b"") -> int:
        if self.sess is None:
            raise NotPaired("secure session not established; call resume() first")
        if rtype in C.UNSAFE_REQUESTS:
            raise UnsafeRequest(f"{C.REQUEST_NAMES.get(rtype, rtype)} is known to wedge the camera")
        with self._lock:
            seq = self._next_seq()
            frame = bqs_frame(self.sess.encrypt(request_payload(rtype, inner, seq)))
            if rtype != C.RT_KEEP_ALIVE:
                logger.debug("-> %s (seq %d, %d B)", C.REQUEST_NAMES.get(rtype, rtype), seq, len(inner))
            self._write(frame)
        return seq

    def _read_raw(self, timeout: float) -> bytes | None:
        self._check_alive()
        raw = self.transport.read_indication(timeout)
        if raw is None:
            self._check_alive()
        return raw

    def _deadline(self, timeout: float) -> float:
        return time.monotonic() + max(0.0, float(timeout))

    def _remaining(self, deadline: float) -> float:
        return deadline - time.monotonic()

    def _read_slice(self, deadline: float, cap: float) -> float | None:
        """Timeout to pass to one indication read, or ``None`` to stop.

        Never starts a read once the monotonic budget is exhausted, and never
        substitutes a fresh ``cap`` (0.5s / 1.0s) for a smaller remainder.
        """
        rem = self._remaining(deadline)
        if rem <= 0:
            return None
        return rem if rem < cap else cap

    def _read_plain(self, timeout: float, want_field: int | None = None) -> bytes | None:
        deadline = self._deadline(timeout)
        while True:
            slice_ = self._read_slice(deadline, _PLAIN_READ_CAP)
            if slice_ is None:
                return None
            raw = self._read_raw(slice_)
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

    def _decrypt(self, raw: bytes) -> bytes | None:
        body = strip_bqs(raw)
        pt = self.sess.decrypt(body)
        if pt is None and body is not raw:
            pt = self.sess.decrypt(raw)
        return pt

    def _apply_bundled_state(self, pt: bytes) -> bool:
        """Merge field-1 state entries. Does not absorb the message as a notification."""
        changes = self.state.update_from(pt)
        if not changes:
            return False
        entries = changes.pop("_entries", [])
        for e in entries:
            logger.debug("<- notification %s %s", e["name"], e["fields"])
            self._emit(EVENT_NOTIFICATION, e["kind"], e["name"], e["fields"])
        if changes:
            logger.debug("state changed: %s", changes)
        self._emit(EVENT_STATE, self.state, changes)
        return True

    def _classify(self, pt: bytes) -> bool:
        """Absorb keep-alive answers and state notifications. True if absorbed."""
        if P.is_keepalive_response(pt):
            return True
        if not self._apply_bundled_state(pt):
            return False
        if len(P.response_fields(pt)) <= 1:
            self.notifications.append(pt)
            return True
        return False

    def _matches(self, pt: bytes, seq: int | None, field: int | None) -> bool:
        """True only when an encrypted RPC reply has the expected field *and* echo.

        A matching field with a different (or missing) sequence echo, or a
        matching echo with the wrong field, does not complete the request.
        There is no field-only fallback for encrypted RPCs.
        """
        try:
            f = parse_pb(pt)
        except ValueError:
            return False
        echo = first(f, C.SEQ_ECHO_FIELD)
        if field is not None:
            if field not in f or echo is None:
                return False
            return seq is None or echo == seq
        return seq is not None and echo == seq

    def _reply_kind(self, pt: bytes) -> str:
        """Response type label for diagnostics. Never includes payloads."""
        try:
            parsed = parse_pb(pt)
        except ValueError:
            return "malformed"
        fields = [k for k in parsed if k != C.SEQ_ECHO_FIELD]
        if not fields:
            return "unknown"
        rpc_fields = [n for n in fields if n != 1] or fields
        field = rpc_fields[0]
        name = C.REQUEST_NAMES.get(field - 1)
        return name if name else f"field {field}"

    def _echo(self, pt: bytes):
        try:
            return first(parse_pb(pt), C.SEQ_ECHO_FIELD)
        except ValueError:
            return None

    def _log_discarded(self, pts: list[bytes], reason: str) -> None:
        if not pts:
            return
        kinds: dict[str, int] = {}
        for pt in pts:
            k = self._reply_kind(pt)
            kinds[k] = kinds.get(k, 0) + 1
        logger.debug("discarded %d %s replies types=%s", len(pts), reason, kinds)

    def _clear_connection_buffers(self) -> None:
        if self._pending:
            self._log_discarded(list(self._pending), "reset")
        self._pending.clear()
        self._retired_seqs.clear()

    def _retire(self, seq: int) -> None:
        self._retired_seqs.append(seq)
        kept: list[bytes] = []
        dropped: list[bytes] = []
        for pt in self._pending:
            if self._echo(pt) == seq:
                dropped.append(pt)
            else:
                kept.append(pt)
        self._pending = kept
        self._log_discarded(dropped, "completed")

    def _buffer_pending(self, pt: bytes) -> None:
        echo = self._echo(pt)
        if echo is not None and echo in self._retired_seqs:
            self._log_discarded([pt], "stale")
            return
        try:
            parse_pb(pt)
        except ValueError:
            self._log_discarded([pt], "malformed")
            return
        dropped: list[bytes] = []
        while len(self._pending) >= PENDING_RESPONSE_LIMIT:
            dropped.append(self._pending.pop(0))
        self._log_discarded(dropped, "overflow")
        self._pending.append(pt)

    def _take_pending_match(self, seq: int | None, field: int | None) -> bytes | None:
        for pt in list(self._pending):
            echo = self._echo(pt)
            if echo is not None and echo in self._retired_seqs:
                self._pending.remove(pt)
                self._log_discarded([pt], "stale")
                continue
            if self._matches(pt, seq, field):
                self._pending.remove(pt)
                return pt
        return None

    def _read_enc(
        self,
        timeout: float,
        seq: int | None = None,
        field: int | None = None,
        *,
        apply_state: bool = True,
    ) -> bytes | None:
        hit = self._take_pending_match(seq, field)
        if hit is not None:
            return hit
        deadline = self._deadline(timeout)
        while True:
            slice_ = self._read_slice(deadline, _ENC_READ_CAP)
            if slice_ is None:
                return None
            raw = self._read_raw(slice_)
            if not raw:
                continue
            pt = self._decrypt(raw)
            if pt is None:
                logger.debug("<- undecryptable indication (%d B)", len(raw))
                continue
            # Keepalive and notification-only frames stay off the RPC path.
            # A matching RPC is returned, not absorbed; ordinary callers still
            # apply bundled field-1 state. resume() passes apply_state=False
            # so it remains the CSC owner.
            if P.is_keepalive_response(pt):
                continue
            if self._matches(pt, seq, field):
                if apply_state:
                    self._apply_bundled_state(pt)
                return pt
            if self._classify(pt):
                continue
            logger.debug("<- stray response types=%s", self._reply_kind(pt))
            self._buffer_pending(pt)

    def request(self, rtype: int, inner: bytes = b"", timeout: float = 12.0) -> bytes | None:
        """Send an encrypted request and wait for its correlated response.

        Completes only on a decrypted reply that carries both the expected
        response field (``rtype + 1``) and the sequence echo for this call.
        Returns ``None`` on timeout. Single-caller: do not overlap with another
        :meth:`request` on the same instance.
        """
        seq = self._send_enc(rtype, inner)
        pt = self._read_enc(timeout, seq=seq, field=C.response_field(rtype))
        self._retire(seq)
        if pt is None:
            logger.debug("<- %s timed out after %.0fs", C.REQUEST_NAMES.get(rtype, rtype), timeout)
        return pt

    def poll(self, timeout: float = 0.5) -> int:
        """Read incoming indications for up to ``timeout`` seconds.

        Fires events for notifications and buffers stray responses. Returns
        the number of notifications processed. Call this from an app loop
        when nothing else is talking to the camera.
        """
        if self.sess is None:
            raise NotPaired("secure session not established")
        n = 0
        deadline = self._deadline(timeout)
        while True:
            slice_ = self._read_slice(deadline, _ENC_READ_CAP)
            if slice_ is None:
                return n
            raw = self._read_raw(slice_)
            if not raw:
                continue
            pt = self._decrypt(raw)
            if pt is None:
                continue
            if self._classify(pt):
                n += 1
            else:
                self._buffer_pending(pt)

    def wait_for(self, predicate: Callable[[P.CameraState], bool], timeout: float, heartbeat: float = 4.0) -> bool:
        """Poll until ``predicate(state)`` holds or ``timeout`` passes.

        Sends a heartbeat every ``heartbeat`` seconds unless a keepalive
        thread is already running. Elapsed time is monotonic; each :meth:`poll`
        receives the remaining budget rather than a fresh 0.5s slice after
        expiry.
        """
        deadline = self._deadline(timeout)
        while True:
            if predicate(self.state):
                return True
            rem = self._remaining(deadline)
            if rem <= 0:
                return False
            if self._hb_thread is None and (time.monotonic() - self._last_hb) > heartbeat:
                self.heartbeat()
            self.poll(rem if rem < 0.5 else 0.5)

    # ------------------------------------------------------------------ keepalive

    def heartbeat(self) -> None:
        """Encrypted KEEP_ALIVE. Send every few seconds during long waits."""
        self._send_enc(C.RT_KEEP_ALIVE)
        self._last_hb = time.monotonic()

    def start_keepalive(self, interval: float = 3.5) -> None:
        """Run heartbeats on a background thread until :meth:`stop_keepalive`."""
        if self._hb_thread is not None:
            return
        stop = threading.Event()

        def loop():
            while not stop.wait(interval):
                try:
                    self.heartbeat()
                except ConnectionLost:
                    return
                except Exception:
                    logger.debug("heartbeat failed", exc_info=True)
                    return

        t = threading.Thread(target=loop, name="openclips-keepalive", daemon=True)
        self._hb_stop, self._hb_thread = stop, t
        t.start()

    def stop_keepalive(self) -> None:
        if self._hb_thread is None:
            return
        self._hb_stop.set()
        self._hb_thread.join(timeout=2)
        self._hb_thread = self._hb_stop = None

    @contextmanager
    def keepalive(self, interval: float = 3.5):
        """Heartbeats for the duration of the block (no-op if already running)."""
        started = self._hb_thread is None
        if started:
            self.start_keepalive(interval)
        try:
            yield
        finally:
            if started:
                self.stop_keepalive()

    # ------------------------------------------------------------------ handshake

    def pair(self, host_key, name: str = "openclips", timeout: float = 30.0) -> tuple[bytes, bytes]:
        """Setup-mode pairing. Returns ``(pairing_key, lens_public_key)``.

        Only works during the setup window after a factory reset (volleying
        white LEDs, ``setup_bit`` set in advertisements). Follow with
        :meth:`resume` on the same connection.

        ``PUBLIC_QUERY`` has a fixed 6 s budget. ``INITIATE_PAIRING`` uses
        ``timeout`` (default 30 s). Both waits are monotonic; nested reads
        receive the remaining budget. Plaintext replies match on response
        field only.
        """
        self._send_plain(C.RT_PUBLIC_QUERY, P.build_public_query())
        # PUBLIC_QUERY has its own budget, not taken from ``timeout``.
        self._read_plain(PAIR_PUBLIC_QUERY_BUDGET, want_field=C.response_field(C.RT_PUBLIC_QUERY))
        self._send_plain(C.RT_INITIATE_PAIRING, P.build_initiate_pairing(host_public_bytes(host_key), name))
        deadline = self._deadline(timeout)
        lens_pub = None
        while lens_pub is None:
            rem = self._remaining(deadline)
            if rem <= 0:
                break
            body = self._read_plain(rem if rem < _HANDSHAKE_READ_CAP else _HANDSHAKE_READ_CAP)
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
        logger.info("paired with lens key %s…", lens_pub.hex()[:16])
        return self.pairing_key, lens_pub

    def resume(self, app_nonce: bytes | None = None, timeout: float = 15.0) -> P.CameraState:
        """ISC + CSC with the stored pairing key. Returns the camera state.

        ISC uses ``timeout`` (default 15 s). CSC uses a fresh ``timeout``,
        not the remainder of ISC. CSC is encrypted and requires the response
        field plus sequence echo; plaintext ISC matches on response field.
        """
        if not self.pairing_key:
            raise NotPaired("no pairing key")
        self._clear_connection_buffers()
        self.app_nonce = app_nonce or os.urandom(16)
        self._send_plain(C.RT_ISC, P.build_isc(self.app_nonce))
        isc = None
        isc_deadline = self._deadline(timeout)
        while isc is None:
            rem = self._remaining(isc_deadline)
            if rem <= 0:
                break
            body = self._read_plain(rem if rem < _HANDSHAKE_READ_CAP else _HANDSHAKE_READ_CAP)
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
        seq = self._send_plain(C.RT_CSC, P.build_csc(proof, self.device_id))
        self.sess = SecureSession(self.pairing_key, self.app_nonce, self.lens_nonce)
        # CSC has its own budget, equal to ``timeout``, not the remainder of ISC.
        pt = self._read_enc(timeout, seq=seq, field=C.response_field(C.RT_CSC), apply_state=False)
        if pt is None:
            self.sess = None
            self._clear_connection_buffers()
            raise RequestTimeout("no CSC response")
        self._retire(seq)
        self.state = P.CameraState()
        changes = self.state.update_from(pt)
        changes.pop("_entries", None)
        self._emit(EVENT_STATE, self.state, changes)
        logger.info("session up: %s", self.state.system_state_name)
        self.disable_timeouts()
        return self.state

    # ------------------------------------------------------------------ control

    def disable_timeouts(self) -> bool:
        return self.request(C.RT_DISABLE_TIMEOUTS, timeout=8) is not None

    def time_sync(self, millis: int | None = None) -> bool:
        """PRIVATE_QUERY with the host clock. Moment timestamps depend on it."""
        return self.request(C.RT_PRIVATE_QUERY, P.build_private_query(millis), timeout=8) is not None

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
        """Moments of a completed session, best score first. Blocks while the session is open."""
        pt = self.request(C.RT_LIST_MOMENTS, P.build_list_moments(session_id), timeout)
        if pt is None:
            raise RequestTimeout("LIST_MOMENTS")
        return P.parse_list_moments(pt, session_id)

    def moments(self, session_id: int, timeout: float = 20.0) -> list[P.MomentInfo]:
        """Typed convenience over :meth:`list_moments`."""
        return self.list_moments(session_id, timeout)["moments"]

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
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            text = raw.decode("utf-8", "replace")
            unavailable = e.code == 500 and (
                "moment could not be opened" in text.lower() or "moment content not present" in text.lower()
            )
            retryable = e.code in (502, 503, 504) and not unavailable
            raise HttpError(path, status=e.code, retryable=retryable) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HttpError(path, retryable=True) from None

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
        self.stop_keepalive()
        self._clear_connection_buffers()
        self.transport.close()
        self.sess = None


def extract_jpeg(data: bytes) -> bytes | None:
    """Return the JPEG starting at the first SOI marker, or ``None``."""
    i = data.find(b"\xff\xd8\xff")
    return data[i:] if i >= 0 else None
