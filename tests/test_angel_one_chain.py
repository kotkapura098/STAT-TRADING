from types import SimpleNamespace

from terminal.market.angel_chain import AngelOneChainProvider
from terminal.market.hub import QuoteHub


def test_angel_one_chain_uses_full_quote_open_interest():
    master = [
        {"exch_seg":"nse_fo","token":"101","symbol":"NIFTY31DEC26000CE","name":"NIFTY",
         "expiry":"31DEC2026","strike":"2600000","lotsize":"65","opttype":"CE"},
        {"exch_seg":"nse_fo","token":"102","symbol":"NIFTY31DEC26000PE","name":"NIFTY",
         "expiry":"31DEC2026","strike":"2600000","lotsize":"65","opttype":"PE"},
    ]
    class Client:
        def getMarketData(self, mode, exchange_tokens):
            assert mode == "FULL"
            assert exchange_tokens == {"NFO":["101","102"]}
            return {"status":True,"data":{"fetched":[
                {"symbolToken":"101","ltp":125.5,"close":120.0,"opnInterest":10000,"tradeVolume":2500},
                {"symbolToken":"102","ltp":98.5,"close":101.0,"opnInterest":12000,"tradeVolume":3100},
            ],"unfetched":[]}}
    session=SimpleNamespace(require_client=lambda:Client())
    ins=SimpleNamespace(_rows=master,load=lambda:None)
    catalog=SimpleNamespace(ins=ins)
    provider=AngelOneChainProvider(session,catalog,QuoteHub())
    raw=provider.fetch("NIFTY","2026-12-31")
    assert raw.lot == 65
    assert raw.rows[26000.0]["CE"].key == "nse_fo|101"
    assert raw.rows[26000.0]["CE"].ltp == 125.5
    assert raw.rows[26000.0]["PE"].oi == 12000
