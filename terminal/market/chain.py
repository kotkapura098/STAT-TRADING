"""Option-chain and open-interest analysis shared by the sample and Angel One providers.

  provider  gives a raw snapshot of one chain (strikes, call/put legs with price and open interest)
              - SampleChainProvider: deterministic invented OI and sample prices
              - AngelOneChainProvider: instrument-master contracts plus FULL quote and OI batches
  service   caches snapshots, overlays live prices/OI from the quote hub, and derives ATM, PCR,
            max pain, largest call/put OI strikes, ATM straddle and per-leg OI buildup labels.

Everything derived here is arithmetic on the displayed data; it is not a trading signal.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..clock import now_ist
from . import commodity as cmd
from .hub import QuoteHub
from .models import Instrument

log = logging.getLogger("chain")

# name, label, Angel One exchange segment, cash-segment spot key
INDEX_UNDERLYINGS = [
    {"name": "NIFTY", "label": "Nifty 50", "fo": "nse_fo", "spot_key": "nse_cm|Nifty 50", "step": 50},
    {"name": "BANKNIFTY", "label": "Nifty Bank", "fo": "nse_fo", "spot_key": "nse_cm|Nifty Bank", "step": 100},
    {"name": "SENSEX", "label": "Sensex", "fo": "bse_fo", "spot_key": "bse_cm|SENSEX", "step": 100},
    {"name": "FINNIFTY", "label": "Fin Service", "fo": "nse_fo", "spot_key": "nse_cm|Nifty Fin Service", "step": 50},
    {"name": "MIDCPNIFTY", "label": "Midcap Select", "fo": "nse_fo", "spot_key": "nse_cm|NIFTY MID SELECT", "step": 25},
]


@dataclass
class Leg:
    key: str
    symbol: str
    ltp: float | None
    prev_close: float | None
    oi: float | None
    oi_prev: float | None
    volume: float | None = None


@dataclass
class RawChain:
    underlying: str
    expiry: str                      # ISO date
    lot: int
    spot_key: str | None
    spot: float | None               # as of the REST snapshot (may be None)
    atm_hint: float | None           # strike the provider flagged ATM, if any
    rows: dict[float, dict[str, Leg]] = field(default_factory=dict)      # strike -> {"CE": Leg, "PE": Leg}
    fetched: float = field(default_factory=time.time)


class ChainUnavailable(Exception):
    """Raised with a user-readable reason."""


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------- analysis
def max_pain(strikes: list[float], ce_oi: dict[float, float], pe_oi: dict[float, float]) -> float | None:
    """Strike at which option BUYERS together would lose the most (writers pay out the least) at expiry."""
    if not strikes:
        return None
    best, best_k = None, None
    for k in strikes:
        pay = 0.0
        for s in strikes:
            pay += ce_oi.get(s, 0.0) * max(k - s, 0.0) + pe_oi.get(s, 0.0) * max(s - k, 0.0)
        if best is None or pay < best:
            best, best_k = pay, k
    return best_k


def buildup(ltp: float | None, prev_close: float | None, oi: float | None, oi_prev: float | None) -> str | None:
    """Standard four-way reading of price direction vs OI direction. A description, not a recommendation."""
    if None in (ltp, prev_close, oi, oi_prev) or not prev_close or not oi_prev:
        return None
    dp, doi = ltp - prev_close, oi - oi_prev
    if abs(dp) < 1e-9 or abs(doi) < 1e-9:
        return None
    if dp > 0 and doi > 0:
        return "Long build-up"
    if dp < 0 and doi > 0:
        return "Short build-up"
    if dp > 0 and doi < 0:
        return "Short covering"
    return "Long unwinding"


def _leg_dict(leg: Leg, hub: QuoteHub) -> dict:
    q = hub.get(leg.key)
    ltp = q.ltp if q else leg.ltp
    pc = (q.prev_close if q and q.prev_close else None) or leg.prev_close
    oi = (q.oi if q and q.oi else None) or leg.oi
    vol = (q.volume if q and q.volume else None) or leg.volume
    prev = leg.oi_prev
    chg = None if oi is None or prev is None else oi - prev
    return {
        "key": leg.key, "symbol": leg.symbol, "ltp": ltp, "pc": pc,
        "chg_pct": None if not (ltp is not None and pc) else round((ltp - pc) / pc * 100, 2),
        "oi": oi, "oi_prev": prev, "oi_chg": chg,
        "oi_chg_pct": None if chg is None or not prev else round(chg / prev * 100, 2),
        "vol": vol, "buildup": buildup(ltp, pc, oi, prev),
    }


def analyse(raw: RawChain, hub: QuoteHub, window: int | None) -> dict:
    spot = hub.ltp(raw.spot_key) if raw.spot_key else None
    spot = spot or raw.spot
    strikes = sorted(raw.rows)
    atm = None
    if spot and strikes:
        atm = min(strikes, key=lambda k: abs(k - spot))
    elif raw.atm_hint in strikes:
        atm = raw.atm_hint

    rows, ce_oi, pe_oi = [], {}, {}
    for k in strikes:
        legs = raw.rows[k]
        ce = _leg_dict(legs["CE"], hub) if "CE" in legs else None
        pe = _leg_dict(legs["PE"], hub) if "PE" in legs else None
        if ce and ce["oi"] is not None:
            ce_oi[k] = ce["oi"]
        if pe and pe["oi"] is not None:
            pe_oi[k] = pe["oi"]
        rows.append({"strike": k, "ce": ce, "pe": pe})

    tot_ce, tot_pe = sum(ce_oi.values()), sum(pe_oi.values())
    chg_ce = sum((r["ce"]["oi_chg"] or 0) for r in rows if r["ce"])
    chg_pe = sum((r["pe"]["oi_chg"] or 0) for r in rows if r["pe"])
    prev_ce = sum((r["ce"]["oi_prev"] or 0) for r in rows if r["ce"])
    prev_pe = sum((r["pe"]["oi_prev"] or 0) for r in rows if r["pe"])

    def top(d: dict[float, float], n: int = 3) -> list[dict]:
        return [{"strike": k, "oi": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]

    straddle = None
    atm_ce = None
    atm_pe = None
    if atm is not None:
        r = next((r for r in rows if r["strike"] == atm), None)
        if r and r["ce"] and r["pe"]:
            atm_ce = r["ce"]["ltp"]
            atm_pe = r["pe"]["ltp"]
            if atm_ce is not None and atm_pe is not None:
                straddle = round(atm_ce + atm_pe, 2)

    # trim the displayed window around ATM (the analysis above always uses every strike we have)
    shown = rows
    if window and atm is not None:
        i = strikes.index(atm)
        shown = rows[max(0, i - window): i + window + 1]

    return {
        "spot": spot, "atm": atm, "rows": shown, "strikes_total": len(rows),
        "summary": {
            "pcr": round(tot_pe / tot_ce, 3) if tot_ce else None,
            "pcr_prev": round(prev_pe / prev_ce, 3) if prev_ce else None,
            "ce_oi": tot_ce, "pe_oi": tot_pe, "ce_oi_chg": chg_ce, "pe_oi_chg": chg_pe,
            "max_pain": max_pain(strikes, ce_oi, pe_oi),
            "ce_top": top(ce_oi), "pe_top": top(pe_oi),
            "straddle": straddle,
            "straddle_pct": round(straddle / spot * 100, 2) if straddle and spot else None,
            "atm_ce": atm_ce,
            "atm_pe": atm_pe,
        },
    }


# ---------------------------------------------------------------------------------------- sample
def sample_oi_base(strike: float, kind: str, spot: float, step: float, seed_key: str) -> float:
    """Invented but plausible OI shape: calls pile up above the market, puts below, with round-number spikes."""
    rng = random.Random(f"{seed_key}|{strike}|{kind}")
    d = (strike - spot) / step
    centre = 4.0 if kind == "CE" else -4.0
    hump = math.exp(-((d - centre) / 5.5) ** 2)
    round_bonus = 1.7 if int(strike) % (step * 4) == 0 else 1.0
    return round(max(5000, (150_000 + 3_800_000 * hump) * round_bonus * rng.uniform(0.8, 1.2)) / 75) * 75


class SampleChainProvider:
    source = "sample"

    def __init__(self, catalog, spots: dict[str, float], derivs: list[tuple], eq_spots: dict[str, float] | None = None) -> None:
        self.cat, self.spots, self.derivs = catalog, spots, derivs
        self.eq_spots = eq_spots or {}                      # stock prices (sample world), for the first snapshot's spot

    def underlyings(self) -> list[dict]:
        names = {d[0] for d in self.derivs}
        return [u for u in INDEX_UNDERLYINGS if u["name"] in names]

    def _stock_names(self) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        for i in self.cat.contracts:
            if i.segment == cmd.SEGMENT:
                continue
            if i.kind in ("CE", "PE") and i.name not in out and not any(u["name"] == i.name for u in INDEX_UNDERLYINGS):
                eq = next((e for e in self.cat.contracts if e.kind == "EQ" and e.name == i.name), None)
                out[i.name] = (eq.desc if eq and eq.desc else i.name, i.lot)
        return out

    def _cm_names(self) -> dict[str, int]:
        return {i.name: i.lot for i in self.cat.contracts if i.segment == cmd.SEGMENT and i.kind in ("CE", "PE")}

    def search(self, q: str, limit: int = 12) -> list[dict]:
        q = q.strip().upper()
        hits = [{"name": u["name"], "label": u["label"], "lot": None, "kind": "index"} for u in self.underlyings()
                if q in u["name"] or q in u["label"].upper()]
        cms = self._cm_names()
        pick = set(cmd.resolve(q.split())) | {n for n in cms if n.startswith(q)}
        hits += [{"name": n, "label": cmd.label_of(n), "lot": cms[n], "kind": "commodity", "size": cmd.info(n)["size"]}
                 for n in sorted(pick) if n in cms]
        for n, (desc, lot) in sorted(self._stock_names().items()):
            if n.startswith(q) or q in desc.upper():
                hits.append({"name": n, "label": desc, "lot": lot, "kind": "stock"})
        return hits[:limit]

    def check(self, und: str) -> None:
        if not any(u["name"] == und for u in INDEX_UNDERLYINGS) and und not in self._stock_names() and und not in self._cm_names():
            raise ChainUnavailable(f"{und} has no options in the sample world. Search for RELIANCE, TCS, SBIN, GOLD, CRUDE ...")

    def _contracts(self, und: str, expiry: date | None = None):
        return [i for i in self.cat.contracts if i.kind in ("CE", "PE") and i.name == und and (expiry is None or i.expiry == expiry)]

    def expiries(self, und: str) -> list[str]:
        return sorted({i.expiry.isoformat() for i in self._contracts(und) if i.expiry})

    def fetch(self, und: str, expiry: str | None) -> RawChain:
        exps = self.expiries(und)
        if not exps:
            raise ChainUnavailable(f"No sample contracts for {und}")
        expiry = expiry if expiry in exps else exps[0]
        from .sample import sample_und, SAMPLE_CM
        if und in SAMPLE_CM:                                                         # commodity: the option is on the matching future
            fut = self.cat.mcx_future(und, date.fromisoformat(expiry))
            spot_key, step = (fut.key if fut else None), SAMPLE_CM[und]["step"]
            base = SAMPLE_CM[und]["price"]
        else:
            spot_key, step = sample_und(und)
            base = self.spots.get(spot_key) or self.eq_spots.get(spot_key, 0.0)
        contracts = self._contracts(und, date.fromisoformat(expiry))
        raw = RawChain(und, expiry, contracts[0].lot, spot_key, base, None)
        for i in contracts:
            oi = sample_oi_base(i.strike, i.kind, base, step, f"{und}{expiry}")
            prev = round(oi * random.Random(f"p|{i.key}").uniform(0.88, 1.1) / 75) * 75
            raw.rows.setdefault(i.strike, {})[i.kind] = Leg(i.key, i.symbol, None, None, oi, prev)
        return raw


COMMODITY_ALIASES = {
    # Typo / abbreviation mappings to official MCX underlying symbols:
    "CRUDEM": "CRUDEOILM",
    "CRUDEMINI": "CRUDEOILM",
    "CRUDEOILM": "CRUDEOILM",
    "CRUDE": "CRUDEOIL",
    "NATGAS": "NATURALGAS",
    "NATGASM": "NATGASMINI",
    "NATGASMINI": "NATGASMINI",
    "NATURALGASM": "NATGASMINI",
    "SILVERMINI": "SILVERM",
    "GOLDMINI": "GOLDM",
}


# --------------------------------------------------------------------------------------- service
class ChainService:
    OWNER = "chain"

    def __init__(self, hub: QuoteHub, store=None, ttl: float = 20.0) -> None:
        self.hub, self.store, self.ttl = hub, store, ttl
        self.provider = None
        self._raw: dict[tuple[str, str], RawChain] = {}
        self._exp: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()
        self._followed: set[str] = set()
        self._last_series: dict[tuple[str, str], float] = {}

    def set_provider(self, p) -> None:
        with self._lock:
            self.provider = p
            self._raw.clear()
            self._exp.clear()

    @property
    def ready(self) -> bool:
        return self.provider is not None

    def underlyings(self) -> list[dict]:
        return self.provider.underlyings() if self.provider else []

    def search(self, q: str, limit: int = 12) -> list[dict]:
        """Any underlying that has options (indices and F&O stocks) whose name starts with, or whose company name contains, q."""
        if not self.provider:
            raise ChainUnavailable("Connect the Angel One account or switch to Sample mode first.")
        return self.provider.search(q, limit) if q.strip() and hasattr(self.provider, "search") else []

    def expiries(self, und: str) -> list[str]:
        if not self.provider:
            raise ChainUnavailable("Not connected to Kotak yet.")
        now = time.time()
        hit = self._exp.get(und)
        if hit and now - hit[0] < 600:
            return hit[1]
        exps = [e for e in self.provider.expiries(und) if e >= now_ist().date().isoformat()]
        self._exp[und] = (now, exps)
        return exps

    def _raw_chain(self, und: str, expiry: str) -> RawChain:
        k = (und, expiry)
        hit = self._raw.get(k)
        if hit and time.time() - hit.fetched < self.ttl:
            return hit
        with self._lock:
            hit = self._raw.get(k)
            if hit and time.time() - hit.fetched < self.ttl:
                return hit
            try:
                raw = self.provider.fetch(und, expiry)
            except ChainUnavailable:
                if hit:                                       # serve the last good snapshot rather than a blank page
                    return hit
                raise
            self._raw[k] = raw
            return raw

    def raw_for(self, und: str, sel: str) -> RawChain:
        """The raw chain for strategies: sel is 'nearest', 'next' or an ISO expiry date."""
        if not self.provider:
            raise ChainUnavailable("Not connected to Kotak yet.")
        exps = self.expiries(und.upper())
        if not exps:
            raise ChainUnavailable(f"No expiries found for {und}.")
        if sel == "nearest":
            pick = exps[0]
        elif sel == "next":
            if len(exps) < 2:
                raise ChainUnavailable(f"Only one expiry is listed for {und}.")
            pick = exps[1]
        elif sel in exps:
            pick = sel
        else:
            raise ChainUnavailable(f"{und} has no expiry on {sel}.")
        return self._raw_chain(und.upper(), pick)

    FOLLOW_STRIKES = 60

    def _follow(self, raw: RawChain) -> bool:
        """Subscribe to live prices for the strikes nearest the money only; far strikes show the chain snapshot."""
        ref = self.hub.ltp(raw.spot_key) if raw.spot_key else None
        ref = ref or raw.spot or raw.atm_hint
        ks = sorted(raw.rows)
        if ref:
            ks = sorted(ks, key=lambda k: abs(k - ref))
        elif len(ks) > self.FOLLOW_STRIKES:
            mid = len(ks) // 2
            ks = ks[max(0, mid - self.FOLLOW_STRIKES // 2): mid + self.FOLLOW_STRIKES // 2]
        keys = {l.key for k in ks[: self.FOLLOW_STRIKES] for l in raw.rows[k].values()}
        if raw.spot_key:
            keys.add(raw.spot_key)
        new, gone = keys - self._followed, self._followed - keys
        if new:
            self.hub.want(self.OWNER, sorted(new))
        if gone:
            self.hub.release(self.OWNER, sorted(gone))
        self._followed = keys
        return bool(new)

    def get(self, und: str, expiry: str | None, window: int | None) -> dict:
        if not self.provider:
            raise ChainUnavailable("Not connected to Kotak yet.")
        und = und.upper()
        if hasattr(self.provider, "check"):
            self.provider.check(und)
        exps = self.expiries(und)
        if not exps:
            raise ChainUnavailable(f"No expiries found for {und}.")
        expiry = expiry if expiry in exps else exps[0]
        raw = self._raw_chain(und, expiry)
        fresh = self._follow(raw)
        if fresh and self.provider.source == "sample":
            time.sleep(0.6)                                   # let the sample feed publish a first price for the new strikes
        out = analyse(raw, self.hub, window)
        age = time.time() - raw.fetched
        out.update({"underlying": und, "expiry": raw.expiry or expiry, "expiries": exps, "lot": raw.lot,
                    "spot_key": raw.spot_key, "source": self.provider.source, "snapshot_age_s": round(age, 1),
                    "stale": age > max(self.ttl * 3, 60)})
        self._record(und, raw.expiry or expiry, out)
        return out

    def _ns(self, und: str) -> str:
        """Invented (sample) chains are saved under their own name, never next to real ones."""
        return "SAMPLE:" + und if self.provider is not None and self.provider.source == "sample" else und

    # ---- intraday series (recorded while the terminal runs and somebody is looking at this chain)
    def _record(self, und: str, expiry: str, out: dict) -> None:
        if not self.store or self.provider is None:
            return
        now = time.time()
        k = (und, expiry)
        if now - self._last_series.get(k, 0) < 60:
            return
        s = out["summary"]
        if s["pcr"] is None or not out["spot"]:
            return
        self._last_series[k] = now
        try:
            self.store.add_oi_point(now_ist().date().isoformat(), self._ns(und), expiry, now, out["spot"],
                                    s["pcr"], s["ce_oi"], s["pe_oi"], s["max_pain"],
                                    s.get("straddle"), s.get("atm_ce"), s.get("atm_pe"))
        except Exception:
            log.exception("could not record OI point")

    def series(self, und: str, expiry: str) -> list[dict]:
        return self.store.oi_series(now_ist().date().isoformat(), self._ns(und.upper()), expiry) if self.store else []
