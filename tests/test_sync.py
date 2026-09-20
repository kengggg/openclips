import os
import threading
import urllib.error
import urllib.request

import pytest
from fake_lens import FakeLens

from openclips import constants as C
from openclips.camera import Camera
from openclips.catalog import Catalog
from openclips.errors import CameraError, HttpError, WifiError
from openclips.jpeg import structural_jpeg, validate_jpeg
from openclips.sync import (
    STAGE_DONE,
    STAGE_DOWNLOAD,
    STAGE_FAILED,
    STAGE_LISTING,
    STAGE_PLANNED,
    STAGE_SAVED,
    STAGE_SKIPPED,
    STAGE_WIFI_JOIN,
    STAGE_WIFI_JOINED,
    STAGE_WIFI_LEAVE,
    STAGE_WIFI_REQUEST,
    Syncer,
)
from openclips.wifi import ManualWifi, NullWifi

SID, OLD = 1789825799491786000, 1789700000000000000


class RecordingWifi:
    def __init__(self, ok=True):
        self.ok, self.joined, self.left = ok, [], 0

    def join(self, creds):
        self.joined.append(creds.ssid)
        return self.ok

    def leave(self):
        self.left += 1


def fake_http(url, path, body, timeout=30.0):
    assert path == "/fetch_moment"
    return b"wrap" + structural_jpeg()


@pytest.fixture
def cam(monkeypatch):
    lens = FakeLens(pairing_key=os.urandom(32), sessions={SID: [2, 3, 1], OLD: [7]}, clock_ms=1789825818164)
    cam = Camera(lens, lens.pairing_key)
    cam.resume()
    monkeypatch.setattr(Camera, "http_post", staticmethod(fake_http))
    cam.lens = lens
    return cam


def test_plan_picks_newest_session_with_moments(cam, tmp_path):
    s = Syncer(cam, tmp_path, NullWifi())
    plan = s.plan()
    assert [i.moment_id for i in plan.items] == [2, 3, 1]
    assert plan.items[0].path == tmp_path / str(SID) / "moment_2.jpg"
    assert plan.items[0].moment.timestamp_ms == 1789825818164
    assert plan.sessions_seen == [SID]
    assert Syncer(cam, tmp_path, NullWifi()).plan(all_sessions=True).total == 4


def test_plan_skips_open_session_and_empty_sessions(cam, tmp_path):
    cam.lens.open_session = 999
    cam.lens.sessions[SID + 1] = []
    plan = Syncer(cam, tmp_path, NullWifi()).plan()
    assert plan.open_session == 999
    assert plan.sessions_seen == [SID + 1, SID] and plan.total == 3


def test_plan_explicit_moments_needs_no_listing(cam, tmp_path):
    seen = []
    s = Syncer(cam, tmp_path, NullWifi(), on_progress=lambda p: seen.append(p.stage))
    plan = s.plan(session_id=SID, moment_ids=[9])
    assert [i.moment_id for i in plan.items] == [9]
    assert STAGE_LISTING not in seen and seen == [STAGE_PLANNED]


def test_run_downloads_catalogs_and_reports(cam, tmp_path):
    wifi = RecordingWifi()
    stages = []
    s = Syncer(cam, tmp_path, wifi, on_progress=lambda p: stages.append(p.stage))
    result = s.sync()
    assert result.ok and len(result.downloaded) == 3
    for item in result.downloaded:
        assert item.path.exists() and item.path.read_bytes()[:3] == b"\xff\xd8\xff"
    assert wifi.joined == ["Clips6013"] and wifi.left == 1
    assert not cam.lens.wifi_open  # CANCEL_WIFI sent after leaving
    assert stages[:2] == [STAGE_LISTING, STAGE_LISTING]
    for st in (STAGE_PLANNED, STAGE_WIFI_REQUEST, STAGE_WIFI_JOIN, STAGE_WIFI_JOINED, STAGE_WIFI_LEAVE, STAGE_DONE):
        assert st in stages
    assert stages.count(STAGE_DOWNLOAD) == 3 and stages.count(STAGE_SAVED) == 3
    assert stages.index(STAGE_WIFI_LEAVE) < stages.index(STAGE_DONE)
    cat = Catalog.for_dir(tmp_path)
    assert cat.has(SID, 2) and cat.get(SID, 2)["timestamp_ms"] == 1789825818164
    assert cat.get(SID, 2)["file"] == f"{SID}/moment_2.jpg"

    # second run: everything is skipped, no Wi-Fi touched
    stages.clear()
    result2 = Syncer(cam, tmp_path, wifi, on_progress=lambda p: stages.append(p.stage)).sync()
    assert result2.ok and not result2.downloaded and len(result2.skipped) == 3
    assert wifi.joined == ["Clips6013"] and STAGE_SKIPPED in stages and STAGE_WIFI_REQUEST not in stages


def test_overwrite_ignores_catalog(cam, tmp_path):
    Syncer(cam, tmp_path, NullWifi()).sync()
    plan = Syncer(cam, tmp_path, NullWifi(), overwrite=True).plan()
    assert plan.total == 3


