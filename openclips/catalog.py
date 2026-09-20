"""Local index of downloaded moments.

Kept next to the photos as ``.openclips-catalog.json``. Session ids are
usually unique, but unsynchronized camera clocks can collide; record
``camera`` provenance when known. Malformed catalogs raise StorageError
and are left on disk; use :meth:`rebuild` to move them aside.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .errors import StorageError
from .persist import atomic_write_json, exclusive_lock

CATALOG_NAME = ".openclips-catalog.json"


def _validate_catalog(loaded: object) -> dict:
    if not isinstance(loaded, dict) or "sessions" not in loaded:
        raise StorageError("catalog is invalid")
    version = loaded.get("version", 1)
    if not isinstance(version, int) or version > 1:
        raise StorageError("catalog version is unsupported")
    sessions = loaded.get("sessions")
    if not isinstance(sessions, dict):
        raise StorageError("catalog is invalid")
    for _sid, sess in sessions.items():
        if not isinstance(sess, dict) or not isinstance(sess.get("moments", {}), dict):
            raise StorageError("catalog is invalid")
    return {
        "version": 1,
        "revision": int(loaded.get("revision", 0) or 0),
        "sessions": sessions,
    }


class Catalog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._data: dict = {"version": 1, "revision": 0, "sessions": {}}
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise StorageError("catalog is invalid") from e
            self._data = _validate_catalog(loaded)

    @classmethod
    def for_dir(cls, out_dir: Path | str) -> Catalog:
        return cls(Path(out_dir) / CATALOG_NAME)

    # -- queries -------------------------------------------------------------

    def sessions(self) -> dict[int, dict]:
        return {int(k): v for k, v in self._data["sessions"].items()}

    def moments(self, session_id: int) -> dict[int, dict]:
        sess = self._data["sessions"].get(str(session_id), {})
        return {int(k): v for k, v in sess.get("moments", {}).items()}

    def get(self, session_id: int, moment_id: int) -> dict | None:
        return self._data["sessions"].get(str(session_id), {}).get("moments", {}).get(str(moment_id))

    def has(self, session_id: int, moment_id: int) -> bool:
        entry = self.get(session_id, moment_id)
        if not entry:
            return False
        path = entry.get("file")
        return bool(path) and (self.path.parent / path).exists()

    # -- updates -------------------------------------------------------------

    def record(
        self,
        session_id: int,
        moment_id: int,
        file: Path | str,
        size: int,
        timestamp_ms: int | None = None,
        score: float | None = None,
        camera: str | None = None,
        resolution: int | None = None,
    ) -> None:
        rel = os.path.relpath(Path(file), self.path.parent)
        sess = self._data["sessions"].setdefault(str(session_id), {"moments": {}})
        existing = sess.get("camera")
        if camera and existing and existing != camera:
            raise StorageError("session id collision between cameras")
        if camera:
            sess["camera"] = camera
        entry = {
            "file": rel,
            "size": size,
            "timestamp_ms": timestamp_ms,
            "score": score,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if resolution is not None:
            entry["resolution"] = resolution
        sess["moments"][str(moment_id)] = entry

    def file_path(self, session_id: int, moment_id: int) -> Path | None:
        entry = self.get(session_id, moment_id)
        if not entry or not entry.get("file"):
            return None
        return self.path.parent / entry["file"]

    def forget(self, session_id: int, moment_id: int) -> bool:
        sess = self._data["sessions"].get(str(session_id))
        if not sess:
            return False
        return sess.get("moments", {}).pop(str(moment_id), None) is not None

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
                    raise StorageError("catalog changed on disk; reload and retry")
            self._data["revision"] = int(self._data.get("revision", 0)) + 1
            self._data["version"] = 1
            atomic_write_json(self.path, self._data, mode=0o600)

    @classmethod
    def rebuild(cls, path: Path | str) -> Catalog:
        """Move an invalid catalog aside and return an empty one at ``path``."""
        src = Path(path)
        if src.exists():
            bak = src.with_name(src.name + ".invalid")
            os.replace(src, bak)
        return cls(src)
