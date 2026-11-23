"""Trading-mode safety checks for the single Angel One account."""
import pytest

from conftest import ROOT
from terminal.config import load_config
from terminal.services import Services


def make(tmp_path, mode="paper", env=None, host=None):
    cfg = load_config(ROOT / "config.yaml")
    cfg.mode = mode
    cfg.data_dir = str(tmp_path / "data")
    if host:
        cfg.host = host
    return Services(cfg, env={} if env is None else env)


def test_sample_to_paper_discards_demo_state_and_waits_for_angel_one_login(tmp_path):
    svc = make(tmp_path, mode="sample")
    svc.start()
    try:
        assert svc.connected and svc.mode == "sample"
        old_feed = svc.feed
        svc.set_mode("paper")
        assert svc.mode == svc.cfg.mode == "paper"
        assert svc.needs_connect and not svc.connected
        assert svc.feed is None and old_feed._stop.is_set()
        assert svc.hub.wanted() == []
        assert svc.catalog.inner is None
        assert svc.chain.provider is None
        assert svc.account is None and svc.gateway is None
    finally:
        svc.shutdown()


def test_live_mode_remains_locked_even_with_confirmation_and_allow_flag(tmp_path):
    svc = make(tmp_path, env={"ALLOW_LIVE": "yes"})
    try:
        with pytest.raises(RuntimeError, match="Live order placement is disabled"):
            svc.set_mode("live", "REAL")
        assert svc.mode == "paper" and not svc.connected
        assert svc.mode_info()["can"]["live"] is False
    finally:
        svc.shutdown()


def test_live_mode_is_locked_without_flag_or_when_terminal_is_network_exposed(tmp_path):
    locked = make(tmp_path, env={})
    try:
        with pytest.raises(RuntimeError, match="Live order placement is disabled"):
            locked.set_mode("live", "REAL")
    finally:
        locked.shutdown()

    exposed = make(tmp_path / "exposed", env={"ALLOW_LIVE": "yes"}, host="0.0.0.0")
    try:
        with pytest.raises(RuntimeError, match="Live order placement is disabled"):
            exposed.set_mode("live", "REAL")
    finally:
        exposed.shutdown()


def test_open_or_unreadable_angel_one_exposure_blocks_leaving_live_mode(tmp_path):
    from terminal.trading.models import PositionRow

    svc = make(tmp_path, mode="live", env={"ALLOW_LIVE": "yes"})
    class FakeGateway:
        def __init__(self, positions=None, orders=None):
            self._positions = positions
            self._orders = orders
        def positions(self):
            return self._positions
        def orders(self):
            return self._orders

    try:
        svc.connected = True
        svc.accounts["angel"] = {"id": "angel", "label": "Angel One"}
        svc.active_account_id = "angel"
        svc.gateways["angel"] = FakeGateway(None, [])
        assert not svc.mode_info()["can"]["paper"]
        with pytest.raises(RuntimeError, match="open real positions or orders"):
            svc.set_mode("paper")

        open_position = PositionRow(
            key="nse_fo|1", symbol="X", label="X", segment="nse_fo", product="MIS",
            lot=1, buy_qty=0, sell_qty=1, net_qty=-1, buy_avg=0, sell_avg=10,
            avg_price=10, ltp=10, realised=0, unrealised=0,
        )
        svc.gateways["angel"] = FakeGateway([open_position], [])
        with pytest.raises(RuntimeError, match="open real positions or orders"):
            svc.set_mode("paper")

        svc.gateways["angel"] = FakeGateway([], [])
        assert svc.set_mode("paper")["mode"] == "paper"
    finally:
        svc.shutdown()


def test_running_strategy_blocks_mode_change(tmp_path, monkeypatch):
    svc = make(tmp_path, env={"ALLOW_LIVE": "yes"})
    try:
        monkeypatch.setattr(svc.rules, "running", lambda: [1])
        with pytest.raises(RuntimeError, match="Stop your running strategies"):
            svc.set_mode("live", "REAL")
    finally:
        svc.shutdown()
