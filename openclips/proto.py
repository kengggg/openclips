"""Request builders and response parsers for the camera RPCs.

Field layouts come from ``docs/PROTOCOL.md``. Builders return the *inner*
message bytes; :func:`openclips.framing.request_payload` wraps them in the
outer ``Request``. Parsers accept a decrypted outer ``Response`` and return
plain dicts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import constants as C
from .pb import (
    first,
    first_bytes,
    parse_packed_varints,
    parse_pb,
    pb_bool,
    pb_bytes,
    pb_fixed64,
    pb_packed_varints,
    pb_uint,
)

# --- Setup / handshake -------------------------------------------------------


def build_public_query(request_id: int | None = None, proto_ver: int = C.PROTO_VER) -> bytes:
    """``{1: request_id, 2: protocol_version}`` (setup mode only)."""
    if request_id is None:
        request_id = int(time.time() * 1000)
    return pb_uint(1, request_id) + pb_uint(2, proto_ver)


def build_initiate_pairing(host_pub64: bytes, name: str | bytes = "openclips") -> bytes:
    """``{1: 1, 2: host_pub64, 3: name}``."""
    if len(host_pub64) != 64:
        raise ValueError("host public key must be 64 bytes")
    return pb_uint(1, 1) + pb_bytes(2, host_pub64) + pb_bytes(3, name)


def parse_initiate_pairing(pt: bytes) -> bytes | None:
    """Lens public key (64 bytes) from the pairing response, or ``None``."""
    inner = _inner(pt, C.RT_INITIATE_PAIRING)
    if inner is None:
        return None
    pub = first_bytes(parse_pb(inner), 3)
    return pub if len(pub) == 64 else None


def build_isc(app_nonce: bytes) -> bytes:
    """``{1: app_nonce16}``."""
    if len(app_nonce) != 16:
        raise ValueError("nonce must be 16 bytes")
    return pb_bytes(1, app_nonce)


@dataclass
class IscResponse:
    status: int | None
    lens_nonce: bytes
    lens_proof: bytes


def parse_isc(pt: bytes) -> IscResponse | None:
    """``{1: status, 2: lens_nonce16, 3: lens_proof32}``."""
    inner = _inner(pt, C.RT_ISC)
    if inner is None:
        return None
    f = parse_pb(inner)
    nonce, proof = first_bytes(f, 2), first_bytes(f, 3)
    if len(nonce) != 16 or len(proof) != 32:
        return None
    return IscResponse(first(f, 1), nonce, proof)


def build_csc(app_proof32: bytes, device_id: int = 1) -> bytes:
    """``{1: app_proof32, 2: device_id}``."""
    if len(app_proof32) != 32:
        raise ValueError("proof must be 32 bytes")
    return pb_bytes(1, app_proof32) + pb_uint(2, device_id)


def build_private_query(millis: int | None = None) -> bytes:
    """Time sync: ``{1: fixed64 unix_millis}``."""
    if millis is None:
        millis = int(time.time() * 1000)
    return pb_fixed64(1, millis)


# --- Simple settings ---------------------------------------------------------


def build_set_update_required(required: bool) -> bytes:
    """``{1: bool}``. Send ``False`` once or the camera never leaves IDLE."""
    return pb_bool(1, required)


def build_active_user(device_id: int = 1, username: str | bytes = "") -> bytes:
    """``{3: device_id, 1: true[, 2: username]}``."""
    body = pb_uint(3, device_id) + pb_bool(1, True)
    if username:
        body += pb_bytes(2, username)
    return body


def build_shutter(event: int) -> bytes:
    """``{1: SHUTTER_PRESSED | SHUTTER_RELEASED}``. Always send both."""
    return pb_uint(1, event)


def build_set_cover(open_: bool) -> bytes:
    """``{1: bool open}``. Updates the notification only; not a capture trigger."""
    return pb_bool(1, open_)


# --- Wi-Fi ---------------------------------------------------------------------

WIFI_MODE_UNKNOWN = 0
WIFI_MODE_CLIENT = 1
WIFI_MODE_AP = 2
WIFI_MODE_DIRECT_CLIENT = 3
WIFI_MODE_DIRECT_GO = 4


def build_initiate_wifi(wifi_direct: bool = False, capture_preview: bool = True) -> bytes:
    """``{1: wifi_direct, 2: capture_preview}``.

    ``{0, 1}`` is the live-validated paired-mode request that opens the
    camera's SoftAP.
    """
    return pb_bool(1, wifi_direct) + pb_bool(2, capture_preview)


def build_initiate_wifi_paired() -> bytes:
    return build_initiate_wifi(wifi_direct=False, capture_preview=True)


def parse_initiate_wifi(pt: bytes) -> dict:
    """``{1: status, 2: ssid, 3: passphrase, 4: http_address, 5: mode}``."""
    inner = _inner(pt, C.RT_INITIATE_WIFI)
    out = {"status": None, "ssid": None, "passphrase": None, "url": None, "mode": None}
    if inner is None:
        return out
    f = parse_pb(inner)
    out["status"] = first(f, 1)
    ssid, psk, url = first_bytes(f, 2), first_bytes(f, 3), first_bytes(f, 4)
    out["ssid"] = ssid.decode("utf-8", "replace") if ssid else None
    out["passphrase"] = psk.decode("latin-1") if psk else None
    out["url"] = url.decode("ascii", "replace") if url else None
    out["mode"] = first(f, 5)
    return out


# --- Sessions and moments ----------------------------------------------------


def build_list_sessions(newer_than: int = 0, max_count: int = 20) -> bytes:
    """``{1: newer_than fixed64, 2: max_count}``."""
    return pb_fixed64(1, newer_than) + pb_uint(2, max_count)


def parse_list_sessions(pt: bytes | None) -> dict:
    """``{1: status, 2: session_ids (8-byte LE chunks), 3: open_session}``."""
    out = {"status": None, "session_ids": [], "open_session": None, "seq": None}
    if not pt:
        return out
    outer = parse_pb(pt)
    inner = first_bytes(outer, C.response_field(C.RT_LIST_SESSIONS))
    h = parse_pb(inner) if inner else outer
    sids: list[int] = []
    for v in h.get(2, []):
        if isinstance(v, (bytes, bytearray)):
            b = bytes(v)
            sids.extend(int.from_bytes(b[i : i + 8], "little") for i in range(0, len(b) - 7, 8))
        else:
            sids.append(int(v))
    out.update(
        status=first(h, 1),
        session_ids=sids,
        open_session=first(h, 3),
        seq=first(outer, C.SEQ_ECHO_FIELD),
    )
    return out


def build_list_moments(
    session_id: int,
    include_all: bool = True,
    include_timestamps: bool = True,
    include_scores: bool = True,
    include_triage: bool = True,
    include_filter: bool = True,
) -> bytes:
    """``{1: session_id fixed64, 2..6: include_* bools}``.

    Field 1 is a **session id**, not a timestamp. Zero yields an empty list.
    """
    return (
        pb_fixed64(1, session_id)
        + pb_bool(2, include_all)
        + pb_bool(3, include_timestamps)
        + pb_bool(4, include_scores)
        + pb_bool(5, include_triage)
        + pb_bool(6, include_filter)
    )


def parse_list_moments(pt: bytes | None) -> dict:
    """``{1: status, 2: packed moment ids, 3: is_final, 4: best_cutoff}``."""
    out = {
        "status": None,
        "ok": False,
        "moment_ids": [],
        "is_final": None,
        "best_cutoff": None,
        "seq": None,
        "raw": None,
    }
    if not pt:
        return out
    outer = parse_pb(pt)
    inner = first_bytes(outer, C.response_field(C.RT_LIST_MOMENTS))
    h = parse_pb(inner) if inner else outer
    ids: list[int] = []
    for v in h.get(2, []):
        if isinstance(v, (bytes, bytearray)):
            ids.extend(parse_packed_varints(bytes(v)))
        else:
            ids.append(int(v))
    status = first(h, 1)
    out.update(
        status=status,
        ok=status == C.STATUS_SUCCESS,
        moment_ids=ids,
        is_final=first(h, 3),
        best_cutoff=first(h, 4),
        seq=first(outer, C.SEQ_ECHO_FIELD),
        raw=(inner or pt).hex(),
    )
    return out


def build_placeholder(session_id: int, moment_id: int) -> bytes:
    """``{1: session_id fixed64, 2: moment_id}``. BLE never returned image bytes live."""
    return pb_fixed64(1, session_id) + pb_uint(2, moment_id)


def build_packed_moment_ids(session_id: int, moment_ids) -> bytes:
    """``{1: session_id fixed64, 2: packed int32 ids}``."""
    return pb_fixed64(1, session_id) + pb_packed_varints(2, moment_ids)


def build_delete_moments(session_id: int, moment_ids) -> bytes:
    """``{1: PackedMomentIds}``. Same shape for trash and restore."""
    return pb_bytes(1, build_packed_moment_ids(session_id, moment_ids))


build_move_moments_to_trash = build_delete_moments
build_restore_moments_from_trash = build_delete_moments


# --- HTTP media bodies ---------------------------------------------------------


def build_moment_id(session_id: int, moment_id: int) -> bytes:
    """``MomentId {1: session_id varint, 2: moment_id varint}`` (HTTP side)."""
    return pb_uint(1, session_id) + pb_uint(2, moment_id)


def build_fetch_moment(
    session_id: int,
    moment_id: int,
    resolution: int = C.RESOLUTION_FULL,
    offset: int | None = None,
    length: int | None = None,
) -> bytes:
    """Body for ``POST /fetch_moment``."""
    body = pb_bytes(1, build_moment_id(session_id, moment_id)) + pb_uint(2, resolution)
    if offset is not None:
        body += pb_uint(3, offset)
    if length is not None:
        body += pb_uint(4, length)
    return body


def build_fetch_moment_metadata(session_id: int, moment_id: int) -> bytes:
    """Body for ``POST /fetch_moment_metadata``."""
    return pb_bytes(1, build_moment_id(session_id, moment_id))


def build_fetch_frame(session_id: int, moment_id: int) -> bytes:
    """Body for ``POST /fetch_frame``."""
    return pb_bytes(1, build_moment_id(session_id, moment_id))


def parse_preview_header(data: bytes) -> dict:
    """Best-effort decode of ``GET /preview_header`` (44 bytes live)."""
    if not data:
        return {}
    f = parse_pb(data)
    out = {"raw": data.hex(), "fields": f}
    if isinstance(first(f, 3), int):
        out["width"] = first(f, 3)
    if isinstance(first(f, 4), int):
        out["height"] = first(f, 4)
    return out


# --- Generic response helpers ---------------------------------------------------


def _inner(pt: bytes | None, rtype: int) -> bytes | None:
    """Payload of the response field for ``rtype``, or ``None`` if absent."""
    if not pt:
        return None
    try:
        outer = parse_pb(pt)
    except ValueError:
        return None
    vals = outer.get(C.response_field(rtype))
    if not vals:
        return None
    v = vals[0]
    return bytes(v) if isinstance(v, (bytes, bytearray)) else b""


def parse_status(pt: bytes | None, rtype: int) -> int | None:
    """Status enum from a ``{1: status}`` response body, or ``None``."""
    inner = _inner(pt, rtype)
    if inner is None:
        return None
    if not inner:
        return None
    return first(parse_pb(inner), 1)


def is_success(pt: bytes | None, rtype: int) -> bool:
    return parse_status(pt, rtype) == C.STATUS_SUCCESS


def is_keepalive_response(pt: bytes) -> bool:
    """``Response {4: {1: 1}, 40: seq}`` — the answer to KEEP_ALIVE."""
    try:
        f = parse_pb(pt)
    except ValueError:
        return False
    return C.response_field(C.RT_KEEP_ALIVE) in f and not (set(f) - {4, C.SEQ_ECHO_FIELD})


def response_fields(pt: bytes) -> list[int]:
    """Response field numbers present, excluding the seq echo."""
    try:
        return [k for k in parse_pb(pt) if k != C.SEQ_ECHO_FIELD]
    except ValueError:
        return []


# --- State bundle ------------------------------------------------------------------


@dataclass
class CameraState:
    """Decoded state notifications (CSC response and async updates)."""

    system_state: int | None = None
    cover_open: bool | None = None
    session_uuid: int | None = None
    session_mode: int | None = None
    storage_level: int | None = None
    storage_state: int | None = None
    occlusion: int | None = None
    battery_pct: int | None = None
    charge_state: int | None = None
    wifi_state: int | None = None
    active_device_id: int | None = None
    raw: list = field(default_factory=list, repr=False)

    @property
    def system_state_name(self) -> str:
        return C.SYSTEM_STATE.get(self.system_state, "?")

    def as_dict(self) -> dict:
        return {
            "system_state": self.system_state,
            "system_state_name": self.system_state_name,
            "cover_open": self.cover_open,
            "session_uuid": self.session_uuid,
            "session_mode": self.session_mode,
            "session_mode_name": C.SESSION_MODE.get(self.session_mode, "?"),
            "storage_level": self.storage_level,
            "storage_state": self.storage_state,
            "storage_state_name": C.STORAGE_STATE.get(self.storage_state, "?"),
            "occlusion": self.occlusion,
            "occlusion_name": C.OCCLUSION.get(self.occlusion, "?"),
            "battery_pct": self.battery_pct,
            "charge_state": self.charge_state,
            "charge_state_name": C.CHARGE_STATE.get(self.charge_state, "?"),
            "wifi_state": self.wifi_state,
            "wifi_state_name": C.WIFI_STATE.get(self.wifi_state, "?"),
            "active_device_id": self.active_device_id,
        }

    def update_from(self, pt: bytes) -> bool:
        """Merge every state entry found in ``pt``. Returns True if any."""
        try:
            outer = parse_pb(pt)
        except ValueError:
            return False
        blobs = [v for v in outer.get(1, []) if isinstance(v, (bytes, bytearray))]
        found = False
        for raw in blobs:
            try:
                entries = parse_pb(bytes(raw))
            except ValueError:
                continue
            for kind, vals in entries.items():
                payload = vals[0]
                if isinstance(payload, (bytes, bytearray)):
                    try:
                        inner = parse_pb(bytes(payload))
                    except ValueError:
                        inner = {}
                else:
                    inner = {1: [payload]}
                self.raw.append({"kind": kind, "name": C.NOTIFICATION_KIND.get(kind, "?"), "fields": inner})
                self._apply(kind, inner)
                found = True
        return found

    def _apply(self, kind: int, inner: dict) -> None:
        if kind == 1:
            self.battery_pct = first(inner, 1, self.battery_pct)
            self.charge_state = first(inner, 2, self.charge_state)
        elif kind == 2:
            self.system_state = first(inner, 1, self.system_state)
        elif kind == 3:
            self.session_uuid = first(inner, 1, self.session_uuid)
        elif kind == 4:
            self.storage_level = first(inner, 1, self.storage_level)
            self.storage_state = first(inner, 2, self.storage_state)
        elif kind == 5:
            self.session_mode = first(inner, 1, self.session_mode)
        elif kind == 9:
            v = first(inner, 1)
            if v is not None:
                self.cover_open = bool(v)
        elif kind == 10:
            self.occlusion = first(inner, 1, self.occlusion)
        elif kind == 13:
            self.wifi_state = first(inner, 1, self.wifi_state)
        elif kind in (16, 100):
            self.active_device_id = first(inner, 2, first(inner, 1, self.active_device_id))


def parse_state(pt: bytes) -> CameraState:
    st = CameraState()
    st.update_from(pt)
    return st
