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