def test_wifi_join_failure_raises_and_leaves(cam, tmp_path):
    wifi = RecordingWifi(ok=False)
    with pytest.raises(WifiError):
        Syncer(cam, tmp_path, wifi).sync()
    assert wifi.left == 1 and not cam.lens.wifi_open


def test_http_failure_is_reported_not_raised(cam, tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(url, path, body, timeout=30.0):
        calls["n"] += 1
        if calls["n"] == 2:
            raise HttpError("/fetch_moment", status=500, retryable=False)
        if calls["n"] == 3:
            return b"not a jpeg"
        return structural_jpeg()

    monkeypatch.setattr(Camera, "http_post", staticmethod(flaky))
    stages = []
    result = Syncer(cam, tmp_path, NullWifi(), on_progress=lambda p: stages.append(p.stage)).sync()
    assert not result.ok and len(result.downloaded) == 1 and len(result.failed) == 2
    assert stages.count(STAGE_FAILED) == 2
    assert "HTTP 500" in result.failed[0][1] and "not a JPEG" in result.failed[1][1]


def test_cancel_between_downloads(cam, tmp_path):
    s = Syncer(cam, tmp_path, NullWifi())

    def on_progress(p):
        if p.stage == STAGE_SAVED and p.index == 1:
            threading.Thread(target=s.cancel).start()

    s.on_progress = on_progress
    result = s.sync()
    assert result.cancelled and len(result.downloaded) == 1
    assert not (tmp_path / str(SID) / "moment_1.jpg").exists()


def test_manual_wifi_prompts_user(cam, tmp_path):
    asked, left = [], []
    wifi = ManualWifi(lambda creds: asked.append(creds.passphrase) or True, on_leave=lambda: left.append(1))
    result = Syncer(cam, tmp_path, wifi, use_catalog=False).sync(session_id=SID, moment_ids=[2])
    assert result.ok and asked == ["0123456789abcdef"] and left == [1]
    assert not (tmp_path / ".openclips-catalog.json").exists()


def test_custom_path_layout(cam, tmp_path):
    def by_time(out, m):
        return out / f"{m.datetime:%Y-%m-%d}" / f"{m.moment_id}.jpg"

    result = Syncer(cam, tmp_path, NullWifi(), path_for=by_time).sync(session_id=SID)
    assert result.ok and (tmp_path / "2026-09-19" / "2.jpg").exists()


def test_listing_error_does_not_hide_other_session(cam, tmp_path, monkeypatch):
    orig = Camera.moments

    def maybe(self, session_id, timeout=20.0):
        if session_id == SID:
            raise CameraError("LIST_MOMENTS")
        return orig(self, session_id, timeout)

    monkeypatch.setattr(Camera, "moments", maybe)
    plan = Syncer(cam, tmp_path, NullWifi()).plan(all_sessions=True)
    assert plan.listing_errors and OLD in plan.sessions_seen
    assert any(i.moment_id == 7 for i in plan.items)
    result = Syncer(cam, tmp_path, NullWifi()).run(plan)
    assert result.listing_errors and not result.ok


def test_listing_error_is_not_nothing_new(cam, tmp_path, monkeypatch):
    def boom(self, session_id, timeout=20.0):
        raise CameraError("LIST_MOMENTS")

    monkeypatch.setattr(Camera, "moments", boom)
    plan = Syncer(cam, tmp_path, NullWifi()).plan(all_sessions=True)
    assert plan.listing_errors and not plan.items
    result = Syncer(cam, tmp_path, NullWifi()).run(plan)
    assert not result.ok and result.listing_errors
    assert result.downloaded == []


def test_truncated_and_orphan_files(cam, tmp_path):
    jpeg = structural_jpeg()
    dest = tmp_path / str(SID) / "moment_2.jpg"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(jpeg[:20])
    plan = Syncer(cam, tmp_path, NullWifi()).plan(session_id=SID)
    assert any(i.moment_id == 2 for i in plan.items)

    dest.write_bytes(jpeg)
    s = Syncer(cam, tmp_path, NullWifi())
    plan = s.plan(session_id=SID)
    assert 2 not in [i.moment_id for i in plan.items]
    assert Catalog.for_dir(tmp_path).has(SID, 2)


def test_catalog_wrong_size_and_wrong_path_are_planned(cam, tmp_path):
    jpeg = structural_jpeg()
    dest = tmp_path / str(SID) / "moment_2.jpg"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(jpeg)
    cat = Catalog.for_dir(tmp_path)
    cat.record(SID, 2, dest, size=999)
    cat.save()
    plan = Syncer(cam, tmp_path, NullWifi()).plan(session_id=SID)
    assert any(i.moment_id == 2 for i in plan.items)

    other = tmp_path / "other.jpg"
    other.write_bytes(jpeg)
    cat.record(SID, 3, other, size=len(jpeg))
    cat.save()
    wanted = tmp_path / str(SID) / "moment_3.jpg"
    plan = Syncer(cam, tmp_path, NullWifi()).plan(session_id=SID)
    assert any(i.path == wanted for i in plan.items)

    cat.record(SID, 1, tmp_path / str(SID) / "moment_1.jpg", size=len(jpeg), resolution=C.RESOLUTION_THUMB)
    (tmp_path / str(SID) / "moment_1.jpg").write_bytes(jpeg)
    cat.save()
    plan = Syncer(cam, tmp_path, NullWifi(), resolution=C.RESOLUTION_FULL).plan(session_id=SID)
    assert any(i.moment_id == 1 for i in plan.items)


def test_cancel_after_plan_does_not_start_wifi(cam, tmp_path):
    wifi = RecordingWifi()
    s = Syncer(cam, tmp_path, wifi)
    plan = s.plan()
    assert plan.items
    s.cancel()
    result = s.run(plan)
    assert result.cancelled and not result.ok
    assert wifi.joined == [] and wifi.left == 0


def test_retry_stops_at_budget_permanent_not_retried(cam, tmp_path, monkeypatch):
    calls = {"n": 0}

    def transient(url, path, body, timeout=30.0):
        calls["n"] += 1
        raise HttpError("/fetch_moment", status=503, retryable=True)

    monkeypatch.setattr(Camera, "http_post", staticmethod(transient))
    result = Syncer(cam, tmp_path, NullWifi(), http_retries=2, http_retry_backoff=0).sync(
        session_id=SID, moment_ids=[2]
    )
    assert not result.ok and calls["n"] == 3
    calls["n"] = 0

    def permanent(url, path, body, timeout=30.0):
        calls["n"] += 1
        raise HttpError("/fetch_moment", status=500, retryable=False)

    monkeypatch.setattr(Camera, "http_post", staticmethod(permanent))
    result = Syncer(cam, tmp_path, NullWifi(), http_retries=4, http_retry_backoff=0).sync(
        session_id=SID, moment_ids=[2]
    )
    assert calls["n"] == 1 and result.failed


def test_http_error_omits_response_body(monkeypatch):
    sentinel = "SYNTHETIC_SECRET_SENTINEL_http"

    class FakeResp:
        def read(self):
            return sentinel.encode()

    def boom(req, timeout=30.0):
        err = urllib.error.HTTPError("http://192.168.49.10:8080/fetch_moment", 500, "err", hdrs=None, fp=None)
        err.read = FakeResp().read
        raise err

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(HttpError) as ei:
        Camera.http_post("http://192.168.49.10:8080", "/fetch_moment", b"x")
    assert ei.value.status == 500
    assert sentinel not in str(ei.value)
    assert "HTTP 500" in str(ei.value)


def test_catalog_checkpoint_failure_keeps_image(cam, tmp_path, monkeypatch):
    jpeg = structural_jpeg()
    monkeypatch.setattr(Camera, "http_post", staticmethod(lambda *a, **k: jpeg))
    s = Syncer(cam, tmp_path, NullWifi())

    def fail_save(self):
        raise OSError("disk")

    monkeypatch.setattr(Catalog, "save", fail_save)
    result = s.sync(session_id=SID, moment_ids=[2])
    saved = tmp_path / str(SID) / "moment_2.jpg"
    assert saved.exists() and validate_jpeg(saved.read_bytes())
    assert not result.ok and result.catalog_errors
    assert result.downloaded


def test_keep_network_skips_leave_on_success_only(cam, tmp_path):
    wifi = RecordingWifi()
    result = Syncer(cam, tmp_path, wifi, keep_network=True).sync(session_id=SID, moment_ids=[2])
    assert result.ok and wifi.joined and wifi.left == 0
    assert cam.lens.wifi_open  # CANCEL_WIFI not sent
    wifi2 = RecordingWifi(ok=False)
    with pytest.raises(WifiError):
        Syncer(cam, tmp_path, wifi2, keep_network=True, overwrite=True).sync(session_id=SID, moment_ids=[2])
    assert wifi2.left == 1


def test_cleanup_runs_after_softap_even_if_join_fails(cam, tmp_path):
    wifi = RecordingWifi(ok=False)
    with pytest.raises(WifiError):
        Syncer(cam, tmp_path, wifi).sync(session_id=SID, moment_ids=[2])
    assert wifi.left == 1 and not cam.lens.wifi_open


def test_previous_valid_destination_survives_failed_overwrite(cam, tmp_path, monkeypatch):
    jpeg = structural_jpeg()
    dest = tmp_path / str(SID) / "moment_2.jpg"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(jpeg)
    monkeypatch.setattr(Camera, "http_post", staticmethod(lambda *a, **k: b"not-a-jpeg"))
    result = Syncer(cam, tmp_path, NullWifi(), overwrite=True).sync(session_id=SID, moment_ids=[2])
    assert not result.ok
    assert dest.read_bytes() == jpeg
    assert not list(dest.parent.glob(".openclips-*.part"))
