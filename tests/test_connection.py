import os

import pytest
from fake_lens import FakeLens

from openclips.connection import ConnectionManager, default_transport_factory
from openclips.errors import CameraAsleep, ConnectionLost, NotPaired, PairingKeyMismatch
from openclips.store import Pairing, PairingStore


class FlakyFactory:
    """Fails the first N connection attempts, then hands out the lens."""

    def __init__(self, lens, failures=0):
        self.lens, self.failures, self.calls = lens, failures, 0

    def __call__(self, address):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("GATT discovery failed")
        self.lens.reopen()
        return self.lens


def test_connect_retries_transport_then_resumes_and_syncs_clock():
    key = os.urandom(32)
    lens = FakeLens(pairing_key=key)
    factory = FlakyFactory(lens, failures=2)
    mgr = ConnectionManager("aa:bb:cc:dd:ee:ff", pairing_key=key, transport_factory=factory, retry_delay=0)
    cam = mgr.connect()
    assert factory.calls == 3
    assert mgr.connected and cam.connected
    assert lens.time_synced_to is not None  # PRIVATE_QUERY sent
    assert cam._hb_thread is not None  # keepalive running
    assert mgr.connect() is cam  # idempotent
    mgr.close()
    assert not mgr.connected and cam._hb_thread is None and lens.closed


def test_gives_up_after_attempts():
    lens = FakeLens(pairing_key=os.urandom(32))
    mgr = ConnectionManager(
        "A", pairing_key=lens.pairing_key, transport_factory=FlakyFactory(lens, 9), attempts=2, retry_delay=0
    )
    with pytest.raises(ConnectionLost):
        mgr.connect()


def test_asleep_closes_transport_and_adds_wake_hint():
    lens = FakeLens(pairing_key=os.urandom(32), asleep=True)
    mgr = ConnectionManager("A", pairing_key=lens.pairing_key, transport_factory=FlakyFactory(lens), attempts=1)
    with pytest.raises(CameraAsleep) as ei:
        mgr.connect()
    assert "shutter" in str(ei.value)
    assert lens.closed and not mgr.connected


def test_key_from_store_and_mismatch():
    key = os.urandom(32)
    lens = FakeLens(pairing_key=key)
    store = PairingStore.__new__(PairingStore)
    store._data = {"host_key_pem": None, "cameras": {}}
    store.put(Pairing("AA:BB:CC:DD:EE:FF", os.urandom(32)))  # wrong key
    mgr = ConnectionManager("aa:bb:cc:dd:ee:ff", store=store, transport_factory=FlakyFactory(lens), attempts=1)
    with pytest.raises(PairingKeyMismatch):
        mgr.connect()
    store.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    mgr = ConnectionManager("aa:bb:cc:dd:ee:ff", store=store, transport_factory=FlakyFactory(lens), attempts=1)
    with mgr as cam:
        assert cam.state.system_state_name == "IDLE"
    assert lens.closed


def test_not_paired():
    with pytest.raises(NotPaired):
        ConnectionManager("A", transport_factory=lambda a: FakeLens()).connect()


def test_ensure_reconnects_after_link_loss():
    key = os.urandom(32)
    lens = FakeLens(pairing_key=key, sessions={1: [1]})
    factory = FlakyFactory(lens)
    mgr = ConnectionManager("A", pairing_key=key, transport_factory=factory, keepalive=False)
    cam = mgr.connect()
    lens.drop_link()
    with pytest.raises(ConnectionLost):
        cam.list_sessions()
    assert not mgr.connected
    cam2 = mgr.ensure()
    assert cam2 is not cam and cam2.list_sessions()["session_ids"] == [1]
    assert factory.calls == 2
    mgr.close()


def test_default_transport_factory_names():
    assert default_transport_factory("btgatt").__name__ == "BtgattTransport"
    with pytest.raises(ValueError):
        default_transport_factory("zigbee")


def test_unexpected_connect_error_still_closes_transport():
    key = os.urandom(32)
    lens = FakeLens(pairing_key=key)

    def factory(addr):
        lens.reopen()
        return lens

    mgr = ConnectionManager("A", pairing_key=key, transport_factory=factory, keepalive=False, attempts=1)

    def boom(self):
        raise RuntimeError("handshake exploded")

    import openclips.camera as camera_mod

    orig = camera_mod.Camera.resume
    camera_mod.Camera.resume = boom
    try:
        with pytest.raises(RuntimeError):
            mgr.connect()
        assert lens.closed and not mgr.connected
    finally:
        camera_mod.Camera.resume = orig


def test_close_is_idempotent():
    key = os.urandom(32)
    lens = FakeLens(pairing_key=key)
    mgr = ConnectionManager("A", pairing_key=key, transport_factory=FlakyFactory(lens), keepalive=False, attempts=1)
    mgr.connect()
    mgr.close()
    mgr.close()
    assert not mgr.connected and lens.closed
