# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
