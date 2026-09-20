import json
import os
import stat
import threading

import pytest

from openclips.errors import StorageError
from openclips.store import Pairing, PairingStore, default_path
from openclips.wifi import WifiCredentials


def test_default_path_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLIPS_STORE", raising=False)
    assert default_path() == tmp_path / "openclips" / "pairings.json"
    monkeypatch.setenv("OPENCLIPS_STORE", str(tmp_path / "x.json"))
    assert default_path() == tmp_path / "x.json"


def test_roundtrip_and_permissions(tmp_path):
    path = tmp_path / "sub" / "pairings.json"
    s = PairingStore(path)
    assert s.addresses() == [] and s.only_address() is None
    s.host_key_pem = "PEM"
    s.put(Pairing("aa:bb:cc:dd:ee:ff", b"\x11" * 32, b"\x22" * 64, name="cam"))
    s.save()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    s2 = PairingStore(path)
    assert s2.host_key_pem == "PEM"
    assert s2.only_address() == "AA:BB:CC:DD:EE:FF"
    p = s2.get("AA:BB:CC:DD:EE:FF")
    assert p.pairing_key == b"\x11" * 32 and p.lens_public_key == b"\x22" * 64 and p.paired_at
    assert s2.remove("aa:bb:cc:dd:ee:ff") and not s2.remove("aa:bb:cc:dd:ee:ff")
    s2.save()
    assert json.load(open(path))["cameras"] == {}


def test_only_address_none_with_two(tmp_path):
    s = PairingStore(tmp_path / "p.json")
    s.put(Pairing("A", b"\x00" * 32))
    s.put(Pairing("B", b"\x00" * 32))
    assert s.only_address() is None


def test_save_mode_under_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        path = tmp_path / "p.json"
        s = PairingStore(path)
        s.put(Pairing("A", b"\x00" * 32))
        s.save()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.umask(old)


def test_malformed_store_not_silently_replaced(tmp_path):
    path = tmp_path / "pairings.json"
    path.write_text("{not json")
    with pytest.raises(StorageError, match="invalid"):
        PairingStore(path)
    assert "{not json" in path.read_text()


def test_invalid_key_length_rejected(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"cameras": {"AA:BB:CC:DD:EE:FF": {"pairing_key": "aa"}}}))
    with pytest.raises(StorageError, match="invalid"):
        PairingStore(path)


def test_stale_revision_rejected(tmp_path):
    path = tmp_path / "p.json"
    a = PairingStore(path)
    a.put(Pairing("A", b"\x00" * 32))
    a.save()
    b = PairingStore(path)
    a.put(Pairing("B", b"\x11" * 32))
    a.save()
    b.put(Pairing("C", b"\x22" * 32))
    with pytest.raises(StorageError, match="changed on disk"):
        b.save()


def test_lock_timeout(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    s = PairingStore(path)
    s.put(Pairing("A", b"\x00" * 32))
    from openclips import persist

    held = threading.Event()
    release = threading.Event()

    def blocker():
        with persist.exclusive_lock(path, timeout=5):
            held.set()
            release.wait(5)

    t = threading.Thread(target=blocker)
    t.start()
    assert held.wait(2)
    with pytest.raises(StorageError, match="busy"):
        with persist.exclusive_lock(path, timeout=0.2):
            pass
    release.set()
    t.join(3)


def test_repr_redacts_secrets():
    sentinel = "SYNTHETIC_SECRET_SENTINEL_key"
    p = Pairing("AA:BB:CC:DD:EE:FF", sentinel.encode().ljust(32, b"x")[:32])
    r = repr(p)
    assert "redacted" in r and sentinel not in r
    w = WifiCredentials("Clips6013", "SYNTHETIC_SECRET_SENTINEL_psk")
    wr = repr(w)
    assert "redacted" in wr and "SYNTHETIC_SECRET_SENTINEL_psk" not in wr
    assert p.pairing_key  # programmatic access still works
    from openclips.sync import SyncProgress, SyncResult

    prog = SyncProgress("wifi_join", credentials=w)
    assert "SYNTHETIC_SECRET_SENTINEL_psk" not in repr(prog)
    res = SyncResult(credentials=w)
    assert "SYNTHETIC_SECRET_SENTINEL_psk" not in repr(res)
