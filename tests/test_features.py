from trader_lib.features import compute_tf_features

KW = dict(point=0.01, atr_period=30, momentum_bars=[5, 15, 30], range_bars=30,
          ema_fast=12, ema_slow=26, atr_pctile_lookback=500)


def test_uptrend_trend_up(uptrend_bars):
    f = compute_tf_features(uptrend_bars, **KW)
    assert f["trend"] == "up"
    assert f["mom_mid_atr"] > 0
    assert 0.0 <= f["pos_in_range"] <= 1.0
    assert f["atr_price"] > 0 and f["atr_points"] > 0


def test_insufficient_bars_returns_null(uptrend_bars):
    f = compute_tf_features(uptrend_bars.head(5), **KW)
    assert f["atr_price"] is None
    assert f["reason"].startswith("insufficient")
