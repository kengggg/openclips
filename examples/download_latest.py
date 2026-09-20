#!/usr/bin/env python3
"""Download every moment of the newest completed session, with progress.

Linux example: btgatt transport and NetworkManager. On another OS pass a
different transport factory to ConnectionManager and a ManualWifi that asks
the user to join the SSID.

    python examples/download_latest.py AA:BB:CC:DD:EE:FF ./photos
"""

import logging
import sys

from openclips import CameraAsleep, ConnectionManager, PairingStore, Syncer
from openclips.sync import STAGE_SAVED
from openclips.wifi_nmcli import NmcliWifi


def main(address: str, out: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = PairingStore()
    if store.get(address) is None:
        print(f"{address} is not paired; run `openclips pair {address}` first")
        return 2

    def progress(p):
        if p.stage == STAGE_SAVED:
            print(f"saved {p.item.path} ({p.bytes} bytes, score {p.item.moment.score:.3f})")

    try:
        with ConnectionManager(address, store=store) as cam:
            cam.set_active_user()
            result = Syncer(cam, out, wifi=NmcliWifi(), on_progress=progress).sync()
    except CameraAsleep as e:
        print(e)
        return 3
    print(f"downloaded {len(result.downloaded)}, failed {len(result.failed)}, already had {len(result.skipped)}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
