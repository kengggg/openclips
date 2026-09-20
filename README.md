# openclips

[![CI](https://github.com/kengggg/openclips/actions/workflows/ci.yml/badge.svg)](https://github.com/kengggg/openclips/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Open-source host client for the **Google Clips** camera (GC-6013, 2018).
Google shut the companion app down; this project lets you keep using the
camera from a Linux machine, and documents the protocol so anyone can port
it elsewhere.

```
$ openclips pair AA:BB:CC:DD:EE:FF        # once, in the camera's setup window
$ openclips capture                       # clear the update gate; close+open the lens cover
$ openclips complete                      # after closing the cover
$ openclips sync --out ~/Pictures/clips   # BLE → SoftAP → full-resolution JPEGs
```

Everything the camera does on its own still works: it curates "moments"
while the cover is open, and `openclips` pulls them down. No cloud, no
account, no Google services.

> **Status:** alpha. The full path (pair, capture, list, Wi-Fi, download)
> is validated live on firmware 1.8 from Linux. macOS and Windows have a
> transport but nobody has tested it against a camera yet.

## What you need

* A Google Clips camera you own, ideally on firmware 1.8.
* Linux with BlueZ and `btgatt-client` (package `bluez-utils` on Arch,
  `bluez` or `bluez-tools` elsewhere), a Bluetooth 4+ adapter and Wi-Fi.
  NetworkManager is used by `openclips sync` to join the camera's network.
* Python 3.10 or newer.

## Install

Until the first PyPI release, install from a checkout:

```
git clone https://github.com/kengggg/openclips
cd openclips
pip install -e .            # library + CLI
pip install -e '.[ble]'     # adds bleak for `openclips scan` and non-Linux BLE
```

## Quick start

1. **Factory-reset the camera** if it was paired to a phone: hold the
   bottom pinhole for 15 s until the LED goes amber, release, wait for the
   two white LEDs to alternate. You now have 1 to 2 minutes.
2. **Find the address**: `openclips scan` (needs `bleak`), or read it from
   `bluetoothctl scan on`.
3. **Pair**: `openclips pair AA:BB:CC:DD:EE:FF`. The key is stored in
   `~/.config/openclips/pairings.json` with mode 0600.
4. **Capture**: `openclips capture`, then close and open the lens cover.
   The camera enters CAPTURE and starts picking moments. Leave it for a
   while pointed at people or pets.
5. **Finish**: close the cover, run `openclips complete`.
6. **Download**: `openclips sync --out ~/Pictures/clips`. Files land in
   `<out>/<session_id>/moment_<id>.jpg`.

Other commands: `status`, `sessions`, `moments <session>` (with time and
score), `watch` (print state changes live), `wifi --hold` (open the SoftAP
and print credentials for manual use), `delete`, `forget`, `encode` (print
request bytes without a camera). Add `--json` for
machine-readable output and `-v` for a log of every step.

If the camera does not answer, press the shutter button once. Myriad
sleeps after a few idle minutes even though it keeps advertising, and it
usually accepts only one connection per wake, so expect to press the
shutter before each command.

## Library

```python
from openclips import ConnectionManager, PairingStore, Syncer, EVENT_STATE
from openclips.wifi_nmcli import NmcliWifi

with ConnectionManager("AA:BB:CC:DD:EE:FF", store=PairingStore()) as cam:
    cam.on(EVENT_STATE, lambda state, changes: print(changes))
    for m in cam.moments(max(cam.list_sessions()["session_ids"])):
        print(m.moment_id, m.datetime, m.score)
    Syncer(cam, "~/Pictures/clips", wifi=NmcliWifi(), on_progress=print).sync()
```

`ConnectionManager` handles the camera's quirks (retries, sleep, keepalive,
clock sync), `Camera` exposes every validated RPC plus state events, and
`Syncer` downloads with progress, cancellation and a local catalog. There
is an asyncio facade in `openclips.aio`. The library has one runtime
dependency (`pycryptodome`); BLE sits behind a three-method `Transport`
interface and Wi-Fi behind `WifiJoiner`, so ports are small. A complete
in-process fake camera under `tests/` lets you build a UI with no
hardware. Read [`docs/LIBRARY.md`](docs/LIBRARY.md) before building on it.

**Linux warning:** do not use `bleak` or any BlueZ D-Bus GATT writer against
this camera. Its BLE sidecar wedges and only a factory reset recovers.
`BtgattTransport` is the proven path.

## Documentation

| Document | Contents |
|---|---|
| [`docs/LIBRARY.md`](docs/LIBRARY.md) | Building an app: layers, threading, events, connection lifecycle, sync |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | Wire protocol, crypto, capture lifecycle, HTTP API. Port from this. |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | Buttons, LEDs, reset procedures, what wedges the camera |
| [`docs/REVERSE-ENGINEERING.md`](docs/REVERSE-ENGINEERING.md) | How the protocol was recovered, tools, dead ends |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | What works, what is next |
| [`proto/`](proto/) | Protocol schemas written from observation |
| [`research/`](research/) | Schema-extraction and emulation tooling used during the work |

## Contributing

Bug reports with `-v` logs, protocol findings, and ports to other BLE
stacks are all welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md). The one
hard rule: nothing derived from Google's app, firmware or models goes in
this repository. See [`SECURITY.md`](SECURITY.md) for how to handle
pairing keys.

## Legal

openclips is an independent project and is not affiliated with, endorsed
by or supported by Google. "Google Clips" is a trademark of Google LLC and
is used here only to identify the hardware this software talks to. The
protocol was recovered by observing a camera the author owns, for the
purpose of interoperability. Use it with cameras you own.

MIT licensed. See [`LICENSE`](LICENSE).
