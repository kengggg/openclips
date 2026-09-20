"""End-to-end Camera behaviour against the in-process fake lens."""

import os
import threading
import time

import pytest
from fake_lens import FakeLens, paired_pair

from openclips import constants as C
from openclips.camera import EVENT_DISCONNECTED, EVENT_NOTIFICATION, EVENT_STATE, Camera, extract_jpeg
from openclips.crypto import generate_host_key
from openclips.errors import CameraAsleep, ConnectionLost, NotPaired, PairingKeyMismatch, UnsafeRequest
from openclips.pb import parse_pb, pb_bytes, pb_uint

SID = 1789825799491786000


def make_paired(**kw):
    lens, key = paired_pair()
    for k, v in kw.items():
        setattr(lens, k, v)
    cam = Camera(lens, key)
    return lens, cam


def test_pair_then_resume_in_setup_mode():
    lens = FakeLens(setup_mode=True, system_state=C.SYSTEM_STATE_SETUP)
    cam = Camera(lens)
    host = generate_host_key()
    pk, lens_pub = cam.pair(host, name="test-host")
    assert pk == lens.pairing_key and len(lens_pub) == 64
    state = cam.resume()
    assert state.system_state_name == "SETUP"
    types = [t for t, _ in lens.requests]
    assert types[:5] == [C.RT_PUBLIC_QUERY, C.RT_INITIATE_PAIRING, C.RT_ISC, C.RT_CSC, C.RT_DISABLE_TIMEOUTS]


def test_pair_times_out_when_not_in_setup_mode():
    lens = FakeLens(asleep=True)
    cam = Camera(lens)
    with pytest.raises(CameraAsleep):
        cam.pair(generate_host_key(), timeout=0.3)


def test_resume_requires_key_and_detects_sleep():
    lens, _ = paired_pair()
    with pytest.raises(NotPaired):
        Camera(lens).resume()
    lens.asleep = True
    with pytest.raises(CameraAsleep):
        Camera(lens, os.urandom(32)).resume(timeout=0.3)


def test_resume_rejects_stale_pairing_key():
    lens, _ = paired_pair()
    with pytest.raises(PairingKeyMismatch):
        Camera(lens, os.urandom(32)).resume()


def test_resume_parses_state_and_seq_continues():
    lens, cam = make_paired(cover_open=True)
    st = cam.resume()
    assert st.system_state_name == "IDLE" and st.cover_open is True and st.battery_pct == 77
    assert cam.sess is not None and lens.sess is not None
    assert cam._seq == 3  # ISC, CSC, DISABLE_TIMEOUTS


def test_encrypted_requests_never_leave_without_session():
    lens, cam = make_paired()
    with pytest.raises(NotPaired):
        cam.list_sessions()


def test_unsafe_requests_are_refused():
    lens, cam = make_paired()
    cam.resume()
    with pytest.raises(UnsafeRequest):
        cam.request(C.RT_ACK_NEW_CONTENT)
    assert C.RT_ACK_NEW_CONTENT not in [t for t, _ in lens.requests]


def test_sessions_and_moments():
    lens, cam = make_paired(sessions={SID: [2, 3, 1, 9, 4, 5], 42: []}, clock_ms=1789825818164)
    cam.resume()
    s = cam.list_sessions()
    assert sorted(s["session_ids"]) == [42, SID] and s["open_session"] is None
    m = cam.list_moments(SID)
    assert m["moment_ids"] == [2, 3, 1, 9, 4, 5] and m["is_final"] == 1
    infos = m["moments"]
    assert [i.moment_id for i in infos] == [2, 3, 1, 9, 4, 5]
    assert infos[0].timestamp_ms == 1789825818164 and infos[0].datetime.year == 2026
    assert infos[0].score > infos[1].score > infos[-1].score
    assert cam.moments(SID)[0].session_id == SID
    assert cam.list_moments(42)["moment_ids"] == []
    assert cam.delete_moments(SID, [2, 3])
    assert lens.sessions[SID] == [1, 9, 4, 5]
    assert not cam.delete_moments(999, [1])


def test_open_session_is_reported_and_lists_empty():
    lens, cam = make_paired(open_session=SID, system_state=C.SYSTEM_STATE_CAPTURE, cover_open=True)
    st = cam.resume()
    assert st.session_uuid == SID and st.system_state_name == "CAPTURE"
    assert cam.list_sessions()["open_session"] == SID
    assert not cam.complete_session()  # cover open -> FAILURE
    lens.cover_open = False
    assert cam.complete_session()
    assert cam.list_sessions()["open_session"] is None


def test_update_gate_active_user_shutter_cover():
    lens, cam = make_paired()
    cam.resume()
    assert lens.update_required is True
    assert cam.set_update_required(False)
    assert lens.update_required is False
    assert cam.set_active_user()
    assert lens.active_device == 1
    assert cam.shutter()
    types = [t for t, _ in lens.requests]
    assert types[-2:] == [C.RT_SHUTTER, C.RT_SHUTTER]
    assert cam.set_cover_state(True) and lens.cover_open is True


