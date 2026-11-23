from types import SimpleNamespace

from terminal.config import Config, InstrumentCfg
from terminal.services import Services
from terminal.brokers.angel_one import AngelOneOrders, AngelOneSession
from terminal.brokers.paper import PaperOrders
from terminal.market.hub import HubEngineFeed, QuoteHub


def _runtime(mode):
    cfg = Config(
        mode=mode,
        instrument="NIFTY",
        instruments={
            "NIFTY": InstrumentCfg(
                underlying="NIFTY",
                index_segment="nse_cm",
                index_token="Nifty 50",
                option_segment="nse_fo",
                strike_step=50,
                paper_lot_size=65,
            )
        },
    )
    return SimpleNamespace(
        cfg=cfg,
        mode=mode,
        _angel_ins=object(),
        session=SimpleNamespace(logged_in=True),
        hub=QuoteHub(),
        alerts=SimpleNamespace(notify=lambda *_: None),
        _ip_problem=lambda: None,
    )


def test_services_builds_paper_strategy_with_angel_instruments_and_shared_hub():
    svc = _runtime("paper")
    engine = Services._build_engine(svc)

    assert engine.instruments is svc._angel_ins
    assert isinstance(engine.feed, HubEngineFeed)
    assert engine.feed.hub is svc.hub
    assert isinstance(engine.orders, PaperOrders)


def test_services_builds_live_strategy_with_angel_one_orders():
    svc = _runtime("live")
    engine = Services._build_engine(svc)

    assert engine.instruments is svc._angel_ins
    assert isinstance(engine.feed, HubEngineFeed)
    assert isinstance(engine.orders, AngelOneOrders)
    assert engine.orders.session is svc.session


def test_angel_one_session_login_state_requires_all_session_tokens():
    session = AngelOneSession({})
    assert session.logged_in is False
    session.client = object()
    session.jwt_token = "jwt"
    assert session.logged_in is False
    session.feed_token = "feed"
    assert session.logged_in is True
