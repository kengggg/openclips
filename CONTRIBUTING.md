# Contributing

Thanks for helping keep these cameras alive.

## Ground rules

* **No Google material.** No decompiled app code, DEX dumps, embedded
  schema strings, firmware images, model files, or photos from a camera.
  Document observed behaviour instead; that is what `docs/PROTOCOL.md` is.
* **Evidence for protocol changes.** If a PR changes what we send to the
  camera, say how you validated it (firmware version, capture, number of
  runs) and update `docs/PROTOCOL.md` in the same PR.
* **Never commit credentials.** Pairing keys, Wi-Fi passphrases and
  `pairings.json` stay out of the repo. Redact them from logs in issues.
* **Be careful with the hardware.** Some requests wedge the camera until a
  factory reset (see the `UNSAFE_REQUESTS` list in `openclips/constants.py`).
  Do not add new request types to the CLI without a live test.

## Development setup

```
git clone https://github.com/kengggg/openclips
cd openclips
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,ble]'
pre-commit install          # ruff + hygiene hooks on every commit
pytest --cov=openclips
ruff check . && ruff format .
```

CI runs lint, the test matrix (Python 3.10 to 3.14) and a packaging build
on every push and pull request. Releases are cut by pushing a `v*` tag
that matches the version in `pyproject.toml`; the Release workflow builds
the artifacts and drafts the GitHub release from `CHANGELOG.md`.

The test suite runs entirely offline against `tests/fake_lens.py`, an
in-process camera that speaks the real wire format including the
handshake and AES-EAX channel. Add a handler there when you add an RPC.

## Testing against a camera

Nothing in CI touches hardware. Before opening a PR that changes camera
traffic, run at least:

```
openclips -v status
openclips -v sessions
```

and, if the change touches capture or media, the full
`capture` → cover cycle → `complete` → `sync` path. Say which firmware
build the camera reports.

## Style

* Python 3.10+, type hints on public functions, docstrings that state the
  wire shape (`{1: session_id fixed64, ...}`) for every builder.
* `ruff` with the repository config. Line length 120.
* Keep the library free of BLE and OS dependencies; those live behind
  `Transport` and in clearly optional modules.

## Reporting a wedge or a new behaviour

Open an issue with the *Protocol finding* template. Include the request
bytes, the response or the silence, the camera state before and after,
and how you recovered.
