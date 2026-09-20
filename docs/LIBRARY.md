# Building an application on openclips

This is the guide for anyone writing a GUI, a tray sync, a daemon or a
port. It covers the layers, the threading model, events, the connection
lifecycle, downloading, and how to develop without a camera.

## Layers

```
openclips.sync         Syncer: plan + download + catalog + progress
openclips.connection   ConnectionManager: retries, wake, keepalive, reconnect
openclips.camera       Camera: RPCs, state, events, poll/wait_for
openclips.proto        builders, parsers, CameraState, MomentInfo
openclips.crypto       pairing key, proofs, AES-EAX channel
openclips.transport    Transport interface  <- ble_btgatt (Linux), ble_bleak, yours
openclips.wifi         WifiJoiner interface <- wifi_nmcli.NmcliWifi, ManualWifi, NullWifi
openclips.catalog      Catalog: what has been downloaded
openclips.store        PairingStore: credentials on disk
openclips.aio          asyncio facade
```

Everything above `transport` and `wifi` is pure Python with one dependency
(`pycryptodome`). Nothing imports BLE or NetworkManager unless you ask for
those modules.

## Threading model

`Camera` is **blocking and single-caller**. Every method waits for the
camera's reply (up to 20 s for a moment listing). Call it from one worker
thread; never from a GUI thread. The keepalive thread only writes
heartbeats and is designed to run alongside your worker.

Event callbacks run on the thread that was reading the camera at the time,
which is your worker. Marshal to the UI thread yourself (Qt signals, GLib
idle_add, `loop.call_soon_threadsafe`). The asyncio facade does this for
you.

## Connection lifecycle

The camera behaves like this, and `ConnectionManager` encodes it:

* The first GATT connect often fails. The manager retries (default 4).
* Myriad idles after a few minutes without traffic and usually accepts one
  connection per wake. The manager runs heartbeats for the life of the
  connection so the camera stays awake while your app is open.
* When the camera is asleep the ISC request goes unanswered. You get
  `CameraAsleep` with a wake hint. Show it to the user: "press the shutter".
* A factory reset voids the key: `PairingKeyMismatch`. Offer re-pairing.
* The link can drop mid-session: `ConnectionLost`, plus an
  `EVENT_DISCONNECTED` callback. `manager.ensure()` reconnects.
* A fresh camera's clock starts at 2000-01-01. The manager sends a time
  sync after every connect so moment timestamps are real.

```python
from openclips import ConnectionManager, PairingStore, CameraAsleep

mgr = ConnectionManager(address, store=PairingStore())
try:
    cam = mgr.connect()
except CameraAsleep as e:
    show(str(e))  # "...press the camera's shutter button once, then retry"
```

Pairing is the one flow that does not go through the manager, because it
needs a plaintext connection in the setup window. See `cli.cmd_pair` for
the sequence: `Camera.pair()` then `Camera.resume()` on the same transport.

## State and events

`cam.state` is a `CameraState` snapshot: system state (IDLE / SETUP /
CAPTURE), cover, open session, storage, battery, occlusion, Wi-Fi state.
It is filled from the handshake and updated by notifications.

```python
from openclips import EVENT_STATE, EVENT_NOTIFICATION, EVENT_DISCONNECTED

cam.on(EVENT_STATE, lambda state, changes: ...)  # changes = {"cover_open": (False, True)}
cam.on(EVENT_NOTIFICATION, lambda kind, name, fields: ...)
cam.on(EVENT_DISCONNECTED, lambda: ...)
```

Notifications only arrive while something is reading the camera. Either
you are inside a request, or you call `cam.poll(seconds)` from your worker
loop when idle. `cam.wait_for(predicate, timeout)` polls until a state
predicate holds, for example waiting for CAPTURE after the user opens the
cover.

## The capture workflow an app should drive

1. `cam.set_update_required(False)` once per camera (persists).
2. `cam.set_active_user()` on each connect.
3. Tell the user to close and open the lens cover. `wait_for` CAPTURE.
4. Leave the camera alone; do not list the open session (it blocks).
5. When the user closes the cover the state returns to IDLE. Call
   `cam.complete_session()`.
6. Download with `Syncer`.

## Downloading

```python
from openclips import Syncer
from openclips.wifi_nmcli import NmcliWifi  # Linux
from openclips.wifi import ManualWifi  # ask the user elsewhere

syncer = Syncer(cam, out_dir, wifi=NmcliWifi(), on_progress=on_progress)
plan = syncer.plan()  # newest completed session with moments
result = syncer.run(plan)  # SoftAP, join, download, restore Wi-Fi, catalog
```

`on_progress` receives `SyncProgress(stage, item, index, total, bytes,
message, credentials)`; stages are the `STAGE_*` constants in
`openclips.sync`. `syncer.cancel()` is safe from any thread and stops after
the current file. Files land at `<out>/<session_id>/moment_<id>.jpg` by
default; pass `path_for` to change the layout.

`Catalog` (`<out>/.openclips-catalog.json`) records every download with
timestamp, score and size, so the plan skips what you already have and
your gallery can list moments without touching the files.

Moment metadata comes from `cam.moments(session_id)` as `MomentInfo`
objects: `timestamp_ms`, `datetime`, `score` (the camera's ranking, list is
best first). Session ids are nanosecond timestamps of the session start
(`session_start_time`) once the clock is synced.

Thumbnails: the camera refuses `RESOLUTION_THUMB` for fresh moments.
Generate your own from the full JPEG (Pillow's `Image.thumbnail`) in a
background job after `STAGE_SAVED`.

## asyncio

```python
from openclips.aio import AsyncConnection
from openclips import EVENT_STATE

async with AsyncConnection(address, store=PairingStore()) as cam:
    sessions = await cam.list_sessions()
    async for name, (state, changes) in cam.events(EVENT_STATE):
        ...
```

Blocking calls run on a one-thread executor, so they stay serialised;
events are delivered on the loop. Run `await cam.poll(0.5)` periodically
when idle so notifications flow.

## Developing without a camera

`tests/fake_lens.py` is a complete in-process camera: setup-mode pairing,
handshake, encrypted channel, sessions, moments with metadata, Wi-Fi
credentials, notifications (`push_notification`), link drops
(`drop_link`) and reconnection (`reopen`). Point `ConnectionManager` at it
with `transport_factory=lambda addr: lens` and develop the whole UI on a
laptop. Pair it with `NullWifi` and monkeypatch `Camera.http_post` to feed
JPEG bytes.

## Logging

Everything logs under the `openclips` logger hierarchy with stdlib
`logging`. Debug level shows every request and notification.

## Porting the transport

Implement three methods (`write`, `read_indication`, `close`) and set
`alive = False` when your stack reports a disconnect. Single ATT writes at
MTU 512 only. See `examples/custom_transport.py` and `docs/PROTOCOL.md`.
