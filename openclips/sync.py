"""Download moments from the camera: plan, fetch, catalog, report progress.

Shared by the CLI and any GUI. Everything blocking happens on the caller's
thread; progress is reported through a callback and the run can be
cancelled between downloads from another thread.

::

    syncer = Syncer(cam, "~/Pictures/clips", wifi=NmcliWifi(), on_progress=print)
    plan = syncer.plan()             # newest completed session with moments
    result = syncer.run(plan)        # SoftAP up, join, download, restore Wi-Fi
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import constants as C
from .camera import Camera, extract_jpeg
from .catalog import Catalog
from .errors import CameraError, HttpError, StorageError, WifiError
from .jpeg import validate_jpeg
from .persist import exclusive_lock
from .proto import MomentInfo
from .wifi import WifiCredentials, WifiJoiner

logger = logging.getLogger(__name__)

# Progress stages, in the order an app will see them.
STAGE_LISTING = "listing"
STAGE_PLANNED = "planned"
STAGE_WIFI_REQUEST = "wifi_request"
STAGE_WIFI_JOIN = "wifi_join"
STAGE_WIFI_JOINED = "wifi_joined"
STAGE_DOWNLOAD = "download"
STAGE_SAVED = "saved"
STAGE_CATALOGED = "cataloged"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"
STAGE_WIFI_LEAVE = "wifi_leave"
STAGE_CANCELLED = "cancelled"
STAGE_DONE = "done"


class _Cancelled(Exception):
    """Internal: stop the current fetch/run without treating it as a failed item."""


@dataclass
class SyncItem:
    moment: MomentInfo
    path: Path

    @property
    def session_id(self) -> int:
        return self.moment.session_id

    @property
    def moment_id(self) -> int:
        return self.moment.moment_id


@dataclass
class SyncPlan:
    items: list[SyncItem] = field(default_factory=list)
    skipped: list[SyncItem] = field(default_factory=list)
    sessions_seen: list[int] = field(default_factory=list)
    open_session: int | None = None
    listing_errors: list[tuple[int | None, str]] = field(default_factory=list)
    unreadable_sessions: list[int] = field(default_factory=list)
    empty_sessions: list[int] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total(self) -> int:
        return len(self.items)


@dataclass
class SyncProgress:
    stage: str
    message: str = ""
    item: SyncItem | None = None
    index: int = 0
    total: int = 0
    bytes: int = 0
    credentials: WifiCredentials | None = None


@dataclass
class SyncResult:
    downloaded: list[SyncItem] = field(default_factory=list)
    failed: list[tuple[SyncItem, str]] = field(default_factory=list)
    skipped: list[SyncItem] = field(default_factory=list)
    cancelled: bool = False
    credentials: WifiCredentials | None = None
    listing_errors: list[tuple[int | None, str]] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    catalog_errors: list[str] = field(default_factory=list)
    aborted: str | None = None

    @property
    def ok(self) -> bool:
        return (
            not self.failed
            and not self.cancelled
            and not self.listing_errors
            and not self.cleanup_errors
            and not self.catalog_errors
            and self.aborted is None
        )


ProgressCallback = Callable[[SyncProgress], None]


def default_path(out_dir: Path, moment: MomentInfo) -> Path:
    """``<out>/<session_id>/moment_<id>.jpg``."""
    return Path(out_dir) / str(moment.session_id) / f"moment_{moment.moment_id}.jpg"


class Syncer:
    def __init__(
        self,
        camera: Camera,
        out_dir: Path | str,
        wifi: WifiJoiner,
        *,
        catalog: Catalog | None = None,
        use_catalog: bool = True,
        on_progress: ProgressCallback | None = None,
        overwrite: bool = False,
        resolution: int = C.RESOLUTION_FULL,
        path_for: Callable[[Path, MomentInfo], Path] = default_path,
        hold_preview: bool = True,
        http_timeout: float = 30.0,
        http_retries: int = 2,
        http_retry_backoff: float = 0.2,
        keep_network: bool = False,
    ):
        self.camera = camera
        self.out_dir = Path(out_dir).expanduser()
        self.wifi = wifi
        self.catalog = catalog if catalog is not None else (Catalog.for_dir(self.out_dir) if use_catalog else None)
        self.on_progress = on_progress or (lambda p: None)
        self.overwrite = overwrite
        self.resolution = resolution
        self.path_for = path_for
        self.hold_preview = hold_preview
        self.http_timeout = http_timeout
        self.http_retries = max(0, int(http_retries))
        self.http_retry_backoff = max(0.0, float(http_retry_backoff))
        self.keep_network = keep_network
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ helpers

    def _report(self, stage: str, **kw) -> None:
        try:
            self.on_progress(SyncProgress(stage, **kw))
        except Exception:
            logger.exception("progress callback failed")

    def cancel(self) -> None:
        """Stop after the current download. Safe from any thread.

        A cancel issued after :meth:`plan` is honoured by :meth:`run` and does
        not start Wi-Fi. It does not interrupt a blocking HTTP read already in
        flight.
        """
        self._cancel.set()

    def _usable_existing(self, item: SyncItem) -> bool:
        """True if the requested path is a regular, structurally valid image we can skip."""
        path = item.path
        try:
            st = path.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        try:
            data = path.read_bytes()
        except OSError:
            return False
        if not validate_jpeg(data):
            return False
        if self.catalog is not None:
            entry = self.catalog.get(item.session_id, item.moment_id)
            if entry:
                cat_path = self.catalog.file_path(item.session_id, item.moment_id)
                if cat_path is not None and cat_path.resolve() != path.resolve():
                    return False
                recorded = entry.get("size")
                if recorded is not None and int(recorded) != st.st_size:
                    return False
                recorded_res = entry.get("resolution")
                if recorded_res is not None and int(recorded_res) != self.resolution:
                    return False
        return True

    def _have(self, item: SyncItem) -> bool:
        if self.overwrite:
            return False
        return self._usable_existing(item)

    def _reconcile(self, item: SyncItem) -> None:
        if self.catalog is None:
            return
        try:
            size = item.path.stat().st_size
            self.catalog.record(
                item.session_id,
                item.moment_id,
                item.path,
                size,
                timestamp_ms=item.moment.timestamp_ms,
                score=item.moment.score,
                resolution=self.resolution,
            )
            self.catalog.save()
        except OSError as e:
            logger.warning("catalog reconcile failed for %s: %s", item.path.name, e)

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        session_id: int | None = None,
        moment_ids: list[int] | None = None,
        all_sessions: bool = False,
        max_sessions: int = 20,
    ) -> SyncPlan:
        """Decide what to download.

        Default: the newest completed session that has moments. With
        ``all_sessions`` every completed session is included. ``session_id``
        restricts to one session; ``moment_ids`` additionally restricts the
        moments (no listing needed).
        """
        plan = SyncPlan()
        cam = self.camera
        if session_id is not None and moment_ids:
            infos = [MomentInfo(session_id, m) for m in moment_ids]
            self._add(plan, infos)
            self._report(STAGE_PLANNED, total=plan.total)
            return plan

        self._report(STAGE_LISTING, message="listing sessions")
        if self._cancel.is_set():
            plan.cancelled = True
            self._report(STAGE_PLANNED, total=0, message="cancelled")
            return plan
        if session_id is not None:
            sids = [session_id]
        else:
            try:
                s = cam.list_sessions(max_sessions)
            except CameraError as e:
                plan.listing_errors.append((None, str(e)))
                self._report(STAGE_PLANNED, total=0, message="session listing failed")
                return plan
            plan.open_session = s.get("open_session")
            sids = sorted(s["session_ids"], reverse=True)
            if plan.open_session in sids:
                sids.remove(plan.open_session)
        for sid in sids:
            if self._cancel.is_set():
                plan.cancelled = True
                break
            self._report(STAGE_LISTING, message=f"listing session {sid}")
            try:
                infos = cam.moments(sid)
            except CameraError as e:
                logger.warning("session %s: %s", sid, e)
                plan.listing_errors.append((sid, str(e)))
                plan.unreadable_sessions.append(sid)
                continue
            plan.sessions_seen.append(sid)
            if not infos:
                plan.empty_sessions.append(sid)
                continue
            self._add(plan, infos)
            if not all_sessions and session_id is None:
                break
        self._report(
            STAGE_PLANNED, total=plan.total, message=f"{plan.total} to download, {len(plan.skipped)} already present"
        )
        return plan

    def _add(self, plan: SyncPlan, infos: list[MomentInfo]) -> None:
        for info in infos:
            item = SyncItem(info, self.path_for(self.out_dir, info))
            if self._have(item):
                plan.skipped.append(item)
                self._reconcile(item)
            else:
                plan.items.append(item)

    # ------------------------------------------------------------------ run

    def run(self, plan: SyncPlan) -> SyncResult:
        """Open the SoftAP, join, download every planned item, restore Wi-Fi."""
        result = SyncResult(skipped=list(plan.skipped), listing_errors=list(plan.listing_errors))
        for item in plan.skipped:
            self._report(STAGE_SKIPPED, item=item)
        if plan.cancelled or self._cancel.is_set():
            result.cancelled = True
            self._report(STAGE_CANCELLED, total=plan.total)
            return result
        if not plan.items:
            self._report(STAGE_DONE, total=0)
            return result
        lock_path = self.out_dir / ".openclips-sync.lock"
        with exclusive_lock(lock_path, timeout=5.0):
            return self._run_locked(plan, result)

    def _run_locked(self, plan: SyncPlan, result: SyncResult) -> SyncResult:
        cam = self.camera
        with cam.keepalive():
            self._report(STAGE_WIFI_REQUEST)
            if self._cancel.is_set():
                result.cancelled = True
                self._report(STAGE_CANCELLED, total=plan.total)
                return result
            creds = cam.initiate_wifi()
            if creds is None:
                raise WifiError("camera did not return SoftAP credentials")
            result.credentials = creds
            joined = False
            try:
                if self.hold_preview:
                    try:
                        cam.start_capture_preview()
                    except CameraError:
                        pass
                if self._cancel.is_set():
                    result.cancelled = True
                    self._report(STAGE_CANCELLED, total=plan.total)
                    return result
                self._report(STAGE_WIFI_JOIN, credentials=creds, message="joining camera network")
                try:
                    joined = bool(self.wifi.join(creds))
                except Exception as e:
                    raise WifiError("could not join camera network") from e
                if not joined:
                    raise WifiError("could not join camera network")
                self._report(STAGE_WIFI_JOINED, credentials=creds)
                self._download_all(plan, result, creds)
            finally:
                keep = self.keep_network and result.ok and not result.cancelled and joined
                if keep:
                    logger.info("keeping camera network (best effort; the SoftAP may still end)")
                else:
                    self._cleanup_wifi(cam, result)
        self._report(
            STAGE_CANCELLED if result.cancelled else STAGE_DONE, total=plan.total, index=len(result.downloaded)
        )
        return result

    def _cleanup_wifi(self, cam: Camera, result: SyncResult) -> None:
        self._report(STAGE_WIFI_LEAVE)
        try:
            self.wifi.leave()
        except Exception as e:
            result.cleanup_errors.append(f"leave:{type(e).__name__}")
            logger.debug("wifi.leave failed", exc_info=True)
        try:
            cam.cancel_wifi()
        except CameraError as e:
            result.cleanup_errors.append(f"cancel_wifi:{type(e).__name__}")
            logger.debug("cancel_wifi failed: %s", e)
        extra = getattr(self.wifi, "leave_errors", None)
        if extra:
            result.cleanup_errors.extend(extra)

    def _fetch(self, item: SyncItem, creds: WifiCredentials) -> bytes:
        attempts = self.http_retries + 1
        last: Exception | None = None
        for n in range(attempts):
            if self._cancel.is_set():
                raise _Cancelled
            try:
                return self.camera.fetch_moment_http(
                    item.session_id,
                    item.moment_id,
                    url=creds.url,
                    resolution=self.resolution,
                    timeout=self.http_timeout,
                )
            except HttpError as e:
                last = e
                if not e.retryable or n + 1 >= attempts:
                    raise
            except CameraError:
                raise
            if self.http_retry_backoff:
                if self._cancel.wait(self.http_retry_backoff * (n + 1)):
                    raise _Cancelled
        raise last or CameraError("fetch failed")

    def _commit_file(self, item: SyncItem, jpeg: bytes) -> None:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".openclips-", suffix=".part", dir=str(item.path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                fd = -1
                f.write(jpeg)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, item.path)
            tmp = ""
        except OSError as e:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if e.errno in (errno.ENOSPC, errno.EDQUOT):
                raise StorageError("output disk is full") from e
            raise
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _download_all(self, plan: SyncPlan, result: SyncResult, creds: WifiCredentials) -> None:
        total = plan.total
        for i, item in enumerate(plan.items, 1):
            if self._cancel.is_set():
                result.cancelled = True
                return
            self._report(STAGE_DOWNLOAD, item=item, index=i, total=total)
            try:
                data = self._fetch(item, creds)
            except _Cancelled:
                result.cancelled = True
                return
            except CameraError as e:
                result.failed.append((item, str(e)))
                self._report(STAGE_FAILED, item=item, index=i, total=total, message=str(e))
                continue
            jpeg = extract_jpeg(data)
            if not jpeg or not validate_jpeg(jpeg):
                msg = f"{len(data)} bytes, not a JPEG"
                result.failed.append((item, msg))
                self._report(STAGE_FAILED, item=item, index=i, total=total, message=msg)
                continue
            try:
                self._commit_file(item, jpeg)
            except StorageError as e:
                result.failed.append((item, str(e)))
                result.aborted = str(e)
                self._report(STAGE_FAILED, item=item, index=i, total=total, message=str(e))
                return
            except OSError as e:
                result.failed.append((item, f"save failed:{type(e).__name__}"))
                self._report(STAGE_FAILED, item=item, index=i, total=total, message=type(e).__name__)
                continue
            self._report(STAGE_SAVED, item=item, index=i, total=total, bytes=len(jpeg))
            if self.catalog is not None:
                try:
                    self.catalog.record(
                        item.session_id,
                        item.moment_id,
                        item.path,
                        len(jpeg),
                        timestamp_ms=item.moment.timestamp_ms,
                        score=item.moment.score,
                        resolution=self.resolution,
                    )
                    self.catalog.save()
                    self._report(STAGE_CATALOGED, item=item, index=i, total=total, bytes=len(jpeg))
                except OSError as e:
                    result.catalog_errors.append(f"{item.moment_id}:{type(e).__name__}")
                    logger.warning("catalog checkpoint failed after saving %s", item.path.name)
            result.downloaded.append(item)

    def sync(self, **plan_kwargs) -> SyncResult:
        """``plan()`` then ``run()``. Clears a previous cancel at the start."""
        self._cancel.clear()
        return self.run(self.plan(**plan_kwargs))
