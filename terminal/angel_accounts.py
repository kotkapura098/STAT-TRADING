"""Encrypted, single-account credential vault for Angel One SmartAPI.

The browser receives only masked metadata. API key, client code, PIN and TOTP secret
are encrypted at rest with a key derived from DASHBOARD_PASSWORD.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any


class VaultError(ValueError):
    """A user-correctable account configuration error."""


class AngelOneAccountVault:
    def __init__(self, path: str | Path, env=os.environ) -> None:
        self.path, self.env = Path(path), env
        self._lock = threading.RLock()
        self._key: bytes | None = None
        self._salt = b""
        self._data: dict[str, Any] = {"account": None}
        self.readable, self.problem = True, ""

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        import hashlib
        return base64.urlsafe_b64encode(hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32))

    def unlock(self, password: str) -> None:
        with self._lock:
            self._salt = secrets.token_bytes(16)
            self._key = self._derive(password, self._salt)
            self.readable, self.problem = True, ""
            if not self.path.exists():
                return
            try:
                blob = json.loads(self.path.read_text())
                self._salt = base64.b64decode(blob["salt"])
                self._key = self._derive(password, self._salt)
                from cryptography.fernet import Fernet, InvalidToken
                try:
                    raw = Fernet(self._key).decrypt(blob["token"].encode())
                except InvalidToken:
                    self.readable = False
                    self.problem = "Saved Angel One credentials cannot be opened with this dashboard password. The file was left untouched."
                    return
                data = json.loads(raw)
                self._data = {"account": data.get("account")}
            except Exception as exc:
                self.readable = False
                self.problem = f"Saved Angel One credentials could not be read ({type(exc).__name__}). The file was left untouched."

    def _save(self) -> None:
        if self._key is None or not self.readable:
            raise VaultError(self.problem or "Account vault is locked.")
        from cryptography.fernet import Fernet
        token = Fernet(self._key).encrypt(json.dumps(self._data).encode()).decode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"v": 1, "salt": base64.b64encode(self._salt).decode(), "token": token}, fh)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _env_account(self):
        keys = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN")
        if all(str(self.env.get(k, "") or "").strip() for k in keys):
            return {"id": "env", "label": "Angel One (.env)", "api_key": str(self.env["ANGEL_API_KEY"]).strip(),
                    "client_code": str(self.env["ANGEL_CLIENT_CODE"]).strip(),
                    "pin": str(self.env["ANGEL_PIN"]).strip(),
                    "totp_secret": str(self.env.get("ANGEL_TOTP_SECRET", "") or "").strip(), "source": "env"}
        return None

    def get(self, account_id: str = "angel") -> dict[str, Any]:
        if account_id == "env":
            env = self._env_account()
            if env:
                return env
        account = self._data.get("account") if self.readable else None
        if account and account_id in ("angel", account.get("id")):
            return dict(account, source="saved")
        env = self._env_account()
        if env and account_id in ("angel", "env"):
            return env
        raise VaultError("No Angel One account is saved. Add your account details first.")

    def default_id(self) -> str:
        return "env" if self._env_account() else ("angel" if self._data.get("account") else "")

    def creds(self, account_id: str = "angel") -> dict[str, str]:
        a = self.get(account_id)
        return {k: str(a.get(k, "")) for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")} if "ANGEL_API_KEY" in a else {
            "ANGEL_API_KEY": a["api_key"], "ANGEL_CLIENT_CODE": a["client_code"],
            "ANGEL_PIN": a["pin"], "ANGEL_TOTP_SECRET": a.get("totp_secret", "")}

    def public(self, connected_ids=None, **kwargs) -> list[dict]:
        a = self._env_account() or (self._data.get("account") if self.readable else None)
        if not a:
            return []
        connected = bool(connected_ids and a.get("id", "env" if a.get("source") == "env" else "angel") in connected_ids)
        return [{"id": a.get("id", "env" if a.get("source") == "env" else "angel"),
                 "label": a.get("label", "My Angel One account"), "source": a.get("source", "saved"),
                 "client_code": self._mask(a.get("client_code", "")), "api_key": self._tail(a.get("api_key", "")),
                 "has_pin": bool(a.get("pin")), "has_totp_secret": bool(a.get("totp_secret")),
                 "default": True, "connected": connected, "editable": a.get("source") != "env"}]

    @staticmethod
    def _tail(value: str) -> str:
        return "••••" + value[-4:] if len(value) > 4 else "••••"

    @staticmethod
    def _mask(value: str) -> str:
        return (value[:2] + "••••" + value[-2:]) if len(value) > 5 else "••••"

    def add(self, fields: dict[str, Any]) -> str:
        if not self.readable:
            raise VaultError(self.problem)
        if self._env_account():
            raise VaultError("An Angel One account is already configured in environment variables.")
        api_key = str(fields.get("api_key", "")).strip()
        client_code = str(fields.get("client_code", "")).strip()
        pin = str(fields.get("pin", "")).strip()
        label = str(fields.get("label", "My Angel One account")).strip() or "My Angel One account"
        totp = str(fields.get("totp_secret", "")).strip().replace(" ", "").upper()
        if not api_key or len(api_key) > 300 or any(c.isspace() for c in api_key):
            raise VaultError("Enter a valid Angel One API Key.")
        if not client_code or len(client_code) > 30:
            raise VaultError("Enter your Angel One Client Code.")
        if not pin or not pin.isdigit() or not 4 <= len(pin) <= 8:
            raise VaultError("Angel One PIN must contain 4 to 8 digits.")
        if totp:
            import pyotp
            try:
                if len(totp) < 16:
                    raise ValueError()
                pyotp.TOTP(totp).now()
            except Exception:
                raise VaultError("TOTP secret is invalid. Enter the authenticator setup key, not the six-digit code.") from None
        with self._lock:
            self._data["account"] = {"id": "angel", "label": label[:40], "api_key": api_key,
                                     "client_code": client_code, "pin": pin, "totp_secret": totp}
            self._save()
        return "angel"

    def update(self, account_id: str, fields: dict[str, Any]) -> None:
        cur = self.get(account_id)
        merged = {**cur, **{k: v for k, v in fields.items() if v not in ("", None)}}
        self._data["account"] = None
        self.add(merged)

    def delete(self, account_id: str) -> None:
        if account_id == "env":
            raise VaultError("Edit the server environment variables to remove this account.")
        with self._lock:
            self._data["account"] = None
            self._save()

    def set_default(self, account_id: str) -> None:
        self.get(account_id)

    def journal_namespace(self, account_id: str, mode: str) -> str:
        return mode + ":angel-one"
