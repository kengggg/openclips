"""BLE transport interface. Platform adapters implement this."""

from __future__ import annotations


class Transport:
    """One connected GATT session with the camera.

    ``write`` sends one ATT Write Request containing a complete frame.
    ``read_indication`` returns the next indication value (a complete frame)
    or ``None`` when ``timeout`` seconds pass without one.
    """

    def write(self, frame: bytes) -> None:
        raise NotImplementedError

    def read_indication(self, timeout: float = 1.0) -> bytes | None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
