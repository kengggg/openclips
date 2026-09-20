"""Atomic JSON writes and interprocess locks for pairing/catalog files."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import StorageError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def atomic_write_json(path: Path | str, data: dict, *, mode: int = 0o600) -> None:
    """Write JSON via an exclusive temp file. Mode is set before contents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".openclips-", suffix=".tmp", dir=str(path.parent))
    tmp_path = tmp
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
        try:
            dirfd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@contextmanager
def exclusive_lock(path: Path | str, timeout: float = 5.0) -> Iterator[None]:
    """Best-effort exclusive lock next to ``path``. POSIX flock; errors on Windows."""
    if fcntl is None:
        yield
        return
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StorageError("store is busy; reload and retry") from None
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
