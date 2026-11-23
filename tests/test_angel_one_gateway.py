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
        "average_price": 125.5, "ltp": 130.0, "pnl": 20.0, "product": "CNC"
    }]


def test_smartapi_place_order_string_response_is_used_as_order_id():
    class Client:
        def placeOrder(self, payload):
            return "ORD-STRING"

    class Instruments:
        def get_row(self, key):
            return {"token": "123"}

    session = SimpleNamespace(require_client=lambda: Client())
    orders = AngelOneOrders(session, Instruments())
    req = OrderRequest(Contract("NIFTY26DEC26000CE", "123", "nse_fo", 26000, "CE",
                                date(2026, 12, 31), 65), "B", 65, "MKT")
    assert orders.place(req) == "ORD-STRING"


def test_smartapi_prefers_full_place_order_response_when_available():
    captured = {}

    class Client:
        def placeOrderFullResponse(self, payload):
            captured.update(payload)
            return {"status": True, "data": {"orderid": "ORD-FULL"}}
        def placeOrder(self, payload):
            raise AssertionError("placeOrder() should not be used when full response exists")

    class Instruments:
        def get_row(self, key):
            return {"token": "123"}

    orders = AngelOneOrders(SimpleNamespace(require_client=lambda: Client()), Instruments())
    req = OrderRequest(Contract("NIFTY26DEC26000CE", "123", "nse_fo", 26000, "CE",
                                date(2026, 12, 31), 65), "S", 65, "L", 101.0)
    assert orders.place(req) == "ORD-FULL"
    assert captured["transactiontype"] == "SELL"


def test_angel_one_session_refresh_keeps_websocket_authorization_header_format(monkeypatch):
    import base64
    import json
    from terminal.brokers.angel_one import AngelOneSession

    class Client:
        def generateToken(self, refresh_token):
            return {"status": True, "data": {"jwtToken": "new-jwt", "feedToken": "new-feed"}}
        def getfeedToken(self):
            return "new-feed"

    session = AngelOneSession({})
    session.client = Client()
    session.refresh_token = "refresh"
    session.jwt_token = "Bearer old-jwt"
    session.feed_token = "old-feed"
    session.refresh()
    assert session.jwt_token == "Bearer new-jwt"
    assert session.feed_token == "new-feed"

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
    session.jwt_token = "Bearer header." + payload + ".signature"
    refreshed = []
    session.refresh = lambda: refreshed.append(True)
    session.ensure_fresh(skew_seconds=120)
    assert refreshed == [True]



def test_unreadable_angel_one_account_books_are_not_reported_as_empty():
    from terminal.trading.service import TradingService

    class Unavailable:
        def positions(self):
            return None
        def orders(self):
            return None
        def trades(self):
            return None

    service = object.__new__(TradingService)
    service.all_gateways = lambda: {"angel": Unavailable()}
    assert service.positions_view()["ok"] is False
    assert service.positions_view()["reason"] == "broker_unavailable"
    assert service.orders_view()["ok"] is False
    assert service.orders_view()["reason"] == "broker_unavailable"
    assert service.trades_view()["ok"] is False
    assert service.trades_view()["reason"] == "broker_unavailable"



def test_ambiguous_modify_and_cancel_transport_failures_raise_order_unknown():
    import pytest
    from requests.exceptions import ReadTimeout
    from terminal.models import OrderUnknown
    from terminal.trading.angel_gateway import AngelOneGateway

    gateway = object.__new__(AngelOneGateway)
    gateway.orders_api = SimpleNamespace(
        cancel=lambda order_id: (_ for _ in ()).throw(ReadTimeout("timeout")),
        modify=lambda order_id, payload: (_ for _ in ()).throw(ReadTimeout("timeout")),
    )
    with pytest.raises(OrderUnknown, match="cancel request may have reached"):
        gateway.cancel("OID-1")

    row = SimpleNamespace(id="OID-1", key="nse_fo|123", product="MIS")
    instrument = SimpleNamespace(symbol="NIFTY26DEC26000CE", token="123", segment="nse_fo")
    gateway.orders = lambda: [row]
    gateway.cat = SimpleNamespace(get=lambda key: instrument)
    with pytest.raises(OrderUnknown, match="modify request may have reached"):
        gateway.modify("OID-1", order_type="L", qty=65, price=100, trigger=0, validity="DAY")



