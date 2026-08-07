from trader_lib.account import account_snapshot


def test_fuel_gauges():
    acc = {"balance": 10000, "equity": 9850}
    snap = account_snapshot(acc, day_start_equity=10000, initial_balance=10000,
                            profit_target_pct=10, daily_limit_pct=3, total_limit_pct=6,
                            positions=[])
    assert round(snap["daily_budget_remaining_pct"], 2) == 1.5
    assert round(snap["total_budget_remaining_pct"], 2) == 4.5
    assert snap["progress_to_target_pct"] < 0
