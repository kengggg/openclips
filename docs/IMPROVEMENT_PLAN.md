# Reliability improvement plan

Status: PR 01 implemented locally; PRs 02–06 have not started. PR numbers
below are local planning identifiers, not GitHub PR numbers. Complete and
review one PR before starting the next.

This plan preserves the current layered library and focuses on reliable camera
requests, recoverable downloads, predictable cleanup, private persistence, and
stable CLI behavior. Findings are based on source and existing test inspection;
they are not claims of reproduced hardware failures. The initial check attempt
could not run because the local Python environment lacked `pytest` and `ruff`.

| Order | Proposed PR title | Priority | Depends on |
| --- | --- | --- | --- |
| PR 01 | Correlate camera replies and enforce elapsed-time deadlines | High | Baseline setup |
| PR 02 | Recover interrupted syncs and report incomplete downloads | High | PR 01 |
| PR 03 | Serialize async lifecycle and release transport resources | High | PR 01; review after PR 02 |
| PR 04 | Restore Wi-Fi reliably and honor keep-network behavior | High | PR 02 and PR 03 |
| PR 05 | Protect and validate local persistence and credential objects | Medium | PR 02 |
| PR 06 | Stabilize CLI contracts and complete regression coverage | Medium | PR 01–05 |

Each PR owns its regression tests and documentation. PR 06 integrates the final
contracts; it is not a reason to defer testing or private-data handling in earlier
PRs. Preserve Python 3.10 support and keep OS/BLE dependencies optional.

**Private-data rules for every PR**

- Work from repository source and synthetic test inputs. Do not read real pairing
  stores, import a user's media/catalog, scan nearby devices, or copy local network
  configuration as part of implementation or automated testing.
- Never include real pairing keys, private keys, Wi-Fi passphrases, camera
  addresses or serials, SSIDs, device names, photos, EXIF, session timestamps, raw
  protocol captures, or personal filesystem paths in commits, PR text, examples,
  screenshots, logs, or CI artifacts. Use labels such as `<camera-address>` and
  generated test values. An encrypted capture is not automatically safe to share.
- Generate cryptographic test keys at runtime. Assert properties or equality
  without printing key material on test failure. Generate tiny synthetic image
  fixtures without EXIF or camera content; document their provenance.
- At each changed logging/error boundary, test that synthetic secret sentinels
  are absent from normal/debug logs, object representations, error output, and
  serialized diagnostics. Sanitization must cover exception chains and subprocess
  command arguments, not only successful output.
- Keep the explicit local `wifi` credential-display command available. Its output
  is intentional and private; never capture it in shared validation evidence.
  Library credential attributes remain usable by Wi-Fi adapters. Ordinary
  progress/result printing must not disclose those attributes accidentally.
- Do not upload full test workspaces, environment dumps, debug traces, pairing
  files, or downloaded media. Any shared evidence must be reconstructed from
  synthetic inputs or reduced to a manually reviewed, redacted summary.
- Before committing or publishing each PR, review the exact staged diff and
  proposed artifacts, run the existing private-key detection hook, and check for
  unexpected binary files and credential-shaped content. Pattern checks supplement
  manual inspection; they do not prove that a document contains no private data.
- If implementation reveals a vulnerability that needs private reporting, follow
  [SECURITY.md](../SECURITY.md). Do not publish exploit details in an ordinary PR
  or issue before that reporting decision is resolved.

**Baseline and completion workflow**

1. Inspect the current working tree and applicable repository instructions before
   each PR. Preserve unrelated changes. Use a dedicated branch when implementation
   begins; this planning change does not open or publish PRs.
2. Set up a project-local development environment using the existing development
   dependencies. Record Python/tool versions and establish the real baseline with
   `pytest --cov=openclips --cov-report=term-missing`, `ruff check .`, and
   `ruff format --check .`. Record failures as failures, not presumed regressions.
3. Reproduce the selected behavior with deterministic offline tests. Prefer fake
   clocks, synchronization events, fake camera replies, and mocked subprocesses
   over wall-clock sleeps or access to real Bluetooth/Wi-Fi.
