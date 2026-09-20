# Google Clips host protocol

Everything a client needs to talk to a Google Clips camera (model GC-6013,
firmware 1.8) without the discontinued Android app. All statements marked
**live** were validated against a real camera from Linux; everything else is
marked as inferred. The Python package in this repository implements this
document; port from here, not from the code.

## Architecture

```
host  --BLE GATT-->  sidecar MCU (Cortex-M, BLE only, no crypto)
                          |
                          v
                      Myriad 2 SoC (SPARC LEON: protocol, AES-EAX, AI curation, HTTP :8080)

host  --Wi-Fi SoftAP "Clips6013"-->  http://192.168.49.10:8080
```

* USB-C is charge-only. All control is BLE. Media comes over HTTP after
  `INITIATE_WIFI`.
* Advertisements carry manufacturer-specific data with company ID **224**
  (`0x00E0`). Byte 0 bit 0 is `setup_bit`: 1 during the unpaired setup
  window (roughly 1 to 2 minutes after a 15-second pinhole factory reset).
* Myriad idles after a few minutes without traffic. The camera keeps
  advertising and GATT connects, but requests go unanswered until a short
  **shutter press** wakes it. A short pinhole press reboots Myriad without
  losing the pairing; holding it 15 s factory-resets.

### GATT

| Role | UUID | ATT handle on 1.8 |
|---|---|---|
| Service | `00000003-0003-1000-8000-001A11000100` | |
| Write (requests) | `00010001-0003-1000-8000-001A11000100` | `0x0009` |
| Indicate (responses) | `80010001-0003-1000-8000-001A11000100` | `0x000C` (CCCD `0x000E`) |

**Only single ATT Write Requests at MTU 512 work** (live). Long or
prepared writes and BlueZ's D-Bus GATT path (what `bleak` uses on Linux)
wedge the sidecar with ATT "Unlikely Error" and the camera needs a factory
reset. The proven Linux path is BlueZ's `btgatt-client -m 512` driven
through a PTY. Other platforms' native GATT stacks are expected to work
because the wedge is specific to how BlueZ issues writes, but this is
untested.

## Framing

Every ATT value in either direction is:

```
frame = varint(len(body)) || body
```

`body` is a plaintext protobuf message in setup mode and
`ciphertext || tag12` (AES-EAX, below) once the secure channel is up. The
length prefix is **not** part of the encrypted data. Indications are always
prefixed on 1.8; strip the prefix before decrypting.

## Request and response envelopes

The outer `Request` uses the **RPC type as the field number** and carries a
sequence number in field 38:

```
Request  { <type>: bytes inner, 38: uint32 seq }
Response { <type + 1>: bytes inner, 40: uint32 seq_echo, 1: repeated StateEntry }
```

Status enum in most inner responses: `1 = SUCCESS`, `2 = FAILURE`. The
keep-alive answer is `Response {4: {1: 1}, 40: seq}`.

Field 1 of a `Response` may repeat a state bundle (see *State
notifications*). The CSC response always carries the full bundle; later
Responses carry single async updates with no request field at all.

### RPCs a replacement client needs