def test_angel_one_account_books_normalize_product_types_for_squareoff_and_journal():
    from datetime import date
    from terminal.trading.angel_gateway import AngelOneGateway

    instrument = SimpleNamespace(
        symbol="NIFTY26DEC26000CE", token="123", segment="nse_fo", lot=65,
        expiry=date(2026, 12, 31), label=lambda: "NIFTY 26000 CE"
    )

    class Client:
        def position(self):
            return {"status": True, "data": [{
                "symboltoken": "123", "exchange": "NFO", "tradingsymbol": instrument.symbol,
                "netqty": "-65", "buyqty": "0", "sellqty": "65", "buyavgprice": "0",
                "sellavgprice": "100", "avgnetprice": "-100", "ltp": "80",
                "realised": "0", "unrealised": "1300", "producttype": "CARRYFORWARD"
            }]}

    class Orders:
        def order_book(self):
            return [{"orderid": "OID", "symboltoken": "123", "exchange": "NFO",
                     "tradingsymbol": instrument.symbol, "transactiontype": "SELL",
                     "quantity": "65", "filledshares": "0", "producttype": "INTRADAY",
                     "ordertype": "LIMIT", "orderstatus": "open", "price": "100"}]
        def trade_book(self):
            return [{"orderid": "OID", "symboltoken": "123", "exchange": "NFO",
                     "tradingsymbol": instrument.symbol, "transactiontype": "SELL",
                     "fillsize": "65", "fillprice": "100", "producttype": "CARRYFORWARD"}]
        def _list_data(self, response, label):
            return response["data"]

    gateway = AngelOneGateway.__new__(AngelOneGateway)
    gateway.orders_api = Orders()
    gateway.s = SimpleNamespace(require_client=lambda: Client())
    gateway.cat = SimpleNamespace(get=lambda key: instrument)
    gateway.hub = SimpleNamespace(ltp=lambda key: 80)
    gateway._tags = {}

    assert gateway.orders()[0].product == "MIS"
    assert gateway.trades()[0].product == "NRML"
    assert gateway.positions()[0].product == "NRML"



def test_static_ip_mismatch_blocks_angel_one_place_modify_and_cancel_before_network():
    import pytest
    from datetime import date
    from terminal.brokers.angel_one import AngelOneOrders
    from terminal.models import Contract, OrderError, OrderRequest

    class Client:
        def placeOrder(self, payload):
            raise AssertionError("order request must be blocked before reaching SmartAPI")
        def modifyOrder(self, variety, payload):
            raise AssertionError("modify request must be blocked before reaching SmartAPI")
        def cancelOrder(self, order_id, variety):
            raise AssertionError("cancel request must be blocked before reaching SmartAPI")

    class Instruments:
        def get_row(self, key):
            return {"token": "123"}

    orders = AngelOneOrders(
        SimpleNamespace(require_client=lambda: Client()),
        Instruments(),
        ip_check=lambda: "Outbound IP does not match the Angel One allowlist.",
    )
    contract = Contract("NIFTY26DEC26000CE", "123", "nse_fo", 26000, "CE",
                        date(2026, 12, 31), 65)
    request = OrderRequest(contract, "B", 65, "L", 100.0)
    with pytest.raises(OrderError, match="blocked"):
        orders.place(request)
    with pytest.raises(OrderError, match="blocked"):
        orders.modify("OID-1", {"orderid": "OID-1"})
    with pytest.raises(OrderError, match="blocked"):
        orders.cancel("OID-1")



def test_individual_order_status_uses_unique_order_id_instead_of_order_book():
    from terminal.brokers.angel_one import AngelOneOrders

    class Client:
        def individual_order_details(self, unique_order_id):
            assert unique_order_id == "UNIQUE-1"
            return {"status": True, "data": {
                "orderid": "OID-1", "uniqueorderid": "UNIQUE-1",
                "orderstatus": "complete", "filledshares": "65", "averageprice": "102.5"
            }}
        def orderBook(self):
            raise AssertionError("status polling must not use the 1-request/sec full order book")

    orders = AngelOneOrders(
        SimpleNamespace(require_client=lambda: Client()),
        SimpleNamespace(get_row=lambda key: None),
    )
    orders._unique_order_ids["OID-1"] = "UNIQUE-1"
    status = orders.status("OID-1")
    assert status.state == "COMPLETE"
    assert status.filled_qty == 65
    assert status.avg_price == 102.5


def test_smartapi_sliding_window_rate_limiter_enforces_configured_limit():
    import time
    from terminal.brokers.angel_one import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(2, 0.05)
    limiter.acquire()
    limiter.acquire()
    started = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - started >= 0.035
