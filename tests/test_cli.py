import json

import pytest
from fake_lens import FakeLens

from openclips import cli
from openclips import constants as C
from openclips.store import Pairing, PairingStore

SID = 1789825799491786000


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "pairings.json"
    monkeypatch.setenv("OPENCLIPS_STORE", str(path))
    monkeypatch.delenv("OPENCLIPS_ADDRESS", raising=False)
    return path


@pytest.fixture
def fake(monkeypatch):
    """Route the CLI transport to a FakeLens and return it for inspection."""
    holder = {}

    def open_transport(args):
        lens = holder["factory"](args.address)
        if hasattr(lens, "reopen"):
            lens.reopen()  # a fresh GATT connection to the same camera
        holder["lens"] = lens
        return lens

    monkeypatch.setattr(cli, "open_transport", open_transport)
    return holder


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_encode_no_camera(capsys):
    rc, out, _ = run(capsys, "--json", "encode", "--session", str(SID), "--moments", "2,3")
    assert rc == 0
    d = json.loads(out)
    assert d["initiate_wifi_paired"] == "08001001"
    assert d["list_moments"].startswith("09")
    assert set(d["fetch_moment_full"]) == {"2", "3"}


def test_pair_saves_key_and_clears_update_gate(store, fake, capsys):
    fake["factory"] = lambda addr: FakeLens(setup_mode=True, system_state=C.SYSTEM_STATE_SETUP)
    rc, out, _ = run(capsys, "--json", "pair", "aa:bb:cc:dd:ee:ff", "--name", "desk")
    assert rc == 0
    lens = fake["lens"]
    s = PairingStore(store)
    p = s.get("AA:BB:CC:DD:EE:FF")
    assert p.pairing_key == lens.pairing_key and p.name == "desk"
    assert s.host_key_pem and "PRIVATE KEY" in s.host_key_pem
    assert lens.update_required is False and lens.active_device == 1
    assert lens.closed


def test_status_uses_single_stored_pairing(store, fake, capsys):
    key = b"\x07" * 32
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    s.save()
    fake["factory"] = lambda addr: FakeLens(pairing_key=key, cover_open=True)
    rc, out, _ = run(capsys, "--json", "status")
    assert rc == 0
    d = json.loads(out)
    assert d["system_state_name"] == "IDLE" and d["cover_open"] is True


def test_unpaired_address_is_an_error(store, fake, capsys):
    fake["factory"] = lambda addr: FakeLens()
    with pytest.raises(SystemExit):
        cli.main(["-a", "11:22:33:44:55:66", "status"])


def test_sessions_moments_delete(store, fake, capsys):
    key = b"\x09" * 32
    PairingStore(store).put(Pairing("AA:BB:CC:DD:EE:FF", key))
    PairingStore(store)  # reload sanity
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    s.save()
    lens = FakeLens(pairing_key=key, sessions={SID: [2, 3, 1]}, open_session=42)
    fake["factory"] = lambda addr: lens
    rc, out, _ = run(capsys, "sessions")
    assert rc == 0 and str(SID) in out and "(open)" in out
    rc, out, _ = run(capsys, "moments", str(SID))
    assert rc == 0 and [line.split()[0] for line in out.strip().splitlines()] == ["2", "3", "1"]
    assert "score=" in out
    rc, out, _ = run(capsys, "--json", "moments", str(SID))
    assert rc == 0 and json.loads(out)["moments"][0]["moment_id"] == 2
    rc, out, _ = run(capsys, "delete", str(SID), "2,3")
    assert rc == 0 and lens.sessions[SID] == [1]
    rc, out, _ = run(capsys, "complete")
    assert rc == 0 and lens.open_session is None


def test_wifi_prints_credentials(store, fake, capsys):
    key = b"\x0a" * 32
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    s.save()
    fake["factory"] = lambda addr: FakeLens(pairing_key=key)
    rc, out, _ = run(capsys, "--json", "wifi")
    assert rc == 0
    assert json.loads(out) == {
        "ssid": "Clips6013",
        "passphrase": "0123456789abcdef",
        "url": "http://192.168.49.10:8080",
    }


