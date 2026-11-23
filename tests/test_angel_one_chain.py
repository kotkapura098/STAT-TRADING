from datetime import date
from types import SimpleNamespace

from terminal.market.angel_chain import AngelOneChainProvider


EXPIRY = date(2026, 12, 31)


class FakeInstruments:
    def __init__(self):
        self._rows = [
            {"exch_seg": "NFO", "name": "NIFTY", "symbol": "NIFTY26DEC26000CE",
             "token": "1001", "opttype": "CE", "expiry": "31DEC2026",
             "strike": "2600000", "lotsize": "65"},
            {"exch_seg": "NFO", "name": "NIFTY", "symbol": "NIFTY26DEC26000PE",
             "token": "1002", "opttype": "PE", "expiry": "31DEC2026",
             "strike": "2600000", "lotsize": "65"},
            {"exch_seg": "NFO", "name": "NIFTY", "symbol": "NIFTY26DEC26050CE",
             "token": "1003", "opttype": "CE", "expiry": "31DEC2026",
             "strike": "2605000", "lotsize": "65"},
            {"exch_seg": "NFO", "name": "NIFTY", "symbol": "NIFTY26DEC26050PE",
             "token": "1004", "opttype": "PE", "expiry": "31DEC2026",
             "strike": "2605000", "lotsize": "65"},
            {"exch_seg": "NFO", "name": "RELIANCE", "symbol": "RELIANCE26DEC1300CE",
             "token": "2001", "opttype": "CE", "expiry": "31DEC2026",
             "strike": "130000", "lotsize": "250"},
        ]

    def load(self):
        pass

    @staticmethod
    def _expiry(value):
        from datetime import datetime
        return datetime.strptime(value, "%d%b%Y").date()


class FakeClient:
    def __init__(self):
        self.calls = []

    def getMarketData(self, mode, exchange_tokens):
        self.calls.append((mode, exchange_tokens))
        tokens = next(iter(exchange_tokens.values()))
        return {"status": True, "data": {"fetched": [
            {"symbolToken": token, "ltp": "125.50", "close": "120.00",
             "opnInterest": "45000", "tradeVolume": "9000"}
            for token in tokens
        ], "unfetched": []}}


def make_provider():
    client = FakeClient()
    session = SimpleNamespace(require_client=lambda: client)
    instruments = FakeInstruments()
    catalog = SimpleNamespace(_reg={})
    hub = SimpleNamespace(ltp=lambda key: None)
    return AngelOneChainProvider(session, instruments, catalog, hub), client


def test_angel_one_chain_discovers_expiries_and_underlyings():
    provider, _ = make_provider()
    assert provider.expiries("NIFTY") == [EXPIRY.isoformat()]
    hits = provider.search("NIFT")
    assert hits[0]["name"] == "NIFTY"
    assert hits[0]["kind"] == "index"
    assert any(x["name"] == "RELIANCE" for x in provider.underlyings())


def test_angel_one_chain_uses_full_quotes_and_real_open_interest():
    provider, client = make_provider()
    raw = provider.fetch("NIFTY", EXPIRY.isoformat())
    assert raw.lot == 65
    assert sorted(raw.rows) == [26000.0, 26050.0]
    ce = raw.rows[26000.0]["CE"]
    pe = raw.rows[26000.0]["PE"]
    assert ce.key == "nse_fo|1001"
    assert ce.ltp == 125.5
    assert ce.prev_close == 120.0
    assert ce.oi == 45000
    assert ce.volume == 9000
    assert pe.oi == 45000
    assert client.calls[0][0] == "FULL"
    assert client.calls[0][1] == {"NFO": ["1001", "1002", "1003", "1004"]}


def test_angel_one_chain_oi_flows_into_pcr_and_max_pain():
    from terminal.market.chain import analyse
    provider, _ = make_provider()
    raw = provider.fetch("NIFTY", EXPIRY.isoformat())
    result = analyse(raw, SimpleNamespace(ltp=lambda key: None, get=lambda key: None), None)
    assert result["summary"]["ce_oi"] == 90000
    assert result["summary"]["pe_oi"] == 90000
    assert result["summary"]["pcr"] == 1.0
    assert result["summary"]["max_pain"] in (26000.0, 26050.0)


def test_angel_one_chain_does_not_fabricate_previous_oi():
    provider, _ = make_provider()
    raw = provider.fetch("NIFTY", EXPIRY.isoformat())
    assert raw.rows[26000.0]["CE"].oi_prev is None


def test_angel_one_chain_rejects_failed_quote_response():
    provider, client = make_provider()
    client.getMarketData = lambda *args: {"status": False, "message": "rate limit"}
    from terminal.market.chain import ChainUnavailable
    try:
        provider.fetch("NIFTY", EXPIRY.isoformat())
    except ChainUnavailable as exc:
        assert "rate limit" in str(exc)
    else:
        raise AssertionError("failed quote response must not produce a fake chain")



def test_historical_oi_baseline_uses_last_prior_session_observation():
    from datetime import timedelta
    provider, client = make_provider()
    today = date(2026, 11, 23)
    captured = {}

    def get_oi(params):
        captured.update(params)
        return {"status": True, "data": [
            {"time": "2026-11-20T15:28:00+05:30", "oi": 41000},
            {"time": "2026-11-20T15:29:00+05:30", "oi": 42000},
            {"time": "2026-11-23T09:16:00+05:30", "oi": 50000},
        ]}

    client.getOIData = get_oi
    result = provider._request_historical_oi("NFO", "1001", today)
    assert result == ("2026-11-20", 42000.0)
    assert captured["exchange"] == "NFO"
    assert captured["symboltoken"] == "1001"
    assert captured["interval"] == "ONE_MINUTE"
    assert captured["todate"] == "2026-11-22 15:30"


def test_historical_oi_baseline_accepts_sdk_array_rows():
    provider, client = make_provider()
    client.getOIData = lambda params: {"status": True, "data": [
        ["2026-11-20T15:29:00+05:30", 76543],
    ]}
    assert provider._request_historical_oi("NFO", "1002", date(2026, 11, 23)) == ("2026-11-20", 76543.0)


def test_historical_oi_baseline_is_cached_by_request_day_and_contract(tmp_path):
    cache = tmp_path / "angel_oi.json"
    provider, _ = make_provider()
    provider._cache_path = cache
    from terminal.clock import now_ist
    day = now_ist().date()
    key = provider._oi_cache_key(day, "NFO", "1001")
    provider._oi_cache[key] = {"source_day": "2026-11-20", "oi": 42000}
    provider._save_oi_cache()

    restored, _ = make_provider()
    restored._cache_path = cache
    restored._load_oi_cache()
    assert restored._previous_oi("NFO", "1001") == 42000



def test_chain_uses_cached_historical_oi_for_buildup_analysis():
    from terminal.clock import now_ist
    from terminal.market.chain import analyse
    provider, _ = make_provider()
    today = now_ist().date()
    for token in ("1001", "1002", "1003", "1004"):
        key = provider._oi_cache_key(today, "NFO", token)
        provider._oi_cache[key] = {"source_day": "2026-11-20", "oi": 40000}
    raw = provider.fetch("NIFTY", EXPIRY.isoformat())
    assert raw.rows[26000.0]["CE"].oi_prev == 40000
    result = analyse(raw, SimpleNamespace(ltp=lambda key: None, get=lambda key: None), None)
    assert result["rows"][0]["ce"]["oi_chg"] == 5000
    assert result["rows"][0]["ce"]["buildup"] == "Long build-up"