4. Implement the smallest cohesive change, update the public documentation and
   changelog where behavior changes, and run the focused tests followed by the
   normal suite/lint checks. Let existing CI cover Python 3.10–3.14 and packaging.
5. For changes to camera traffic, follow [CONTRIBUTING.md](../CONTRIBUTING.md):
   update [PROTOCOL.md](PROTOCOL.md), record firmware and sanitized observations,
   and run the applicable live workflow when hardware validation is available.
   Otherwise label it untested and leave the hardware-validation task open. Do
   not infer firmware support from FakeLens or macOS/Windows support from mocks.
6. Prepare a PR description with problem, resulting behavior, compatibility notes,
   exact checks/results, remaining limitations, and private-data review outcome.
   Review and finish this PR before moving to the next one.

**PR 01 — Correlate camera replies and enforce elapsed-time deadlines**

Outcome: a delayed reply cannot satisfy a newer request of the same type, and
system-clock adjustments cannot extend or prematurely end a timeout.

Evidence: `Camera._matches()` currently accepts a matching response field without
checking the sequence echo. `_pending` can retain unrelated responses indefinitely.
Camera polling, handshakes, and heartbeat scheduling use `time.time()` for elapsed
time. The documented response envelope includes a sequence echo.

Primary files: `openclips/camera.py`, `tests/test_camera.py`,
`tests/fake_lens.py`, `docs/PROTOCOL.md`, `docs/LIBRARY.md`.

Tasks:

- [x] 01.1 Extend fake-camera support to delay, omit, duplicate, and interleave
  replies deliberately. Generate fresh valid encrypted frames in actual delivery
  order so a sequence-matching test is not accidentally a crypto-counter test.
  Keep these controls opt-in so existing happy-path tests retain their meaning.
- [x] 01.2 Define matching for ordinary encrypted RPCs: require the expected
  response field and the matching sequence echo. A matching echo with the wrong
  field, or a matching field with a different echo, cannot complete the request.
  Keep unsolicited state notifications and keepalive replies on their own paths.
- [x] 01.3 Define missing-echo handling explicitly. Default to rejecting an
  uncorrelatable RPC reply. Inspect existing protocol evidence before tightening
  any handshake-specific rule; if a firmware-specific exception is necessary,
  scope and document it and test its stale-reply behavior. Do not silently fall
  back to field-only matching for all RPCs.
- [x] 01.4 Expire replies belonging to completed/timed-out requests and bound any
  remaining pending-response buffer. Clear connection-specific buffers on session
  reset/close. Emit only response types/counts for discarded replies, not payloads.
- [x] 01.5 Replace elapsed-time uses inside Camera with monotonic deadlines and
  propagate remaining budgets into reads/polls, including `pair`, `resume`, and
  `wait_for`. Document whether each handshake phase has its own budget. Preserve
  wall-clock use for actual camera clock synchronization and timestamps.
- [x] 01.6 Preserve the public single-caller model and existing `request()` timeout
  return contract in this PR. Keep encryption/write serialization with heartbeats
  intact. Document correlation and deadline behavior; record any firmware caveats.

Validation:

- [x] An old response for request A arrives while same-type request B waits; B
  completes only on its own reply. Cover a stale response already buffered by poll.
- [x] Wrong-field, wrong-sequence, absent-echo, duplicate, and malformed replies
  cannot return false success. Notifications bundled with replies still update state.
- [x] Notification/keepalive interleaving does not lose the valid response or
  disturb secure-channel counters. Buffer limits are exercised under a stray flood.
- [x] Forward/backward wall-clock changes do not affect elapsed deadlines; zero
  and near-expired budgets do not start another full-duration read.
- [x] Existing pairing, resume, notification, and concurrent-heartbeat tests pass.
- [ ] Hardware check: status, sessions, repeated same-type reads, and notification
  processing on the recorded firmware; report observations without raw captures.
  Untested here (no camera present). Do not infer firmware support from FakeLens.

