"""Manual-trading gateway adapting Angel One SmartAPI to terminal UI models."""
from __future__ import annotations

import logging
from requests.exceptions import RequestException
from ..brokers.angel_one import AngelOneOrders, SEGMENT_TO_EXCHANGE, _num
from ..models import OrderRequest, OrderError, OrderUnknown
from ..market.models import Instrument
from .models import Funds, OrderRow, PositionRow, TradeRow

log = logging.getLogger("angel-one-gateway")


class AngelOneGateway:
    simulated = False

    def __init__(self, session, instruments, catalog, hub, cfg=None, ip_check=None):
        self.s, self.ins, self.cat, self.hub = session, instruments, catalog, hub
        self.orders_api = AngelOneOrders(session, instruments, cfg, ip_check=ip_check)
        self._tags = {}

    def place(self, inst: Instrument, ticket, qty: int) -> str:
        contract = self.ins.resolve_contract(inst.key) if inst.kind in ("CE", "PE") else None
        if contract is None:
            from ..models import Contract
            from datetime import date
            contract = Contract(inst.symbol, inst.token, inst.segment, 0, "", inst.expiry or date.today(), inst.lot)
        product_type = {"MIS": "INTRADAY", "NRML": "CARRYFORWARD", "CNC": "DELIVERY"}.get(ticket.product)
        if product_type is None:
            raise OrderError(f"Unsupported Angel One product: {ticket.product}")
        req = OrderRequest(contract, ticket.side, qty, ticket.order_type, ticket.price, ticket.trigger,
                           ticket.tag, product_type=product_type, validity=ticket.validity)
        try:
            oid = self.orders_api.place(req)
        except (TimeoutError, ConnectionError, RequestException) as exc:
            raise OrderUnknown("Angel One order request may have reached the broker but its reply was not received; verify the order book before retrying.") from exc
        if ticket.tag:
            self._tags[oid] = ticket.tag
        return oid

    def modify(self, order_id, *, order_type, qty, price, trigger, validity):
        row = next((r for r in self.orders() or [] if r.id == str(order_id)), None)
        if row is None or not row.key:
            raise OrderError("Order not found in Angel One order book.")
        inst = self.cat.get(row.key)
        if inst is None:
            raise OrderError("Cannot resolve the instrument for this order.")
        types = {"MKT": "MARKET", "L": "LIMIT", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}
        payload = {"variety": "NORMAL", "orderid": str(order_id), "tradingsymbol": inst.symbol,
                   "symboltoken": inst.token, "exchange": SEGMENT_TO_EXCHANGE[inst.segment],
                   "ordertype": types[order_type], "producttype": self._product(row.product),
                   "duration": validity, "price": str(price or 0), "triggerprice": str(trigger or 0),
                   "quantity": str(qty)}
        try:
            self.orders_api.modify(order_id, payload)
        except (TimeoutError, ConnectionError, RequestException) as exc:
            raise OrderUnknown("Angel One modify request may have reached the broker; verify the order book before retrying.") from exc

    @staticmethod
    def _product(value):
        return {"MIS": "INTRADAY", "INTRADAY": "INTRADAY",
                "NRML": "CARRYFORWARD", "CARRYFORWARD": "CARRYFORWARD",
                "CNC": "DELIVERY", "DELIVERY": "DELIVERY"}.get(value, "INTRADAY")

    @staticmethod
    def _internal_product(value):
        return {"MIS": "MIS", "INTRADAY": "MIS",
                "NRML": "NRML", "CARRYFORWARD": "NRML",
                "CNC": "CNC", "DELIVERY": "CNC"}.get(str(value or "").upper(), "MIS")

    @staticmethod
    def _status(value):
        raw = str(value or "").upper().replace("_", " ").strip()
        return {"COMPLETE": "COMPLETE", "OPEN": "OPEN", "PENDING": "OPEN",
                "TRIGGER PENDING": "TRIGGER_PENDING", "PARTIALLY EXECUTED": "OPEN", "PARTIALLY FILLED": "OPEN", "MODIFY PENDING": "OPEN", "AMO REQ RECEIVED": "OPEN", "REJECTED": "REJECTED",
                "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED"}.get(raw, "UNKNOWN")

    def cancel(self, order_id):
        try:
            self.orders_api.cancel(str(order_id))
        except (TimeoutError, ConnectionError, RequestException) as exc:
            raise OrderUnknown("Angel One cancel request may have reached the broker; verify the order book before retrying.") from exc

    def orders(self):
        try:
            rows = self.orders_api.order_book()
        except Exception:
            log.exception("Angel One order book unavailable")
            return None
        out = []
        for r in rows:
            oid = str(r.get("orderid") or r.get("order_id") or "")
            token = str(r.get("symboltoken") or "")
            exchange = str(r.get("exchange") or "").upper()
            segment = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), "")
            key = f"{segment}|{token}" if segment and token else ""
            inst = self.cat.get(key) if key else None
            out.append(OrderRow(oid, key, str(r.get("tradingsymbol") or ""), inst.label() if inst else str(r.get("tradingsymbol") or ""),
                str(r.get("transactiontype") or ""), int(_num(r.get("quantity"))), int(_num(r.get("filledshares"))),
                inst.lot if inst else 1, self._internal_product(r.get("producttype")), str(r.get("ordertype") or ""),
                _num(r.get("price")), _num(r.get("triggerprice")), _num(r.get("averageprice")),
                self._status(r.get("orderstatus") or r.get("status")), str(r.get("text") or r.get("rejreason") or ""),
                str(r.get("updatetime") or r.get("exchtime") or ""), str(r.get("duration") or "DAY"),
                self._tags.get(oid, str(r.get("ordertag") or ""))))
        return out

    def trades(self):
        try:
            rows = self.orders_api.trade_book()
        except Exception:
            log.exception("Angel One trade book unavailable")
            return None
        out = []
        for r in rows:
            token = str(r.get("symboltoken") or "")
            exchange = str(r.get("exchange") or "").upper()
            segment = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), "")
            key = f"{segment}|{token}" if segment and token else ""
            sym = str(r.get("tradingsymbol") or "")
            out.append(TradeRow(str(r.get("orderid") or ""), key, sym, sym,
                str(r.get("transactiontype") or ""), int(_num(r.get("fillsize") or r.get("quantity"))),
                _num(r.get("fillprice") or r.get("price")), self._internal_product(r.get("producttype")),
                str(r.get("filltime") or r.get("updatetime") or ""), str(r.get("tradeid") or "")))
        return out

    def positions(self):
        try:
            rows = self.orders_api._list_data(self.s.require_client().position(), "positions")
        except Exception:
            log.exception("Angel One positions unavailable")
            return None
        out = []
        for r in rows:
            token = str(r.get("symboltoken") or "")
            exchange = str(r.get("exchange") or "").upper()
            segment = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), "")
            if not token or not segment:
                continue
            key = f"{segment}|{token}"
            inst = self.cat.get(key)
            symbol = str(r.get("tradingsymbol") or (inst.symbol if inst else ""))
            net = int(_num(r.get("netqty")))
            buy_qty, sell_qty = int(_num(r.get("buyqty"))), int(_num(r.get("sellqty")))
            buy_avg, sell_avg = _num(r.get("buyavgprice")), _num(r.get("sellavgprice"))
            ltp = self.hub.ltp(key) or _num(r.get("ltp")) or None
            lot = inst.lot if inst else 1
            realised = _num(r.get("realised"))
            unrealised = _num(r.get("unrealised"))
            avg = _num(r.get("avgnetprice"))
            if avg <= 0:
                avg = buy_avg if net > 0 else sell_avg
            out.append(PositionRow(key, symbol, inst.label() if inst else symbol, segment,
                self._internal_product(r.get("producttype")), lot, buy_qty, sell_qty, net, buy_avg, sell_avg,
                avg if net else 0, ltp, realised, unrealised, inst.expiry.isoformat() if inst and inst.expiry else None))
        return out

    def holdings(self):
        """Return normalized Angel One delivery holdings without guessing unavailable values."""
        try:
            rows = self.orders_api.holdings()
        except Exception:
            log.exception("Angel One holdings unavailable")
            return None
        out = []
        for row in rows:
            token = str(row.get("symboltoken") or "")
            exchange = str(row.get("exchange") or "NSE").upper()
            segment = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), "nse_cm")
            key = f"{segment}|{token}" if token else ""
            inst = self.cat.get(key) if key else None
            out.append({
                "key": key, "symbol": str(row.get("tradingsymbol") or (inst.symbol if inst else "")),
                "quantity": int(_num(row.get("quantity") or row.get("totalquantity"))),
                "average_price": _num(row.get("averageprice")),
                "ltp": self.hub.ltp(key) if key else _num(row.get("ltp")) or None,
                "pnl": _num(row.get("profitandloss") or row.get("pnl")),
                "product": self._internal_product(row.get("producttype") or "DELIVERY"),
            })
        return out

    def funds(self):
        try:
            d = self.orders_api.funds()
        except Exception:
            log.exception("Angel One funds unavailable")
            return None
        return Funds(available=_num(d.get("availablecash") or d.get("net")), used=_num(d.get("utiliseddebits") or d.get("utilisedamount")),
                     collateral=_num(d.get("collateral")),
                     realised=_num(d.get("m2mrealized")) if d.get("m2mrealized") is not None else None,
                     unrealised=_num(d.get("m2munrealised")) if d.get("m2munrealised") is not None else None,
                     simulated=False, note="Values are mapped from Angel One RMS limits response.", extra=d)
