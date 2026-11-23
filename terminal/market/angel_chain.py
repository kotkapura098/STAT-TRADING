"""Angel One option-chain provider using the scrip master and SmartAPI FULL quotes."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from ..brokers.angel_one import EXCHANGE_TO_SEGMENT, SEGMENT_TO_EXCHANGE, _num, _ok
from ..clock import now_ist
from .chain import ChainUnavailable, Leg, RawChain
from . import commodity as cmd

log = logging.getLogger("angel-one-chain")

INDEXES = [
    {"name":"NIFTY","label":"Nifty 50","segment":"nse_fo","spot_key":"nse_cm|99926000","step":50},
    {"name":"BANKNIFTY","label":"Nifty Bank","segment":"nse_fo","spot_key":"nse_cm|99926009","step":100},
    {"name":"SENSEX","label":"Sensex","segment":"bse_fo","spot_key":"bse_cm|99919000","step":100},
    {"name":"FINNIFTY","label":"Fin Service","segment":"nse_fo","spot_key":"nse_cm|99926037","step":50},
    {"name":"MIDCPNIFTY","label":"Midcap Select","segment":"nse_fo","spot_key":"nse_cm|99926074","step":25},
]


def _expiry(value):
    raw=str(value or "").strip().upper()
    for fmt in ("%d%b%Y","%d-%b-%Y","%Y-%m-%d"):
        try:
            return datetime.strptime(raw,fmt).date()
        except ValueError:
            pass
    return None


class AngelOneChainProvider:
    source = "angel_one"

    def __init__(self, session, catalog, hub):
        self.session, self.cat, self.hub = session, catalog, hub
        self._rate_lock = threading.Lock()
        self._last_quote_call = 0.0

    def _client(self):
        try:
            return self.session.require_client()
        except Exception:
            raise ChainUnavailable("Not connected to Angel One.") from None

    def _master(self):
        try:
            self.cat.ins.load()
            return self.cat.ins._rows
        except Exception as exc:
            raise ChainUnavailable(f"Angel One instrument master unavailable ({type(exc).__name__}).") from None

    def underlyings(self):
        return INDEXES

    def _fo(self):
        return self.cat

    def search(self, q: str, limit: int = 12):
        needle=q.strip().upper()
        hits=[{"name":x["name"],"label":x["label"],"lot":None,"kind":"index"}
              for x in INDEXES if needle in x["name"] or needle in x["label"].upper()]
        try:
            seen={x["name"] for x in hits}
            hits += [{**x,"kind":"stock"} for x in self.cat.fo_underlyings(q,limit) if x["name"] not in seen]
        except Exception as exc:
            raise ChainUnavailable(f"Angel One F&O instrument list unavailable ({type(exc).__name__}).") from None
        return hits[:limit]

    def check(self, underlying: str):
        name=underlying.strip().upper()
        if name in {x["name"] for x in INDEXES}:
            return
        try:
            if name not in self.cat.fo_names():
                raise ChainUnavailable(f"{name} is not listed in Angel One's NSE/BSE F&O instrument master.")
        except ChainUnavailable:
            raise
        except Exception:
            raise ChainUnavailable("Angel One F&O instrument list could not be checked.") from None

    def _segment(self, underlying: str):
        idx=next((x for x in INDEXES if x["name"]==underlying.upper()),None)
        if idx:
            return idx["segment"]
        # Stock options are primarily NSE; use the segment actually present in the master.
        return "nse_fo"

    def _spot_key(self, underlying: str):
        idx=next((x for x in INDEXES if x["name"]==underlying.upper()),None)
        if idx:
            return idx["spot_key"]
        try:
            found=next((x for x in self.cat.search(underlying,10) if x.kind=="EQ" and x.name.upper()==underlying.upper()),None)
            return found.key if found else None
        except Exception:
            return None

    def expiries(self, underlying: str):
        target=underlying.upper()
        rows=[]
        for row in self._master():
            if str(row.get("name") or "").upper()!=target:
                continue
            if str(row.get("opttype") or "").upper() not in ("CE","PE"):
                continue
            if str(row.get("exch_seg") or "").upper() not in ("NSE_FO","NFO","BSE_FO","BFO"):
                continue
            exp=_expiry(row.get("expiry"))
            if exp and exp >= now_ist().date():
                rows.append(exp.isoformat())
        return sorted(set(rows))

    def _quotes(self, segment: str, tokens: list[str]):
        exchange=SEGMENT_TO_EXCHANGE[segment]
        out={}
        for offset in range(0,len(tokens),50):
            chunk=tokens[offset:offset+50]
            with self._rate_lock:
                wait=1.05-(time.time()-self._last_quote_call)
                if self._last_quote_call and wait>0:
                    time.sleep(wait)
                try:
                    response=self._client().getMarketData("FULL",{exchange:chunk})
                    data=_ok(response,"FULL market quotes")
                except Exception as exc:
                    raise ChainUnavailable(f"Angel One market quote request failed ({type(exc).__name__}).") from None
                self._last_quote_call=time.time()
            fetched=data.get("fetched") if isinstance(data,dict) else None
            if not isinstance(fetched,list):
                raise ChainUnavailable("Angel One returned an unreadable FULL market-quote response.")
            for row in fetched:
                out[str(row.get("symbolToken") or "")]=row
        return out

    def fetch(self, underlying: str, expiry: str | None):
        target=underlying.upper()
        selected=expiry or (self.expiries(target) or [None])[0]
        if not selected:
            raise ChainUnavailable(f"No active option expiry is available for {target}.")
        exp_date=datetime.strptime(selected,"%Y-%m-%d").date()
        rows=[]
        for row in self._master():
            if str(row.get("name") or "").upper()!=target:
                continue
            if str(row.get("opttype") or "").upper() not in ("CE","PE"):
                continue
            if str(row.get("exch_seg") or "").upper() not in ("NSE_FO","NFO","BSE_FO","BFO"):
                continue
            if _expiry(row.get("expiry"))==exp_date:
                rows.append(row)
        if not rows:
            raise ChainUnavailable(f"Angel One instrument master has no option contracts for {target} on {selected}.")
        # A chain must not mix NSE and BSE contracts in one quote request.
        by_segment={}
        for row in rows:
            seg=EXCHANGE_TO_SEGMENT.get(str(row.get("exch_seg") or "").upper())
            if seg:
                by_segment.setdefault(seg,[]).append(row)
        spot_key=self._spot_key(target)
        spot=self.hub.ltp(spot_key) if spot_key else None
        raw=RawChain(target,selected, max(int(_num(rows[0].get("lotsize"),1)),1),spot_key,spot,None)
        for segment, contracts in by_segment.items():
            tokens=[str(row.get("token") or "") for row in contracts if str(row.get("token") or "")]
            quotes=self._quotes(segment,tokens)
            for row in contracts:
                token=str(row.get("token") or "")
                quote=quotes.get(token,{})
                strike=_num(row.get("strike"))/100
                kind=str(row.get("opttype") or "").upper()
                if not strike or kind not in ("CE","PE"):
                    continue
                key=f"{segment}|{token}"
                raw.rows.setdefault(strike,{})[kind]=Leg(
                    key, str(row.get("symbol") or ""),
                    _num(quote.get("ltp")) or None,
                    _num(quote.get("close")) or None,
                    _num(quote.get("opnInterest")) or None,
                    None,
                    _num(quote.get("tradeVolume")) or None,
                )
        if not raw.rows:
            raise ChainUnavailable(f"Angel One returned no option quotes for {target}.")
        return raw