Completion: no tested stale reply is accepted, waits obey their documented
budgets, and protocol compatibility is documented. Rollback: revert code/tests
together; there is no persisted-data migration.

**PR 02 — Recover interrupted syncs and report incomplete downloads**

Outcome: only validated completed downloads are skipped, successfully saved files
remain recoverable after interruption, and incomplete work is visible to callers.

Evidence: `_have()` trusts path existence; `extract_jpeg()` finds only a starting
marker; `plan()` logs and suppresses listing errors; filesystem errors escape the
per-item failure handling; catalog persistence occurs after the whole run. The CLI
also returns success if any file was downloaded, even when other files failed.

Primary files: `openclips/sync.py`, `openclips/catalog.py`,
`openclips/camera.py`, `openclips/errors.py`, `openclips/cli.py`,
`tests/test_sync.py`, `tests/test_catalog.py`, `tests/test_cli.py`.

Tasks:

- [x] 02.1 Add explicit planning failures and run-level failures/cleanup warnings
  to sync results, while retaining existing fields where practical. Distinguish
  an empty session, an already-downloaded session, an unreadable session, a failed
  transfer, a failed save, and a failed catalog update. Define `ok` across all
  failure categories. Do not turn total listing failure into "nothing new".
- [x] 02.2 Add bounded structural JPEG validation before committing a file:
  validate segment lengths, required image/scan structure, and a complete end
  marker, with support for the observed response wrapper. Keep `extract_jpeg()`
  as the extraction helper if needed for compatibility. State clearly that
  structural checks do not prove every pixel is decodable; do not claim that a
  start/end-marker check detects arbitrary corruption. Avoid a new runtime image
  dependency in this PR. Replace current marker-only fake downloads with valid
  synthetic fixtures and add deliberately damaged variants.
- [x] 02.3 For existing files, require a regular, structurally valid image and
  compare recorded size when available. A missing/truncated file becomes planned
  work. Honor the requested target path and resolution rather than allowing an
  unrelated catalog entry to suppress a requested output. Reconcile valid orphan
  files into the catalog. Document any schema additions and read old entries.
- [x] 02.4 Keep same-directory atomic replacement, use uniquely created temporary
  files, and remove owned partial files on failure. Preserve a previously valid
  destination when overwrite/download fails. Convert expected filesystem failures
  into explicit results; stop on systemic failures such as a full output disk.
- [x] 02.5 Checkpoint catalog updates after each saved file for the current small
  workload. Handle the crash window between image replacement and catalog save
  by reconciling on the next plan. If checkpointing fails, retain the image and
  report the run incomplete. File-save events must distinguish image saved from
  metadata persisted. Defer the shared persistence helper to PR 05.
- [x] 02.6 Preserve structured HTTP failure information, including status where
  available, without retaining arbitrary response bodies in diagnostics. Add a
  small configurable retry limit/backoff for explicitly transient read failures.
  Do not retry permanent errors or known camera "moment unavailable" responses
  just because they use HTTP 500. Never apply transfer retries to destructive RPCs.
- [x] 02.7 Check cancellation between planning calls, retries, and files. Define
  cancellation lifetime explicitly so `run()` does not silently discard a cancel
  issued after planning. Keep the documented current-file boundary; do not claim
  immediate interruption of blocking HTTP. Avoid starting Wi-Fi for cancelled work.
- [x] 02.8 Make cleanup execute after every successful SoftAP acquisition, including
  early preview/setup failure. Preserve primary and cleanup errors separately.
  PR 04 will provide concrete NetworkManager restoration behavior.
- [x] 02.9 Update CLI sync immediately to return nonzero for partial/failed results
  and represent failures without inspecting error strings. PR 06 documents and
  normalizes the full CLI schema. Update sync examples and recovery documentation.

Validation:

- [x] Empty, truncated, incorrectly wrapped, structurally malformed, and valid
  synthetic images produce the expected saved/failed outcomes.
