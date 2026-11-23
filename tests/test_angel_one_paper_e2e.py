"""End-to-end paper-mode integration using a fake SmartAPI boundary.

This exercises the real Services wiring, shared quote hub, simulated manual broker,
order lifecycle, and strategy-engine paper router without placing broker orders.
"""
from datetime import date
from types import SimpleNamespace

from conftest import ROOT
from terminal.brokers.base import Subscription
from terminal.brokers.paper import PaperOrders
from terminal.clock import now_ist
from terminal.config import load_config
from terminal.market.catalog import Catalog
from terminal.market.models import Instrument
from terminal.models import Tick
from terminal.services import Services
from terminal.trading.models import OrderTicket
from terminal.trading.sim import SimBroker


class FakeAngelSession:
    def __init__(self, credentials):
        self.credentials = credentials
        self.logged_in = False
        self.profile = {"clientcode": "TEST123"}

    def login(self, totp):
        assert totp == "123456"
        self.logged_in = True

    def logout(self):
        self.logged_in = False


class FakeAngelInstruments:
    def __init__(self, session, cfg):
        self.session, self.cfg = session, cfg
        self.rows = []

    def index_subscription(self, label):
        item = next(x for x in self.cfg.dashboard_indices if x["label"] == label)
        token = str(990000 + len(self.rows))
        self.rows.append((item["segment"], token, label))
        return Subscription(item["segment"], token, label)


class FakeAngelCatalog(Catalog):
    def __init__(self, instruments, cfg):
        super().__init__()
        for segment, token, label in instruments.rows:
            self.register(Instrument(
                key=f"{segment}|{token}", segment=segment, token=token,
                symbol=label, name=label, kind="INDEX",
            ))
        self.stock = Instrument(
            key="nse_cm|999001", segment="nse_cm", token="999001",
            symbol="PAPERTEST-EQ", name="PAPERTEST", kind="EQ", lot=1, tick=0.05,
        )
        self.register(self.stock)

    def warm(self):
        return None


class FakeAngelFeed:
    def __init__(self, session, cfg):
        self.callback = None
        self.subscriptions = []
        self.stopped = False

    def start(self, callback):
        self.callback = callback

    def wait_connected(self, timeout=20):
        return True

    def subscribe_keys(self, keys):
        self.subscriptions.extend(k for k in keys if k not in self.subscriptions)

    def emit(self, key, price):
        assert self.callback is not None
        self.callback(Tick(key, price, now_ist(), prev_close=price, open=price, high=price, low=price, volume=1))

    def stop(self):
        self.stopped = True


class FakeAngelChainProvider:
    def __init__(self, *args, **kwargs):
        pass


def test_paper_account_connect_manual_order_lifecycle_and_strategy_router(monkeypatch, tmp_path):
    import terminal.brokers.angel_one as angel
    import terminal.market.angel_catalog as angel_catalog
    import terminal.market.angel_chain as angel_chain

    monkeypatch.setattr(angel, "AngelOneSession", FakeAngelSession)
    monkeypatch.setattr(angel, "AngelOneInstruments", FakeAngelInstruments)
    monkeypatch.setattr(angel, "AngelOneFeed", FakeAngelFeed)
    monkeypatch.setattr(angel_catalog, "AngelOneCatalog", FakeAngelCatalog)
    monkeypatch.setattr(angel_chain, "AngelOneChainProvider", FakeAngelChainProvider)

    cfg = load_config(ROOT / "config.yaml")
    cfg.mode = "paper"
    cfg.host = "127.0.0.1"
    cfg.data_dir = str(tmp_path / "data")
    env = {
        "ANGEL_API_KEY": "fake-api-key",
        "ANGEL_CLIENT_CODE": "TEST123",
        "ANGEL_PIN": "1234",
        "DASHBOARD_PASSWORD": "test-dashboard-password",
    }
    svc = Services(cfg, env=env)
    try:
        svc.start()
        svc.connect("123456")

        assert svc.connected
        assert svc.mode == "paper"
        assert isinstance(svc.gateway, SimBroker)
        assert svc.gateway.simulated is True
        assert svc.session.logged_in is True

        stock = svc.catalog.get("nse_cm|999001")
        assert stock is not None
        svc.feed.emit(stock.key, 100.0)
        assert svc.hub.ltp(stock.key) == 100.0

        buy = svc.trading.place(OrderTicket(
            key=stock.key, side="B", qty=10, product="MIS", order_type="MKT",
        ))
        assert buy["order_id"].startswith("SIM")
        assert svc.gateway.positions()[0].net_qty == 10

        sell = svc.trading.place(OrderTicket(
            key=stock.key, side="S", qty=10, product="MIS", order_type="MKT",
        ))
        assert sell["order_id"].startswith("SIM")
        assert svc.gateway.positions()[0].net_qty == 0
        assert len(svc.gateway.trades()) == 2
        assert all(row.status == "COMPLETE" for row in svc.gateway.orders())

        resting = svc.trading.place(OrderTicket(
            key=stock.key, side="B", qty=5, product="MIS",
            order_type="L", price=90.0,
        ))
        oid = resting["order_id"]
        assert next(row for row in svc.gateway.orders() if row.id == oid).status == "OPEN"
        svc.trading.modify(
            oid, order_type="L", lots=0, qty=7, price=89.0, trigger=0.0, validity="DAY",
        )
        assert next(row for row in svc.gateway.orders() if row.id == oid).qty == 7
        svc.trading.cancel(oid)
        assert next(row for row in svc.gateway.orders() if row.id == oid).status == "CANCELLED"

        # Strategy engine uses the same authenticated Angel One feed, but a
        # separate simulated order router in paper mode (never AngelOneOrders).
        engine = svc._build_engine()
        assert isinstance(engine.orders, PaperOrders)
        assert engine.feed.hub is svc.hub
        assert engine.instruments is svc._angel_ins
        assert svc.gateway.simulated is True
    finally:
        svc.shutdown()
