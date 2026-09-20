# How the protocol was recovered

A record of the methods, tools and dead ends behind `docs/PROTOCOL.md`, so
the work can be checked and extended. No Google code or binaries are in
this repository; you need your own camera and your own copy of the
discontinued Android app to repeat any of it.

## Sources of truth, in order of trust

1. **Live captures** from the camera. Every formula in the protocol
   document was checked against bytes the camera actually sent.
2. **Firmware strings** from the camera's update package (log messages such
   as "Moment list request missing session_id." or "Cover closed while in
   idle, staying in idle.") which explained several failures.
3. **Schema metadata** embedded in the Android app's DEX (protobuf-javalite
   descriptor strings) which gives field numbers and wire types but not
   names.
4. **Decompiled app code**, used only to confirm request ordering and
   which fields the app populates. Nothing derived from it is
   redistributed here.

## Toolchain

| Task | Tool |
|---|---|
| APK unpacking and decompilation | jadx, dex-tools, androguard |
| Protobuf schema recovery | `research/tools/schema_dex_extract.py`, `decode_javalite.py`, `gen_links_proto.py` (ours) |
| Embedded FileDescriptorProto recovery from the native lib | `research/tools/extract_fdp3.py`, `fdp_to_proto.py` (ours) |
| Native crypto library analysis (arm64) | capstone disassembly, Unicorn emulation (`research/tools/emu_*.py`) |
| BLE capture | `btmon`, and `btgatt-client -v` whose console prints indication payloads |
| Firmware | binwalk-style carving of the update tar; SPARC ELF inspection |

## Timeline of findings

**Transport.** The first days were lost to the BLE sidecar. Fragmented
writes, prepared writes and BlueZ D-Bus writes all wedged it. The only
reliable path turned out to be single ATT Write Requests at MTU 512 through
`btgatt-client`. `btmon` block-buffers its output and is useless for
scripting; the `btgatt-client` console prints indications live.

**Framing and envelope.** The javalite schemas showed the outer `Request`
as a oneof whose field number is the RPC type, with `seq` in field 38, and
the `Response` mirroring it at type + 1 with the echo in field 40.

**Pairing key.** ECDH P-256 was obvious from a 64-byte public key on the
wire. The standard X-only KDF did not verify against the camera's proof.
The camera hashes **both** coordinates of the shared point, each
little-endian: `SHA256(x_le || y_le)`. This was confirmed when the lens
proof verified live.

**Session keys.** HKDF-SHA256 with the label
`links-session-key-hkdf-sha256`, info `app_nonce || lens_nonce`, 48 bytes
out: two 16-byte AES keys and a 16-byte nonce base. AES-EAX with a 12-byte
tag and a 3-byte little-endian counter appended to the nonce base.

**The emulation artefact (a lesson worth keeping).** An early Unicorn
oracle of the app's native HMAC produced `MAC(key, msg) = SHA256(opad(key))`
with the message discarded, which would have meant constant session keys
independent of nonces. It was wrong: the emulator's output buffer aliased
the key buffer, so the inner hash was fed zeros. Cross-checking against
the live lens proof exposed it. The library uses plain FIPS HMAC. Never
trust an emulated primitive until it reproduces a captured vector.

**Wi-Fi.** `INITIATE_WIFI` returns SSID, a 16-byte passphrase and the
HTTP base URL. The app's javalite schema for this request was incomplete;
the firmware's own request shape is `{1: wifi_direct, 2: capture_preview}`
and `{0, 1}` opens the SoftAP even in paired mode. Field 2 is not a session
id, as an earlier guess had it.

**HTTP.** The server answers roughly two to five requests per join before
a Wi-Fi Direct group-owner negotiation times out and it stops. Keeping BLE
heartbeats running and issuing `INITIATE_CAPTURE_PREVIEW` after the Wi-Fi
request kept it alive long enough for six full-resolution downloads.
`GET /fetch_moment` returns "Package entity is deprecated."; the working
form is `POST` with a protobuf body.

**Capture.** The hardest part had nothing to do with crypto. Manual
shutter events returned SUCCESS but produced no moments. `TRIGGER_CAPTURE`
always failed. `SET_SESSION_MODE` with a guessed payload stalled the link.
Moments come from the AI curator, which only runs in `SYSTEM_STATE_CAPTURE`,
and the camera would not enter CAPTURE. Firmware strings pointed at two
gates: an update-required flag that defaults to required
(`SET_UPDATE_REQUIRED {false}` clears it) and a hardware cover-open edge
(`SetCoverOpen` in the firmware; the BLE cover request only updates a
notification). With the flag cleared, closing and opening the physical
cover put the camera into CAPTURE and, after completing the session with
the cover closed, `LIST_MOMENTS` returned six moment ids.

**LIST_MOMENTS field 1.** Documented in the app as a since-timestamp, it
is a `fixed64 session_id`. Sending 0 yields an empty list and the
firmware log says "Moment list request missing session_id."

## Firmware facts (for future work)

* `myriad.bin` is a SPARC ELF32 little-endian with one LOAD segment;
  virtual address = `0x80000000 + file offset` for offsets below
  `0x102c4b4`. Stripped symbol table.
* Crypto sources referenced by strings: `aes_eax.cc`, `spake2.cc`,
  `hkdf.cc` under `wear/links/common/crypto/`. SPAKE2 appears unused on
  the wire; the session uses plain HMAC proofs.
* The SHA-256 K-table sits at virtual address `0x80cecee8`.
* `sidecar.bin` is a bare-metal Cortex-M image with no crypto.
* The update package ships 51 AI model blobs and a `manifest.proto`
  naming firmware `1.8.245834322` with `--lens_env=prod`.

## Redistribution policy

This repository contains only original code and documentation of observed
behaviour. It does not include the Android app, its decompiled sources,
its embedded schema dumps, firmware images, model files or media captured
by the camera. Interoperability facts (UUIDs, field numbers, formulas) are
documented from observation. If you contribute, keep it that way.