- [x] Missing, wrong-sized, wrong-resolution, changed-layout, and orphan files
  are handled correctly; successful reruns download only required work.
- [x] Inject failure at write, replace, catalog save, and cleanup boundaries.
  Previous valid destinations survive, partial files are handled, and a new
  Syncer process can recover saved files after interruption.
- [x] Failed session enumeration remains visible even if another session succeeds;
  all-listing-failed and partially-downloaded runs return nonzero in the CLI.
- [x] Transient retries stop at the configured budget; permanent errors are not
  retried; cancellation interrupts backoff and prevents new transfers.
- [x] Progress order and result totals agree, including cancellation and no-op runs.
- [ ] Hardware check: capture/cover cycle/complete/sync, rerun, and interrupted
  download recovery. Keep all resulting camera photos outside repository evidence.
  Untested here (no camera present).

Completion: completed files and catalog state reconcile after tested failures,
and partial work cannot report full success. Compatibility: document stronger
validation, new result fields, and the corrected CLI exit behavior. Rollback must
retain downloaded images and any backward-readable catalog metadata.

**PR 03 — Serialize async lifecycle and release transport resources**

Outcome: close/cancellation cannot race normal camera work, disconnected transports
report their state, and failed setup does not leave threads, scanners, or processes.

Evidence: AsyncConnection closes the manager on the default executor while
AsyncCamera uses a separate executor; cancelled executor work may still run.
BleakTransport does not track disconnection or clean up failed construction.
ConnectionManager only cleans up selected exception types. Btgatt close can block
in `waitpid`, and its default process wrapper ends a session after a fixed duration.
Scan lacks a cancellation-safe stop. Async event queues/history are unbounded and
state-event arguments refer to a mutable CameraState.

Primary files: `openclips/aio.py`, `openclips/connection.py`,
`openclips/camera.py`, `openclips/transport.py`, `openclips/ble_bleak.py`,
`openclips/ble_btgatt.py`, `openclips/scan.py`, `tests/test_aio.py`,
`tests/test_connection.py`; add focused mocked adapter/scanner tests.

Tasks:

- [ ] 03.1 Give AsyncConnection one owned single-worker execution path for connect,
  camera calls, and manager close; let its AsyncCamera share that executor. Keep
  standalone AsyncCamera ownership explicit. Make repeated connect return the
  same live wrapper, and reject work once shutdown has started.
- [ ] 03.2 Define cancellation as cancellation of the await, not proof that a
  blocking operation stopped. Retain ownership of in-flight connect/call futures,
  arrange cleanup when they complete, and shield necessary cleanup from repeated
  cancellation. Close must wait/serialize safely without blocking the event loop.
  Give shutdown a documented bound based on underlying operation timeouts.
- [ ] 03.3 Make manager/camera/adapter close idempotent, including partial setup.
  Release acquired resources on all setup failures while preserving the original
  error. Do not drop references to a still-running keepalive worker after a timed
  join; avoid joining a worker from its own disconnect callback.
- [ ] 03.4 Track Bleak disconnects through the adapter callback and set `alive`
  consistently. On failed connect/subscribe/write timeout, cancel and settle
  pending work, disconnect where possible, stop/join the loop thread, and close
  the loop. Map adapter errors to the documented transport/connection contract.
- [ ] 03.5 Bound Btgatt process teardown with terminate/wait/kill/reap behavior
  and guarantee descriptor cleanup. Use monotonic transport deadlines and the
  configured MTU in write-size checks. Make the existing fixed session lifetime
  an explicit option rather than silently breaking long-running connections;
  validate the new default on Linux before claiming long-lived support.
- [ ] 03.6 Put scanner stop in cancellation/failure cleanup after successful start.
  Test with a fake scanner; do not start a real BLE scan during automated checks.
- [ ] 03.7 Bound async event delivery and raw notification history. Copy state and
  mutable event data at emission so queued events retain their historical meaning.
  Document a fixed queue limit and overflow policy: end an overloaded subscription
  with an explicit overflow error rather than silently discard arbitrary events.
  Ensure close/disconnect wakes event consumers and unregisters handlers, including
  full-queue and event-loop-shutdown cases. Preserve the synchronous callback API.
