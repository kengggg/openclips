"""Portable transport on top of ``bleak`` (macOS / Windows).

Untested against a real camera at the time of writing. On Linux prefer
:class:`openclips.ble_btgatt.BtgattTransport`; BlueZ's D-Bus path wedges
the camera's BLE sidecar.
"""

from __future__ import annotations

import asyncio
import queue
import threading

from .constants import INDICATE_UUID, WRITE_UUID
from .transport import Transport


class BleakTransport(Transport):
    """Synchronous wrapper running a bleak client on a background loop."""

    def __init__(self, address: str, connect_timeout: float = 20.0):
        try:
            from bleak import BleakClient
        except ImportError as e:  # pragma: no cover
            raise ImportError("pip install 'openclips[ble]' to use BleakTransport") from e
        self._q: queue.Queue[bytes] = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._client = BleakClient(address)

        async def _go():
            await self._client.connect()
            await self._client.start_notify(str(INDICATE_UUID), self._on_notify)

        asyncio.run_coroutine_threadsafe(_go(), self._loop).result(timeout=connect_timeout)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _on_notify(self, _handle, data: bytearray) -> None:
        self._q.put(bytes(data))

    def write(self, frame: bytes) -> None:
        async def _w():
            await self._client.write_gatt_char(str(WRITE_UUID), frame, response=True)

        asyncio.run_coroutine_threadsafe(_w(), self._loop).result(timeout=10)

    def read_indication(self, timeout: float = 1.0) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        async def _c():
            try:
                await self._client.disconnect()
            except Exception:
                pass

        try:
            asyncio.run_coroutine_threadsafe(_c(), self._loop).result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
