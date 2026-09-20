"""Discover cameras from BLE advertisements (requires ``bleak``).

Scanning through BlueZ is safe on Linux; only GATT *writes* through BlueZ's
D-Bus API wedge the camera.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .constants import MANUFACTURER_ID


@dataclass
class CameraAdvertisement:
    address: str
    name: str | None
    rssi: int | None
    manufacturer_data: bytes
    #: True while the camera is in its post-factory-reset setup window.
    setup_mode: bool


def _from_adv(device, adv) -> CameraAdvertisement | None:
    md = adv.manufacturer_data or {}
    if MANUFACTURER_ID not in md:
        return None
    payload = bytes(md[MANUFACTURER_ID])
    setup = bool(payload[0] & 1) if payload else False
    return CameraAdvertisement(device.address, device.name, adv.rssi, payload, setup)


async def scan_async(timeout: float = 8.0) -> list[CameraAdvertisement]:
    from bleak import BleakScanner

    found: dict[str, CameraAdvertisement] = {}

    def cb(device, adv):
        hit = _from_adv(device, adv)
        if hit:
            found[hit.address] = hit

    scanner = BleakScanner(detection_callback=cb, scanning_mode="active")
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return sorted(found.values(), key=lambda h: -(h.rssi or -999))


def scan(timeout: float = 8.0) -> list[CameraAdvertisement]:
    """Blocking scan; returns cameras seen, strongest signal first."""
    try:
        import bleak  # noqa: F401
    except ImportError as e:
        raise ImportError("pip install 'openclips[ble]' to scan") from e
    return asyncio.run(scan_async(timeout))