- [ ] 03.8 Document executor ownership, cancellation limits, closing, event
  snapshots, and overflow. Add transport contract tests reusable across fake adapters.

Validation:

- [ ] Synchronization barriers prove close never overlaps an active camera call;
  queued calls are completed or rejected according to the documented policy.
- [ ] Cancellation during connect, RPC, and shutdown eventually releases resources;
  failed `async with` entry does not orphan a later successful connection.
- [ ] Repeated close/connect and failed handshake/notification subscription leave
  no owned live thread, unclosed loop, child process, scanner, or descriptor.
- [ ] Disconnect marks the camera unavailable and emits one disconnect event;
  reconnect works. Shutdown from a callback does not deadlock.
- [ ] Slow consumers hit a bounded overflow outcome, historical state is stable,
  and iterators terminate/unsubscribe on close without unhandled callback errors.
- [ ] Mocked Btgatt ignores termination until escalation and is still reaped;
  adapter deadlines remain bounded under simulated clock changes.
- [ ] Hardware check: long-running Linux session and reconnect. Keep portable
  adapter hardware support explicitly unvalidated until each OS has been tested.

Completion: lifecycle tests establish ownership and eventual cleanup for every
acquisition path. Rollback: no data migration; document event semantics changes
for downstream async consumers. No Linux Bleak GATT hardware tests are permitted
by the project's documented camera compatibility constraints.

**PR 04 — Restore Wi-Fi reliably and honor keep-network behavior**

Outcome: sync cleanup attempts to restore the exact prior connection even when
another cleanup action fails, and keep-network behavior matches its name.

Evidence: `NmcliWifi.leave()` can skip restore when deletion raises; profile
deletion uses the SSID rather than an owned profile identity. Subprocess errors can
escape as raw exceptions. `--keep-wifi` only disables restoration while Syncer
still leaves the network and requests camera Wi-Fi cancellation.

Primary files: `openclips/wifi_nmcli.py`, `openclips/wifi.py`,
`openclips/sync.py`, `openclips/cli.py`, `openclips/errors.py`;
add `tests/test_wifi_nmcli.py`, extend sync/CLI tests and library documentation.

Tasks:

- [ ] 04.1 Make nmcli execution return structured success/failure information and
  map missing executable, timeout, and nonzero exit to safe WifiError messages.
  Never stringify a subprocess exception containing a password-bearing command,
  or print unfiltered stdout/stderr. Keep diagnostic stage/code information.
- [ ] 04.2 Identify the prior connection by UUID and interface. Parse escaped
  nmcli fields correctly and test names/SSIDs containing separators or backslashes.
  Give created camera profiles unique identities and track ownership; never delete
  a pre-existing unrelated profile merely because its name matches the SSID.
- [ ] 04.3 Make join/leave state transitions explicit. Remember acquired resources
  as each step succeeds, handle partial join, and make repeated leave harmless.
  Ensure both deletion and restoration are attempted independently, with each
  failure reported. Retain enough state for a retry after partial cleanup failure.
- [ ] 04.4 Use monotonic scan/join deadlines, cap each subprocess wait by remaining
  budget, and prevent retry sleeps from extending the configured total deadline.
  Apply restrictive permissions to any temporary credential-bearing file and use
  the same private-data assertions for all timeout and retry paths.
- [ ] 04.5 Define a shared cleanup policy between Syncer and the Wi-Fi adapter.
  Default: leave owned camera connection, restore prior network, cancel SoftAP.
  On completed success with `--keep-wifi`: retain the camera connection/profile,
  skip prior-network restore, and do not send CANCEL_WIFI. On cancellation or
  failure: perform normal restoration, documenting this exception to retention.
