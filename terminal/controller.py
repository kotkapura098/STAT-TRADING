"""Owns the login session and the (one) running engine; the dashboard talks only to this."""
from __future__ import annotations

import threading
from typing import Callable

from .config import Config
from .engine import Engine


class Controller:
    def __init__(self, cfg: Config, build_engine: Callable[[], Engine], session=None) -> None:
        self.cfg, self._build, self.session = cfg, build_engine, session
        self.engine: Engine | None = None
        self._lock = threading.Lock()

    @property
    def needs_login(self) -> bool:
        return self.session is not None

    @property
    def logged_in(self) -> bool:
        return (not self.needs_login) or bool(self.session.logged_in)

    def login(self, totp: str | None) -> None:
        if not self.needs_login:
            return
        self.session.login(totp)

    def start(self) -> None:
        with self._lock:
            if self.engine and not self.engine.finished.is_set():
                raise RuntimeError("A session is already running")
            if not self.logged_in:
                raise RuntimeError("Connect your Angel One account first")
            self.engine = self._build()
            self.engine.start()

    def pause(self, paused: bool) -> None:
        self._need_engine().pause_entries(paused)

    def kill(self) -> None:
        self._need_engine().kill()

    def _need_engine(self) -> Engine:
        if not self.engine or self.engine.finished.is_set():
            raise RuntimeError("No session is running")
        return self.engine

    def snapshot(self) -> dict:
        base = {"mode": self.cfg.mode, "instrument": self.cfg.instrument,
                "needs_login": self.needs_login, "logged_in": self.logged_in,
                "max_daily_loss": self.cfg.risk.max_daily_loss,
                "my_ip": getattr(self.session, "my_ip", None) if self.needs_login else None}
        if not self.engine:
            return {**base, "phase": "IDLE", "events": [], "strategy": None}
        return {**base, **self.engine.snapshot()}
