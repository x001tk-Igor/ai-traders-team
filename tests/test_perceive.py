import scripts.perceive as perceive
from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket


def test_build_snapshot_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(perceive, "state_dir", lambda cfg: str(tmp_path))
    cfg = load_config("config/trader.config.json")
    market = FakeMarket(point=0.01, digits=2, spread_points=20,
                        account={"balance": 10000, "equity": 10000})
    snap = perceive.build_snapshot(market, cfg, "XAUUSD", ["M5", "H1"])
    assert snap["symbol"] == "XAUUSD"
    assert set(snap["tf"].keys()) == {"M5", "H1"}
    acc = snap["account"]
    assert round(acc["total_budget_remaining_pct"]) == 6
    assert round(acc["daily_budget_remaining_pct"]) == 3
    # фичи посчитаны (не null) при достаточной истории
    assert snap["tf"]["M5"]["atr_price"] is not None
