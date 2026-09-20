import os
import threading

import pytest
from fake_lens import FakeLens

from openclips.camera import Camera
from openclips.catalog import Catalog
from openclips.errors import CameraError, WifiError
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
    return b"\xff\xd8\xff\xe0" + body


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
            raise CameraError("HTTP 500 on /fetch_moment: Moment could not be opened")
        if calls["n"] == 3:
            return b"not a jpeg"
        return b"\xff\xd8\xff\xe0ok"

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
