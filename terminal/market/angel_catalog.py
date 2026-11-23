"""Catalog adapter that exposes Angel One's instrument master to the terminal."""
from __future__ import annotations

from datetime import datetime, date
from ..brokers.angel_one import AngelOneInstruments, SEGMENT_TO_WS
from ..clock import now_ist
from .catalog import Catalog, CASH, DERIV
from .models import Instrument


class AngelOneCatalog(Catalog):
    def __init__(self, instruments: AngelOneInstruments):
        super().__init__()
        self.ins = instruments

    @staticmethod
    def _expiry(value):
        raw = str(value or "").strip().upper()
        for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
        return None

    def warm(self, segments=None):
        self.ins.load()

    def _row_to_instrument(self, key: str, row: dict) -> Instrument | None:
        try:
            seg, token = key.split("|", 1)
            symbol = str(row.get("symbol") or "").strip()
            name = str(row.get("name") or symbol).strip().upper()
            inst_type = str(row.get("instrumenttype") or "").upper()
            opt = str(row.get("opttype") or "").upper()
            if opt in ("CE", "PE"):
                kind = opt
            elif "FUT" in inst_type:
                kind = "FUT"
            elif seg in CASH:
                kind = "EQ"
            else:
                kind = "FUT"
            expiry = self._expiry(row.get("expiry"))
            strike_raw = float(row.get("strike") or 0)
            strike = strike_raw / 100 if kind in ("CE", "PE") else None
            lot = max(int(float(row.get("lotsize") or 1)), 1)
            tick_raw = float(row.get("tick_size") or 5)
            tick = max(tick_raw / 100, 0.01)
            return Instrument(key, seg, token, symbol, name, kind, lot, tick, expiry, strike)
        except (TypeError, ValueError):
            return None

    def _lookup(self, key: str):
        row = self.ins.get_row(key)
        return self._row_to_instrument(key, row) if row else None

    def _search_loaded(self, toks: list[str], limit: int):
        self.ins.load()
        out=[]
        for key,row in self.ins._by_key.items():
            inst=self._row_to_instrument(key,row)
            if inst and all(t in f"{inst.name} {inst.symbol}".upper() for t in toks):
                out.append(inst)
        return out[:limit]

    def candidates(self, instrument: str, ref_price: float, window: int, expiry_offset: int = 0):
        return self.ins.candidates(instrument, ref_price, window, expiry_offset)

    def fo_names(self):
        self.ins.load()
        names={}
        for row in self.ins._rows:
            if str(row.get("exch_seg") or "").upper() not in ("NFO", "NSE_FO", "BFO", "BSE_FO"):
                continue
            if str(row.get("opttype") or "").upper() in ("CE","PE"):
                name=str(row.get("name") or "").upper()
                if name:
                    names[name]=max(int(float(row.get("lotsize") or 1)),1)
        return names

    def fo_underlyings(self, q: str, limit: int = 12):
        needle=q.strip().upper()
        return [{"name":name,"lot":lot} for name,lot in sorted(self.fo_names().items()) if needle in name][:limit]

    def index_subscription(self, instrument: str):
        return self.ins.index_subscription(instrument)

    def mcx_loaded(self):
        return bool(self.ins._rows)

    def mcx_future(self, name: str, on_or_after: date | None = None):
        self.ins.load()
        day=on_or_after or now_ist().date()
        choices=[]
        for key,row in self.ins._by_key.items():
            if str(row.get("exch_seg") or "").upper() not in ("MCX", "MCX_FO"):
                continue
            if str(row.get("name") or "").upper() != name.upper():
                continue
            expiry=self._expiry(row.get("expiry"))
            if expiry and expiry >= day and "FUT" in str(row.get("instrumenttype") or "").upper():
                inst=self._row_to_instrument(key,row)
                if inst: choices.append(inst)
        return min(choices,key=lambda x:x.expiry) if choices else None
