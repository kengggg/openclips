"""openclips: talk to a Google Clips camera without the Android app.

See ``docs/PROTOCOL.md`` for the wire protocol and ``README.md`` for usage.
"""

from . import constants
from .camera import (
    Camera,
    CameraAsleep,
    CameraError,
    NotPaired,
    PairingKeyMismatch,
    RequestTimeout,
    extract_jpeg,
)
from .constants import (
    HTTP_DEFAULT,
    INDICATE_UUID,
    MANUFACTURER_ID,
    RESOLUTION_FULL,
    RESOLUTION_THUMB,
    SERVICE_UUID,
    WRITE_UUID,
)
from .crypto import SecureSession, derive_pairing_key, generate_host_key
from .proto import (
    CameraState,
    build_fetch_moment,
    build_initiate_wifi_paired,
    build_list_moments,
    parse_list_moments,
    parse_list_sessions,
)
from .store import Pairing, PairingStore
from .transport import Transport
from .wifi import WifiCredentials

__version__ = "0.1.0"

__all__ = [
    "Camera",
    "CameraAsleep",
    "CameraError",
    "CameraState",
    "NotPaired",
    "Pairing",
    "PairingKeyMismatch",
    "PairingStore",
    "RequestTimeout",
    "SecureSession",
    "Transport",
    "WifiCredentials",
    "build_fetch_moment",
    "build_initiate_wifi_paired",
    "build_list_moments",
    "constants",
    "derive_pairing_key",
    "extract_jpeg",
    "generate_host_key",
    "parse_list_moments",
    "parse_list_sessions",
    "HTTP_DEFAULT",
    "INDICATE_UUID",
    "MANUFACTURER_ID",
    "RESOLUTION_FULL",
    "RESOLUTION_THUMB",
    "SERVICE_UUID",
    "WRITE_UUID",
    "__version__",
]
