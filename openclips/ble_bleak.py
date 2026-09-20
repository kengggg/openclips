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

    def __init__(self, address: str, connect_timeout: float = 20.0, *, client=None):
        self._q: queue.Queue[bytes] = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="openclips-bleak")
        self._thread.start()
        self._client = client
        try:
            if self._client is None:
                try:
                    from bleak import BleakClient
                except ImportError as e:  # pragma: no cover
                    raise ImportError("pip install 'openclips[ble]' to use BleakTransport") from e
                self._client = BleakClient(address)
            self._bind_disconnect(self._client)

            async def _go():
                await self._client.connect()
                await self._client.start_notify(str(INDICATE_UUID), self._on_notify)

            asyncio.run_coroutine_threadsafe(_go(), self._loop).result(timeout=connect_timeout)
        except Exception:
            self.close()
            raise

    def _bind_disconnect(self, client) -> None:
        def on_dc(_client=None):
            self.alive = False

        binder = getattr(client, "set_disconnected_callback", None)
        if callable(binder):
            binder(on_dc)
        else:
            try:
                client.disconnected_callback = on_dc
            except Exception:
                pass

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _on_notify(self, _handle, data: bytearray) -> None:
        self._q.put(bytes(data))

    def write(self, frame: bytes) -> None:
        if not self.alive:
            raise OSError("BLE link dropped")

        async def _w():
            await self._client.write_gatt_char(str(WRITE_UUID), frame, response=True)

        asyncio.run_coroutine_threadsafe(_w(), self._loop).result(timeout=10)

    def read_indication(self, timeout: float = 1.0) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.alive = False

        async def _c():
            try:
                await self._client.disconnect()
            except Exception:
                pass

        if getattr(self, "_client", None) is not None and self._loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(_c(), self._loop)
                fut.result(timeout=5)
            except Exception:
                pass
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)
        if not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:
                pass