- [ ] 04.6 Explain that retention is best effort: the command closing its BLE
  session and camera firmware timeouts can still end the SoftAP. Do not imply
  indefinite Wi-Fi availability. Give the retained profile an explicit ownership
  and subsequent-reuse/cleanup policy so repeated runs do not accumulate profiles.
- [ ] 04.7 Surface cleanup failures in PR 02's result contract without hiding
  completed downloads or masking the primary error. Update help and documentation.

Validation:

- [ ] Mock all subprocess calls. Cover no interface, no prior network, SSID absent,
  failed scan, join timeout, retry exhaustion, and multiple-interface selection.
- [ ] Failure deleting an owned profile still attempts restore; failure restoring
  is visible; repeated cleanup cannot delete somebody else's connection.
- [ ] Escaped names, duplicate profile names, existing camera profiles, and UUID
  restoration behave correctly. Retained profiles can be reused or cleaned later.
- [ ] Test the cleanup-policy matrix for success, partial failure, cancellation,
  and join failure with both default and keep-network settings.
- [ ] Synthetic passphrases never appear in logs, errors, or exception chains.
- [ ] Hardware check: join/download/restore and keep-network behavior on a known
  Linux setup. Report only sanitized outcomes, not nmcli dumps or network names.

Completion: tests verify each policy branch and restoration failures are visible.
Compatibility: document the corrected `--keep-wifi` semantics and any additive
adapter policy options; preserve existing custom WifiJoiner implementations where
possible. Rollback includes handling profiles created by the new ownership scheme.

**PR 05 — Protect and validate local persistence and credential objects**

Outcome: credential files are private from creation, malformed persisted data
produces actionable errors, and concurrent processes cannot silently lose updates.

Evidence: PairingStore writes the temporary file before chmod; both stores use
predictable temporary names; loaded JSON is only lightly checked. Generated
dataclass representations can include pairing keys and Wi-Fi passphrases, including
through nested sync progress/results. Catalog keys assume session IDs are globally
unique, although camera clocks may be unsynchronized.

Primary files: `openclips/store.py`, `openclips/catalog.py`,
`openclips/wifi.py`, `openclips/errors.py`, `openclips/sync.py`;
add a small internal persistence helper if useful; extend store/catalog/sync tests.

Tasks:

- [ ] 05.1 Create a shared atomic JSON-write helper using a unique exclusively
  created temporary file in the destination directory. Credential files must be
  mode 0600 before any contents are written; use private permissions for catalogs
  too because they contain photo metadata. Flush/fsync the file before replacement,
  fsync the directory where supported, and always clean up owned temporary files.
  Do not chmod unrelated existing parent directories.
- [ ] 05.2 Define permission behavior on supported platforms and detect unsafe
  existing store permissions. Report remediation without reading credentials into
  logs. Do not claim POSIX mode bits enforce Windows ACL privacy; document/test
  platform limitations and require an appropriate ACL implementation before making
  that claim for a Windows release.
- [ ] 05.3 Validate JSON root/container types, schema versions, identifier types,
  credential encodings/lengths, and catalog entry fields. Introduce clear storage
  errors that expose neither file contents nor raw decoder snippets. A malformed
  pairing store must not be silently replaced with an empty store. Preserve an
  invalid catalog and offer an explicit rebuild/recovery path rather than destroy it.
- [ ] 05.4 Read current unversioned pairing files and version-1 catalogs. Add
  versioning/migrations only where required by PR 02 metadata or validation;
  reject unsupported future versions without rewriting them. Test migration from
  synthetic legacy files and document downgrade behavior before release.
- [ ] 05.5 Define and enforce a persistence concurrency contract. Serialize saves
  with an interprocess lock and compare the on-disk revision with the loaded
  revision while holding it. Reject stale writers with a clear reload/retry error;
  do not silently merge credentials or overwrite concurrent changes. Unique temp
  files alone do not solve lost updates. Cover lock timeout and process-exit cleanup.
