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
