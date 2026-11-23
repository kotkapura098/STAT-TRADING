"""Angel One instrument-master-backed catalog for the terminal UI."""
from __future__ import annotations

from datetime import date
from ..clock import now_ist
from ..market.models import Instrument
from ..brokers.angel_one import AngelOneInstruments, _num, SEGMENT_TO_EXCHANGE
from .catalog import Catalog, CASH, DERIV
from . import commodity as cm


class AngelOneCatalog(Catalog):
    def __init__(self, instruments: AngelOneInstruments, cfg=None):
        super().__init__()
        self.ins, self.cfg = instruments, cfg
        self.status = "loading"

    def warm(self, segments=None):
        self.ins.load()
        for item in getattr(self.cfg, "dashboard_indices", []) if self.cfg else []:
            sub = self.ins.index_subscription(item["label"])
            key = f"{sub.segment}|{sub.token}"
            self.register(Instrument(key, sub.segment, sub.token, item["label"], item["label"], "INDEX"))
        self.status = "ready"

    def _from_row(self, row):
        exchange = str(row.get("exch_seg") or "").upper()
        segment = next((s for s, e in SEGMENT_TO_EXCHANGE.items() if e == exchange), None)
        token = str(row.get("token") or "").strip()
        if not segment or not token.isdigit():
            return None
        opt = str(row.get("opttype") or "").upper()
        inst_type = str(row.get("instrumenttype") or "").upper()
        kind = opt if opt in ("CE", "PE") else ("FUT" if inst_type in ("FUTIDX", "FUTSTK", "FUTCUR", "FUTCOM") else "EQ")
        expiry = self.ins._expiry(row.get("expiry"))
        if kind in ("CE", "PE", "FUT") and expiry is None:
            return None
        if expiry and expiry < now_ist().date():
            return None
        strike = _num(row.get("strike")) / 100 if kind in ("CE", "PE") else None
        lot = max(int(_num(row.get("lotsize"), 1)), 1)
        symbol = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or symbol).strip().upper()
        desc = cm.label_of(name) if segment == "mcx_fo" else ""
        return Instrument(f"{segment}|{token}", segment, token, symbol, name, kind, lot=lot,
                          tick=max(_num(row.get("tick_size"), .05), .01), expiry=expiry, strike=strike, desc=desc)

    def _lookup(self, key):
        row = self.ins.get_row(key)
        return self._from_row(row) if row else None

    def _search_loaded(self, toks, limit):
        self.ins.load()
        words = [x for x in toks if x not in ("CE", "PE", "FUT")]
        wanted = next((x for x in toks if x in ("CE", "PE", "FUT")), None)
        if not words:
            return []
        q = " ".join(words)
        out = []
        for row in self.ins._rows:
            inst = self._from_row(row)
            if not inst:
                continue
            if wanted and inst.kind != wanted:
                continue
            if all(w in (inst.name + " " + inst.symbol).upper() for w in words):
                out.append(inst)
                if len(out) >= limit * 5:
                    break
        return out

    def mcx_future(self, name, on_or_after: date | None = None):
        self.ins.load()
        target = name.upper()
        today = on_or_after or now_ist().date()
        rows = [self._from_row(r) for r in self.ins._rows
                if str(r.get("exch_seg", "")).upper() == "MCX"
                and str(r.get("name", "")).upper() == target
                and str(r.get("instrumenttype", "")).upper().startswith("FUT")]
        rows = sorted((i for i in rows if i and i.expiry and i.expiry >= today), key=lambda i: i.expiry)
        return rows[0] if rows else None

    def mcx_loaded(self):
        return self.status == "ready"
