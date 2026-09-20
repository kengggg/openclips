"""Linux NetworkManager helper: join the camera's SoftAP and restore afterwards.

Nothing here is required by the library. Other platforms provide their own
:class:`~openclips.wifi.WifiJoiner`.

Camera profiles are created with unique names ``openclips-<hex>`` and tracked
by UUID so leave() never deletes an unrelated SSID-named connection. ``nmcli``
failures become :class:`~openclips.errors.WifiError` without command lines or
passphrases.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
import uuid as uuidlib

from .errors import WifiError
from .wifi import WifiCredentials

logger = logging.getLogger(__name__)

PROFILE_PREFIX = "openclips-"


def parse_nmcli_fields(line: str) -> list[str]:
    """Split a ``nmcli -t`` line, honouring backslash escapes."""
    out: list[str] = []
    cur: list[str] = []
    esc = False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class NmcliResult:
    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _run(args: list[str], timeout: float = 20, *, stage: str = "nmcli") -> NmcliResult:
    """Run nmcli. Never put argv or stderr into WifiError (passwords leak there)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=max(0.05, timeout))
    except FileNotFoundError:
        raise WifiError("nmcli is not installed") from None
    except subprocess.TimeoutExpired:
        raise WifiError(f"{stage} timed out") from None
    if r.returncode != 0:
        logger.debug("%s rc=%s", stage, r.returncode)
    return NmcliResult(r.returncode, r.stdout or "", r.stderr or "")


def detect_wifi_iface() -> str | None:
    """First Wi-Fi device NetworkManager knows about, or ``None``."""
    try:
        r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"], stage="detect-iface")
    except WifiError:
        return None
    for line in r.stdout.splitlines():
        parts = parse_nmcli_fields(line)
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return None


def active_connection(iface: str) -> str | None:
    """UUID of the connection currently active on ``iface``."""
    try:
        r = _run(["nmcli", "-t", "-f", "UUID,DEVICE", "connection", "show", "--active"], stage="active-connection")
    except WifiError:
        return None
    for line in r.stdout.splitlines():
        parts = parse_nmcli_fields(line)
        if len(parts) >= 2 and parts[1] == iface and parts[0]:
            return parts[0]
    return None


def wait_for_ssid(ssid: str, iface: str, timeout: float = 25.0, log=None) -> bool:
    """Rescan until ``ssid`` shows up. Uses a monotonic deadline."""
    deadline = time.monotonic() + timeout
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            return False
        try:
            _run(["nmcli", "device", "wifi", "rescan", "ifname", iface], timeout=min(5.0, rem), stage="wifi-rescan")
        except WifiError:
            pass
        rem = deadline - time.monotonic()
        if rem <= 0:
            return False
        time.sleep(min(1.2, rem))
        rem = deadline - time.monotonic()
        if rem <= 0:
            return False
        try:
            r = _run(
                ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "ifname", iface],
                timeout=min(5.0, rem),
                stage="wifi-list",
            )
        except WifiError:
            continue
        ssids = [parse_nmcli_fields(line)[0] for line in r.stdout.splitlines() if line]
        if ssid in ssids:
            return True
        (log or logger.debug)("waiting for camera SSID")


