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

    @property
    def logged_in(self) -> bool:
        return self.client is not None and bool(self.jwt_token and self.feed_token)

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
        try:
            response = client.generateSession(self.client_code, self.pin, code)
            data = _ok(response, "login")
            self.jwt_token = str(data.get("jwtToken") or "")
            self.refresh_token = str(data.get("refreshToken") or "")
            self.feed_token = str(client.getfeedToken() or data.get("feedToken") or "")
            if not self.jwt_token or not self.refresh_token or not self.feed_token:
                raise RuntimeError("Angel One login returned an incomplete session.")
            self.client = client
            self.profile = _ok(client.getProfile(self.refresh_token), "profile")
        except Exception:
            self.client = None
            self.jwt_token = self.refresh_token = self.feed_token = ""
            self.profile = {}
            try:
                client.terminateSession(self.client_code)
            except Exception:
                pass
            raise

    def refresh(self) -> None:
        """Refresh the REST JWT using the SDK refresh token and rotate the feed token."""
        client = self.require_client()
        if not self.refresh_token:
            raise RuntimeError("Angel One refresh token is unavailable; reconnect the account.")
        response = client.generateToken(self.refresh_token)
        data = _ok(response, "token refresh")
        self.jwt_token = str(data.get("jwtToken") or self.jwt_token)
        self.refresh_token = str(data.get("refreshToken") or self.refresh_token)
        self.feed_token = str(client.getfeedToken() or data.get("feedToken") or self.feed_token)
        if not self.jwt_token or not self.feed_token:
            raise RuntimeError("Angel One token refresh returned an incomplete session.")

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

    @staticmethod
    def _expiry(value: Any) -> date | None:
        raw = str(value or "").strip().upper()
        for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def index_subscription(self, instrument: str) -> Subscription:
        """Resolve an index's real numeric token from the downloaded Angel One master."""
        self.load()
        configured = getattr(self.cfg, "dashboard_indices", []) if self.cfg else []
        aliases = {
            "NIFTY 50": {"NIFTY", "NIFTY 50"},
            "NIFTY BANK": {"BANKNIFTY", "NIFTY BANK"},
            "NIFTY FIN SERVICE": {"FINNIFTY", "NIFTY FIN SERVICE"},
            "NIFTY MID SELECT": {"MIDCPNIFTY", "NIFTY MID SELECT"},
            "INDIA VIX": {"INDIA VIX", "INDIAVIX"},
            "SENSEX": {"SENSEX"},
        }
        wanted = instrument.upper().strip()
        item = next((x for x in configured
                     if wanted == str(x.get("label", "")).upper()
                     or wanted in aliases.get(str(x.get("label", "")).upper(), set())), None)
        if item is None:
            raise RuntimeError(f"Index {instrument!r} is not configured in dashboard_indices.")
        segment = str(item.get("segment", "")).lower()
        exchange = SEGMENT_TO_EXCHANGE.get(segment)
        if not exchange:
            raise RuntimeError(f"Unsupported index exchange segment: {segment!r}")
        hints = {str(item.get("token", "")).strip().upper(), str(item.get("label", "")).strip().upper()}
        hints.update(aliases.get(str(item.get("label", "")).upper(), set()))
        hints.add(wanted)
        matches = []
        for row in self._rows:
            if str(row.get("exch_seg", "")).upper() != exchange:
                continue
            if str(row.get("instrumenttype", "")).upper() not in ("AMXIDX", ""):
                continue
            token = str(row.get("token", "")).strip()
            if not token.isdigit():
                continue
            symbol = str(row.get("symbol", "")).strip().upper()
            name = str(row.get("name", "")).strip().upper()
            if symbol in hints or name in hints or f"{name} {symbol}" in hints:
                matches.append(row)
        if not matches:
            raise RuntimeError(f"Angel One instrument master has no verified token for index {instrument!r} on {exchange}.")
        # Prefer an exact symbol match; never send the configured display label as a token.
        matches.sort(key=lambda r: (str(r.get("symbol", "")).upper() not in hints,
                                    str(r.get("name", "")).upper() not in hints))
        token = str(matches[0]["token"])
        return Subscription(segment, token, "INDEX")

    def get_row(self, key: str) -> dict | None:
        self.load()
        return self._by_key.get(key)

    def resolve_contract(self, key: str) -> Contract:
        row = self.get_row(key)
        if not row:
            raise RuntimeError(f"Instrument {key!r} is not present in the Angel One master.")
        seg, token = key.split("|", 1)
        expiry = self._expiry(row.get("expiry"))
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
            exp = self._expiry(row.get("expiry"))
            if exp is None:
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
    """SmartWebSocketV2 feed with a supervised reconnect loop and subscription replay."""

    def __init__(self, session: AngelOneSession, cfg=None, reconnect_min_s: float = 1.0,
                 reconnect_max_s: float = 30.0):
        self.session, self.cfg = session, cfg
        self._ws = None
        self._on_tick: Callable[[Tick], None] | None = None
        self._subscriptions: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._reconnect_min_s = max(0.2, reconnect_min_s)
        self._reconnect_max_s = max(self._reconnect_min_s, reconnect_max_s)
        self._correlation_id = "goldeneagle"
        self._mode = 1

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self, on_tick: Callable[[Tick], None]) -> None:
        if not self.session.jwt_token or not self.session.feed_token:
            raise RuntimeError("Log in to Angel One before starting the market feed.")
        self._on_tick = on_tick
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="angel-one-feed-supervisor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        delay = self._reconnect_min_s
        while not self._stop.is_set():
            try:
                from SmartApi.smartWebSocketV2 import SmartWebSocketV2
                s = self.session
                ws = SmartWebSocketV2(s.jwt_token, s.api_key, s.client_code, s.feed_token)
                with self._lock:
                    self._ws = ws
                ws.on_open = self._on_open
                ws.on_data = self._on_data
                ws.on_error = self._on_error
                ws.on_close = self._on_close
                # connect() blocks until the socket closes in the SmartAPI SDK.
                ws.connect()
            except Exception:
                if not self._stop.is_set():
                    log.exception("Angel One WebSocket connection failed")
            finally:
                self._connected.clear()
                with self._lock:
                    old, self._ws = self._ws, None
                if old is not None:
                    try:
                        old.close_connection()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(self._reconnect_max_s, delay * 2)

    def _on_open(self, ws) -> None:
        self._connected.set()
        log.info("Angel One market stream connected")
        self._replay_subscriptions(ws)

    def _on_error(self, ws, error) -> None:
        self._connected.clear()
        log.warning("Angel One market stream error: %s", error)

    def _on_close(self, ws) -> None:
        self._connected.clear()
        log.warning("Angel One market stream closed")

    def _replay_subscriptions(self, ws=None) -> None:
        target = ws or self._ws
        if target is None:
            return
        grouped: dict[int, list[str]] = {}
        with self._lock:
            for (segment, token) in self._subscriptions:
                exchange_type = SEGMENT_TO_WS.get(segment)
                if exchange_type is not None:
                    grouped.setdefault(exchange_type, []).append(token)
        for exchange_type, tokens in grouped.items():
            target.subscribe(self._correlation_id, self._mode, [
                {"exchangeType": exchange_type, "tokens": sorted(set(tokens))}
            ])

    def subscribe(self, subs: list[Subscription]) -> None:
        with self._lock:
            for sub in subs:
                if sub.segment not in SEGMENT_TO_WS:
                    raise ValueError(f"Unsupported Angel One segment: {sub.segment}")
                if not str(sub.token).strip().isdigit():
                    raise ValueError(f"Invalid Angel One instrument token: {sub.token!r}")
                self._subscriptions[(sub.segment, str(sub.token))] = sub.symbol
            ws = self._ws
        if ws is not None and self.connected:
            self._replay_subscriptions(ws)

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
        token = str(message.get("token") or "").strip()
        exchange_type = int(_num(message.get("exchange_type"), 0))
        segment = next((k for k, v in SEGMENT_TO_WS.items() if v == exchange_type), None)
        if not segment:
            return
        with self._lock:
            symbol = self._subscriptions.get((segment, token))
        # SmartAPI sends last_traded_price in paise (1/100 rupee).
        price = _num(message.get("last_traded_price")) / 100
        if symbol and price > 0 and self._on_tick:
            try:
                self._on_tick(Tick(symbol, price, now_ist()))
            except Exception:
                log.exception("Angel One tick callback failed")

    def stop(self) -> None:
        self._stop.set()
        self._connected.clear()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close_connection()
            except Exception:
                log.exception("Error closing Angel One feed")
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None

