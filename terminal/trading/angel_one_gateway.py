"""Manual-trading gateway backed by Angel One SmartAPI."""
from __future__ import annotations

import logging
from datetime import datetime

from ..models import OrderError
from ..market.models import Instrument
from ..clock import now_ist
from .models import Funds, OrderRow, PositionRow, TradeRow
from ..brokers.angel_one import SEGMENT_TO_EXCHANGE, EXCHANGE_TO_SEGMENT, _ok, _num

log = logging.getLogger("angel-one-gateway")


def _data(client, method: str):
    response = getattr(client, method)()
    data = _ok(response, method)
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise OrderError(f"Angel One {method} returned an unreadable response.")
    return rows


class AngelOneGateway:
    simulated = False

    def __init__(self, session, catalog, hub):
        self.s, self.cat, self.hub = session, catalog, hub
        self._tags: dict[str, str] = {}

    @property
    def client(self):
        return self.s.require_client()

    def _instrument(self, exchange: str, token: str):
        seg = EXCHANGE_TO_SEGMENT.get(str(exchange).upper())
        return self.cat.get(f"{seg}|{token}") if seg else None

    def place(self, inst: Instrument, ticket, qty: int) -> str:
        if inst.segment not in SEGMENT_TO_EXCHANGE:
            raise OrderError("Unsupported Angel One exchange segment.")
        order_type = {"L": "LIMIT", "MKT": "MARKET", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}.get(ticket.order_type)
        if not order_type:
            raise OrderError("Unsupported order type for Angel One.")
        product = {"MIS": "INTRADAY", "NRML": "CARRYFORWARD", "CNC": "DELIVERY"}.get(ticket.product)
        if not product:
            raise OrderError("Unsupported Angel One product type.")
        payload = {"variety": "NORMAL", "tradingsymbol": inst.symbol, "symboltoken": inst.token,
                   "transactiontype": "BUY" if ticket.side == "B" else "SELL",
                   "exchange": SEGMENT_TO_EXCHANGE[inst.segment], "ordertype": order_type,
                   "producttype": product, "duration": ticket.validity,
                   "price": str(ticket.price if ticket.order_type in ("L", "SL") else 0),
                   "triggerprice": str(ticket.trigger or 0), "quantity": str(qty),
                   "squareoff": "0", "stoploss": "0", "ordertag": (ticket.tag or "GET")[:20]}
        try:
            result = _ok(self.client.placeOrder(payload), "place order")
        except Exception as exc:
            # A network failure after submission is ambiguous. Never auto-retry.
            raise OrderError(f"Angel One order submission outcome is uncertain. Check the broker order book before retrying: {exc}") from None
        oid = result.get("orderid") or result.get("order_id")
        if not oid:
            raise OrderError("Angel One returned no order ID. Check the broker order book before retrying.")
        self._tags[str(oid)] = ticket.tag or ""
        return str(oid)

    def cancel(self, order_id: str) -> None:
        _ok(self.client.cancelOrder(str(order_id), "NORMAL"), "cancel order")

    def modify(self, order_id: str, *, order_type: str, qty: int, price: float, trigger: float, validity: str) -> None:
        rows = self.orders()
        row = next((x for x in rows or [] if x.id == str(order_id)), None)
        if row is None or not row.is_open:
            raise OrderError("Angel One order is not open or could not be found.")
        mapped = {"L": "LIMIT", "MKT": "MARKET", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}.get(order_type)
        if not mapped:
            raise OrderError("Unsupported order type.")
        inst = self.cat.get(row.key) if row.key else None
        if inst is None:
            raise OrderError("Cannot safely modify this order: its Angel One instrument mapping is unavailable.")
        product = {"MIS": "INTRADAY", "NRML": "CARRYFORWARD", "CNC": "DELIVERY"}.get(row.product, row.product)
        payload = {"variety": "NORMAL", "orderid": str(order_id), "tradingsymbol": inst.symbol,
                   "symboltoken": inst.token, "exchange": SEGMENT_TO_EXCHANGE[inst.segment],
                   "ordertype": mapped, "producttype": product, "duration": validity,
                   "quantity": str(qty), "price": str(price if order_type in ("L", "SL") else 0),
                   "triggerprice": str(trigger or 0)}
        _ok(self.client.modifyOrder(payload), "modify order")

    def orders(self):
        try:
            rows = _data(self.client, "orderBook")
        except Exception:
            log.exception("Angel One order book read failed")
            return None
        out = []
        for r in rows:
            exchange, token = str(r.get("exchange") or ""), str(r.get("symboltoken") or "")
            inst = self._instrument(exchange, token)
            oid = str(r.get("orderid") or "")
            raw = str(r.get("orderstatus") or r.get("status") or "").upper()
            status = {"COMPLETE":"COMPLETE","OPEN":"OPEN","PENDING":"OPEN","TRIGGER PENDING":"TRIGGER_PENDING",
                      "REJECTED":"REJECTED","CANCELLED":"CANCELLED","CANCELED":"CANCELLED"}.get(raw, "UNKNOWN")
            qty = int(_num(r.get("quantity")))
            out.append(OrderRow(id=oid, key=inst.key if inst else "", symbol=str(r.get("tradingsymbol") or ""),
                label=inst.label() if inst else str(r.get("tradingsymbol") or ""), side="B" if str(r.get("transactiontype")).upper()=="BUY" else "S",
                qty=qty, filled=int(_num(r.get("filledshares"))), lot=inst.lot if inst else 1,
                product=str(r.get("producttype") or ""), order_type=str(r.get("ordertype") or ""),
                price=_num(r.get("price")), trigger=_num(r.get("triggerprice")), avg_price=_num(r.get("averageprice")),
                status=status, reason=str(r.get("text") or ""), time=str(r.get("updatetime") or r.get("exchtime") or ""),
                validity=str(r.get("duration") or "DAY"), tag=self._tags.get(oid, str(r.get("ordertag") or ""))))
        return out

    def trades(self):
        try:
            rows = _data(self.client, "tradeBook")
        except Exception:
            log.exception("Angel One trade book read failed")
            return None
        orders = {o.id: o for o in (self.orders() or [])}
        out=[]
        for r in rows:
            oid=str(r.get("orderid") or "")
            order=orders.get(oid)
            exchange,token=str(r.get("exchange") or ""),str(r.get("symboltoken") or "")
            inst=self._instrument(exchange,token)
            out.append(TradeRow(order_id=oid,key=inst.key if inst else (order.key if order else ""),
                symbol=str(r.get("tradingsymbol") or ""),label=inst.label() if inst else str(r.get("tradingsymbol") or ""),
                side="B" if str(r.get("transactiontype")).upper()=="BUY" else "S",
                qty=int(_num(r.get("fillshares") or r.get("quantity"))),price=_num(r.get("fillprice") or r.get("price")),
                product=str(r.get("producttype") or ""),time=str(r.get("filltime") or r.get("exchtime") or ""),
                trade_id=str(r.get("tradeid") or r.get("fillid") or "")))
        return out

    def positions(self):
        try:
            rows = _data(self.client, "position")
        except Exception:
            log.exception("Angel One positions read failed")
            return None
        out=[]
        for r in rows:
            exchange,token=str(r.get("exchange") or ""),str(r.get("symboltoken") or "")
            inst=self._instrument(exchange,token)
            if not inst:
                continue
            net=int(_num(r.get("netqty")))
            bq=int(_num(r.get("buyqty"))); sq=int(_num(r.get("sellqty")))
            ba=_num(r.get("buyamount")); sa=_num(r.get("sellamount"))
            buy_avg=_num(r.get("buyavgprice")); sell_avg=_num(r.get("sellavgprice"))
            ltp=self.hub.ltp(inst.key) or (_num(r.get("ltp")) or None)
            realised=_num(r.get("realised")) + _num(r.get("realisedprofitloss"))
            unreal=_num(r.get("unrealised")) + _num(r.get("unrealisedprofitloss"))
            if not (realised or unreal) and ltp is not None:
                unreal=(ltp-(buy_avg if net>0 else sell_avg))*net if net else 0.0
            out.append(PositionRow(key=inst.key,symbol=inst.symbol,label=inst.label(),segment=inst.segment,
                product=str(r.get("producttype") or ""),lot=inst.lot,buy_qty=bq,sell_qty=sq,net_qty=net,
                buy_avg=buy_avg,sell_avg=sell_avg,avg_price=buy_avg if net>0 else (sell_avg if net<0 else 0),
                ltp=ltp,realised=realised,unrealised=unreal,expiry=inst.expiry.isoformat() if inst.expiry else None))
        return out

    def funds(self):
        try:
            data=_ok(self.client.rmsLimit(),"funds")
        except Exception:
            log.exception("Angel One funds read failed")
            return None
        if not isinstance(data,dict):
            return None
        available=_num(data.get("availablecash") or data.get("net"))
        used=_num(data.get("utiliseddebits") or data.get("utilisedamount"))
        return Funds(available=available,used=used,collateral=_num(data.get("collateral")),
                     simulated=False,note="Values reported by Angel One RMS limits.",extra=data)
