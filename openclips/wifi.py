"""Wi-Fi credentials the camera returns from INITIATE_WIFI."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import HTTP_DEFAULT


@dataclass
class WifiCredentials:
    """SoftAP credentials. Joining the network is OS-specific."""

    ssid: str
    passphrase: str
    url: str = HTTP_DEFAULT
    raw: bytes = field(default=b"", repr=False)