def test_sync_downloads_jpegs(store, fake, capsys, tmp_path, monkeypatch):
    key = b"\x0b" * 32
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    s.save()
    lens = FakeLens(pairing_key=key, sessions={SID: [2, 3], 5: [9]})
    fake["factory"] = lambda addr: lens

    from openclips import wifi_nmcli as nm

    calls = []
    monkeypatch.setattr(nm, "detect_wifi_iface", lambda: "wlan0")
    monkeypatch.setattr(nm, "active_connection", lambda iface: "HomeNet")
    monkeypatch.setattr(nm, "wait_for_ssid", lambda ssid, iface, timeout, log=None: True)
    monkeypatch.setattr(nm, "join_nmcli", lambda creds, iface, log=None: calls.append(("join", creds.ssid)) or True)
    monkeypatch.setattr(nm, "forget_nmcli", lambda ssid: calls.append(("forget", ssid)))
    monkeypatch.setattr(nm, "restore_nmcli", lambda conn: calls.append(("restore", conn)))

    def fake_post(url, path, body, timeout=30.0):
        assert path == "/fetch_moment" and url == "http://192.168.49.10:8080"
        from openclips.jpeg import structural_jpeg

        return structural_jpeg()

    monkeypatch.setattr(cli.Camera, "http_post", staticmethod(fake_post))

    out_dir = tmp_path / "photos"
    rc, out, err = run(capsys, "sync", "--out", str(out_dir))
    assert rc == 0, err
    saved = sorted(p.name for p in (out_dir / str(SID)).iterdir())
    assert saved == ["moment_2.jpg", "moment_3.jpg"]
    assert not (out_dir / "5").exists()  # newest session only by default
    assert ("join", "Clips6013") in calls and ("restore", "HomeNet") in calls and ("forget", "Clips6013") in calls
    assert not lens.wifi_open  # CANCEL_WIFI after the download
    assert (out_dir / ".openclips-catalog.json").exists()

    rc, out, err = run(capsys, "sync", "--out", str(out_dir), "--all-sessions")
    assert rc == 0
    assert (out_dir / "5" / "moment_9.jpg").exists()

    rc, out, err = run(capsys, "sync", "--out", str(out_dir), "--all-sessions")
    assert rc == 0 and "nothing new" in out


def test_sync_partial_failure_is_nonzero(store, fake, capsys, tmp_path, monkeypatch):
    key = b"\x0d" * 32
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    s.save()
    lens = FakeLens(pairing_key=key, sessions={SID: [2, 3]})
    fake["factory"] = lambda addr: lens
    from openclips import wifi_nmcli as nm
    from openclips.jpeg import structural_jpeg

    monkeypatch.setattr(nm, "detect_wifi_iface", lambda: "wlan0")
    monkeypatch.setattr(nm, "active_connection", lambda iface: None)
    monkeypatch.setattr(nm, "wait_for_ssid", lambda ssid, iface, timeout, log=None: True)
    monkeypatch.setattr(nm, "join_nmcli", lambda creds, iface, log=None: True)
    monkeypatch.setattr(nm, "forget_nmcli", lambda ssid: None)
    monkeypatch.setattr(nm, "restore_nmcli", lambda conn: None)
    n = {"i": 0}

    def fake_post(url, path, body, timeout=30.0):
        n["i"] += 1
        if n["i"] == 2:
            raise cli.CameraError("HTTP 500 on /fetch_moment")
        return structural_jpeg()

    monkeypatch.setattr(cli.Camera, "http_post", staticmethod(fake_post))
    rc, out, err = run(capsys, "--json", "sync", "--out", str(tmp_path / "photos"))
    assert rc == cli.EXIT_PARTIAL
    data = json.loads(out)
    assert len(data["downloaded"]) == 1 and len(data["failed"]) == 1


def test_watch_prints_state_changes(store, fake, capsys):
    key = b"\x0c" * 32
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", key))
    s.save()
    lens = FakeLens(pairing_key=key)
    fake["factory"] = lambda addr: lens
    from openclips.pb import pb_uint

    orig_connect = cli.connect_paired

    def connect_and_push(*a, **kw):
        mgr = orig_connect(*a, **kw)
        lens.push_notification(9, pb_uint(1, 1))
        return mgr

    fake_module = cli
    fake_module.connect_paired = connect_and_push
    try:
        rc, out, _ = run(capsys, "watch", "--duration", "0.5")
    finally:
        fake_module.connect_paired = orig_connect
    assert rc == 0 and "cover_open: False -> True" in out


def test_forget(store, fake, capsys):
    s = PairingStore(store)
    s.put(Pairing("AA:BB:CC:DD:EE:FF", b"\x00" * 32))
    s.save()
    rc, out, _ = run(capsys, "forget")
    assert rc == 0 and PairingStore(store).addresses() == []
