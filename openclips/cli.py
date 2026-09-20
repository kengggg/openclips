"""``openclips`` command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from . import constants as C
from . import proto as P
from .camera import Camera, CameraAsleep, CameraError, PairingKeyMismatch, extract_jpeg
from .crypto import generate_host_key, host_key_from_pem, host_key_to_pem
from .store import Pairing, PairingStore

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CAMERA = 3
EXIT_WIFI = 4
EXIT_NOTHING = 5


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def emit(args, human: str, data: dict | list) -> None:
    if args.json:
        print(json.dumps(data, indent=1, default=str))
    else:
        print(human)


# ----------------------------------------------------------------------------- helpers


def open_transport(args):
    if args.transport == "bleak":
        from .ble_bleak import BleakTransport

        return BleakTransport(args.address)
    from .ble_btgatt import BtgattTransport

    return BtgattTransport(args.address)


def resolve_address(args, store: PairingStore) -> str | None:
    addr = args.address or os.environ.get("OPENCLIPS_ADDRESS") or store.only_address()
    if addr:
        args.address = store.norm(addr)
    return args.address


def connect_paired(args, store: PairingStore) -> Camera:
    """Open the transport and resume the secure session with the stored key."""
    if not resolve_address(args, store):
        raise SystemExit("no camera address: pass --address or pair first")
    pairing = store.get(args.address)
    if pairing is None:
        raise SystemExit(f"{args.address} is not paired. Run: openclips pair {args.address}")
    t = open_transport(args)
    cam = Camera(t, pairing.pairing_key, pairing.device_id, log=log if args.verbose else None)
    try:
        cam.resume()
    except Exception:
        t.close()
        raise
    return cam


def parse_ids(text: str | None) -> list[int]:
    if not text:
        return []
    return [int(x) for x in text.replace(" ", "").split(",") if x]


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
    log(f"connecting to {args.address} (camera must be in setup mode)")
    with open_transport(args) as t:
        cam = Camera(t, device_id=args.device_id, log=log)
        try:
            pairing_key, lens_pub = cam.pair(host_key, name=args.name)
            state = cam.resume()
        except CameraAsleep as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_CAMERA
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
        cam.set_active_user()
        if not args.keep_update_required:
            ok = cam.set_update_required(False)
            log(f"SET_UPDATE_REQUIRED false -> {'ok' if ok else 'failed'}")
    emit(
        args,
        f"paired {args.address}; state={state.system_state_name}; saved to {store.path}",
        {"address": args.address, "store": str(store.path), "state": state.as_dict()},
    )
    return EXIT_OK


def cmd_status(args) -> int:
    store = PairingStore(args.store)
    cam = connect_paired(args, store)
    try:
        st = cam.state.as_dict()
    finally:
        cam.close()
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
    cam = connect_paired(args, store)
    try:
        s = cam.list_sessions(args.max)
    finally:
        cam.close()
    ids = sorted(s["session_ids"], reverse=True)
    lines = [f"{sid}{'  (open)' if sid == s['open_session'] else ''}" for sid in ids]
    emit(args, "\n".join(lines) or "no sessions", {"session_ids": ids, "open_session": s["open_session"]})
    return EXIT_OK if ids else EXIT_NOTHING


def cmd_moments(args) -> int:
    store = PairingStore(args.store)
    cam = connect_paired(args, store)
    try:
        m = cam.list_moments(args.session, timeout=args.timeout)
    finally:
        cam.close()
    emit(
        args,
        " ".join(str(i) for i in m["moment_ids"]) or "no moments",
        {"session_id": args.session, **{k: v for k, v in m.items() if k != "raw"}},
    )
    return EXIT_OK if m["moment_ids"] else EXIT_NOTHING


def cmd_capture(args) -> int:
    store = PairingStore(args.store)
    cam = connect_paired(args, store)
    try:
        ok = cam.set_update_required(False)
        cam.set_active_user()
        st = cam.state
    finally:
        cam.close()
    msg = (
        f"update_required=false -> {'ok' if ok else 'FAILED'}; state={st.system_state_name}\n"
        "Close the lens cover, then open it: the camera enters CAPTURE and starts\n"
        "curating moments. Close the cover when done and run: openclips complete"
    )
    emit(args, msg, {"ok": ok, "state": st.as_dict()})
    return EXIT_OK if ok else EXIT_CAMERA


def cmd_complete(args) -> int:
    store = PairingStore(args.store)
    cam = connect_paired(args, store)
    try:
        ok = cam.complete_session()
    finally:
        cam.close()
    emit(
        args,
        "session completed" if ok else "FAILED (is the cover closed?)",
        {"ok": ok},
    )
    return EXIT_OK if ok else EXIT_CAMERA


def cmd_delete(args) -> int:
    ids = parse_ids(args.moments)
    if not ids:
        print("no moment ids", file=sys.stderr)
        return EXIT_USAGE
    store = PairingStore(args.store)
    cam = connect_paired(args, store)
    try:
        ok = cam.move_to_trash(args.session, ids) if args.trash else cam.delete_moments(args.session, ids)
    finally:
        cam.close()
    emit(args, "ok" if ok else "FAILED", {"ok": ok, "session_id": args.session, "moment_ids": ids})
    return EXIT_OK if ok else EXIT_CAMERA


def cmd_wifi(args) -> int:
    store = PairingStore(args.store)
    cam = connect_paired(args, store)
    try:
        with cam.keepalive():
            creds = cam.initiate_wifi()
            if creds is None:
                print("camera did not return Wi-Fi credentials", file=sys.stderr)
                return EXIT_CAMERA
            data = {"ssid": creds.ssid, "passphrase": creds.passphrase, "url": creds.url}
            emit(args, f"ssid={creds.ssid}\npassphrase={creds.passphrase}\nurl={creds.url}", data)
            if args.hold:
                log("holding the session with heartbeats; Ctrl-C to stop")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
    finally:
        cam.close()
    return EXIT_OK


def cmd_sync(args) -> int:
    from . import wifi_nmcli as nm

    store = PairingStore(args.store)
    out_root = os.path.abspath(args.out)
    cam = connect_paired(args, store)
    try:
        cam.set_active_user()
        plan = _plan_downloads(cam, args)
        if not plan:
            log("nothing to download")
            return EXIT_NOTHING
        total = sum(len(ids) for ids in plan.values())
        log(f"{total} moment(s) in {len(plan)} session(s)")
        with cam.keepalive():
            creds = cam.initiate_wifi()
            if creds is None:
                log("camera did not open its SoftAP")
                return EXIT_CAMERA
            log(f"SoftAP {creds.ssid} announced")
            cam.start_capture_preview()  # helps hold the group owner; failure is harmless
            iface = args.iface or nm.detect_wifi_iface()
            if not iface:
                log("no Wi-Fi interface found; join manually and use --url")
                return EXIT_WIFI
            previous = nm.active_connection(iface)
            if not nm.wait_for_ssid(creds.ssid, iface, timeout=30, log=log if args.verbose else None):
                log(f"{creds.ssid} never appeared")
                return EXIT_WIFI
            if not nm.join_nmcli(creds, iface, log=log):
                log("could not join the SoftAP")
                nm.restore_nmcli(previous)
                return EXIT_WIFI
            log(f"joined {creds.ssid} on {iface}")
            got = 0
            try:
                for sid, ids in plan.items():
                    folder = os.path.join(out_root, str(sid))
                    os.makedirs(folder, exist_ok=True)
                    for mid in ids:
                        path = os.path.join(folder, f"moment_{mid}.jpg")
                        if os.path.exists(path) and not args.overwrite:
                            log(f"skip {path} (exists)")
                            continue
                        try:
                            data = cam.fetch_moment_http(sid, mid, url=creds.url)
                        except CameraError as e:
                            log(f"moment {mid}: {e}")
                            continue
                        jpeg = extract_jpeg(data)
                        if not jpeg:
                            log(f"moment {mid}: {len(data)} bytes, not a JPEG")
                            continue
                        with open(path, "wb") as f:
                            f.write(jpeg)
                        got += 1
                        log(f"saved {path} ({len(jpeg)} bytes)")
            finally:
                if not args.keep_wifi:
                    nm.forget_nmcli(creds.ssid)
                    nm.restore_nmcli(previous)
        emit(args, f"downloaded {got} of {total}", {"downloaded": got, "planned": total, "out": out_root})
        return EXIT_OK if got else EXIT_NOTHING
    finally:
        cam.close()


def _plan_downloads(cam: Camera, args) -> dict[int, list[int]]:
    """Return ``{session_id: [moment ids]}`` honouring --session / --moments."""
    if args.session and args.moments:
        return {args.session: parse_ids(args.moments)}
    if args.session:
        return {args.session: cam.list_moments(args.session)["moment_ids"]}
    s = cam.list_sessions()
    sids = sorted(s["session_ids"], reverse=True)
    if s.get("open_session") in sids:
        sids.remove(s["open_session"])  # listing an open session blocks
        log(f"session {s['open_session']} is still open; close the cover and run `openclips complete`")
    plan: dict[int, list[int]] = {}
    for sid in sids:
        try:
            ids = cam.list_moments(sid)["moment_ids"]
        except CameraError as e:
            log(f"session {sid}: {e}")
            continue
        log(f"session {sid}: {len(ids)} moment(s)")
        if ids:
            plan[sid] = ids
            if not args.all_sessions:
                break
    return plan


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
    s.set_defaults(func=cmd_pair, address_positional=True)

    sub.add_parser("status", help="connect and print camera state").set_defaults(func=cmd_status)

    s = sub.add_parser("sessions", help="list capture sessions")
    s.add_argument("--max", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("moments", help="list moments of a completed session")
    s.add_argument("session", type=int)
    s.add_argument("--timeout", type=float, default=20.0)
    s.set_defaults(func=cmd_moments)

    sub.add_parser("capture", help="arm capture (clear update gate, become active user)").set_defaults(func=cmd_capture)
    sub.add_parser("complete", help="complete the current session (cover must be closed)").set_defaults(
        func=cmd_complete
    )

    s = sub.add_parser("delete", help="delete (or trash) moments")
    s.add_argument("session", type=int)
    s.add_argument("moments", help="comma-separated moment ids")
    s.add_argument("--trash", action="store_true", help="move to trash instead of deleting")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("wifi", help="open the camera SoftAP and print credentials")
    s.add_argument("--hold", action="store_true", help="keep heartbeats running until Ctrl-C")
    s.set_defaults(func=cmd_wifi)

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
    try:
        return args.func(args)
    except PairingKeyMismatch as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CAMERA
    except CameraAsleep as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CAMERA
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
