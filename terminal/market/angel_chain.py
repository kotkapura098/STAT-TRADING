"""Angel One option-chain snapshots assembled from its instrument master and FULL quotes.

SmartAPI does not expose a single option-chain endpoint. We build the chain from
listed option contracts and fetch FULL quote batches (max 50 tokens/request), which
include current open interest. Previous-session OI is hydrated from the historical OI endpoint.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .chain import ChainUnavailable, INDEX_UNDERLYINGS, Leg, RawChain, _f
from . import commodity as cm
from ..brokers.angel_one import SEGMENT_TO_EXCHANGE
from ..clock import now_ist

log = logging.getLogger("angel-chain")


class AngelOneChainProvider:
    source = "angel-one"
    MAX_OPTIONS = 200
    BATCH_SIZE = 50
    REQUEST_INTERVAL = 1.05
    HISTORY_INTERVAL = 0.55
    BASELINE_LOOKBACK_DAYS = 15
    BASELINE_RETRY_SECONDS = 300

    def __init__(self, session, instruments, catalog, hub, cache_path=None):
        self.session, self.ins, self.cat, self.hub = session, instruments, catalog, hub
        self._request_lock = threading.Lock()
        self._last_request = 0.0
        self._oi_lock = threading.RLock()
        self._oi_queue = []
        self._oi_queued = set()
        self._oi_attempted = {}
        self._oi_cache = {}
        self._oi_thread = None
        self._oi_last_request = 0.0
        self._cache_path = Path(cache_path) if cache_path else None
        self._load_oi_cache()

    def _load_oi_cache(self):
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._oi_cache = {k: v for k, v in payload.items()
                                  if isinstance(v, dict) and v.get("oi") is not None}
        except Exception:
            log.warning("Could not read cached Angel One OI baselines; rebuilding them.")

    def _save_oi_cache(self):
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._oi_cache, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._cache_path)
        except Exception:
            log.exception("Could not persist Angel One OI baselines")

    @staticmethod
    def _oi_cache_key(request_day, exchange, token):
        return f"{request_day.isoformat()}|{exchange}|{token}"

    @staticmethod
    def _oi_point_time(value):
        raw = str(value or "").strip()
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M:%S"):
                try:
                    return datetime.strptime(raw, fmt)
                except ValueError:
                    pass
        return None

    def _request_historical_oi(self, exchange, token, request_day):
        """Return the final available OI observation from the last prior trading session."""
        client = self.session.require_client()
        start = request_day - timedelta(days=self.BASELINE_LOOKBACK_DAYS)
        end = request_day - timedelta(days=1)
        params = {
            "exchange": exchange, "symboltoken": str(token), "interval": "ONE_MINUTE",
            "fromdate": f"{start.isoformat()} 09:00", "todate": f"{end.isoformat()} 15:30",
        }
        with self._oi_lock:
            wait = self.HISTORY_INTERVAL - (time.monotonic() - self._oi_last_request)
            if wait > 0:
                time.sleep(wait)
            response = client.getOIData(params)
            self._oi_last_request = time.monotonic()
        if not isinstance(response, dict) or response.get("status") is not True:
            message = response.get("message") if isinstance(response, dict) else "unreadable response"
            raise ChainUnavailable(f"Angel One historical OI request failed: {message or 'unknown error'}")
        latest = None
        points = response.get("data") or []
        for point in points if isinstance(points, list) else []:
            if isinstance(point, dict):
                stamp, value = point.get("time"), point.get("oi")
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                stamp, value = point[0], point[1]
            else:
                continue
            when, oi = self._oi_point_time(stamp), _f(value)
            if when is None or oi is None or when.date() >= request_day:
                continue
            if when.time() < datetime.strptime("09:00", "%H:%M").time() or when.time() > datetime.strptime("15:30", "%H:%M").time():
                continue
            if latest is None or when > latest[0]:
                latest = (when, oi)
        return (latest[0].date().isoformat(), latest[1]) if latest else None

    def _oi_worker(self):
        while True:
            with self._oi_lock:
                if not self._oi_queue:
                    self._oi_thread = None
                    return
                exchange, token, request_day = self._oi_queue.pop(0)
                self._oi_queued.discard((request_day, exchange, token))
            cache_key = self._oi_cache_key(request_day, exchange, token)
            try:
                baseline = self._request_historical_oi(exchange, token, request_day)
                if baseline:
                    with self._oi_lock:
                        self._oi_cache[cache_key] = {"source_day": baseline[0], "oi": baseline[1]}
                        self._save_oi_cache()
            except Exception as exc:
                log.warning("Historical OI baseline unavailable for %s/%s: %s", exchange, token, exc)

    def _queue_oi_baselines(self, contracts):
        request_day = now_ist().date()
        with self._oi_lock:
            for _, row in contracts:
                exchange = str(row.get("exch_seg") or "").upper()
                token = str(row.get("token") or "")
                if exchange not in ("NFO", "BFO", "MCX") or not token.isdigit():
                    continue
                key = (request_day, exchange, token)
                cache_key = self._oi_cache_key(*key)
                if cache_key in self._oi_cache or key in self._oi_queued:
                    continue
                if time.time() - self._oi_attempted.get(key, 0) < self.BASELINE_RETRY_SECONDS:
                    continue
                self._oi_attempted[key] = time.time()
                self._oi_queued.add(key)
                self._oi_queue.append((exchange, token, request_day))
            if self._oi_queue and (self._oi_thread is None or not self._oi_thread.is_alive()):
                self._oi_thread = threading.Thread(target=self._oi_worker, name="angel-oi-baselines", daemon=True)
                self._oi_thread.start()

    def _previous_oi(self, exchange, token):
        key = self._oi_cache_key(now_ist().date(), exchange, token)
        item = self._oi_cache.get(key)
        return _f(item.get("oi")) if item else None

    @staticmethod
    def _name(row):
        return str(row.get("name") or "").strip().upper()

    def _option_rows(self, underlying):
        self.ins.load()
        target = underlying.strip().upper()
        rows = []
        for row in self.ins._rows:
            exchange = str(row.get("exch_seg") or "").upper()
            opt = str(row.get("opttype") or "").upper()
            if exchange not in ("NFO", "BFO", "MCX") or opt not in ("CE", "PE"):
                continue
            if self._name(row) != target:
                continue
            exp = self.ins._expiry(row.get("expiry"))
            token = str(row.get("token") or "").strip()
            if exp and exp >= now_ist().date() and token.isdigit():
                rows.append((exp, row))
        return rows

    def _label(self, underlying):
        fixed = next((u["label"] for u in INDEX_UNDERLYINGS if u["name"] == underlying), None)
        if fixed:
            return fixed
        for row in self.ins._rows:
            if str(row.get("exch_seg") or "").upper() in ("NSE", "BSE") and self._name(row) == underlying:
                return str(row.get("symbol") or underlying).replace("-EQ", "") or underlying
        return underlying

    def underlyings(self):
        names = sorted({self._name(r) for r in self.ins._rows
                        if str(r.get("exch_seg") or "").upper() in ("NFO", "BFO", "MCX")
                        and str(r.get("opttype") or "").upper() in ("CE", "PE")})
        known = {u["name"]: u for u in INDEX_UNDERLYINGS}
        out = []
        for name in names:
            rows = self._option_rows(name)
            if not rows:
                continue
            lot = max(int((_f(rows[0][1].get("lotsize")) or 1)), 1)
            out.append({"name": name, "label": self._label(name), "lot": lot,
                        "kind": "index" if name in known else ("commodity" if str(rows[0][1].get("exch_seg")).upper() == "MCX" else "stock")})
        return out

    def search(self, q, limit=12):
        query = q.strip().upper()
        hits = self.underlyings()
        if not query:
            return hits[:limit]
        starts = [x for x in hits if x["name"].startswith(query)]
        contains = [x for x in hits if x not in starts and query in (x["name"] + " " + x["label"]).upper()]
        return (starts + contains)[:limit]

    def check(self, underlying):
        if not self._option_rows(underlying):
            raise ChainUnavailable(f"{underlying} has no current Angel One option contracts in the instrument master.")

    def expiries(self, underlying):
        return sorted({exp.isoformat() for exp, _ in self._option_rows(underlying)})

    def _spot_key(self, underlying):
        aliases = {
            "NIFTY": {"NIFTY", "NIFTY 50"}, "BANKNIFTY": {"BANKNIFTY", "NIFTY BANK"},
            "FINNIFTY": {"FINNIFTY", "NIFTY FIN SERVICE"},
            "MIDCPNIFTY": {"MIDCPNIFTY", "NIFTY MID SELECT"},
            "SENSEX": {"SENSEX"},
        }
        wanted = aliases.get(underlying, {underlying})
        for inst in getattr(self.cat, "_reg", {}).values():
            if inst.kind == "INDEX" and (inst.name.upper() in wanted or inst.symbol.upper() in wanted):
                return inst.key
        for row in self.ins._rows:
            if str(row.get("exch_seg") or "").upper() in ("NSE", "BSE") and self._name(row) == underlying:
                if str(row.get("instrumenttype") or "").upper() in ("AMXIDX", ""):
                    return f"{'nse_cm' if row.get('exch_seg') == 'NSE' else 'bse_cm'}|{row.get('token')}"
        for row in self.ins._rows:
            if str(row.get("exch_seg") or "").upper() in ("NSE", "BSE") and self._name(row) == underlying and str(row.get("symbol") or "").endswith("-EQ"):
                return f"{'nse_cm' if row.get('exch_seg') == 'NSE' else 'bse_cm'}|{row.get('token')}"
        return None

    def _full_quotes(self, rows):
        by_exchange = {}
        for row in rows:
            ex = str(row.get("exch_seg") or "").upper()
            token = str(row.get("token") or "")
            if ex in ("NFO", "BFO", "MCX") and token.isdigit():
                by_exchange.setdefault(ex, []).append(token)
        result = {}
        client = self.session.require_client()
        for exchange, tokens in by_exchange.items():
            for offset in range(0, len(tokens), self.BATCH_SIZE):
                batch = tokens[offset:offset + self.BATCH_SIZE]
                with self._request_lock:
                    wait = self.REQUEST_INTERVAL - (time.monotonic() - self._last_request)
                    if wait > 0:
                        time.sleep(wait)
                    response = client.getMarketData("FULL", {exchange: batch})
                    self._last_request = time.monotonic()
                if not isinstance(response, dict) or response.get("status") is not True:
                    message = response.get("message") if isinstance(response, dict) else "unreadable response"
                    raise ChainUnavailable(f"Angel One FULL quote request failed: {message or 'unknown error'}")
                data = response.get("data") or {}
                fetched = data.get("fetched", []) if isinstance(data, dict) else []
                for quote in fetched if isinstance(fetched, list) else []:
                    token = str(quote.get("symbolToken") or quote.get("symboltoken") or "")
                    if token:
                        result[(exchange, token)] = quote
                unfetched = data.get("unfetched", []) if isinstance(data, dict) else []
                if unfetched:
                    log.warning("Angel One did not return %d option quotes for %s", len(unfetched), exchange)
        return result

    def fetch(self, underlying, expiry):
        target = underlying.strip().upper()
        exp = date.fromisoformat(expiry)
        contracts = [(e, r) for e, r in self._option_rows(target) if e == exp]
        if not contracts:
            raise ChainUnavailable(f"No Angel One option contracts for {target} expiring {expiry}.")
        # Bound the number of option tokens so the provider respects SmartAPI's 50-token
        # FULL quote batch size and keeps refresh latency predictable.
        strikes = sorted({_f(r.get("strike")) / 100 for _, r in contracts})
        spot_key = self._spot_key(target)
        spot = self.hub.ltp(spot_key) if spot_key else None
        if len(strikes) * 2 > self.MAX_OPTIONS:
            centre = spot if spot is not None else strikes[len(strikes) // 2]
            chosen = set(sorted(strikes, key=lambda k: abs(k - centre))[:self.MAX_OPTIONS // 2])
            contracts = [(e, r) for e, r in contracts if _f(r.get("strike")) / 100 in chosen]
        # Historical OI is token-specific; warm baselines in the background so
        # the option-chain request does not block on hundreds of history calls.
        self._queue_oi_baselines(contracts)
        quotes = self._full_quotes([r for _, r in contracts])
        lot = max(int((_f(contracts[0][1].get("lotsize")) or 1)), 1)
        raw = RawChain(target, expiry, lot, spot_key, spot, None)
        for _, row in contracts:
            exchange = str(row.get("exch_seg") or "").upper()
            segment = {"NFO": "nse_fo", "BFO": "bse_fo", "MCX": "mcx_fo"}.get(exchange)
            token = str(row.get("token") or "")
            kind = str(row.get("opttype") or "").upper()
            strike = _f(row.get("strike"))
            if not segment or kind not in ("CE", "PE") or strike is None:
                continue
            strike /= 100
            quote = quotes.get((exchange, token), {})
            ltp = _f(quote.get("ltp"))
            # SmartAPI FULL REST quote values are rupee-denominated (WebSocket LTP is paise).
            prev_close = _f(quote.get("close"))
            oi = _f(quote.get("opnInterest"))
            if oi is None:
                oi = _f(quote.get("openInterest"))
            volume = _f(quote.get("tradeVolume"))
            previous_oi = self._previous_oi(exchange, token)
            key = f"{segment}|{token}"
            raw.rows.setdefault(strike, {})[kind] = Leg(
                key, str(row.get("symbol") or ""), ltp, prev_close, oi, previous_oi, volume)
        if not raw.rows:
            raise ChainUnavailable("Angel One returned no usable option contracts for this expiry.")
        return raw
