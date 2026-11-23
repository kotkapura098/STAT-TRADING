from terminal.market.angel_catalog import AngelOneCatalog


def test_angel_catalog_maps_real_master_fields():
    row = {"exch_seg":"nse_fo","token":"777","symbol":"NIFTY31DEC26000CE","name":"NIFTY",
           "instrumenttype":"OPTIDX","opttype":"CE","expiry":"31DEC2026",
           "strike":"2600000","lotsize":"65","tick_size":"5.000000"}
    class Instruments:
        def load(self, force=False): pass
        def get_row(self, key): return row if key=="nse_fo|777" else None
    catalog=AngelOneCatalog(Instruments())
    instrument=catalog.get("nse_fo|777")
    assert instrument is not None
    assert instrument.kind=="CE"
    assert instrument.strike==26000
    assert instrument.lot==65
    assert instrument.tick==0.05
