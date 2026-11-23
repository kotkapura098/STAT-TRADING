"""Angel One SmartAPI primitives.

This module is intentionally isolated from the existing engine interfaces until the
service/account lifecycle migration is completed. It provides authenticated REST,
instrument-master parsing, market-stream and order/account operations.
Never enable live mode until the service wiring and live-account validation are done.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

import pyotp
import requests

from ..models import Contract, OrderError, OrderRequest, OrderStatus, Tick
from ..clock import now_ist
from .base import Subscription

log = logging.getLogger("angel-one")

API_ROOT = "https://apiconnect.angelone.in"
MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
SEGMENT_TO_EXCHANGE = {
    "nse_cm": "NSE", "bse_cm": "BSE", "nse_fo": "NFO",
    "bse_fo": "BFO", "mcx_fo": "MCX",
}
SEGMENT_TO_WS = {"nse_cm": 1, "nse_fo": 2, "bse_cm": 3, "bse_fo": 4, "mcx_fo": 5}
EXCHANGE_TO_SEGMENT = {v: k for k, v in SEGMENT_TO_EXCHANGE.items()}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _ok(response: Any, action: str) -> dict:
    if not isinstance(response, dict) or response.get("status") is not True:
        message = response.get("message") if isinstance(response, dict) else str(response)
        raise OrderError(f"Angel One {action} failed: {message or 'unreadable response'}")
    data = response.get("data")
    return data if isinstance(data, dict) else {"data": data}


class AngelOneSession:
    """One SmartAPI session. Credentials are read from environment and never logged."""

    def __init__(self, env=os.environ):
        self.api_key = env.get("ANGEL_API_KEY", "").strip()
        self.client_code = env.get("ANGEL_CLIENT_CODE", "").strip()
        self.pin = env.get("ANGEL_PIN", "").strip()
        self.totp_secret = env.get("ANGEL_TOTP_SECRET", "").strip()
        self.client = None
        self.jwt_token = ""
        self.refresh_token = ""
        self.feed_token = ""
        self.profile: dict = {}
        self.my_ip = ""

    def login(self, totp: str | None = None) -> None:
        if not all((self.api_key, self.client_code, self.pin)):
            raise RuntimeError("Set ANGEL_API_KEY, ANGEL_CLIENT_CODE and ANGEL_PIN in the server environment.")
        code = (totp or "").strip()
        if not code and self.totp_secret:
            code = pyotp.TOTP(self.totp_secret).now()
        if not code or len(code) != 6 or not code.isdigit():
            raise RuntimeError("Enter the current six-digit Angel One TOTP.")
        try:
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise RuntimeError("Angel One SDK is missing. Install smartapi-python.") from exc
        client = SmartConnect(api_key=self.api_key)
        response = client.generateSession(self.client_code, self.pin, code)
        data = _ok(response, "login")
        self.jwt_token = str(data.get("jwtToken") or "")
        self.refresh_token = str(data.get("refreshToken") or "")
        self.feed_token = str(client.getfeedToken() or data.get("feedToken") or "")
        if not self.jwt_token or not self.feed_token:
            raise RuntimeError("Angel One login returned an incomplete session.")
        self.client = client
        profile = client.getProfile(self.refresh_token)
        self.profile = _ok(profile, "profile")

    def require_client(self):
        if self.client is None:
            raise RuntimeError("Angel One account is not connected.")
        return self.client

    def logout(self) -> None:
        if self.client is not None:
            try:
                self.client.terminateSession(self.client_code)
            finally:
                self.client = None
                self.jwt_token = self.refresh_token = self.feed_token = ""


class AngelOneInstruments:
    """Angel One instrument master indexed by exchange/token, not browser-supplied symbols."""

    def __init__(self, session: AngelOneSession, cfg=None, cache_seconds: int = 86400):
        self.session, self.cfg = session, cfg
        self.cache_seconds = cache_seconds
        self._rows: list[dict] = []
        self._by_key: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._lock = threading.RLock()

    def load(self, force: bool = False) -> None:
        with self._lock:
            if self._rows and not force and time.time() - self._loaded_at < self.cache_seconds:
                return
            response = requests.get(MASTER_URL, timeout=30)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("Angel One instrument master returned no instruments.")
            indexed = {}
            for row in rows:
                exchange = str(row.get("exch_seg") or "").upper()
                segment = EXCHANGE_TO_SEGMENT.get(exchange)
                token = str(row.get("token") or "").strip()
                if segment and token:
                    indexed[f"{segment}|{token}"] = row
            self._rows, self._by_key, self._loaded_at = rows, indexed, time.time()

    def index_subscription(self, instrument: str) -> Subscription:
        # Index token aliases are account/instrument-master dependent; require configured token.
        configured = getattr(self.cfg, "dashboard_indices", []) if self.cfg else []
        for item in configured:
            if str(item.get("label", "")).upper() == instrument.upper():
                seg = str(item["segment"]).lower()
                token = str(item["token"])
                if seg not in SEGMENT_TO_WS:
                    break
                return Subscription(seg, token, "INDEX")
        raise RuntimeError(f"Configure the Angel One exchange token for index {instrument!r} in dashboard_indices.")

    def get_row(self, key: str) -> dict | None:
        self.load()
        return self._by_key.get(key)

    def resolve_contract(self, key: str) -> Contract:
        row = self.get_row(key)
        if not row:
            raise RuntimeError(f"Instrument {key!r} is not present in the Angel One master.")
        seg, token = key.split("|", 1)
        expiry_raw = str(row.get("expiry") or "")
        expiry = None
        if expiry_raw:
            for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
                try:
                    expiry = datetime.strptime(expiry_raw.upper(), fmt).date()
                    break
                except ValueError:
                    pass
        opt = str(row.get("opttype") or "").upper()
        if opt not in ("CE", "PE"):
            raise RuntimeError(f"{key} is not an option contract.")
        strike = _num(row.get("strike")) / 100
        lot = max(int(_num(row.get("lotsize"), 1)), 1)
        if expiry is None:
            raise RuntimeError(f"Angel One master has no valid expiry for {key}.")
        return Contract(str(row.get("symbol") or ""), token, seg, strike, opt, expiry, lot)

    def candidates(self, instrument: str, ref_price: float, window: int, expiry_offset: int = 0):
        self.load()
        today = now_ist().date()
        rows = []
        for row in self._rows:
            if str(row.get("exch_seg", "")).upper() != "NFO":
                continue
            if str(row.get("name", "")).upper() != instrument.upper():
                continue
            try:
                exp = datetime.strptime(str(row.get("expiry", "")).upper(), "%d%b%Y").date()
            except ValueError:
                continue
            if exp >= today:
                rows.append((exp, row))
        expiries = sorted({x[0] for x in rows})
        if expiry_offset >= len(expiries):
            return {}
        selected = expiries[expiry_offset]
        strikes: dict[float, dict[str, Contract]] = {}
        for exp, row in rows:
            if exp != selected:
                continue
            opt = str(row.get("opttype", "")).upper()
            if opt not in ("CE", "PE"):
                continue
            strike = _num(row.get("strike")) / 100
            token = str(row.get("token"))
            c = Contract(str(row.get("symbol") or ""), token, "nse_fo", strike, opt, exp,
                         max(int(_num(row.get("lotsize"), 1)), 1))
            strikes.setdefault(strike, {})[opt] = c
        ordered = sorted(strikes)
        if not ordered:
            return {}
        atm_i = min(range(len(ordered)), key=lambda i: abs(ordered[i] - ref_price))
        chosen = ordered[max(0, atm_i-window):atm_i+window+1]
        return {k: (strikes[k]["CE"], strikes[k]["PE"]) for k in chosen
                if "CE" in strikes[k] and "PE" in strikes[k]}


class AngelOneFeed:
    """SmartWebSocketV2 feed adapter; LTP values arrive in paise and are normalized to rupees."""

    def __init__(self, session: AngelOneSession, cfg=None):
        self.session, self.cfg = session, cfg
        self._ws = None
        self._on_tick: Callable[[Tick], None] | None = None
        self._subscriptions: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._thread = None

    def start(self, on_tick: Callable[[Tick], None]) -> None:
        self._on_tick = on_tick
        self._connect()

    def _connect(self):
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        s = self.session
        self._ws = SmartWebSocketV2(s.jwt_token, s.api_key, s.client_code, s.feed_token)
        self._ws.on_data = self._on_data
        self._ws.on_error = lambda ws, err: log.error("Angel One market stream error: %s", err)
        self._ws.on_close = lambda ws: log.warning("Angel One market stream closed")
        self._thread = threading.Thread(target=self._ws.connect, name="angel-one-feed", daemon=True)
        self._thread.start()

    def subscribe(self, subs: list[Subscription]) -> None:
        grouped: dict[int, list[str]] = {}
        with self._lock:
            for sub in subs:
                ex = SEGMENT_TO_WS.get(sub.segment)
                if ex is None:
                    continue
                grouped.setdefault(ex, []).append(str(sub.token))
                self._subscriptions[(sub.segment, str(sub.token))] = sub.symbol
        if self._ws and grouped:
            token_list = [{"exchangeType": ex, "tokens": sorted(set(tokens))} for ex, tokens in grouped.items()]
            self._ws.subscribe("golden-eagle", 1, token_list)

    def subscribe_keys(self, keys: list[str]) -> None:
        subs = []
        for key in keys:
            if "|" in key:
                seg, token = key.split("|", 1)
                subs.append(Subscription(seg, token, key))
        self.subscribe(subs)

    def _on_data(self, ws, message):
        if not isinstance(message, dict):
            return
        token = str(message.get("token") or "")
        ex = int(_num(message.get("exchange_type"), 0))
        seg = next((k for k, v in SEGMENT_TO_WS.items() if v == ex), None)
        if not seg:
            return
        with self._lock:
            symbol = self._subscriptions.get((seg, token))
        price = _num(message.get("last_traded_price")) / 100
        if symbol and price > 0 and self._on_tick:
            self._on_tick(Tick(symbol, price, now_ist()))

    def stop(self) -> None:
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                log.exception("Error closing Angel One feed")
        self._ws = None


class AngelOneOrders:
    """SmartAPI order and account operations. Quantities are units, not lots."""

    def __init__(self, session: AngelOneSession, instruments: AngelOneInstruments, cfg=None):
        self.session, self.instruments, self.cfg = session, instruments, cfg

    def place(self, req: OrderRequest) -> str:
        row = self.instruments.get_row(f"{req.contract.segment}|{req.contract.token}")
        if not row:
            raise OrderError("Contract is missing from Angel One instrument master.")
        ordertype = {"MKT": "MARKET", "L": "LIMIT", "SL": "STOPLOSS_LIMIT"}.get(req.order_type)
        if not ordertype:
            raise OrderError(f"Unsupported internal order type: {req.order_type}")
        payload = {
            "variety": "NORMAL", "tradingsymbol": req.contract.symbol,
            "symboltoken": req.contract.token, "transactiontype": "BUY" if req.side == "B" else "SELL",
            "exchange": SEGMENT_TO_EXCHANGE[req.contract.segment], "ordertype": ordertype,
            "producttype": getattr(self.cfg, "angel_product_type", "INTRADAY"),
            "duration": "DAY", "price": str(req.price or 0),
            "triggerprice": str(req.trigger or 0), "quantity": str(req.qty),
            "squareoff": "0", "stoploss": "0", "ordertag": (req.tag or "GET")[:20],
        }
        response = self.session.require_client().placeOrder(payload)
        data = _ok(response, "place order")
        order_id = data.get("orderid") or data.get("order_id")
        if not order_id:
            raise OrderError("Angel One accepted no order id; check order book before retrying.")
        return str(order_id)

    def cancel(self, order_id: str) -> None:
        response = self.session.require_client().cancelOrder(order_id, "NORMAL")
        _ok(response, "cancel order")

    def modify(self, order_id: str, payload: dict) -> None:
        _ok(self.session.require_client().modifyOrder(payload), "modify order")

    def status(self, order_id: str) -> OrderStatus:
        book = self.session.require_client().orderBook()
        data = _ok(book, "order book")
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        row = next((x for x in rows if str(x.get("orderid")) == str(order_id)), None)
        if row is None:
            return OrderStatus(str(order_id), "UNKNOWN", reason="Order not present in Angel One order book.")
        raw = str(row.get("orderstatus") or row.get("status") or "").upper()
        state = {"COMPLETE": "COMPLETE", "OPEN": "OPEN", "PENDING": "OPEN",
                 "TRIGGER PENDING": "TRIGGER_PENDING", "REJECTED": "REJECTED",
                 "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED"}.get(raw, "UNKNOWN")
        return OrderStatus(str(order_id), state, int(_num(row.get("filledshares"))),
                           _num(row.get("averageprice")), str(row.get("text") or ""))

    def positions(self) -> dict[str, int] | None:
        response = self.session.require_client().position()
        data = _ok(response, "positions")
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        out: dict[str, int] = {}
        for row in rows:
            symbol = str(row.get("tradingsymbol") or "")
            if symbol:
                out[symbol] = int(_num(row.get("netqty")))
        return out

    def holdings(self) -> list[dict]:
        data = _ok(self.session.require_client().holding(), "holdings")
        return data.get("data", []) if isinstance(data.get("data"), list) else []

    def funds(self) -> dict:
        data = _ok(self.session.require_client().rmsLimit(), "funds")
        return data
