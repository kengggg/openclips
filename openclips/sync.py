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

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import constants as C
from .camera import Camera, extract_jpeg
from .catalog import Catalog
from .errors import CameraError, WifiError
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
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"
STAGE_WIFI_LEAVE = "wifi_leave"
STAGE_CANCELLED = "cancelled"
STAGE_DONE = "done"


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

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled


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
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ helpers

    def _report(self, stage: str, **kw) -> None:
        try:
            self.on_progress(SyncProgress(stage, **kw))
        except Exception:
            logger.exception("progress callback failed")

    def cancel(self) -> None:
        """Stop after the current download. Safe from any thread."""
        self._cancel.set()

    def _have(self, item: SyncItem) -> bool:
        if self.overwrite:
            return False
        if self.catalog is not None and self.catalog.has(item.session_id, item.moment_id):
            return True
        return item.path.exists()

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
        if session_id is not None:
            sids = [session_id]
        else:
            s = cam.list_sessions(max_sessions)
            plan.open_session = s.get("open_session")
            sids = sorted(s["session_ids"], reverse=True)
            if plan.open_session in sids:
                sids.remove(plan.open_session)
        for sid in sids:
            self._report(STAGE_LISTING, message=f"listing session {sid}")
            try:
                infos = cam.moments(sid)
            except CameraError as e:
                logger.warning("session %s: %s", sid, e)
                continue
            plan.sessions_seen.append(sid)
            if infos:
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
            (plan.skipped if self._have(item) else plan.items).append(item)

    # ------------------------------------------------------------------ run

    def run(self, plan: SyncPlan) -> SyncResult:
        """Open the SoftAP, join, download every planned item, restore Wi-Fi."""
        result = SyncResult(skipped=list(plan.skipped))
        for item in plan.skipped:
            self._report(STAGE_SKIPPED, item=item)
        if not plan.items:
            self._report(STAGE_DONE, total=0)
            return result
        self._cancel.clear()
        cam = self.camera
        with cam.keepalive():
            self._report(STAGE_WIFI_REQUEST)
            creds = cam.initiate_wifi()
            if creds is None:
                raise WifiError("camera did not return SoftAP credentials")
            result.credentials = creds
            if self.hold_preview:
                try:
                    cam.start_capture_preview()  # helps hold the group owner; FAILURE is harmless
                except CameraError:
                    pass
            self._report(STAGE_WIFI_JOIN, credentials=creds, message=f"joining {creds.ssid}")
            joined = False
            try:
                joined = self.wifi.join(creds)
                if not joined:
                    raise WifiError(f"could not join {creds.ssid}")
                self._report(STAGE_WIFI_JOINED, credentials=creds)
                self._download_all(plan, result, creds)
            finally:
                self._report(STAGE_WIFI_LEAVE)
                try:
                    self.wifi.leave()
                except Exception:
                    logger.debug("wifi.leave failed", exc_info=True)
                try:
                    cam.cancel_wifi()
                except CameraError:
                    pass
        if self.catalog is not None:
            self.catalog.save()
        self._report(
            STAGE_CANCELLED if result.cancelled else STAGE_DONE, total=plan.total, index=len(result.downloaded)
        )
        return result

    def _download_all(self, plan: SyncPlan, result: SyncResult, creds: WifiCredentials) -> None:
        total = plan.total
        for i, item in enumerate(plan.items, 1):
            if self._cancel.is_set():
                result.cancelled = True
                return
            self._report(STAGE_DOWNLOAD, item=item, index=i, total=total)
            try:
                data = self.camera.fetch_moment_http(
                    item.session_id,
                    item.moment_id,
                    url=creds.url,
                    resolution=self.resolution,
                    timeout=self.http_timeout,
                )
            except CameraError as e:
                result.failed.append((item, str(e)))
                self._report(STAGE_FAILED, item=item, index=i, total=total, message=str(e))
                continue
            jpeg = extract_jpeg(data)
            if not jpeg:
                msg = f"{len(data)} bytes, not a JPEG"
                result.failed.append((item, msg))
                self._report(STAGE_FAILED, item=item, index=i, total=total, message=msg)
                continue
            item.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = item.path.with_suffix(".part")
            tmp.write_bytes(jpeg)
            tmp.replace(item.path)
            if self.catalog is not None:
                self.catalog.record(
                    item.session_id,
                    item.moment_id,
                    item.path,
                    len(jpeg),
                    timestamp_ms=item.moment.timestamp_ms,
                    score=item.moment.score,
                )
            result.downloaded.append(item)
            self._report(STAGE_SAVED, item=item, index=i, total=total, bytes=len(jpeg))

    def sync(self, **plan_kwargs) -> SyncResult:
        """``plan()`` then ``run()``."""
        return self.run(self.plan(**plan_kwargs))
