"""Exception hierarchy shared by every layer of openclips."""


class CameraError(Exception):
    """Base class for camera protocol and session errors."""


class CameraAsleep(CameraError):
    """The camera advertises but Myriad is idle. A short shutter press wakes it."""


class PairingKeyMismatch(CameraError):
    """The lens proof did not verify: the camera was reset, pair again."""


class NotPaired(CameraError):
    """Operation needs a pairing key or an established secure session."""


class RequestTimeout(CameraError):
    """No response to a request within the timeout."""


class ConnectionLost(CameraError):
    """The BLE link dropped while a session was in use."""


class UnsafeRequest(CameraError):
    """Refused to send a request known to wedge the camera."""


class WifiError(CameraError):
    """The host could not join the camera's SoftAP."""


class HttpError(CameraError):
    """HTTP media fetch failed. ``status`` is set when the camera answered.

    The message never includes response bodies. ``retryable`` is True only for
    explicitly transient network/gateway failures, not HTTP 500 "moment
    unavailable" responses.
    """

    def __init__(self, path: str, status: int | None = None, *, retryable: bool = False):
        self.path = path
        self.status = status
        self.retryable = retryable
        if status is None:
            msg = f"HTTP request to {path} failed"
        else:
            msg = f"HTTP {status} on {path}"
        super().__init__(msg)


class StorageError(CameraError):
    """Local disk failed in a way that should stop the rest of a sync."""


class EventOverflow(CameraError):
    """An async event subscription exceeded its bounded queue."""
