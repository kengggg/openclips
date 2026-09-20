"""``openclips`` command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from . import __version__
from . import constants as C
from . import proto as P
from .camera import EVENT_STATE, Camera
from .connection import ConnectionManager, default_transport_factory
from .crypto import generate_host_key, host_key_from_pem, host_key_to_pem
from .errors import CameraAsleep, CameraError, ConnectionLost, PairingKeyMismatch, WifiError
from .store import Pairing, PairingStore
from .sync import (
    STAGE_DONE,
    STAGE_DOWNLOAD,
    STAGE_FAILED,
    STAGE_LISTING,
    STAGE_PLANNED,
    STAGE_SAVED,
    STAGE_SKIPPED,
    STAGE_WIFI_JOIN,
    STAGE_WIFI_JOINED,
    STAGE_WIFI_LEAVE,
    STAGE_WIFI_REQUEST,
    Syncer,
    SyncProgress,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CAMERA = 3
EXIT_WIFI = 4
EXIT_NOTHING = 5

logger = logging.getLogger("openclips.cli")


def setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger("openclips")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def emit(args, human: str, data: dict | list) -> None:
    if args.json:
        print(json.dumps(data, indent=1, default=str))
    else:
        print(human)


# ----------------------------------------------------------------------------- helpers


def open_transport(args):
    return default_transport_factory(args.transport)(args.address)


def resolve_address(args, store: PairingStore) -> str | None:
    addr = args.address or os.environ.get("OPENCLIPS_ADDRESS") or store.only_address()
    if addr:
        args.address = store.norm(addr)
    return args.address


def connect_paired(args, store: PairingStore, keepalive: bool = True) -> ConnectionManager:
    """Connect and resume the secure session with the stored key."""
    if not resolve_address(args, store):
        raise SystemExit("no camera address: pass --address or pair first")
    pairing = store.get(args.address)
    if pairing is None:
        raise SystemExit(f"{args.address} is not paired. Run: openclips pair {args.address}")
    mgr = ConnectionManager(
        args.address,
        pairing=pairing,
        transport_factory=lambda addr: open_transport(args),
        attempts=args.retries,
        keepalive=keepalive,
    )
    mgr.connect()
    return mgr


def parse_ids(text: str | None) -> list[int]:
    if not text:
        return []
    return [int(x) for x in text.replace(" ", "").split(",") if x]


def fmt_time(m: P.MomentInfo) -> str:
    dt = m.datetime
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


# ----------------------------------------------------------------------------- commands


def cmd_scan(args) -> int:
    from .scan import scan

    hits = scan(args.timeout)
    rows = [
        {
            "address": h.address,
            "name": h.name,
            "rssi": h.rssi,
            "setup_mode": h.setup_mode,
            "manufacturer_data": h.manufacturer_data.hex(),
        }
        for h in hits
    ]
    if not hits:
        emit(args, "no Google Clips cameras seen", rows)
        return EXIT_NOTHING
    lines = [
        f"{r['address']}  rssi={r['rssi']}  {'SETUP MODE' if r['setup_mode'] else 'paired/idle'}  {r['name'] or ''}"
        for r in rows
    ]
    emit(args, "\n".join(lines), rows)
    return EXIT_OK


def cmd_pair(args) -> int:
    store = PairingStore(args.store)
    args.address = store.norm(args.address)
    if store.host_key_pem:
        host_key = host_key_from_pem(store.host_key_pem)
    else:
        host_key = generate_host_key()
        store.host_key_pem = host_key_to_pem(host_key)
    logger.info("connecting to %s (camera must be in setup mode)", args.address)
    last_error = None
    for attempt in range(1, args.retries + 1):
        try:
            transport = open_transport(args)
            break
        except (ConnectionError, OSError) as e:
            last_error = e
            logger.info("GATT attempt %d/%d failed: %s", attempt, args.retries, e)
            time.sleep(1.5)
    else:
        print(f"error: could not connect: {last_error}", file=sys.stderr)
        return EXIT_CAMERA
    with transport:
        cam = Camera(transport, device_id=args.device_id)
        pairing_key, lens_pub = cam.pair(host_key, name=args.name)
        state = cam.resume()
        store.put(
            Pairing(
                address=args.address,
                pairing_key=pairing_key,
                lens_public_key=lens_pub,
                device_id=args.device_id,
                name=args.name,
            )
        )
        store.save()
        cam.time_sync()
        cam.set_active_user()
        if not args.keep_update_required:
            ok = cam.set_update_required(False)
            logger.info("SET_UPDATE_REQUIRED false -> %s", "ok" if ok else "failed")
    emit(
        args,
        f"paired {args.address}; state={state.system_state_name}; saved to {store.path}",
        {"address": args.address, "store": str(store.path), "state": state.as_dict()},
    )
    return EXIT_OK


def cmd_status(args) -> int:
    store = PairingStore(args.store)
    with connect_paired(args, store, keepalive=False) as cam:
        st = cam.state.as_dict()
    human = "\n".join(
        [
            f"camera        {args.address}",
            f"system state  {st['system_state_name']} ({st['system_state']})",
            f"cover open    {st['cover_open']}",
            f"session       {st['session_uuid'] or '-'}  mode={st['session_mode_name']}",
            f"storage       level={st['storage_level']} {st['storage_state_name']}",
            f"battery       {st['battery_pct']}% {st['charge_state_name']}",
            f"occlusion     {st['occlusion_name']}",
        ]
    )
    emit(args, human, st)
    return EXIT_OK


def cmd_sessions(args) -> int:
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:
        s = cam.list_sessions(args.max)
    ids = sorted(s["session_ids"], reverse=True)
    rows = []
    for sid in ids:
        start = P.session_start_time(sid)
        rows.append(
            {
                "session_id": sid,
                "open": sid == s["open_session"],
                "started": start.strftime("%Y-%m-%d %H:%M:%S") if start else None,
            }
        )
    lines = [
        f"{r['session_id']}  {r['started'] or 'clock not synced':<19}{'  (open)' if r['open'] else ''}" for r in rows
    ]
    emit(args, "\n".join(lines) or "no sessions", {"sessions": rows, "open_session": s["open_session"]})
    return EXIT_OK if ids else EXIT_NOTHING


def cmd_moments(args) -> int:
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:
        m = cam.list_moments(args.session, timeout=args.timeout)
    rows = [
        {"moment_id": x.moment_id, "timestamp_ms": x.timestamp_ms, "time": fmt_time(x), "score": x.score}
        for x in m["moments"]
    ]
    lines = [
        f"{r['moment_id']:<6} {r['time']:<19} score={r['score']:.4f}" if r["score"] is not None else f"{r['moment_id']}"
        for r in rows
    ]
    emit(
        args,
        "\n".join(lines) or "no moments",
        {"session_id": args.session, "is_final": m["is_final"], "moments": rows},
    )
    return EXIT_OK if rows else EXIT_NOTHING


def cmd_capture(args) -> int:
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:
        ok = cam.set_update_required(False)
        cam.set_active_user()
        st = cam.state
        if args.wait:
            logger.info("waiting up to %ds for CAPTURE (close then open the lens cover)", args.wait)
            entered = cam.wait_for(lambda s: s.system_state == C.SYSTEM_STATE_CAPTURE, args.wait)
            st = cam.state
            msg = "camera is in CAPTURE" if entered else "camera did not enter CAPTURE"
        else:
            msg = (
                f"update_required=false -> {'ok' if ok else 'FAILED'}; state={st.system_state_name}\n"
                "Close the lens cover, then open it: the camera enters CAPTURE and starts\n"
                "curating moments. Close the cover when done and run: openclips complete"
            )
    emit(args, msg, {"ok": ok, "state": st.as_dict()})
    return EXIT_OK if ok else EXIT_CAMERA


def cmd_complete(args) -> int:
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:
        ok = cam.complete_session()
    emit(args, "session completed" if ok else "FAILED (is the cover closed?)", {"ok": ok})
    return EXIT_OK if ok else EXIT_CAMERA


def cmd_delete(args) -> int:
    ids = parse_ids(args.moments)
    if not ids:
        print("no moment ids", file=sys.stderr)
        return EXIT_USAGE
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:
        ok = cam.move_to_trash(args.session, ids) if args.trash else cam.delete_moments(args.session, ids)
    emit(args, "ok" if ok else "FAILED", {"ok": ok, "session_id": args.session, "moment_ids": ids})
    return EXIT_OK if ok else EXIT_CAMERA


def cmd_wifi(args) -> int:
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:
        creds = cam.initiate_wifi()
        if creds is None:
            print("camera did not return Wi-Fi credentials", file=sys.stderr)
            return EXIT_CAMERA
        data = {"ssid": creds.ssid, "passphrase": creds.passphrase, "url": creds.url}
        emit(args, f"ssid={creds.ssid}\npassphrase={creds.passphrase}\nurl={creds.url}", data)
        if args.hold:
            logger.info("holding the session with heartbeats; Ctrl-C to stop")
            try:
                while True:
                    cam.poll(1.0)
            except KeyboardInterrupt:
                pass
    return EXIT_OK


def cmd_watch(args) -> int:
    """Hold a connection and print state changes as they happen."""
    store = PairingStore(args.store)
    with connect_paired(args, store) as cam:

        def on_state(state, changes):
            for k, (old, new) in changes.items():
                print(f"{time.strftime('%H:%M:%S')}  {k}: {old} -> {new}", flush=True)

        cam.on(EVENT_STATE, on_state)
        print(json.dumps(cam.state.as_dict(), default=str) if args.json else f"state: {cam.state.system_state_name}")
        end = time.time() + args.duration if args.duration else None
        try:
            while end is None or time.time() < end:
                cam.poll(1.0)
        except KeyboardInterrupt:
            pass
    return EXIT_OK


def _progress_printer(verbose: bool):
    def show(p: SyncProgress) -> None:
        if p.stage == STAGE_LISTING and verbose:
            logger.info(p.message)
        elif p.stage == STAGE_PLANNED:
            logger.info(p.message or f"{p.total} to download")
        elif p.stage == STAGE_SKIPPED and verbose:
            logger.info("skip %s (already present)", p.item.path)
        elif p.stage == STAGE_WIFI_REQUEST:
            logger.info("asking the camera to open its SoftAP")
        elif p.stage == STAGE_WIFI_JOIN:
            logger.info("joining %s", p.credentials.ssid)
        elif p.stage == STAGE_WIFI_JOINED:
            logger.info("joined %s", p.credentials.ssid)
        elif p.stage == STAGE_DOWNLOAD:
            logger.info("[%d/%d] moment %d …", p.index, p.total, p.item.moment_id)
        elif p.stage == STAGE_SAVED:
            logger.info("[%d/%d] saved %s (%d bytes)", p.index, p.total, p.item.path, p.bytes)
        elif p.stage == STAGE_FAILED:
            logger.warning("[%d/%d] moment %d failed: %s", p.index, p.total, p.item.moment_id, p.message)
        elif p.stage == STAGE_WIFI_LEAVE:
            logger.info("restoring Wi-Fi")
        elif p.stage == STAGE_DONE and p.total:
            logger.info("done: %d of %d", p.index, p.total)

    return show


def cmd_sync(args) -> int:
    from .wifi_nmcli import NmcliWifi

    store = PairingStore(args.store)
    out_root = os.path.abspath(os.path.expanduser(args.out))
    with connect_paired(args, store) as cam:
        cam.set_active_user()
        syncer = Syncer(
            cam,
            out_root,
            wifi=NmcliWifi(iface=args.iface, restore=not args.keep_wifi),
            on_progress=_progress_printer(args.verbose),
            overwrite=args.overwrite,
        )
        plan = syncer.plan(
            session_id=args.session,
            moment_ids=parse_ids(args.moments) or None,
            all_sessions=args.all_sessions,
        )
        if plan.open_session:
            logger.info(
                "session %s is still open; close the cover and run `openclips complete` to include it",
                plan.open_session,
            )
        if not plan.items:
            emit(args, "nothing new to download", {"downloaded": 0, "skipped": len(plan.skipped), "out": out_root})
            return EXIT_OK if plan.skipped else EXIT_NOTHING
        result = syncer.run(plan)
    data = {
        "downloaded": [str(i.path) for i in result.downloaded],
        "failed": [{"moment_id": i.moment_id, "error": e} for i, e in result.failed],
        "skipped": len(result.skipped),
        "out": out_root,
    }
    emit(args, f"downloaded {len(result.downloaded)} of {plan.total}", data)
    if result.downloaded:
        return EXIT_OK
    return EXIT_WIFI if any("HTTP" not in e for _, e in result.failed) and not result.downloaded else EXIT_NOTHING


def cmd_forget(args) -> int:
    store = PairingStore(args.store)
    if not resolve_address(args, store):
        print("no address given", file=sys.stderr)
        return EXIT_USAGE
    ok = store.remove(args.address)
    store.save()
    emit(args, "removed" if ok else "not found", {"removed": ok, "address": args.address})
    return EXIT_OK if ok else EXIT_NOTHING


def cmd_encode(args) -> int:
    """Print request bodies without a camera. Useful for porting."""
    ids = parse_ids(args.moments)
    data = {
        "list_sessions": P.build_list_sessions(0, 20).hex(),
        "list_moments": P.build_list_moments(args.session).hex(),
        "initiate_wifi_paired": P.build_initiate_wifi_paired().hex(),
        "fetch_moment_full": {str(m): P.build_fetch_moment(args.session, m, C.RESOLUTION_FULL).hex() for m in ids},
    }
    human = "\n".join(
        [f"list_sessions         {data['list_sessions']}", f"list_moments          {data['list_moments']}"]
        + [f"initiate_wifi_paired  {data['initiate_wifi_paired']}"]
        + [f"fetch_moment {m:<9} {h}" for m, h in data["fetch_moment_full"].items()]
    )
    emit(args, human, data)
    return EXIT_OK


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openclips",
        description="Pair with, control and download photos from a Google Clips camera.",
    )
    p.add_argument("--version", action="version", version=f"openclips {__version__}")
    p.add_argument("-a", "--address", help="camera BLE address (default: the single stored pairing)")
    p.add_argument(
        "--transport",
        choices=["btgatt", "bleak"],
        default=os.environ.get("OPENCLIPS_TRANSPORT", "btgatt"),
        help="BLE backend (Linux: btgatt; macOS/Windows: bleak)",
    )
    p.add_argument("--store", help="pairing store path (default: ~/.config/openclips/pairings.json)")
    p.add_argument("--retries", type=int, default=4, help="GATT connect attempts (default 4)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list nearby cameras (needs bleak)")
    s.add_argument("--timeout", type=float, default=8.0)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("pair", help="pair with a camera in setup mode")
    s.add_argument("address", help="camera BLE address")
    s.add_argument("--name", default="openclips", help="host name shown to the camera")
    s.add_argument("--device-id", type=int, default=1)
    s.add_argument("--keep-update-required", action="store_true", help="do not clear the update gate")
    s.set_defaults(func=cmd_pair)

    sub.add_parser("status", help="connect and print camera state").set_defaults(func=cmd_status)

    s = sub.add_parser("sessions", help="list capture sessions")
    s.add_argument("--max", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("moments", help="list moments of a completed session with time and score")
    s.add_argument("session", type=int)
    s.add_argument("--timeout", type=float, default=20.0)
    s.set_defaults(func=cmd_moments)

    s = sub.add_parser("capture", help="arm capture (clear update gate, become active user)")
    s.add_argument("--wait", type=int, default=0, help="seconds to wait for CAPTURE after arming")
    s.set_defaults(func=cmd_capture)

    sub.add_parser("complete", help="complete the current session (cover must be closed)").set_defaults(
        func=cmd_complete
    )

    s = sub.add_parser("delete", help="delete (or trash) moments")
    s.add_argument("session", type=int)
    s.add_argument("moments", help="comma-separated moment ids")
    s.add_argument("--trash", action="store_true", help="move to trash instead of deleting")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("wifi", help="open the camera SoftAP and print credentials")
    s.add_argument("--hold", action="store_true", help="keep the session up until Ctrl-C")
    s.set_defaults(func=cmd_wifi)

    s = sub.add_parser("watch", help="hold a connection and print state changes")
    s.add_argument("--duration", type=float, default=0, help="seconds (default: until Ctrl-C)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("sync", help="download moments as JPEG files (Linux + NetworkManager)")
    s.add_argument("--out", default="./clips", help="output folder (default ./clips)")
    s.add_argument("--session", type=int, help="only this session id")
    s.add_argument("--moments", help="comma-separated moment ids (with --session)")
    s.add_argument("--all-sessions", action="store_true", help="every session with moments")
    s.add_argument("--iface", help="Wi-Fi interface (default: auto-detect)")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--keep-wifi", action="store_true", help="stay on the camera network afterwards")
    s.set_defaults(func=cmd_sync)

    sub.add_parser("forget", help="remove a stored pairing").set_defaults(func=cmd_forget)

    s = sub.add_parser("encode", help="print request bodies (no camera needed)")
    s.add_argument("--session", type=int, required=True)
    s.add_argument("--moments", default="", help="comma-separated moment ids")
    s.set_defaults(func=cmd_encode)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except (PairingKeyMismatch, CameraAsleep, ConnectionLost) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CAMERA
    except WifiError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_WIFI
    except CameraError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CAMERA
    except (ConnectionError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CAMERA
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
