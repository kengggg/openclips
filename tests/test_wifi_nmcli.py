"""Mocked nmcli tests. Passphrases must never appear in errors or logs."""

import logging
import subprocess

import pytest

from openclips.errors import WifiError
from openclips.wifi import WifiCredentials
from openclips.wifi_nmcli import (
    NmcliResult,
    NmcliWifi,
    active_connection,
    detect_wifi_iface,
    forget_nmcli,
    join_nmcli,
    parse_nmcli_fields,
    restore_nmcli,
    wait_for_ssid,
)

SENTINEL = "SYNTHETIC_SECRET_SENTINEL_psk"


def test_parse_escaped_nmcli_fields():
    assert parse_nmcli_fields("Home\\:Net:wlan0") == ["Home:Net", "wlan0"]
    assert parse_nmcli_fields("a\\\\b:wlan0") == ["a\\b", "wlan0"]
    assert parse_nmcli_fields("plain:wifi") == ["plain", "wifi"]


def test_detect_iface_and_active_uuid(monkeypatch):
    calls = []

    def fake_run(args, timeout=20, stage="nmcli"):
        calls.append(args)
        if "DEVICE,TYPE" in args:
            return NmcliResult(0, "eth0:ethernet\nwlan0:wifi\n")
        if "UUID,DEVICE" in args:
            return NmcliResult(0, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:wlan0\n")
        return NmcliResult(0, "")

    monkeypatch.setattr("openclips.wifi_nmcli._run", fake_run)
    assert detect_wifi_iface() == "wlan0"
    assert active_connection("wlan0") == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_wait_for_ssid_monotonic_and_absent(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("openclips.wifi_nmcli.time.monotonic", lambda: clock["t"])

    def fake_run(args, timeout=20, stage="nmcli"):
        clock["t"] += 0.5
        if "list" in args:
            return NmcliResult(0, "OtherNet\n")
        return NmcliResult(0, "")

    monkeypatch.setattr("openclips.wifi_nmcli._run", fake_run)
    monkeypatch.setattr("openclips.wifi_nmcli.time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    assert wait_for_ssid("Clips6013", "wlan0", timeout=2.0) is False
    assert clock["t"] >= 2.0


def test_join_creates_unique_profile_without_leaking_psk(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="openclips.wifi_nmcli")
    seen = []

    def fake_run(args, timeout=20, stage="nmcli"):
        seen.append(list(args))
        if args[:3] == ["nmcli", "connection", "add"]:
            return NmcliResult(0, "")
        if "NAME,UUID" in args:
            return NmcliResult(0, "openclips-deadbeef:11111111-2222-3333-4444-555555555555\n")
        if args[:3] == ["nmcli", "connection", "up"]:
            return NmcliResult(0, "")
        return NmcliResult(0, "")

    monkeypatch.setattr("openclips.wifi_nmcli._run", fake_run)
    monkeypatch.setattr("openclips.wifi_nmcli.uuidlib.uuid4", lambda: type("U", (), {"hex": "deadbeefcafebabe"})())
    creds = WifiCredentials("Clips6013", SENTINEL)
    uid = join_nmcli(creds, iface="wlan0", timeout=5, retries=1)
    assert uid == "11111111-2222-3333-4444-555555555555"
    add = next(a for a in seen if a[:3] == ["nmcli", "connection", "add"])
    assert PROFILE_PREFIX_IN_ADD(add)
    text = caplog.text + str(WifiError("could not create camera Wi-Fi profile"))
    assert SENTINEL not in text


def PROFILE_PREFIX_IN_ADD(args):
    return any(str(a).startswith("openclips-") for a in args)


def test_forget_restore_independent(monkeypatch):
    calls = []

    def fake_run(args, timeout=20, stage="nmcli"):
        calls.append(args)
        if "delete" in args:
            return NmcliResult(1, "", "fail")
        if args[:3] == ["nmcli", "connection", "up"]:
            return NmcliResult(0, "")
        return NmcliResult(0, "")

    monkeypatch.setattr("openclips.wifi_nmcli._run", fake_run)
    with pytest.raises(WifiError):
        forget_nmcli("11111111-2222-3333-4444-555555555555")
    restore_nmcli("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert any("delete" in a for a in calls)
    assert any("up" in a for a in calls)


def test_leave_restores_even_if_delete_fails(monkeypatch):
    w = NmcliWifi(iface="wlan0", restore=True)
    w.owned_uuid = "11111111-2222-3333-4444-555555555555"
    w.previous = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    calls = []

    def fake_run(args, timeout=20, stage="nmcli"):
        calls.append(args[:])
        if "delete" in args:
            return NmcliResult(1, "", SENTINEL)
        return NmcliResult(0, "")

    monkeypatch.setattr("openclips.wifi_nmcli._run", fake_run)
    w.leave()
    assert w.leave_errors
    assert SENTINEL not in "".join(w.leave_errors)
    assert any("delete" in a for a in calls)
    assert any(a[:3] == ["nmcli", "connection", "up"] for a in calls)
    w.leave()  # repeated, harmless


def test_nmcli_missing_is_wifierror(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("nmcli")

    monkeypatch.setattr("openclips.wifi_nmcli.subprocess.run", boom)
    from openclips.wifi_nmcli import _run

    with pytest.raises(WifiError, match="not installed") as ei:
        _run(["nmcli"])
    assert SENTINEL not in str(ei.value)


def test_nmcli_timeout_is_wifierror(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("nmcli", 1)

    monkeypatch.setattr("openclips.wifi_nmcli.subprocess.run", boom)
    from openclips.wifi_nmcli import _run

    with pytest.raises(WifiError, match="timed out") as ei:
        _run(["nmcli", "device"])
    assert SENTINEL not in str(ei.value)


def test_does_not_delete_unrelated_ssid_profile(monkeypatch):
    """leave() deletes owned UUID only, never an SSID-named stranger."""
    w = NmcliWifi()
    w.owned_uuid = "owned-uuid-000000000000000000000000000"
    w.ssid = "Clips6013"
    seen = []

    def fake_run(args, timeout=20, stage="nmcli"):
        seen.append(args)
        return NmcliResult(0, "")

    monkeypatch.setattr("openclips.wifi_nmcli._run", fake_run)
    w.leave()
    delete = [a for a in seen if "delete" in a]
    assert delete and "Clips6013" not in delete[0]
    assert "owned-uuid-000000000000000000000000000" in delete[0]
