"""BLE transport interface. Platform adapters implement this."""

from __future__ import annotations


class Transport:
    """One connected GATT session with the camera.

    ``write`` sends one ATT Write Request containing a complete frame.
    ``read_indication`` returns the next indication value (a complete frame)
    or ``None`` when ``timeout`` seconds pass without one.
    ``alive`` turns False once the adapter notices the link is gone; the
    :class:`~openclips.camera.Camera` then raises ``ConnectionLost``.
    """

    alive: bool = True

    def write(self, frame: bytes) -> None:
        raise NotImplementedError

    def read_indication(self, timeout: float = 1.0) -> bytes | None:
        raise NotImplementedError

    def close(self) -> None:
        self.alive = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
