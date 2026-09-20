#!/usr/bin/env python3
"""Download every moment of the newest completed session.

Linux example using the btgatt transport and NetworkManager. On another OS
swap the transport and join the SSID yourself between initiate_wifi() and
fetch_moment_http().

    python examples/download_latest.py AA:BB:CC:DD:EE:FF ./photos
"""

import sys
from pathlib import Path

from openclips import Camera, PairingStore, extract_jpeg
from openclips.ble_btgatt import BtgattTransport
from openclips.wifi_nmcli import active_connection, detect_wifi_iface, join_nmcli, restore_nmcli, wait_for_ssid


def main(address: str, out: str) -> int:
    pairing = PairingStore().get(address)
    if pairing is None:
        print(f"{address} is not paired; run `openclips pair {address}` first")
        return 2
    with BtgattTransport(address) as transport:
        cam = Camera(transport, pairing.pairing_key, log=print)
        cam.resume()
        sessions = cam.list_sessions()
        closed = sorted(s for s in sessions["session_ids"] if s != sessions["open_session"])
        if not closed:
            print("no completed sessions")
            return 5
        sid = closed[-1]
        ids = cam.list_moments(sid)["moment_ids"]
        print(f"session {sid}: {len(ids)} moments")
        with cam.keepalive():
            creds = cam.initiate_wifi()
            iface = detect_wifi_iface()
            previous = active_connection(iface)
            if not (wait_for_ssid(creds.ssid, iface) and join_nmcli(creds, iface)):
                print("could not join the camera network")
                return 4
            try:
                folder = Path(out) / str(sid)
                folder.mkdir(parents=True, exist_ok=True)
                for mid in ids:
                    jpeg = extract_jpeg(cam.fetch_moment_http(sid, mid, url=creds.url))
                    if jpeg:
                        (folder / f"moment_{mid}.jpg").write_bytes(jpeg)
                        print(f"saved moment {mid} ({len(jpeg)} bytes)")
            finally:
                restore_nmcli(previous)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
