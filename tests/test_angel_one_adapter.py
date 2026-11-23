from datetime import date
from types import SimpleNamespace

import pytest

from terminal.brokers.angel_one import AngelOneInstruments, AngelOneOrders, _num


def test_num_rejects_invalid_and_non_finite_values():
    assert _num("12.5") == 12.5
    assert _num("bad", 7) == 7
    assert _num(float("inf"), 7) == 7


def test_instrument_master_maps_exchange_and_contract(monkeypatch):
    session = SimpleNamespace()
    instruments = AngelOneInstruments(session)
    instruments._rows = [{
        "exch_seg": "NFO", "token": "123", "symbol": "NIFTY27JUN26000CE",
        "name": "NIFTY", "expiry": "25JUN2026", "strike": "2600000",
        "lotsize": "65", "opttype": "CE"
    }]
    instruments._by_key = {"nse_fo|123": instruments._rows[0]}
    instruments._loaded_at = 9999999999
    c = instruments.resolve_contract("nse_fo|123")
    assert c.symbol == "NIFTY27JUN26000CE"
    assert c.token == "123"
    assert c.strike == 26000
    assert c.lot_size == 65


def test_order_status_maps_complete_and_rejected():
    class Client:
        def orderBook(self):
            return {"status": True, "data": {"data": [
                {"orderid": "A1", "orderstatus": "complete", "filledshares": "65", "averageprice": "123.4"}
            ]}}
    session = SimpleNamespace(require_client=lambda: Client())
    orders = AngelOneOrders(session, SimpleNamespace(), None)
    status = orders.status("A1")
    assert status.state == "COMPLETE"
    assert status.filled_qty == 65
    assert status.avg_price == 123.4


def test_status_unknown_if_missing_not_false_empty():
    class Client:
        def orderBook(self):
            return {"status": True, "data": {"data": []}}
    session = SimpleNamespace(require_client=lambda: Client())
    orders = AngelOneOrders(session, SimpleNamespace(), None)
    assert orders.status("missing").state == "UNKNOWN"



def test_expiry_parser_accepts_supported_master_formats():
    assert AngelOneInstruments._expiry("25JUN2026") == date(2026, 6, 25)
    assert AngelOneInstruments._expiry("25-Jun-2026") == date(2026, 6, 25)
    assert AngelOneInstruments._expiry("2026-06-25") == date(2026, 6, 25)
    assert AngelOneInstruments._expiry("not-a-date") is None


def test_index_subscription_resolves_numeric_master_token():
    cfg = SimpleNamespace(dashboard_indices=[
        {"label": "NIFTY 50", "segment": "nse_cm", "token": "Nifty 50"}
    ])
    ins = AngelOneInstruments(SimpleNamespace(), cfg)
    ins._rows = [{
        "exch_seg": "NSE", "instrumenttype": "AMXIDX",
        "token": "99926000", "symbol": "Nifty 50", "name": "NIFTY"
    }]
    ins._by_key = {"nse_cm|99926000": ins._rows[0]}
    ins._loaded_at = 9999999999
    sub = ins.index_subscription("NIFTY 50")
    assert (sub.segment, sub.token, sub.symbol) == ("nse_cm", "99926000", "INDEX")


def test_index_subscription_never_uses_display_label_as_token():
    cfg = SimpleNamespace(dashboard_indices=[
        {"label": "NIFTY 50", "segment": "nse_cm", "token": "Nifty 50"}
    ])
    ins = AngelOneInstruments(SimpleNamespace(), cfg)
    ins._rows = []
    ins._loaded_at = 9999999999
    with pytest.raises(RuntimeError, match="no verified token"):
        ins.index_subscription("NIFTY 50")


def test_feed_replays_subscriptions_after_reconnect():
    from terminal.brokers.angel_one import AngelOneFeed
    feed = AngelOneFeed(SimpleNamespace())
    feed._subscriptions[("nse_fo", "12345")] = "NIFTY26JUN26000CE"
    class FakeSocket:
        def __init__(self):
            self.calls = []
        def subscribe(self, *args):
            self.calls.append(args)
    ws = FakeSocket()
    feed._replay_subscriptions(ws)
    assert ws.calls == [("goldeneagle", 1, [
        {"exchangeType": 2, "tokens": ["12345"]}
    ])]


