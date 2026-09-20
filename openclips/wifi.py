"""Wi-Fi credentials and the joiner abstraction used by :mod:`openclips.sync`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .constants import HTTP_DEFAULT


@dataclass
class WifiCredentials:
    """SoftAP credentials returned by INITIATE_WIFI. Joining is OS-specific."""

    ssid: str
    passphrase: str
    url: str = HTTP_DEFAULT
    raw: bytes = field(default=b"", repr=False)


@runtime_checkable
class WifiJoiner(Protocol):
    """Join and leave the camera's network. Implement per platform."""

    def join(self, creds: WifiCredentials) -> bool: ...

    def leave(self) -> None: ...


class ManualWifi:
    """Ask the user to join by hand; for GUIs and platforms without a helper.

    ``prompt`` receives the credentials and returns True once the host is on
    the network. ``on_leave`` is called afterwards, if given.
    """

    def __init__(self, prompt: Callable[[WifiCredentials], bool], on_leave: Callable[[], None] | None = None):
        self._prompt = prompt
        self._on_leave = on_leave

    def join(self, creds: WifiCredentials) -> bool:
        return bool(self._prompt(creds))

    def leave(self) -> None:
        if self._on_leave:
            self._on_leave()


class NullWifi:
    """Assume the host is already on the camera network (tests, manual setups)."""

    def join(self, creds: WifiCredentials) -> bool:
        return True

    def leave(self) -> None:
        pass
