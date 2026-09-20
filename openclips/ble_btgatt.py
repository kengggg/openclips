"""Linux transport: drive BlueZ's ``btgatt-client`` through a pseudo-terminal.

This is the only path proven to work with the camera on Linux. BlueZ's D-Bus
GATT API (what ``bleak`` uses on Linux) issues writes the camera's BLE
sidecar cannot handle and the link wedges until a factory reset.

``btgatt-client`` ships with BlueZ (package ``bluez-utils`` or
``bluez-tools`` depending on the distribution). It needs no root when the
user can access the adapter.
"""

from __future__ import annotations

import os
import pty
import re
import select
import shutil
import threading
import time

from .constants import ATT_MTU, INDICATE_HANDLE, WRITE_HANDLE
from .transport import Transport

_IND_RE = re.compile(rb"Not/Ind: 0x%04x - \(\d+ bytes\): (.*)$" % INDICATE_HANDLE)


class BtgattTransport(Transport):
    """Connect to ``address`` and subscribe to indications."""

    def __init__(
        self,
        address: str,
        mtu: int = ATT_MTU,
        connect_timeout: float = 20.0,
        session_timeout_s: int = 600,
        binary: str = "btgatt-client",
    ):
        if shutil.which(binary) is None:
            raise FileNotFoundError(f"{binary!r} not found. Install BlueZ utilities (bluez-utils / bluez-tools).")
        self.address = address
        self._lock = threading.Lock()
        self.buf = b""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            argv = [binary, "-d", address, "-m", str(mtu), "-v"]
            if shutil.which("timeout"):
                argv = ["timeout", str(session_timeout_s)] + argv
            os.execvp(argv[0], argv)
        deadline = time.time() + connect_timeout
        while time.time() < deadline:
            self._pump(0.3)
            if b"discovery procedures complete" in self.buf:
                break
            if b"Function not implemented" in self.buf or b"procedures failed" in self.buf:
                self.close()
                raise ConnectionError("GATT connection dropped (camera idle or sidecar wedged)")
        else:
            self.close()
            raise ConnectionError(f"GATT discovery to {address} did not complete")
        self._cmd(f"register-notify 0x{INDICATE_HANDLE:04x}")
        self._pump(0.8)

    # -- internals ---------------------------------------------------------

    def _pump(self, duration: float) -> None:
        end = time.time() + duration
        while time.time() < end:
            if self.fd is None:
                return
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 8192)
            except OSError:
                return
            if not chunk:
                return
            self.buf += chunk

    def _cmd(self, line: str) -> None:
        with self._lock:
            os.write(self.fd, line.encode() + b"\n")

    # -- Transport ---------------------------------------------------------

    def write(self, frame: bytes) -> None:
        if len(frame) > ATT_MTU - 3:
            raise ValueError(f"frame of {len(frame)} bytes exceeds single-write limit")
        hx = " ".join(f"0x{b:02x}" for b in frame)
        self._cmd(f"write-value 0x{WRITE_HANDLE:04x} {hx}")

    def read_indication(self, timeout: float = 1.0) -> bytes | None:
        end = time.time() + timeout
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                m = _IND_RE.search(line.rstrip(b"\r"))
                if m:
                    try:
                        return bytes(int(t, 16) for t in m.group(1).split())
                    except ValueError:
                        continue
            remaining = end - time.time()
            if remaining <= 0:
                return None
            self._pump(min(0.25, remaining))

    def close(self) -> None:
        pid, self.pid = self.pid, None
        if pid:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
