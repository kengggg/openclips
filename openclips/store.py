"""Persist pairings under the user's config directory.

Layout of ``$XDG_CONFIG_HOME/openclips/pairings.json``::

    {
      "host_key_pem": "-----BEGIN PRIVATE KEY-----...",
      "cameras": {
        "AA:BB:CC:DD:EE:FF": {
          "pairing_key": "<64 hex>",
          "lens_public_key": "<128 hex>",
          "device_id": 1,
          "name": "Clips",
          "paired_at": "2026-09-20T10:00:00"
        }
      }
    }

The pairing key is a device credential: anyone holding it within BLE range
can control the camera and download its photos. The file is written with
mode 0600. Never commit it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import StorageError
from .persist import atomic_write_json, exclusive_lock


def default_path() -> Path:
    env = os.environ.get("OPENCLIPS_STORE")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "openclips" / "pairings.json"


@dataclass
class Pairing:
    address: str
    pairing_key: bytes
    lens_public_key: bytes = b""
    device_id: int = 1
    name: str = ""
    paired_at: str = ""

    def __repr__(self) -> str:
        return (
            f"Pairing(address={self.address!r}, pairing_key=<redacted>, device_id={self.device_id}, name={self.name!r})"
        )

    def to_json(self) -> dict:
        d = asdict(self)
        d["pairing_key"] = self.pairing_key.hex()
        d["lens_public_key"] = self.lens_public_key.hex()
        return d

    @classmethod
    def from_json(cls, address: str, d: dict) -> Pairing:
        return cls(
            address=address,
            pairing_key=bytes.fromhex(d["pairing_key"]),
            lens_public_key=bytes.fromhex(d.get("lens_public_key", "")),
            device_id=int(d.get("device_id", 1)),
            name=d.get("name", ""),
            paired_at=d.get("paired_at", ""),
        )


def _validate_pairing_store(loaded: object) -> dict:
    if not isinstance(loaded, dict):
        raise StorageError("pairing store is invalid")
    version = loaded.get("version", 1)
    if not isinstance(version, int) or version > 1:
        raise StorageError("pairing store version is unsupported")
    cameras = loaded.get("cameras", {})
    if cameras is None:
        cameras = {}
    if not isinstance(cameras, dict):
        raise StorageError("pairing store is invalid")
    for addr, entry in cameras.items():
        if not isinstance(addr, str) or not isinstance(entry, dict):
            raise StorageError("pairing store is invalid")
        key = entry.get("pairing_key", "")
        if not isinstance(key, str) or len(key) != 64:
            raise StorageError("pairing store is invalid")
        try:
            bytes.fromhex(key)
        except ValueError:
            raise StorageError("pairing store is invalid") from None
    pem = loaded.get("host_key_pem")
    if pem is not None and not isinstance(pem, str):
        raise StorageError("pairing store is invalid")
    return {
        "version": 1,
        "revision": int(loaded.get("revision", 0) or 0),
        "host_key_pem": pem,
        "cameras": dict(cameras),
    }


class PairingStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()
        self._data = {"version": 1, "revision": 0, "host_key_pem": None, "cameras": {}}
        if self.path.exists():
            self._warn_permissions()
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise StorageError("pairing store is invalid") from e
            self._data = _validate_pairing_store(loaded)

    # -- host key ------------------------------------------------------------

    @property
    def host_key_pem(self) -> str | None:
        return self._data["host_key_pem"]

    @host_key_pem.setter
    def host_key_pem(self, pem: str) -> None:
        self._data["host_key_pem"] = pem

    # -- cameras -------------------------------------------------------------

    @staticmethod
    def norm(address: str) -> str:
        return address.strip().upper()

    def addresses(self) -> list[str]:
        return sorted(self._data["cameras"])

    def get(self, address: str) -> Pairing | None:
        d = self._data["cameras"].get(self.norm(address))
        return Pairing.from_json(self.norm(address), d) if d else None

    def put(self, pairing: Pairing) -> None:
        if not pairing.paired_at:
            pairing.paired_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._data["cameras"][self.norm(pairing.address)] = pairing.to_json()

    def remove(self, address: str) -> bool:
        return self._data["cameras"].pop(self.norm(address), None) is not None

    def only_address(self) -> str | None:
        """The single stored camera, if exactly one exists."""
        addrs = self.addresses()
        return addrs[0] if len(addrs) == 1 else None

    # -- persistence -----------------------------------------------------------

    def _warn_permissions(self) -> None:
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
        except OSError:
            return
        if mode & 0o077:
            logging.getLogger(__name__).warning("pairing store permissions are too open; chmod 600 the store file")

    def save(self) -> None:
        with exclusive_lock(self.path):
            if self.path.exists():
                try:
                    with open(self.path, encoding="utf-8") as f:
                        disk = json.load(f)
                    disk_rev = int(disk.get("revision", 0) or 0) if isinstance(disk, dict) else 0
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    disk_rev = self._data.get("revision", 0)
                if disk_rev != self._data.get("revision", 0):
                    raise StorageError("pairing store changed on disk; reload and retry")
            self._data["revision"] = int(self._data.get("revision", 0)) + 1
            self._data["version"] = 1
            atomic_write_json(self.path, self._data, mode=0o600)
