from types import SimpleNamespace

from terminal.market.models import Instrument
from terminal.trading.angel_one_gateway import AngelOneGateway
from terminal.trading.models import OrderTicket


class FakeClient:
    def __init__(self):
        self.placed = None

    def placeOrder(self, payload):
        self.placed = payload
        return {"status": True, "data": {"orderid": "AO-1"}}

    def orderBook(self):
        return {"status": True, "data": {"data": [{
            "orderid": "AO-1", "tradingsymbol": "NIFTY26DEC25000CE",
            "exchange": "NFO", "symboltoken": "12345", "transactiontype": "BUY",
            "quantity": "75", "filledshares": "75", "orderstatus": "complete",
            "averageprice": "120.5", "ordertag": "manual", "producttype": "INTRADAY",
            "ordertype": "MARKET", "duration": "DAY"
        }]}}

    def tradeBook(self):
        return {"status": True, "data": [{"orderid": "AO-1", "tradingsymbol": "NIFTY26DEC25000CE",
            "transactiontype": "BUY", "fillsize": "75", "fillprice": "120.5", "tradeid": "T-1"}]}


def setup_gateway():
    client = FakeClient()
    session = SimpleNamespace(require_client=lambda: client)
    instruments = SimpleNamespace()
    hub = SimpleNamespace(ltp=lambda key: 121.0)
    return AngelOneGateway(session, instruments, hub), client


def test_manual_order_payload_uses_angel_one_fields_and_returns_order_id():
    gateway, client = setup_gateway()
    inst = Instrument("nse_fo|12345", "nse_fo", "12345", "NIFTY26DEC25000CE",
                      "NIFTY", "CE", lot=75)
    ticket = OrderTicket(key=inst.key, side="B", lots=1, product="MIS",
                         order_type="L", price=120.5, tag="manual")
    assert gateway.place(inst, ticket, 75) == "AO-1"
    assert client.placed["exchange"] == "NFO"
    assert client.placed["transactiontype"] == "BUY"
    assert client.placed["quantity"] == "75"
    assert client.placed["price"] == "120.5"
    assert client.placed["producttype"] == "INTRADAY"


def test_order_book_and_trade_book_are_parsed_as_distinct_confirmations():
    gateway, _ = setup_gateway()
    order = gateway.orders()[0]
    trade = gateway.trades()[0]
    assert order.id == "AO-1"
    assert order.status == "COMPLETE"
    assert order.filled == 75
    assert order.avg_price == 120.5
    assert trade.order_id == "AO-1"
    assert trade.trade_id == "T-1"
    assert trade.qty == 75
