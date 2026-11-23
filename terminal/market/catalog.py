"""
Instrument catalog: search by name / "NIFTY 25000 CE" / "RELIANCE", and resolve a key back to an Instrument.
The server always resolves the trading symbol and lot size from HERE - never from what the browser sends.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date

from .models import Instrument

log = logging.getLogger("catalog")

# Shared exchange-segment groups used by Angel One and sample instrument catalogues.
CASH = ("nse_cm", "bse_cm")
DERIV = ("nse_fo", "bse_fo", "mcx_fo")
MCX = "mcx_fo"


class Catalog:
    """Base class: registered instruments (indices etc.) + whatever a subclass can load."""

    def __init__(self) -> None:
        self._reg: dict[str, Instrument] = {}
        self._lock = threading.Lock()
        self.status = "ready"

    def register(self, *insts: Instrument) -> None:
        with self._lock:
            for i in insts:
                self._reg[i.key] = i

    def get(self, key: str) -> Instrument | None:
        with self._lock:
            hit = self._reg.get(key)
        if hit:
            return hit
        found = self._lookup(key)
        if found:
            self.register(found)                     # cache it: order/position pages look the same keys up repeatedly
        return found

    def _lookup(self, key: str) -> Instrument | None:            # subclasses
        return None

    def mcx_future(self, name: str, on_or_after: date | None = None) -> Instrument | None:   # subclasses with commodities
        return None

    def mcx_loaded(self) -> bool:
        return False

    def _search_loaded(self, toks: list[str], limit: int) -> list[Instrument]:   # subclasses
        return []

    def search(self, q: str, limit: int = 25) -> list[Instrument]:
        toks = [t for t in re.split(r"[\s,]+", q.strip().upper()) if t]
        if not toks:
            return []
        out: list[Instrument] = []
        with self._lock:
            regs = list(self._reg.values())
        word = toks[0]
        for i in regs:                                            # indices and other registered items
            hay = f"{i.name} {i.symbol}".upper()
            if all(t in hay for t in toks) and i.kind == "INDEX":
                out.append(i)
        out.extend(self._search_loaded(toks, limit))
        seen, uniq = set(), []
        for i in out:
            if i.key not in seen:
                seen.add(i.key)
                uniq.append(i)
        uniq.sort(key=lambda i: (0 if i.name == word else 1, 0 if i.kind in ("INDEX", "EQ") else 1,
                                 i.expiry or date.max, i.strike or 0, i.kind, i.name))
        return uniq[:limit]


# ----------------------------------------------------------------------------------- lazy catalog
class LazyCatalog(Catalog):
    """A stable object the rest of the app can hold while the real catalog (sample or Angel One) is created later."""

    def __init__(self) -> None:
        super().__init__()
        self.inner: Catalog | None = None

    def set(self, inner: Catalog | None) -> None:
        self.inner = inner

    def _lookup(self, key: str) -> Instrument | None:
        return self.inner.get(key) if self.inner else None

    def search(self, q: str, limit: int = 25) -> list[Instrument]:
        return self.inner.search(q, limit) if self.inner else []

    def mcx_future(self, name: str, on_or_after: date | None = None) -> Instrument | None:
        f = getattr(self.inner, "mcx_future", None)
        return f(name, on_or_after) if f else None

    def mcx_loaded(self) -> bool:
        f = getattr(self.inner, "mcx_loaded", None)
        return bool(f and f())

    @property
    def loading(self) -> bool:
        return getattr(self.inner, "status", "ready") == "loading"