def test_async_notifications_are_absorbed_into_state():
    lens, cam = make_paired()
    cam.resume()
    lens.push_notification(9, pb_uint(1, 1))
    lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE))
    lens.push_notification(13, pb_uint(1, 6))
    s = cam.list_sessions()  # the notifications arrive before the response
    assert s["status"] == C.STATUS_SUCCESS
    assert cam.state.cover_open is True
    assert cam.state.system_state_name == "CAPTURE"
    assert cam.state.wifi_state == 6
    assert len(cam.notifications) == 3


def test_initiate_wifi_returns_credentials():
    lens, cam = make_paired()
    cam.resume()
    creds = cam.initiate_wifi()
    assert creds.ssid == "Clips6013" and creds.passphrase == "0123456789abcdef"
    assert creds.url == "http://192.168.49.10:8080"
    assert lens.wifi_open and cam.state.wifi_state == 2
    assert cam.cancel_wifi() and not lens.wifi_open


def test_keepalive_thread_sends_heartbeats_and_channel_stays_in_sync():
    lens, cam = make_paired(sessions={SID: [1]})
    cam.resume()
    before = len(lens.requests)
    with cam.keepalive(interval=0.05):
        time.sleep(0.3)
        assert cam.list_moments(SID)["moment_ids"] == [1]
    hbs = [t for t, _ in lens.requests[before:] if t == C.RT_KEEP_ALIVE]
    assert len(hbs) >= 3
    # channel still consistent after concurrent traffic
    assert cam.list_sessions()["status"] == C.STATUS_SUCCESS


def test_concurrent_encrypt_is_serialised():
    lens, cam = make_paired()
    cam.resume()
    errors = []

    def spam():
        try:
            for _ in range(50):
                cam.heartbeat()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=spam) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert cam.list_sessions()["status"] == C.STATUS_SUCCESS  # counters never diverged


def test_request_timeout_returns_none():
    lens, cam = make_paired()
    cam.resume()
    lens.asleep = True
    assert cam.request(C.RT_FLASH_IDENTIFY_LEDS, timeout=0.2) is None


def test_extract_jpeg():
    assert extract_jpeg(b"junk\xff\xd8\xff\xe0abc") == b"\xff\xd8\xff\xe0abc"
    assert extract_jpeg(b"nothing") is None


def test_state_bundle_fields_visible_in_response_helper():
    lens, cam = make_paired()
    pt = lens.state_bundle() + pb_bytes(8, pb_uint(1, 1))
    assert 1 in parse_pb(pt)


# ---------------------------------------------------------------- events / lifecycle


def test_state_event_carries_diff_and_notification_event_fires():
    lens, cam = make_paired()
    seen_state, seen_notif = [], []
    cam.on(EVENT_STATE, lambda st, ch: seen_state.append(ch))
    cam.on(EVENT_NOTIFICATION, lambda kind, name, fields: seen_notif.append(name))
    cam.resume()
    assert seen_state and "system_state" in seen_state[0]  # CSC bundle populates state
    seen_state.clear()
    lens.push_notification(9, pb_uint(1, 1))
    lens.push_notification(9, pb_uint(1, 1))  # same value again: no diff
    cam.poll(0.1)
    assert seen_notif.count("COVER") == 2
    assert len(seen_state) == 2
    assert seen_state[0] == {"cover_open": (False, True)}
    assert seen_state[1] == {}


def test_off_removes_listener_and_bad_handler_does_not_break():
    lens, cam = make_paired()
    cam.resume()
    hits = []
    cb = cam.on(EVENT_STATE, lambda st, ch: hits.append(1))
    cam.on(EVENT_STATE, lambda st, ch: 1 / 0)
    lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE))
    cam.poll(0.1)
    assert hits == [1]
    cam.off(EVENT_STATE, cb)
    lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_IDLE))
    cam.poll(0.1)
    assert hits == [1]
    with pytest.raises(ValueError):
        cam.on("nope", lambda: None)


def test_wait_for_state_predicate():
    lens, cam = make_paired()
    cam.resume()
    lens.push_notification(2, pb_uint(1, C.SYSTEM_STATE_CAPTURE))
    assert cam.wait_for(lambda s: s.system_state == C.SYSTEM_STATE_CAPTURE, timeout=2)
    assert not cam.wait_for(lambda s: s.battery_pct == 1, timeout=0.3)


def test_connection_lost_raises_and_emits_once():
    lens, cam = make_paired()
    cam.resume()
    drops = []
    cam.on(EVENT_DISCONNECTED, lambda: drops.append(1))
    lens.drop_link()
    with pytest.raises(ConnectionLost):
        cam.list_sessions()
    with pytest.raises(ConnectionLost):
        cam.heartbeat()
    assert drops == [1]
    assert not cam.connected


def test_start_stop_keepalive_idempotent_and_close_stops_it():
    lens, cam = make_paired()
    cam.resume()
    cam.start_keepalive(0.05)
    cam.start_keepalive(0.05)
    time.sleep(0.2)
    assert cam._hb_thread is not None
    with cam.keepalive():  # no-op while already running
        pass
    assert cam._hb_thread is not None
    cam.close()
    assert cam._hb_thread is None and lens.closed
