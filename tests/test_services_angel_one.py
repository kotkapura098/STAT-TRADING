from types import SimpleNamespace

import terminal.services as services_module
from terminal.services import Services


def test_live_engine_uses_angel_one_orders_and_shared_quote_hub(monkeypatch):
    seen = {}

    class FakeEngine:
        def __init__(self, cfg, instruments, feed, orders):
            seen.update(cfg=cfg, instruments=instruments, feed=feed, orders=orders)

    class FakeOrders:
        def __init__(self, session, instruments, cfg, ip_check=None):
            seen["orders_args"] = (session, instruments, cfg)

    class FakeHubFeed:
        def __init__(self, hub):
            seen["hub"] = hub

    monkeypatch.setattr(services_module, "Engine", FakeEngine)
    monkeypatch.setattr("terminal.brokers.angel_one.AngelOneOrders", FakeOrders)
    monkeypatch.setattr(services_module, "HubEngineFeed", FakeHubFeed)

    svc = Services.__new__(Services)
    svc.mode = "live"
    svc.cfg = object()
    svc._angel_ins = object()
    svc._default_session = SimpleNamespace(logged_in=True)
    svc.sessions = {"angel-one": svc._default_session}
    svc.active_account_id = "angel-one"
    svc.hub = object()
    svc.alerts = SimpleNamespace(notify=lambda *args: None)

    engine = svc._build_engine()
    assert isinstance(engine, FakeEngine)
    assert seen["instruments"] is svc._angel_ins
    assert isinstance(seen["orders"], FakeOrders)
    assert seen["orders_args"] == (svc.session, svc._angel_ins, svc.cfg)
    assert seen["hub"] is svc.hub


def test_paper_engine_keeps_simulated_orders_with_angel_one_feed(monkeypatch):
    seen = {}

    class FakeEngine:
        def __init__(self, cfg, instruments, feed, orders):
            seen.update(cfg=cfg, instruments=instruments, feed=feed, orders=orders)

    class FakeHubFeed:
        def __init__(self, hub):
            seen["hub"] = hub

    monkeypatch.setattr(services_module, "Engine", FakeEngine)
    monkeypatch.setattr(services_module, "HubEngineFeed", FakeHubFeed)

    svc = Services.__new__(Services)
    svc.mode = "paper"
    svc.cfg = object()
    svc._angel_ins = object()
    svc._default_session = SimpleNamespace(logged_in=True)
    svc.sessions = {"angel-one": svc._default_session}
    svc.active_account_id = "angel-one"
    svc.hub = object()
    svc.alerts = SimpleNamespace(notify=lambda *args: None)

    engine = svc._build_engine()
    from terminal.brokers.paper import PaperOrders
    assert isinstance(engine, FakeEngine)
    assert isinstance(seen["orders"], PaperOrders)
    assert seen["instruments"] is svc._angel_ins
    assert seen["hub"] is svc.hub