| Type | Name | Inner request | Notes |
|---|---|---|---|
| 1 | PUBLIC_QUERY | `{1: request_id, 2: protocol_version=26}` | Setup only. Plaintext after pairing drops the link. **live** |
| 4 | INITIATE_PAIRING | `{1: 1, 2: host_pub64, 3: name}` | Setup window. Response `{1: status, 3: lens_pub64}`. **live** |
| 6 | INITIATE_SECURE_CONNECTION | `{1: app_nonce16}` | Response `{1: status, 2: lens_nonce16, 3: lens_proof32}`. **live** |
| 7 | COMPLETE_SECURE_CONNECTION | `{1: app_proof32, 2: device_id}` | Encrypted channel starts with the response. **live** |
| 3 | KEEP_ALIVE | empty | Send every ~4 s while waiting for anything. **live** |
| 2048 | DISABLE_CONNECTION_TIMEOUTS | empty | Keeps Myriad talking. **live** |
| 2 | PRIVATE_QUERY | `{1: fixed64 unix_millis}` | Time sync; response has serial in field 13. **live** |
| 8 | SET_UPDATE_REQUIRED | `{1: bool}` | Send **false** once or the camera never enters CAPTURE. Firmware default is required. **live** |
| 51 | ACTIVE_USER_SETTINGS | `{3: device_id, 1: true[, 2: username]}` | Become the active phone. **live** |
| 9 | INITIATE_WIFI | `{1: wifi_direct, 2: capture_preview}` | `{0, 1}` opens the SoftAP in paired mode. Response `{1: status, 2: ssid, 3: psk16, 4: url}`. **live** |
| 10 | CANCEL_WIFI | empty | **live** |
| 11 | LIST_SESSIONS | `{1: fixed64 newer_than, 2: max_count}` | Response field 2 is concatenated 8-byte LE ids, field 3 the open session. **live** |
| 12 | LIST_MOMENTS | `{1: fixed64 session_id, 2..6: include_* bools}` | Field 1 is a **session id**, not a timestamp. `0` yields nothing. Blocks while the session is open. Response `{1: status, 2: packed ids, 3: is_final, 4: best_cutoff}`. **live** |
| 13 | GET_PLACEHOLDER_IMAGE | `{1: fixed64 session_id, 2: moment}` | Accepted, but returned no image bytes over BLE. Use HTTP. **live** |
| 14 | DELETE_MOMENTS | `{1: {1: fixed64 session_id, 2: packed ids}}` | Trash (49) and restore (50) share the shape. Delete **live**, others inferred. |
| 21 | COMPLETE_CURRENT_SESSION | empty | SUCCESS only after the hardware cover is closed. **live** |
| 27 | INITIATE_CAPTURE_PREVIEW | empty | FAILURE unless in CAPTURE. Helps hold the SoftAP group owner. **live** |
| 28 | CANCEL_CAPTURE_PREVIEW | empty | **live** |
| 29 | SET_COVER_STATE | `{1: bool open}` | Updates the notification only. Does **not** start capture. **live** |
| 42 | shutter (SET_PREFERENCES) | `{1: 1 PRESSED}` then `{1: 2 RELEASED}` | Always send both; PRESSED alone wedges the link. Ignored in IDLE. **live** |
| 35 | FLASH_IDENTIFY_LEDS | empty | **live** |
| 37 | TRIGGER_CAPTURE | empty | Always FAILURE on 1.8. Not the app's capture path. **live** |

**Never send** ACK_NEW_CONTENT (40), GET_SYSTEM_LOGS (36) or
SET_SESSION_MODE (22) with guessed payloads. Each of them stalled or poisoned
a live link until a factory reset.

### State notifications

A `StateEntry` is a oneof keyed by field number. Known kinds:

| Field | Kind | Payload |
|---|---|---|
| 1 | BATTERY | `{1: percent, 2: charge_state}` |
| 2 | ACTIVITY | `{1: system_state}` — 2 IDLE, 3 SETUP, 4 CAPTURE |
| 3 | SESSION | `{1: uuid}` (0 when nothing is open) |
| 4 | STORAGE | `{1: level, 2: state}` |
| 5 | SESSION_MODE | `{1: mode}` — 1 NORMAL |
| 9 | COVER | `{1: cover_open}` |
| 10 | OCCLUSION | `{1: state}` — 1 CLEARED |
| 13 | WIFI | `{1: state}` — 2 starting, 3 ready, 6 group-owner timeout |
| 100 | ACTIVE_USER | `{2: device_id}` |

## Crypto (all live-verified)

### Pairing

ECDH on P-256. Public keys travel as uncompressed `X || Y` (64 bytes,
no `0x04` prefix). Reusing the same host key across factory resets is
accepted.

