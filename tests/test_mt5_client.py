from trader_lib.mt5_client import FakeMarket


def test_fake_bars_and_symbol():
    m = FakeMarket(point=0.01, digits=2, spread_points=20)
    bars = m.copy_rates("XAUUSD", "M5", 100)
    assert len(bars) == 100
    assert {"time", "open", "high", "low", "close"} <= set(bars.columns)
    si = m.symbol_info("XAUUSD")
    assert si["point"] == 0.01 and si["digits"] == 2
