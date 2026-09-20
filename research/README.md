# Research tooling

Scripts written during the reverse engineering, kept so the schema
recovery and crypto analysis can be repeated or audited. They are **not
part of the library**, are not tested in CI, and expect you to supply your
own copy of the Android app (`com.google.android.apps.cerebra.links`) and
its native library. Nothing from Google is included here.

| Script | Purpose |
|---|---|
| `tools/decode_javalite.py` | Decode protobuf-javalite descriptor strings (the "13-bit varint" packed char format) |
| `tools/schema_dex_extract.py` | Pull descriptor strings and class lists straight from `classes.dex` and produce a field table per message |
| `tools/gen_links_proto.py` | Turn the extracted field tables into a `.proto` skeleton, mapping request type numbers to messages |
| `tools/extract_fdp3.py` | Carve embedded `FileDescriptorProto` blobs out of the native library |
| `tools/fdp_to_proto.py` | Render carved descriptors as `.proto` text |
| `tools/emu_oracle.py` | Unicorn-based oracle for the native library's HKDF and proof functions |
| `tools/emu_derive_keys.py` | Emulate the native `deriveSessionKeys` and dump the key schedule |

Paths are taken from environment variables:

| Variable | Meaning |
|---|---|
| `CLIPS_NATIVE_LIB` | Path to `liblinks_port_android_jni_native_library.so` (arm64) |
| `CLIPS_SESSION_STATE` | JSON with `pairing_key`, `our_nonce`, `lens_nonce` hex for the emulators to reproduce |

Read `docs/REVERSE-ENGINEERING.md` first. In particular, the emulators
once produced a wrong HMAC because of a buffer aliasing bug in the harness;
the live-validated formulas are the ones in `openclips/crypto.py`.