- [ ] 05.6 Detect overlapping syncs into the same output/catalog and prevent
  concurrent writers from racing file replacement. Record camera provenance when
  available and reject known camera/session collisions. Document remaining legacy
  ambiguity; defer a camera-namespaced directory/schema migration to a separate
  feature unless implementation proves it necessary for these guarantees.
- [ ] 05.7 Exclude pairing/private material and passphrases from repr, including
  nested credential-bearing objects. Provide explicit safe diagnostic summaries
  rather than generic dataclass-to-dict dumps. Raw notification and response fields
  are not diagnostic summaries. Preserve explicit programmatic credential access.
- [ ] 05.8 Adopt the helper in catalog checkpointing and PairingStore, update
  credential-handling documentation/examples, and keep recovery instructions local
  without asking users to upload stores or catalogs.

Validation:

- [ ] Observe permissions at temporary-file creation and during writing, including
  a permissive umask; check successful destination permissions on POSIX.
- [ ] Simulated serialization, disk-full, flush/fsync, replace, and interruption
  failures retain the last usable destination and clean up owned temporary files.
  Distinguish failures before replacement from uncertain durability after replacement.
- [ ] Malformed JSON, wrong field types, invalid key encodings/lengths, unknown
  versions, and legacy valid files produce the specified outcomes without payloads
  in errors. Corrupt data is preserved for local recovery.
- [ ] Two independent processes cannot silently overwrite each other's updates;
  lock timeout, stale revisions, process death, and overlapping sync are exercised.
- [ ] repr/log/serialization tests with synthetic sentinels cover Pairing,
  WifiCredentials, SyncProgress, SyncResult, and nested errors. Intentional local
  credential access still works and is not included in shared test artifacts.

Completion: atomicity, permissions, validation, and concurrency guarantees are
tested and documented. Rollback must preserve stores and downloaded files; any
schema change requires an explicit supported downgrade or export procedure before
merge. Do not rotate keys or modify a real pairing store during automated tests.

**PR 06 — Stabilize CLI contracts and complete regression coverage**

Outcome: scripts get consistent output and exit statuses, users get actionable
errors, and repository support claims match tested behavior.

Evidence: `watch --json` starts with JSON but later prints human event lines;
moment-list parsing can raise ValueError outside argparse; sync output changes
the type of `downloaded` between no-op and download runs. Some errors are detected
by string inspection. The CLI exposes `delete --trash` although the roadmap says
trash has not been tested live. CI does not currently exercise real OS adapters.

Primary files: `openclips/cli.py`, `openclips/errors.py`,
`tests/test_cli.py`, adapter/scanner tests introduced above,
`.github/workflows/ci.yml`, `.pre-commit-config.yaml`,
`README.md`, `docs/LIBRARY.md`, `docs/ROADMAP.md`, `CHANGELOG.md`.

Tasks:

- [ ] 06.1 Specify a JSON contract for every command and normalized errors. Keep
  single-result commands as one JSON document and define watch as one JSON object
  per line, including initial state and later events. Keep human progress on stderr.
  Preserve documented fields where possible; use a stable list type for downloaded
  items and additive count/error fields. Document unavoidable shape corrections.
- [ ] 06.2 Publish an exit-code table: success, usage error, camera failure, Wi-Fi
  failure, nothing found, partial sync, local storage failure, and interruption.
  Preserve existing codes where meaningful and add distinct codes where needed.
  Use structured failures from earlier PRs; never infer categories from message text.
  Choose and test deterministic precedence when more than one category occurs.
- [ ] 06.3 Move moment-ID, timeout, retry-count, and dependent-option validation
  into argparse before creating a store/connection. Validate `--moments` requires
  `--session`; reject malformed/out-of-range values using wire-contract limits.
  Keep meaningful zero values, such as unlimited watch duration, where documented.
- [ ] 06.4 Normalize expected optional-dependency, adapter, storage, and timeout
  failures into actionable CLI errors without raw tracebacks or secret-bearing
  exception strings. Make `capture --wait` return nonzero when the requested
  CAPTURE state is not reached even if arming succeeded. Define JSON errors for
  parse failures as well as command failures and test both.
