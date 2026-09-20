import json
import os
import stat

from openclips.store import Pairing, PairingStore, default_path


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
