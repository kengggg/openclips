"""Connection lifecycle for applications.

The camera accepts roughly one GATT connection per wake, the first connect
attempt often fails, and Myriad idles after a few minutes without traffic.
:class:`ConnectionManager` wraps all of that: it retries the transport,
resumes the secure session, syncs the clock, keeps heartbeats running for
as long as the connection is open, and turns the failure modes into
exceptions an app can show to the user.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .camera import Camera
from .errors import CameraAsleep, CameraError, ConnectionLost, NotPaired
from .store import Pairing, PairingStore
from .transport import Transport

logger = logging.getLogger(__name__)

TransportFactory = Callable[[str], Transport]

WAKE_HINT = "press the camera's shutter button once, then retry"


def default_transport_factory(name: str = "btgatt") -> TransportFactory:
    """Factory for the bundled transports by name (``btgatt`` or ``bleak``)."""
    if name == "bleak":
        from .ble_bleak import BleakTransport

        return BleakTransport
    if name == "btgatt":
        from .ble_btgatt import BtgattTransport

        return BtgattTransport
    raise ValueError(f"unknown transport {name!r}")


class ConnectionManager:
    """Open, hold and close one camera session.

    ::

        mgr = ConnectionManager("AA:BB:CC:DD:EE:FF", store=PairingStore())
        try:
            cam = mgr.connect()
        except CameraAsleep:
            ...tell the user to press the shutter...
        ...
        mgr.close()

    Parameters
    ----------
    address:
        Camera BLE address.
    pairing_key / pairing / store:
        Any one source of the pairing key. ``store`` looks the address up.
    transport_factory:
        ``callable(address) -> Transport``. Defaults to the Linux btgatt adapter.
    attempts / retry_delay:
        GATT connect retries. The first attempt fails often on this camera.
    keepalive / keepalive_interval:
        Run heartbeats for the life of the connection (default on).
    time_sync:
        Send PRIVATE_QUERY with the host clock after resume so moment
        timestamps are meaningful (default on).
    """

    def __init__(
        self,
        address: str,
        pairing_key: bytes | None = None,
        *,
        pairing: Pairing | None = None,
        store: PairingStore | None = None,
        transport_factory: TransportFactory | None = None,
        attempts: int = 4,
        retry_delay: float = 1.5,
        keepalive: bool = True,
        keepalive_interval: float = 4.0,
        time_sync: bool = True,
        device_id: int | None = None,
    ):
        self.address = PairingStore.norm(address)
        if pairing is None and store is not None:
            pairing = store.get(self.address)
        self.pairing = pairing
        self.pairing_key = pairing_key or (pairing.pairing_key if pairing else None)
        self.device_id = device_id or (pairing.device_id if pairing else 1)
        self.transport_factory = transport_factory or default_transport_factory()
        self.attempts = max(1, attempts)
        self.retry_delay = retry_delay
        self.keepalive = keepalive
        self.keepalive_interval = keepalive_interval
        self.time_sync = time_sync
        self.camera: Camera | None = None
        self.transport: Transport | None = None

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self.camera is not None and self.camera.connected

    # ------------------------------------------------------------------ connect

    def _open_transport(self) -> Transport:
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                t = self.transport_factory(self.address)
                logger.info("GATT connected to %s (attempt %d)", self.address, attempt)
                return t
            except (ConnectionError, OSError, TimeoutError) as e:
                last = e
                logger.info("GATT attempt %d/%d failed: %s", attempt, self.attempts, e)
                if attempt < self.attempts:
                    time.sleep(self.retry_delay)
        raise ConnectionLost(f"could not connect to {self.address} after {self.attempts} attempts: {last}")

    def connect(self) -> Camera:
        """Connect, resume the secure session and start heartbeats."""
        if self.connected:
            return self.camera
        if not self.pairing_key:
            raise NotPaired(f"{self.address} has no stored pairing; pair first")
        self.close()
        transport = self._open_transport()
        cam = Camera(transport, self.pairing_key, self.device_id)
        try:
            cam.resume()
            if self.time_sync:
                cam.time_sync()
            if self.keepalive:
                cam.start_keepalive(self.keepalive_interval)
        except CameraAsleep as e:
            transport.close()
            raise CameraAsleep(f"{e}; {WAKE_HINT}") from None
        except CameraError:
            transport.close()
            raise
        self.transport, self.camera = transport, cam
        return cam

    def reconnect(self) -> Camera:
        """Drop the current session (if any) and connect again."""
        self.close()
        return self.connect()

    def ensure(self) -> Camera:
        """Return a live camera, reconnecting if the link was lost."""
        if self.connected:
            return self.camera
        return self.reconnect()

    def close(self) -> None:
        cam, self.camera = self.camera, None
        transport, self.transport = self.transport, None
        if cam is not None:
            try:
                cam.close()
            except Exception:
                logger.debug("error closing camera", exc_info=True)
        elif transport is not None:
            transport.close()

    def __enter__(self) -> Camera:
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False