class AngelOneOrders:
    """SmartAPI order and account operations. Quantities are units, not lots."""

    def __init__(self, session: AngelOneSession, instruments: AngelOneInstruments, cfg=None):
        self.session, self.instruments, self.cfg = session, instruments, cfg

    def place(self, req: OrderRequest) -> str:
        row = self.instruments.get_row(f"{req.contract.segment}|{req.contract.token}")
        if not row:
            raise OrderError("Contract is missing from Angel One instrument master.")
        if req.side not in ("B", "S") or req.qty <= 0:
            raise OrderError("Order side or quantity is invalid.")
        ordertype = {"MKT": "MARKET", "L": "LIMIT", "SL": "STOPLOSS_LIMIT",
                     "SL-M": "STOPLOSS_MARKET"}.get(req.order_type)
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
        """Modify using the SmartAPI SDK's required orderid-bearing payload."""
        if not isinstance(payload, dict) or not payload.get("orderid"):
            raise OrderError("Angel One modify requires a complete payload containing orderid.")
        if str(payload["orderid"]) != str(order_id):
            raise OrderError("Modify payload orderid does not match the requested order.")
        _ok(self.session.require_client().modifyOrder(payload), "modify order")

    @staticmethod
    def _list_data(response: Any, action: str) -> list[dict]:
        data = _ok(response, action)
        # SmartAPI SDK responses have appeared as either data=[...] or data={"data":[...]}.
        rows = data.get("data") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = rows.get("data")
        if not isinstance(rows, list):
            raise OrderError(f"Angel One {action} returned an unreadable list.")
        return [r for r in rows if isinstance(r, dict)]

    def order_book(self) -> list[dict]:
        return self._list_data(self.session.require_client().orderBook(), "order book")

    def trade_book(self) -> list[dict]:
        client = self.session.require_client()
        method = getattr(client, "tradeBook", None) or getattr(client, "tradebook", None)
        if method is None:
            raise OrderError("The installed Angel One SDK does not expose tradeBook().")
        return self._list_data(method(), "trade book")

    def status(self, order_id: str) -> OrderStatus:
        rows = self.order_book()
        row = next((x for x in rows if str(x.get("orderid") or x.get("order_id")) == str(order_id)), None)
        if row is None:
            return OrderStatus(str(order_id), "UNKNOWN", reason="Order not present in Angel One order book.")
        raw = str(row.get("orderstatus") or row.get("status") or "").strip().upper().replace("_", " ")
        state = {"COMPLETE": "COMPLETE", "OPEN": "OPEN", "PENDING": "OPEN",
                 "TRIGGER PENDING": "TRIGGER_PENDING", "REJECTED": "REJECTED",
                 "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED"}.get(raw, "UNKNOWN")
        return OrderStatus(str(order_id), state,
                           int(_num(row.get("filledshares") or row.get("filledsharesqty"))),
                           _num(row.get("averageprice") or row.get("average_price")),
                           str(row.get("text") or row.get("rejreason") or ""))

    def positions(self) -> dict[str, int] | None:
        rows = self._list_data(self.session.require_client().position(), "positions")
        out: dict[str, int] = {}
        for row in rows:
            symbol = str(row.get("tradingsymbol") or "")
            if symbol:
                out[symbol] = int(_num(row.get("netqty")))
        return out

    def holdings(self) -> list[dict]:
        return self._list_data(self.session.require_client().holding(), "holdings")

    def funds(self) -> dict:
        data = _ok(self.session.require_client().rmsLimit(), "funds")
        if isinstance(data.get("data"), dict):
            data = data["data"]
        return data
