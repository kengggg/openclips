# Roadmap

Where the project stands and what would make it more useful. Issues are
the place to claim an item.

The [reliability improvement plan](IMPROVEMENT_PLAN.md) breaks the next code
improvements into six sequential PRs, with implementation tasks, validation,
compatibility notes, and private-data handling requirements for each.

## Works today (live-validated)

- Pair in setup mode, store the key, resume later without a reset
- Read camera state (system state, cover, battery, storage, session)
- Arm capture (`openclips capture`) and complete a session
- List sessions and moments, delete moments
- Open the SoftAP and download full-resolution JPEGs (`openclips sync`)

## Near term

- **Validate `BleakTransport` on macOS and Windows.** The wedge is a
  BlueZ artefact; native stacks should work but nobody has tried.
- **Thumbnails.** `RESOLUTION_THUMB` answers 500 on freshly captured
  moments. Find when the camera generates them, or decode
  `/preview_header` and `/fetch_frame` for a cheap preview.
- **Moment metadata over HTTP.** Timestamps and scores are parsed from
  `LIST_MOMENTS`; `/fetch_moment_metadata` likely returns more and is not
  decoded yet.
- **Trash and restore.** Builders exist; neither has been sent live. The
  CLI `delete --trash` flag fails before connecting until that changes.
- **Longer SoftAP life.** Work out what the Android app does to keep the
  Wi-Fi Direct group owner alive past the first handful of requests.
- **Wake without touching the camera.** Nothing over BLE has woken an idle
  Myriad so far. Worth a systematic sweep of harmless requests.

## Later

- A small GUI or system tray sync for Linux desktops, on top of
  `ConnectionManager`, `Syncer` and the event API (see `docs/LIBRARY.md`)
- A cross-platform `sync` that joins Wi-Fi via the OS APIs
- Firmware preservation notes: model formats in the update package, whether
  the AI pipeline can run outside the camera

## Out of scope

- Anything that requires redistributing Google's app, firmware or models
- Cloud upload, the original companion features that depended on Google
  services