def _psk_file(passphrase: str) -> str:
    fd, path = tempfile.mkstemp(prefix=".openclips-psk-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            fd = -1
            f.write(passphrase)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def join_nmcli(
    creds: WifiCredentials,
    iface: str | None = None,
    timeout: float = 18.0,
    retries: int = 3,
    log=None,
    *,
    profile_name: str | None = None,
) -> tuple[str | None, bool]:
    """Create an owned camera profile and try to bring it up.

    Returns ``(uuid, connected)``. ``uuid`` is set as soon as ``connection add``
    succeeds so the caller can delete the profile if ``connection up`` fails.
    """
    iface = iface or detect_wifi_iface()
    if not iface:
        raise WifiError("no Wi-Fi interface found via nmcli")
    name = profile_name or (PROFILE_PREFIX + uuidlib.uuid4().hex[:8])
    deadline = time.monotonic() + timeout
    psk_path = _psk_file(creds.passphrase)
    try:
        with open(psk_path, encoding="utf-8") as f:
            psk = f.read()
        add = _run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                iface,
                "con-name",
                name,
                "ssid",
                creds.ssid,
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk",
                psk,
            ],
            timeout=max(0.05, deadline - time.monotonic()),
            stage="connection-add",
        )
        if add.rc != 0:
            raise WifiError("could not create camera Wi-Fi profile")
        owned = _uuid_for_name(name)
        if not owned:
            raise WifiError("camera Wi-Fi profile has no UUID")
        for attempt in range(max(1, retries)):
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            up = _run(
                ["nmcli", "connection", "up", "uuid", owned],
                timeout=rem,
                stage="connection-up",
            )
            if up.ok:
                return owned, True
            (log or logger.info)("join attempt %s failed rc=%s", attempt + 1, up.rc)
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            time.sleep(min(1.5, rem))
        return owned, False
    finally:
        try:
            os.unlink(psk_path)
        except OSError:
            pass


def _uuid_for_name(name: str) -> str | None:
    try:
        r = _run(["nmcli", "-t", "-f", "NAME,UUID", "connection", "show"], stage="connection-show")
    except WifiError:
        return None
    for line in r.stdout.splitlines():
        parts = parse_nmcli_fields(line)
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None


def forget_nmcli(ident: str) -> None:
    """Delete an owned camera profile by UUID (preferred) or name."""
    if not ident:
        return
    key = "uuid" if len(ident) >= 32 and "-" in ident else "id"
    r = _run(["nmcli", "connection", "delete", key, ident], stage="connection-delete")
    if r.rc != 0:
        raise WifiError("could not delete camera Wi-Fi profile")


def restore_nmcli(connection: str | None) -> None:
    """Bring a previously active connection back up by UUID or name."""
    if not connection:
        return
    key = "uuid" if len(connection) >= 32 and "-" in connection else "id"
    r = _run(["nmcli", "connection", "up", key, connection], timeout=30, stage="connection-restore")
    if r.rc != 0:
        raise WifiError("could not restore previous Wi-Fi connection")


class NmcliWifi:
    """:class:`~openclips.wifi.WifiJoiner` on top of ``nmcli``.

    Remembers the prior connection UUID and restores it in :meth:`leave`
    unless ``restore=False``. Camera profiles are owned by UUID.
    """

    def __init__(self, iface: str | None = None, scan_timeout: float = 30.0, restore: bool = True):
        self.iface = iface
        self.scan_timeout = scan_timeout
        self.restore = restore
        self.previous: str | None = None
        self.ssid: str | None = None
        self.owned_uuid: str | None = None
        self.leave_errors: list[str] = []

    def join(self, creds: WifiCredentials) -> bool:
        iface = self.iface or detect_wifi_iface()
        if not iface:
            logger.error("no Wi-Fi interface found via nmcli")
            return False
        self.iface = iface
        self.previous = active_connection(iface)
        self.ssid = creds.ssid
        if not wait_for_ssid(creds.ssid, iface, timeout=self.scan_timeout):
            logger.error("camera SSID never appeared")
            return False
        owned, connected = join_nmcli(creds, iface)
        self.owned_uuid = owned
        if not owned or not connected:
            self.leave()
            return False
        return True

    def leave(self) -> None:
        self.leave_errors = []
        owned, self.owned_uuid = self.owned_uuid, None
        if owned:
            try:
                forget_nmcli(owned)
            except WifiError as e:
                self.leave_errors.append(str(e))
                logger.debug("forget camera profile failed")
        self.ssid = None
        if self.restore and self.previous:
            try:
                restore_nmcli(self.previous)
            except WifiError as e:
                self.leave_errors.append(str(e))
                logger.debug("restore previous connection failed")
