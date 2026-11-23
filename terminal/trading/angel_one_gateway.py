"""Angel One gateway implementing the terminal's manual-trading UI contract.

This adapter deliberately keeps broker-specific response parsing at the edge. It does
not turn a REST acknowledgement into a fill: callers must inspect the order book for
execution confirmation.
"""
from __future__ import annotations

from ..market.models import Instrument
from ..models import OrderError
from .models import Funds, OrderRow, OrderTicket, PositionRow, TradeRow
from ..brokers.angel_one import SEGMENT_TO_EXCHANGE, _num, _ok


def _rows(response, action: str) -> list[dict]:
    data = _ok(response, action)
    rows = data.get("data") if isinstance(data, dict) else None
    if rows is None and isinstance(data, list):
        rows = data
    if not isinstance(rows, list):
        raise OrderError(f"Angel One {action} returned an unreadable list.")
    return [r for r in rows if isinstance(r, dict)]


def _status(raw: str) -> str:
    value = raw.strip().upper().replace("_", " ")
    return {
        "COMPLETE": "COMPLETE", "COMPLETED": "COMPLETE",
        "OPEN": "OPEN", "PENDING": "OPEN", "AMO REQ RECEIVED": "OPEN",
        "TRIGGER PENDING": "TRIGGER_PENDING", "MODIFY PENDING": "OPEN",
        "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
        "REJECTED": "REJECTED",
    }.get(value, "UNKNOWN")


