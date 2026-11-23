"""Network utilities and Angel One static egress-IP enforcement."""
from conftest import ROOT
from fastapi.testclient import TestClient

from terminal.config import load_config
from terminal.netip import NetIp, NetIpError, parse_ips, parse_public, parse_public_ipv4_list
from terminal.services import Services
from terminal.store import Store
from terminal.web.server import create_app


PW = "correct-horse-battery"


def test_ipv4_ipv6_normalization_and_mapped_addresses():
    assert same_ip("2401:4900:1c2a:77::17", "2401:4900:1C2A:0077:0000:0000:0000:0017")
    assert same_ip("::ffff:49.36.10.20", "49.36.10.20")
    assert not same_ip("49.36.10.20", "49.36.10.21")


def test_parse_ips_accepts_one_public_address_per_family():
    assert parse_ips("49.36.10.20, 2401:4900:1c2a:77::17") == [
        "49.36.10.20", "2401:4900:1c2a:77::17"
    ]


def test_parse_ips_rejects_private_malformed_and_duplicate_family():
    for value in ("192.168.1.10", "not-an-ip", "49.36.10.20, 49.36.10.21"):
        try:
            parse_ips(value, public_only=True)
        except NetIpError:
            pass
        else:
            raise AssertionError(f"invalid public IP input accepted: {value}")


def test_public_detection_parser_rejects_wrong_family_and_non_public():
    assert parse_public("49.36.10.20\n", "v4") == "49.36.10.20"
    assert parse_public("49.36.10.20", "v6") == ""
    assert parse_public("192.168.1.1", "v4") == ""


def test_netip_detection_caches_results_and_does_not_raise():
    calls = []
    def fetch(family):
        calls.append(family)
        return ("49.36.10.20", "") if family == "v4" else ("", "no IPv6 route")
    net = NetIp(Store(":memory:"), fetch=fetch, clock=lambda: 1000.0)
    first = net.detect()
    second = net.detect()
    assert first["v4"] == second["v4"] == "49.36.10.20"
    assert first["v6"] == second["v6"] == ""
    assert calls == ["v4", "v6"]


def test_parse_public_ipv4_list_accepts_primary_and_secondary_addresses():
    assert parse_public_ipv4_list("1.1.1.1, 8.8.8.8") == ["1.1.1.1", "8.8.8.8"]


def test_parse_public_ipv4_list_rejects_private_ipv6_and_malformed_values():
    for value in ("192.168.1.10", "2401:4900::1", "not-an-ip", "1.1.1.1,8.8.8.8,9.9.9.9"):
        try:
            parse_public_ipv4_list(value)
        except NetIpError:
            pass
        else:
            raise AssertionError(f"invalid Angel One static IP accepted: {value}")


def _network_service(expected, outbound):
    from types import SimpleNamespace
    svc = object.__new__(Services)
    svc.env = {"ANGEL_STATIC_IP": expected} if expected is not None else {}
    svc.cfg = SimpleNamespace(static_ip="")
    svc.netip = SimpleNamespace(detect=lambda: {
        "at": 1, "v4": outbound, "v6": "", "errors": {"v4": "", "v6": ""}
    })
    svc.mode = "paper"
    svc.accounts = {}
    svc.active_account_id = ""
    return svc


def test_angel_one_network_status_accepts_any_registered_static_ipv4():
    svc = _network_service("1.1.1.1,8.8.8.8", "8.8.8.8")
    status = svc.network_status()
    assert status["verdict"] == "ok"
    assert status["expected"] == ["1.1.1.1", "8.8.8.8"]
    assert svc._ip_problem() is None


def test_angel_one_network_status_blocks_missing_or_mismatched_static_ipv4():
    missing = _network_service(None, "8.8.8.8")
    assert missing.network_status()["verdict"] == "unset"
    assert "ANGEL_STATIC_IP" in missing._ip_problem()
    mismatch = _network_service("1.1.1.1", "8.8.8.8")
    assert mismatch.network_status()["verdict"] == "mismatch"
    assert "blocked" in mismatch._ip_problem().lower()


def test_live_session_warns_when_angel_one_static_ip_is_unconfigured(tmp_path):
    cfg = load_config(ROOT / "config.yaml")
    cfg.mode, cfg.data_dir = "sample", str(tmp_path / "data")
    svc = Services(cfg, env={})
    svc.start()
    svc.netip.detect = lambda force=False: {
        "at": 1, "v4": "8.8.8.8", "v6": "", "errors": {"v4": "", "v6": ""}
    }
    client = TestClient(create_app(svc, PW))
    try:
        response = client.post("/api/auth/login", json={"password": PW}, headers={"x-terminal": "1"})
        assert response.status_code == 200
        client.headers["x-terminal"] = "1"
        svc.mode = "live"
        session = client.get("/api/session").json()
        assert "ANGEL_STATIC_IP" in session["ip_alert"]
    finally:
        svc.shutdown()
