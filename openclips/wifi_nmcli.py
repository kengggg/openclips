"""Optional Linux helper: join the camera's SoftAP with NetworkManager.

Nothing here is required by the library. Other platforms join the SSID with
their own tooling and call :meth:`openclips.camera.Camera.fetch_moment_http`.
"""

from __future__ import annotations

import subprocess
import time

from .wifi import WifiCredentials


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
        if log:
            log(f"waiting for SSID {ssid!r}")
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
        if log:
            log(f"join attempt {attempt + 1} failed: {r.stderr.strip()}")
        time.sleep(1.5)
    return False


def forget_nmcli(ssid: str) -> None:
    """Delete the connection profile nmcli created for the camera's SSID."""
    _run(["nmcli", "connection", "delete", ssid])


def restore_nmcli(connection: str | None) -> None:
    """Bring a previously active connection back up."""
    if connection:
        _run(["nmcli", "connection", "up", connection], timeout=30)
