"""openclips: talk to a Google Clips camera without the Android app.

See ``docs/PROTOCOL.md`` for the wire protocol, ``docs/LIBRARY.md`` for
building applications on this package, and ``README.md`` for the CLI.
"""

from . import constants
from .camera import (
    EVENT_DISCONNECTED,
    EVENT_NOTIFICATION,
    EVENT_STATE,
    Camera,
    extract_jpeg,
)
from .catalog import Catalog
from .connection import ConnectionManager, default_transport_factory
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
from .errors import (
    CameraAsleep,
    CameraError,
    ConnectionLost,
    NotPaired,
    PairingKeyMismatch,
    RequestTimeout,
    UnsafeRequest,
    WifiError,
)
from .proto import (
    CameraState,
    MomentInfo,
    build_fetch_moment,
    build_initiate_wifi_paired,
    build_list_moments,
    parse_list_moments,
    parse_list_sessions,
    session_start_time,
)
from .store import Pairing, PairingStore
from .sync import Syncer, SyncItem, SyncPlan, SyncProgress, SyncResult
from .transport import Transport
from .wifi import ManualWifi, NullWifi, WifiCredentials, WifiJoiner

__version__ = "0.2.0"

__all__ = [
    "Camera",
    "CameraAsleep",
    "CameraError",
    "CameraState",
    "Catalog",
    "ConnectionLost",
    "ConnectionManager",
    "EVENT_DISCONNECTED",
    "EVENT_NOTIFICATION",
    "EVENT_STATE",
    "HTTP_DEFAULT",
    "INDICATE_UUID",
    "MANUFACTURER_ID",
    "ManualWifi",
    "MomentInfo",
    "NotPaired",
    "NullWifi",
    "Pairing",
    "PairingKeyMismatch",
    "PairingStore",
    "RESOLUTION_FULL",
    "RESOLUTION_THUMB",
    "RequestTimeout",
    "SERVICE_UUID",
    "SecureSession",
    "SyncItem",
    "SyncPlan",
    "SyncProgress",
    "SyncResult",
    "Syncer",
    "Transport",
    "UnsafeRequest",
    "WRITE_UUID",
    "WifiCredentials",
    "WifiError",
    "WifiJoiner",
    "__version__",
    "build_fetch_moment",
    "build_initiate_wifi_paired",
    "build_list_moments",
    "constants",
    "default_transport_factory",
    "derive_pairing_key",
    "extract_jpeg",
    "generate_host_key",
    "parse_list_moments",
    "parse_list_sessions",
    "session_start_time",
]
