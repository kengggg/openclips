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
