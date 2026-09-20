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
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


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


class PairingStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()
        self._data = {"host_key_pem": None, "cameras": {}}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            self._data["host_key_pem"] = loaded.get("host_key_pem")
            self._data["cameras"] = dict(loaded.get("cameras", {}))

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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
