from trader_lib.size_position import compute_lots

SI = {"point": 0.01, "trade_contract_size": 100, "volume_min": 0.01,
      "volume_max": 100.0, "volume_step": 0.01}


def test_lots_from_risk():
    # риск $100, SL 3.0 цены (=300 пунктов). value_per_point_per_lot = contract*point = 1.0
    # loss_per_lot = 300 * 1.0 = $300 → lots = 100/300 = 0.33
    lots = compute_lots(risk_usd=100, entry=2634.0, sl=2631.0, symbol_info=SI)
    assert abs(lots - 0.33) < 1e-9


def test_budget_below_min_lot_returns_zero():
    # бюджет $1 покрывает лишь 0.0033 лота < min 0.01 → 0.0 (сделки нет),
    # округлять вверх к min нельзя — это пробьёт риск-бюджет.
    lots = compute_lots(risk_usd=1, entry=2634.0, sl=2631.0, symbol_info=SI)
    assert lots == 0.0
