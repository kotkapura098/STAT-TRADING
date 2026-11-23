import json

import pytest

from terminal.angel_accounts import AngelOneAccountVault, VaultError


def test_single_account_is_encrypted_and_masked(tmp_path):
    env = {}
    vault = AngelOneAccountVault(tmp_path / "angel.enc", env)
    vault.unlock("a-long-dashboard-password")
    aid = vault.add({"label":"Main account","api_key":"api-key-example-12345",
                     "client_code":"AB1234","pin":"1234","totp_secret":""})
    assert aid == "angel"
    public = vault.public()
    assert len(public) == 1
    assert public[0]["client_code"] != "AB1234"
    assert "pin" not in public[0]
    raw = (tmp_path / "angel.enc").read_text()
    assert "api-key-example-12345" not in raw
    assert "AB1234" not in raw
    assert vault.creds("angel")["ANGEL_API_KEY"] == "api-key-example-12345"


def test_only_one_saved_account_is_allowed(tmp_path):
    vault = AngelOneAccountVault(tmp_path / "angel.enc", {})
    vault.unlock("a-long-dashboard-password")
    vault.add({"api_key":"api-key-example-12345","client_code":"AB1234","pin":"1234"})
    with pytest.raises(VaultError):
        vault.add({"api_key":"another-api-key-12345","client_code":"CD5678","pin":"5678"})


def test_environment_credentials_are_read_only_and_take_precedence(tmp_path):
    env = {"ANGEL_API_KEY":"env-api-key-12345","ANGEL_CLIENT_CODE":"ENV123","ANGEL_PIN":"4321"}
    vault = AngelOneAccountVault(tmp_path / "angel.enc", env)
    vault.unlock("a-long-dashboard-password")
    assert vault.default_id() == "env"
    assert vault.public()[0]["source"] == "env"
    assert vault.creds("env")["ANGEL_CLIENT_CODE"] == "ENV123"
    with pytest.raises(VaultError):
        vault.add({"api_key":"api-key-example-12345","client_code":"AB1234","pin":"1234"})


def test_wrong_dashboard_password_does_not_overwrite_saved_account(tmp_path):
    path = tmp_path / "angel.enc"
    first = AngelOneAccountVault(path, {})
    first.unlock("correct-dashboard-password")
    first.add({"api_key":"api-key-example-12345","client_code":"AB1234","pin":"1234"})
    second = AngelOneAccountVault(path, {})
    second.unlock("wrong-dashboard-password")
    assert not second.readable
    assert second.problem
    assert "api-key-example-12345" not in path.read_text()
