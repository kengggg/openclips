"""Local index of downloaded moments.

Kept next to the photos as ``.openclips-catalog.json`` so an app knows what
it already has without asking the camera or hashing files. Session ids are
unique across cameras (they are nanosecond clock values), so the catalog is
keyed by session then moment.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

CATALOG_NAME = ".openclips-catalog.json"


class Catalog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._data: dict = {"version": 1, "sessions": {}}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and "sessions" in loaded:
                self._data = loaded

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
    ) -> None:
        rel = os.path.relpath(Path(file), self.path.parent)
        sess = self._data["sessions"].setdefault(str(session_id), {"moments": {}})
        if camera:
            sess["camera"] = camera
        sess["moments"][str(moment_id)] = {
            "file": rel,
            "size": size,
            "timestamp_ms": timestamp_ms,
            "score": score,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def forget(self, session_id: int, moment_id: int) -> bool:
        sess = self._data["sessions"].get(str(session_id))
        if not sess:
            return False
        return sess.get("moments", {}).pop(str(moment_id), None) is not None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
