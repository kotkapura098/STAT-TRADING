from datetime import date
from types import SimpleNamespace

from terminal.brokers.angel_one import AngelOneOrders
from terminal.models import Contract, OrderRequest
from terminal.trading.angel_gateway import AngelOneGateway


def test_strategy_order_defaults_to_intraday_day():
    req = OrderRequest(Contract("NIFTY26DEC26000CE", "123", "nse_fo", 26000, "CE", date(2026, 12, 31), 65),
                       "S", 65, "MKT")
    assert req.product_type == "INTRADAY"
    assert req.validity == "DAY"


def test_smartapi_order_payload_honours_product_and_validity():
    captured = {}

    class Client:
        def placeOrder(self, payload):
            captured.update(payload)
            return {"status": True, "data": {"orderid": "ORD-1"}}

    class Instruments:
        def get_row(self, key):
            return {"token": "123"}

    session = SimpleNamespace(require_client=lambda: Client())
    orders = AngelOneOrders(session, Instruments())
    req = OrderRequest(Contract("NIFTY26DEC26000CE", "123", "nse_fo", 26000, "CE", date(2026, 12, 31), 65),
                       "B", 65, "L", 100.0, 0.0, "manual",
                       product_type="CARRYFORWARD", validity="IOC")
    assert orders.place(req) == "ORD-1"
    assert captured["producttype"] == "CARRYFORWARD"
    assert captured["duration"] == "IOC"
    assert captured["quantity"] == "65"


def test_manual_gateway_maps_selected_products_and_validity():
    calls = {}

    class Orders:
        def place(self, req):
            calls["req"] = req
            return "ORD-2"

    gateway = AngelOneGateway.__new__(AngelOneGateway)
    gateway.ins = SimpleNamespace(resolve_contract=lambda key: Contract(
        "NIFTY26DEC26000CE", "123", "nse_fo", 26000, "CE", date(2026, 12, 31), 65))
    gateway.orders_api = Orders()
    gateway._tags = {}
    inst = SimpleNamespace(kind="CE", key="nse_fo|123")
    ticket = SimpleNamespace(side="B", order_type="L", price=100.0, trigger=0.0,
                             tag="manual", product="NRML", validity="IOC")
    assert gateway.place(inst, ticket, 65) == "ORD-2"
    assert calls["req"].product_type == "CARRYFORWARD"
    assert calls["req"].validity == "IOC"


def test_funds_realised_and_unrealised_are_not_swapped():
    gateway = AngelOneGateway.__new__(AngelOneGateway)
    gateway.orders_api = SimpleNamespace(funds=lambda: {
        "availablecash": "10000", "utiliseddebits": "2000",
        "collateral": "500", "m2mrealized": "120", "m2munrealised": "-35"
    })
    funds = gateway.funds()
    assert funds.available == 10000
    assert funds.used == 2000
    assert funds.realised == 120
    assert funds.unrealised == -35


def test_holdings_are_exposed_as_normalized_rows():
    gateway = AngelOneGateway.__new__(AngelOneGateway)
    gateway.orders_api = SimpleNamespace(holdings=lambda: [{
        "symboltoken": "999", "exchange": "NSE", "tradingsymbol": "ABC-EQ",
        "quantity": "10", "averageprice": "125.5", "profitandloss": "20"
    }])
    gateway.cat = SimpleNamespace(get=lambda key: None)
    gateway.hub = SimpleNamespace(ltp=lambda key: 130.0)
    rows = gateway.holdings()
    assert rows == [{
        "key": "nse_cm|999", "symbol": "ABC-EQ", "quantity": 10,
        "average_price": 125.5, "ltp": 130.0, "pnl": 20.0, "product": "DELIVERY"
    }]