class AngelOneGateway:
    """Manual-trading gateway for one authenticated Angel One account."""

    simulated = False

    def __init__(self, session, instruments, hub):
        self.session, self.instruments, self.hub = session, instruments, hub
        self._tags: dict[str, str] = {}

    @property
    def client(self):
        return self.session.require_client()

    def _payload(self, inst: Instrument, ticket: OrderTicket, qty: int) -> dict:
        if inst.segment not in SEGMENT_TO_EXCHANGE:
            raise OrderError(f"Unsupported Angel One segment: {inst.segment}")
        kinds = {"MKT": "MARKET", "L": "LIMIT", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}
        if ticket.order_type not in kinds:
            raise OrderError(f"Unsupported order type: {ticket.order_type}")
        products = {"MIS": "INTRADAY", "NRML": "CARRYFORWARD", "CNC": "DELIVERY"}
        if ticket.product not in products:
            raise OrderError(f"Unsupported product: {ticket.product}")
        return {
            "variety": "NORMAL",
            "tradingsymbol": inst.symbol,
            "symboltoken": str(inst.token),
            "transactiontype": "BUY" if ticket.side == "B" else "SELL",
            "exchange": SEGMENT_TO_EXCHANGE[inst.segment],
            "ordertype": kinds[ticket.order_type],
            "producttype": products[ticket.product],
            "duration": ticket.validity,
            "price": str(ticket.price if ticket.order_type in ("L", "SL") else 0),
            "triggerprice": str(ticket.trigger if ticket.order_type in ("SL", "SL-M") else 0),
            "quantity": str(qty),
            "squareoff": "0",
            "stoploss": "0",
            "ordertag": (ticket.tag or "GET")[:20],
        }

    def place(self, inst: Instrument, ticket: OrderTicket, qty: int) -> str:
        response = self.client.placeOrder(self._payload(inst, ticket, qty))
        data = _ok(response, "place order")
        oid = data.get("orderid") or data.get("order_id")
        if not oid:
            raise OrderError("Angel One did not return an order ID. Check the order book before retrying.")
        if ticket.tag:
            self._tags[str(oid)] = ticket.tag
        return str(oid)

    def modify(self, order_id: str, *, order_type: str, qty: int, price: float,
               trigger: float, validity: str) -> None:
        rows = self.orders()
        row = next((r for r in (rows or []) if r.id == str(order_id)), None)
        if row is None:
            raise OrderError("Order was not found in the Angel One order book; it cannot be modified safely.")
        kinds = {"MKT": "MARKET", "L": "LIMIT", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}
        if order_type not in kinds:
            raise OrderError(f"Unsupported order type: {order_type}")
        payload = {
            "variety": "NORMAL", "orderid": str(order_id),
            "ordertype": kinds[order_type], "quantity": str(qty),
            "price": str(price if order_type in ("L", "SL") else 0),
            "triggerprice": str(trigger if order_type in ("SL", "SL-M") else 0),
            "duration": validity,
        }
        _ok(self.client.modifyOrder(payload), "modify order")

    def cancel(self, order_id: str) -> None:
        _ok(self.client.cancelOrder(str(order_id), "NORMAL"), "cancel order")

    def _order_rows(self) -> list[dict]:
        return _rows(self.client.orderBook(), "order book")

    def orders(self) -> list[OrderRow]:
        out = []
        for r in self._order_rows():
            oid = str(r.get("orderid") or "")
            symbol = str(r.get("tradingsymbol") or "")
            exchange = str(r.get("exchange") or "").upper()
            seg = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), "")
            token = str(r.get("symboltoken") or "")
            key = f"{seg}|{token}" if seg and token else ""
            out.append(OrderRow(
                id=oid, key=key, symbol=symbol, label=symbol,
                side="B" if str(r.get("transactiontype", "")).upper() == "BUY" else "S",
                qty=int(_num(r.get("quantity"))), filled=int(_num(r.get("filledshares"))),
                lot=1, product=str(r.get("producttype") or ""),
                order_type=str(r.get("ordertype") or ""), price=_num(r.get("price")),
                trigger=_num(r.get("triggerprice")), avg_price=_num(r.get("averageprice")),
                status=_status(str(r.get("orderstatus") or r.get("status") or "")),
                reason=str(r.get("text") or ""), time=str(r.get("updatetime") or r.get(" exchorderupdatetime") or ""),
                validity=str(r.get("duration") or "DAY"), tag=self._tags.get(oid, str(r.get("ordertag") or ""))))
        return out

    def trades(self) -> list[TradeRow]:
        rows = _rows(self.client.tradeBook(), "trade book")
        orders = {o.id: o for o in self.orders()}
        out = []
        for r in rows:
            oid = str(r.get("orderid") or "")
            order = orders.get(oid)
            symbol = str(r.get("tradingsymbol") or (order.symbol if order else ""))
            out.append(TradeRow(
                order_id=oid, key=order.key if order else "", symbol=symbol, label=symbol,
                side="B" if str(r.get("transactiontype", "")).upper() == "BUY" else "S",
                qty=int(_num(r.get("fillsize") or r.get("quantity"))),
                price=_num(r.get("fillprice") or r.get("price")),
                product=str(r.get("producttype") or ""), time=str(r.get("filltime") or r.get("exchtime") or ""),
                trade_id=str(r.get("tradeid") or r.get("fillid") or "")))
        return out

    def positions(self) -> list[PositionRow]:
        rows = _rows(self.client.position(), "positions")
        out = []
        for r in rows:
            exchange = str(r.get("exchange") or "").upper()
            seg = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), "")
            token = str(r.get("symboltoken") or "")
            key = f"{seg}|{token}" if seg and token else ""
            symbol = str(r.get("tradingsymbol") or "")
            net = int(_num(r.get("netqty")))
            buy_qty = int(_num(r.get("buyqty")))
            sell_qty = int(_num(r.get("sellqty")))
            buy_avg = _num(r.get("buyavgprice"))
            sell_avg = _num(r.get("sellavgprice"))
            avg = _num(r.get("avgnetprice") or r.get("netprice"))
            ltp = self.hub.ltp(key) if key else None
            if ltp is None and r.get("ltp") not in (None, ""):
                ltp = _num(r.get("ltp"))
            realised = _num(r.get("realised"))
            unrealised = _num(r.get("unrealised"))
            if r.get("pnl") not in (None, "") and not (realised or unrealised):
                unrealised = _num(r.get("pnl"))
            out.append(PositionRow(
                key=key, symbol=symbol, label=symbol, segment=seg,
                product=str(r.get("producttype") or ""), lot=max(int(_num(r.get("lotsize"), 1)), 1),
                buy_qty=buy_qty, sell_qty=sell_qty, net_qty=net,
                buy_avg=buy_avg, sell_avg=sell_avg, avg_price=avg if net else 0.0,
                ltp=ltp, realised=realised, unrealised=unrealised,
                expiry=str(r.get("expiry") or "") or None))
        return out

    def holdings(self) -> list[dict]:
        return _rows(self.client.holding(), "holdings")

    def funds(self) -> Funds:
        data = _ok(self.client.rmsLimit(), "funds")
        if isinstance(data.get("data"), dict):
            data = data["data"]
        available = _num(data.get("net") or data.get("availablecash") or data.get("availablemargin"))
        used = _num(data.get("utiliseddebits") or data.get("utilisedmargin") or data.get("margins"))
        return Funds(available=available, used=used, collateral=_num(data.get("collateral")),
                     realised=None, unrealised=None, simulated=False,
                     note="Values are mapped from Angel One RMS limits; verify field availability against your account response.",
                     extra={k: v for k, v in data.items() if k not in {"net", "availablecash", "availablemargin", "utiliseddebits", "utilisedmargin", "margins", "collateral"}})