- [ ] 06.5 Reconcile unsupported actions. Until live evidence exists, make
  `delete --trash` fail before connecting with a clear unsupported/experimental
  explanation; keep the documented builder/library status explicit. Do not send
  unvalidated requests merely to test a CLI flag. Align roadmap, help, and library
  claims with the actual per-platform/per-firmware validation record.
- [ ] 06.6 Audit changed command/progress/debug paths for raw protocol bytes,
  network credentials, private identifiers, metadata, and personal paths. Use
  explicit allowlisted diagnostic fields and document redacted bug-report examples.
  Keep user-requested local operational output distinct from shareable diagnostics.
- [ ] 06.7 Consolidate regression coverage from PR 01–05, including mocked scan
  start/stop and adapter exception mapping. Keep real hardware out of CI. Maintain
  the Python matrix and wheel smoke test; add a clean-wheel core-import smoke test
  proving optional BLE/OS dependencies are not imported by the base library.
- [ ] 06.8 Establish measured coverage after dependencies are available and use a
  justified floor or targeted branch expectations for critical changed code; do
  not invent a baseline or require 100% coverage. Limit uploaded artifacts to the
  intended package and sanitized reports. Ensure pre-commit/private-key checks
  also run in CI where their dependencies are available.
- [ ] 06.9 Update command examples, release notes, and this plan's completion
  status. Record each PR's actual validation and unresolved hardware limitations.
  Cross-platform Wi-Fi, GUI, thumbnails, new RPCs, parser rewrites, and broad
  performance work remain separate follow-up projects.

Validation:

- [ ] Parse stdout as JSON/JSON Lines for every JSON-mode success and expected
  failure, including watch events, partial sync, cancellation, and no-op sync.
- [ ] Verify exit-code precedence, stable field types, stderr behavior, and no
  traceback for expected errors. Capture-wait timeout must not return success.
- [ ] Invalid options and unsupported trash mode fail before transport/store
  side effects. Missing optional packages provide an actionable installation hint.
- [ ] Use synthetic secrets and identifiers to verify diagnostic redaction; verify
  the intentional local Wi-Fi credential display separately without shared output.
- [ ] Full suite, lint, formatting, package metadata check, clean-wheel install,
  core import, CLI help/version, and offline encode smoke tests pass in CI.
- [ ] Publish only a sanitized validation summary, listing hardware checks actually
  performed and explicitly distinguishing mocked portable-adapter coverage.

Completion: public contracts are documented and tested, privacy assertions cover
the changed paths, and the six PRs form a usable release candidate. Release/tag
publication is a separate action. Rollback: retain prior CLI compatibility notes
and do not roll back data-format support beneath files already written by PR 05.

**Execution record**

Update this table as work is completed; never mark a task done based on a plan.

| PR | Status | Actual PR/commit | Validation and remaining limitations |
| --- | --- | --- | --- |
| 01 | GitHub PR open (not merged) | [#4](https://github.com/kengggg/openclips/pull/4) `pr-01-correlate-replies-deadlines` | Offline pytest against FakeLens, including correlation, pending bound, monotonic budgets, pairing/resume/notification/heartbeat, bundled field-1 state. `ruff check` / `ruff format --check` clean. Hardware status/sessions/repeated reads: **untested** (no camera). Missing-echo CSC on firmware 1.8 untested; plaintext handshake still field-only as documented. GitHub PRs #1–#3 are Dependabot Actions bumps, unrelated. |
| 02 | GitHub PR open (not merged) | [#5](https://github.com/kengggg/openclips/pull/5) `pr-02-sync-recovery` | Offline pytest (JPEG fixtures, listing errors, retries, cancel-after-plan, CLI partial exit). Hardware capture/sync recovery: **untested** (no camera). |
| 03 | Planned | — | Baseline and implementation pending |
| 04 | Planned | — | Baseline and implementation pending |
| 05 | Planned | — | Baseline and implementation pending |
| 06 | Planned | — | Baseline and implementation pending |