```
S           = host_priv * lens_pub
pairing_key = SHA256(S.x_le || S.y_le)      # BOTH coordinates, little-endian
```

This is not an X-only KDF and not HKDF.

### Session proofs

```
app_proof  = HMAC-SHA256(pairing_key, 0x01 || app_nonce || lens_nonce)
lens_proof = HMAC-SHA256(pairing_key, 0x02 || app_nonce || lens_nonce)
```

Verify `lens_proof` before sending `app_proof`; a mismatch means the camera
was factory-reset and the stored key is void.

### Session keys

```
info   = app_nonce || lens_nonce
PRK    = HMAC-SHA256("links-session-key-hkdf-sha256", pairing_key)
T1     = HMAC-SHA256(PRK, info || 0x01)
T2     = HMAC-SHA256(PRK, T1 || info || 0x02)
keys48 = T1 || T2[:16]

host rx key (lens → host) = keys48[0:16]
host tx key (host → lens) = keys48[16:32]
nonce_base                = keys48[32:48]
```

### Channel

AES-EAX with a 12-byte tag appended to the ciphertext. The nonce is
19 bytes: `nonce_base || counter_le3`. Each direction has its own counter
starting at 1 for the first message. The CSC response is lens counter 1.
Decryption failures must not advance the counter.

## Capture lifecycle

The Android app never documented this; it was worked out live.

1. Pair in setup mode, or resume with a stored `pairing_key`.
2. Send `SET_UPDATE_REQUIRED {false}` once (persists across reboots).
3. Send `ACTIVE_USER_SETTINGS`.
4. Physically **close then open** the lens cover. The firmware enters
   CAPTURE on the cover-open edge only; sitting open in IDLE does nothing,
   and BLE `SET_COVER_STATE` does not fire the edge.
5. The camera curates moments on its own while in CAPTURE. Listing the open
   session blocks, so do not poll it.
6. Close the cover (state returns to IDLE), then `COMPLETE_CURRENT_SESSION`.
7. `LIST_SESSIONS`, then `LIST_MOMENTS(session_id)`, newest session first.
8. `INITIATE_WIFI {0, 1}` with heartbeats running, join the SoftAP, and
   `POST /fetch_moment` for each id.

## HTTP media API

Base URL is whatever `INITIATE_WIFI` returned, `http://192.168.49.10:8080`
on 1.8. Bodies are raw protobuf; no `Content-Type` is needed.

```
POST /fetch_moment           {1: {1: session_id, 2: moment_id}, 2: resolution}
POST /fetch_moment_metadata  {1: {1: session_id, 2: moment_id}}
POST /fetch_frame            {1: {1: session_id, 2: moment_id}}
GET  /lens_version           -> {2: build}
GET  /preview_header         -> 44-byte metadata
GET  /keep_alive             -> 200 empty
```

`resolution`: 1 FULL, 2 THUMB. FULL returns a JFIF of roughly 16 to 21 MB
(1332×1000 observed). THUMB may answer `500 Moment content not present.`
`GET /fetch_moment` answers `500 Package entity is deprecated.`

Operational facts:

* The DHCP lease on the SoftAP is about 55 to 59 s. Fetch immediately.
* Keep BLE heartbeats running while on Wi-Fi or the group owner dies
  within seconds (WIFI state 6). `INITIATE_CAPTURE_PREVIEW` after
  `INITIATE_WIFI` seems to help hold it.
* A factory reset is **not** needed to reopen the SoftAP in paired mode.

## What a port must implement

1. BLE writes and indications on the UUIDs above, single writes only.
2. Framing, protobuf, and the crypto in this document (or reuse
   `openclips`, which has no BLE dependency of its own).
3. Joining the WPA2 network the camera announces. This is OS-specific; the
   library only ships a Linux NetworkManager helper.
4. HTTP POST to the returned URL.

## Pairing persistence

Store the 32-byte `pairing_key` per camera. The host picks `device_id` at
CSC time; `1` works. After a factory reset the key is void: pair again.
