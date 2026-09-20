"""Linux NetworkManager helper: join the camera's SoftAP and restore afterwards.

Nothing here is required by the library. Other platforms provide their own
:class:`~openclips.wifi.WifiJoiner`.
"""

from __future__ import annotations

import logging
import subprocess
import time

from .wifi import WifiCredentials

logger = logging.getLogger(__name__)


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def detect_wifi_iface() -> str | None:
    """First Wi-Fi device NetworkManager knows about, or ``None``."""
    try:
        r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return None


def active_connection(iface: str) -> str | None:
    """Name of the connection currently active on ``iface``."""
    try:
        r = _run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        name, _, dev = line.rpartition(":")
        if dev == iface and name:
            return name
    return None


def wait_for_ssid(ssid: str, iface: str, timeout: float = 25.0, log=None) -> bool:
    """Rescan until ``ssid`` shows up."""
    end = time.time() + timeout
    while time.time() < end:
        _run(["nmcli", "device", "wifi", "rescan", "ifname", iface])
        time.sleep(1.2)
        r = _run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", iface])
        if ssid in r.stdout.splitlines():
            return True
        (log or logger.debug)(f"waiting for SSID {ssid!r}")
    return False


def join_nmcli(
    creds: WifiCredentials,
    iface: str | None = None,
    timeout: float = 18.0,
    retries: int = 3,
    log=None,
) -> bool:
    """Connect ``iface`` to ``creds.ssid``. Returns True on success."""
    iface = iface or detect_wifi_iface()
    if not iface:
        raise RuntimeError("no Wi-Fi interface found via nmcli")
    for attempt in range(retries):
        r = _run(
            ["nmcli", "device", "wifi", "connect", creds.ssid, "password", creds.passphrase, "ifname", iface],
            timeout=timeout,
        )
        if r.returncode == 0:
            return True
        (log or logger.info)(f"join attempt {attempt + 1} failed: {r.stderr.strip()}")
        time.sleep(1.5)
    return False


def forget_nmcli(ssid: str) -> None:
    """Delete the connection profile nmcli created for the camera's SSID."""
    _run(["nmcli", "connection", "delete", ssid])


def restore_nmcli(connection: str | None) -> None:
    """Bring a previously active connection back up."""
    if connection:
        _run(["nmcli", "connection", "up", connection], timeout=30)


class NmcliWifi:
    """:class:`~openclips.wifi.WifiJoiner` on top of ``nmcli``.

    Remembers the connection that was active before joining and restores it
    in :meth:`leave`, unless ``restore=False``.
    """

    def __init__(self, iface: str | None = None, scan_timeout: float = 30.0, restore: bool = True):
        self.iface = iface
        self.scan_timeout = scan_timeout
        self.restore = restore
        self.previous: str | None = None
        self.ssid: str | None = None

    def join(self, creds: WifiCredentials) -> bool:
        iface = self.iface or detect_wifi_iface()
        if not iface:
            logger.error("no Wi-Fi interface found via nmcli")
            return False
        self.iface = iface
        self.previous = active_connection(iface)
        self.ssid = creds.ssid
        if not wait_for_ssid(creds.ssid, iface, timeout=self.scan_timeout):
            logger.error("SSID %s never appeared", creds.ssid)
            return False
        ok = join_nmcli(creds, iface)
        if not ok:
            self.leave()
        return ok

    def leave(self) -> None:
        if self.ssid:
            forget_nmcli(self.ssid)
            self.ssid = None
        if self.restore and self.previous:
            restore_nmcli(self.previous)
