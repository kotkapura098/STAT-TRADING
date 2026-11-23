"""Angel One single-account credential storage, settings API and service connection tests."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT
from terminal.angel_accounts import AngelOneAccountVault, VaultError
from terminal.config import load_config
from terminal.services import Services
from terminal.web.server import create_app

PW = "correct-horse-battery"
GOOD = {
    "label": "Main Angel One",
    "api_key": "api-key-example-12345",
    "client_code": "AB1234",
    "pin": "135790",
    "totp_secret": "",
}


def make_vault(tmp_path, env=None, password=PW):
    vault = AngelOneAccountVault(tmp_path / "angel-account.enc", env or {})
    vault.unlock(password)
    return vault


def test_single_account_is_encrypted_masked_and_persistent(tmp_path):
    path = tmp_path / "angel-account.enc"
    vault = make_vault(tmp_path)
    assert vault.add(GOOD) == "angel"
    public = vault.public()[0]
    assert public["label"] == "Main Angel One"
    assert public["client_code"] != "AB1234"
    assert public["api_key"] != GOOD["api_key"]
    assert "pin" not in public
    assert GOOD["api_key"] not in path.read_text()
    assert GOOD["client_code"] not in path.read_text()
    assert vault.creds("angel")["ANGEL_PIN"] == "135790"

    restored = make_vault(tmp_path)
    assert restored.get("angel")["client_code"] == "AB1234"


def test_only_one_saved_account_is_allowed(tmp_path):
    vault = make_vault(tmp_path)
    vault.add(GOOD)
    with pytest.raises(VaultError, match="already configured|already saved|one"):
        vault.add({**GOOD, "client_code": "CD5678"})


def test_environment_account_takes_precedence_and_is_read_only(tmp_path):
    env = {"ANGEL_API_KEY": "env-api-key-12345", "ANGEL_CLIENT_CODE": "ENV123", "ANGEL_PIN": "4321"}
    vault = make_vault(tmp_path, env)
    assert vault.default_id() == "env"
    assert vault.public()[0]["source"] == "env"
    assert vault.creds("env")["ANGEL_CLIENT_CODE"] == "ENV123"
    with pytest.raises(VaultError):
        vault.add(GOOD)


def test_wrong_dashboard_password_does_not_destroy_credentials(tmp_path):
    path = tmp_path / "angel-account.enc"
    make_vault(tmp_path).add(GOOD)
    before = path.read_bytes()
    wrong = AngelOneAccountVault(path, {})
    wrong.unlock("wrong-dashboard-password")
    assert wrong.readable is False
    assert wrong.problem
    assert path.read_bytes() == before


@pytest.fixture
def sample_api(tmp_path):
    cfg = load_config(ROOT / "config.yaml")
    cfg.mode, cfg.data_dir = "sample", str(tmp_path / "data")
    svc = Services(cfg, env={})
    svc.start()
    client = TestClient(create_app(svc, PW))
    response = client.post("/api/auth/login", json={"password": PW}, headers={"x-terminal": "1"})
    assert response.status_code == 200
    client.headers["x-terminal"] = "1"
    yield svc, client
    svc.shutdown()


def test_account_api_saves_masks_edits_and_deletes_without_exposing_secrets(sample_api):
    svc, client = sample_api
    created = client.post("/api/accounts", json=GOOD)
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["ok"] and payload["default"] == "angel"
    assert len(payload["accounts"]) == 1
    serialized = json.dumps(payload) + client.get("/api/accounts").text
    for secret in (GOOD["api_key"], GOOD["client_code"], GOOD["pin"]):
        assert secret not in serialized

    invalid = client.put("/api/accounts/angel", json={**GOOD, "pin": "12"})
    assert invalid.status_code == 400
    assert "PIN" in invalid.json()["error"]
    assert client.get("/api/accounts").json()["accounts"][0]["label"] == "Main Angel One"

    edited = client.put("/api/accounts/angel", json={**GOOD, "label": "Updated Angel"})
    assert edited.status_code == 200
    assert edited.json()["accounts"][0]["label"] == "Updated Angel"

    deleted = client.delete("/api/accounts/angel")
    assert deleted.status_code == 200
    assert deleted.json()["accounts"] == []


def test_account_api_requires_login_and_csrf_header(sample_api):
    svc, client = sample_api
    client.headers.pop("x-terminal")
    assert client.get("/api/accounts").status_code == 200
    assert client.post("/api/accounts", json=GOOD).status_code == 403


def test_service_connect_uses_saved_angel_one_credentials(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import terminal.brokers.angel_one as broker
    import terminal.market.angel_catalog as catalog_module
    import terminal.market.angel_chain as chain_module

    cfg = load_config(ROOT / "config.yaml")
    cfg.mode, cfg.data_dir = "paper", str(tmp_path / "data")
    svc = Services(cfg, env={})
    svc.vault.unlock(PW)
    svc.vault.add(GOOD)
    captured = {}

    class FakeSession:
        def __init__(self, creds):
            captured["creds"] = dict(creds)
            self.profile = {"clientcode": creds["ANGEL_CLIENT_CODE"]}
            self.client_code = creds["ANGEL_CLIENT_CODE"]
            self.logged_in = False
            self.logged_out = False
        def login(self, totp):
            captured["totp"] = totp
            self.logged_in = True
        def logout(self):
            self.logged_in = False
            self.logged_out = True

    class FakeInstruments:
        def __init__(self, session, config):
            self.session = session
        def index_subscription(self, label):
            from terminal.brokers.base import Subscription
            item = next(x for x in cfg.dashboard_indices if x["label"] == label)
            return Subscription(item["segment"], item["token"], "INDEX")

    class FakeCatalog:
        def __init__(self, instruments, config):
            self.instruments = instruments
        def warm(self):
            pass
        def get(self, key):
            return None
        def search(self, query, limit=25):
            return []

    class FakeChain:
        def __init__(self, *args, **kwargs):
            pass

    class FakeFeed:
        def __init__(self, session, config):
            self.connected = False
            self._thread = None
        def start(self, callback):
            self.connected = True
        def wait_connected(self, timeout=20):
            return True
        def subscribe_keys(self, keys):
            pass
        def stop(self):
            self.connected = False

    monkeypatch.setattr(broker, "AngelOneSession", FakeSession)
    monkeypatch.setattr(broker, "AngelOneInstruments", FakeInstruments)
    monkeypatch.setattr(catalog_module, "AngelOneCatalog", FakeCatalog)
    monkeypatch.setattr(chain_module, "AngelOneChainProvider", FakeChain)
    monkeypatch.setattr(broker, "AngelOneFeed", FakeFeed)

    try:
        svc.connect("123456", "angel")
        assert svc.connected
        assert captured["creds"] == {
            "ANGEL_API_KEY": GOOD["api_key"],
            "ANGEL_CLIENT_CODE": GOOD["client_code"],
            "ANGEL_PIN": GOOD["pin"],
            "ANGEL_TOTP_SECRET": "",
        }
        assert captured["totp"] == "123456"
        assert svc.account["label"] == "Main Angel One"
        assert svc.session is svc.sessions["angel"]
        assert svc.journal.ns == "angel-one:paper"
        connected_session = svc.session
        svc.disconnect()
        assert connected_session.logged_out
        assert svc.session is None and not svc.connected
        assert svc._angel_ins is None and svc.catalog.inner is None
    finally:
        svc.shutdown()
