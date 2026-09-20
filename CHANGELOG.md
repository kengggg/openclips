# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

App-readiness pass: the library is now a foundation for GUIs and daemons,
not just the CLI. See `docs/LIBRARY.md`.

### Added
- `ConnectionManager`: GATT connect retries, resume, clock sync,
  session-long keepalive, `ensure()` reconnect, wake hint on `CameraAsleep`.
- `Camera` events (`EVENT_STATE` with a field diff, `EVENT_NOTIFICATION`,
  `EVENT_DISCONNECTED`), `poll()`, `wait_for()`, `start_keepalive()` /
  `stop_keepalive()`, `moments()` returning typed `MomentInfo`.
- Moment metadata: per-moment timestamps and scores from LIST_MOMENTS
  (live-validated), `session_start_time()`.
- `Syncer` with plan/run, progress callbacks, cancellation, custom path
  layout; `Catalog` index of downloads; `WifiJoiner` abstraction with
  `NmcliWifi`, `ManualWifi`, `NullWifi`.
- `openclips.aio`: `AsyncCamera` and `AsyncConnection`.
- `ConnectionLost` detection in the btgatt transport; `UnsafeRequest`,
  `WifiError` exceptions in `openclips.errors`.
- CLI: `watch` command, `capture --wait`, `moments` shows time and score,
  `sessions` shows start time, `--retries`, catalog-aware `sync`.
- Packaging build check, Python 3.14 in CI, coverage, Dependabot, release and
  PyPI publish workflows, pre-commit config, CITATION.cff.

### Changed
- `Camera(..., log=)` removed in favour of stdlib `logging` under `openclips.*`.
- `CameraState.update_from()` returns a dict of changes instead of a bool.
- `parse_list_moments()` takes an optional `session_id` and returns
  `timestamps_ms`, `scores`, `triage` and `moments`.
- `Camera.close()` stops the keepalive thread and clears the session.
- Exceptions moved to `openclips.errors` (still importable from `openclips`).
- Encrypted RPCs complete only on a matching response field **and** sequence
  echo. Missing echoes are rejected; there is no field-only fallback.
  `request()` still returns `None` on timeout. Elapsed waits use a monotonic
  clock and pass remaining budget into nested reads. Wall-clock time remains
  only for `PRIVATE_QUERY` / timestamps. Handshake phases keep separate
  budgets (`pair`: 6 s `PUBLIC_QUERY` then `timeout` for pairing; `resume`:
  `timeout` for ISC and a fresh `timeout` for CSC). Plaintext handshake
  replies still match on response field only.

### Fixed
- A delayed or poll-buffered reply for an older sequence can no longer
  complete a later request of the same type. Pending stray replies are
  bounded and cleared on session reset or close. Discarded-reply diagnostics
  report types and counts, not payloads.
- A field-1 state bundle on the same encrypted frame as a matching RPC
  updates `Camera.state` and emits events; the RPC is still returned, not
  absorbed. `resume()` remains the CSC owner.

## [0.1.0] - 2026-09-20

First public release. Everything below is validated live against a GC-6013
on firmware 1.8 from Linux (pair, status, sessions, complete and a full
`sync` of three moments on 2026-09-20).

### Added
- `openclips` package: protobuf codec, framing, P-256 pairing, HMAC proofs,
  HKDF key schedule, AES-EAX channel, request builders and parsers.
- `Camera` session class: pair, resume, state notifications, heartbeat
  thread, sessions/moments, delete, SoftAP credentials, HTTP media fetch.
- Transports: `BtgattTransport` (Linux, proven) and `BleakTransport`
  (macOS/Windows, untested).
- CLI: `scan`, `pair`, `status`, `sessions`, `moments`, `capture`,
  `complete`, `delete`, `wifi`, `sync`, `forget`, `encode`.
- Pairing store under `$XDG_CONFIG_HOME/openclips/pairings.json`.
- Offline test suite with an in-process fake camera.
- Documentation: protocol specification, hardware notes, reverse
  engineering record, roadmap; authored `.proto` schemas.