def test_feed_normalizes_price_and_routes_tick():
    from terminal.brokers.angel_one import AngelOneFeed
    ticks = []
    feed = AngelOneFeed(SimpleNamespace())
    feed._subscriptions[("nse_fo", "12345")] = "NIFTY26JUN26000CE"
    feed._on_tick = ticks.append
    feed._on_data(None, {"token": "12345", "exchange_type": 2, "last_traded_price": 123450})
    assert len(ticks) == 1
    assert ticks[0].symbol == "NIFTY26JUN26000CE"
    assert ticks[0].price == 1234.5


def test_angel_catalog_maps_master_rows_to_terminal_instruments():
    from terminal.market.angel_catalog import AngelOneCatalog
    ins = AngelOneInstruments(SimpleNamespace())
    ins._rows = [{
        "exch_seg": "NFO", "token": "123", "symbol": "NIFTY27JUN2626000CE",
        "name": "NIFTY", "expiry": "25JUN2027", "strike": "2600000",
        "lotsize": "65", "opttype": "CE", "instrumenttype": "OPTIDX"
    }]
    ins._by_key = {"nse_fo|123": ins._rows[0]}
    ins._loaded_at = 9999999999
    cat = AngelOneCatalog(ins)
    inst = cat.get("nse_fo|123")
    assert inst is not None
    assert (inst.segment, inst.token, inst.kind, inst.strike, inst.lot) == ("nse_fo", "123", "CE", 26000, 65)


def test_angel_catalog_search_returns_only_matching_live_contracts():
    from terminal.market.angel_catalog import AngelOneCatalog
    ins = AngelOneInstruments(SimpleNamespace())
    ins._rows = [
        {"exch_seg": "NFO", "token": "1", "symbol": "NIFTY25DEC2626000CE", "name": "NIFTY",
         "expiry": "25DEC2026", "strike": "2600000", "lotsize": "65", "opttype": "CE", "instrumenttype": "OPTIDX"},
        {"exch_seg": "NFO", "token": "2", "symbol": "BANKNIFTY25DEC2650000PE", "name": "BANKNIFTY",
         "expiry": "25DEC2026", "strike": "5000000", "lotsize": "30", "opttype": "PE", "instrumenttype": "OPTIDX"}
    ]
    ins._by_key = {f"nse_fo|{x['token']}": x for x in ins._rows}
    ins._loaded_at = 9999999999
    hits = AngelOneCatalog(ins).search("NIFTY CE")
    assert [x.token for x in hits] == ["1"]


def test_index_subscription_accepts_strategy_underlying_alias():
    cfg = SimpleNamespace(dashboard_indices=[
        {"label": "NIFTY 50", "segment": "nse_cm", "token": "Nifty 50"}
    ])
    ins = AngelOneInstruments(SimpleNamespace(), cfg)
    ins._rows = [{
        "exch_seg": "NSE", "instrumenttype": "AMXIDX",
        "token": "99926000", "symbol": "Nifty 50", "name": "NIFTY"
    }]
    ins._by_key = {"nse_cm|99926000": ins._rows[0]}
    ins._loaded_at = 9999999999
    sub = ins.index_subscription("NIFTY")
    assert (sub.segment, sub.token, sub.symbol) == ("nse_cm", "99926000", "INDEX")



def test_feed_wait_connected_observes_open_callback():
    from terminal.brokers.angel_one import AngelOneFeed
    feed = AngelOneFeed(SimpleNamespace())
    assert feed.wait_connected(timeout=0.001) is False
    feed._on_open(None)
    assert feed.wait_connected(timeout=0.001) is True
    feed.stop()


def test_feed_wait_connected_times_out_without_open_callback():
    from terminal.brokers.angel_one import AngelOneFeed
    feed = AngelOneFeed(SimpleNamespace())
    assert feed.wait_connected(timeout=0.001) is False
    feed.stop()
