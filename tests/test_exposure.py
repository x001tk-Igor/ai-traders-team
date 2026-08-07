import pytest

from trader_lib.exposure import net_currency_exposure, open_risk_usd

# XAUUSD: point=0.01, contract=100 → value_per_point_per_lot = 1.0
SI_XAU = {"point": 0.01, "digits": 2, "spread": 20, "trade_contract_size": 100,
          "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}
# EURUSD: point=0.0001, contract=100000 → value_per_point_per_lot = 10.0
SI_EUR = {"point": 0.0001, "digits": 5, "spread": 10, "trade_contract_size": 100000,
          "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}

SI_MAP = {"XAUUSD": SI_XAU, "EURUSD": SI_EUR}


def pos(ticket, symbol, ptype, volume, price_open, sl, tp=0.0):
    return {"ticket": ticket, "symbol": symbol, "type": ptype, "volume": volume,
            "price_open": price_open, "sl": sl, "tp": tp, "price_current": price_open,
            "profit": 0.0, "magic": 770425}


# ---------- open_risk_usd ----------

def test_be_position_risk_zero():
    # SL переведён в безубыток (buy, sl == entry) → риск 0.0, protected=True
    p = pos(1, "XAUUSD", 0, 0.05, 2400.0, 2400.0)
    v = open_risk_usd([p], SI_MAP)
    assert v["per_ticket"][1] == {"risk_usd": 0.0, "protected": True}
    assert v["total_usd"] == 0.0
    assert v["unprotected"] == 0
    assert "риск $0.00" in v["reason"]
    assert "без стопа: 0" in v["reason"]


def test_position_without_sl_unprotected():
    p = pos(2, "XAUUSD", 0, 0.05, 2400.0, 0.0)  # sl=0.0 == нет стопа
    v = open_risk_usd([p], SI_MAP)
    assert v["per_ticket"][2] == {"risk_usd": None, "protected": False}
    assert v["unprotected"] == 1
    assert v["total_usd"] == 0.0  # неизвестный риск НЕ входит в сумму
    assert "1 позиций" in v["reason"]
    assert "без стопа: 1" in v["reason"]


def test_position_without_sl_key_unprotected():
    # ключ sl вовсе отсутствует — тоже считается отсутствием стопа
    p = pos(3, "XAUUSD", 0, 0.05, 2400.0, 0.0)
    del p["sl"]
    v = open_risk_usd([p], SI_MAP)
    assert v["per_ticket"][3] == {"risk_usd": None, "protected": False}
    assert v["unprotected"] == 1


def test_sum_open_risk():
    # XAUUSD buy: distance=10 → 1000 points * 1.0 * 0.05 lots = 50.0
    p1 = pos(10, "XAUUSD", 0, 0.05, 2400.0, 2390.0)
    # EURUSD buy: distance=0.005 → 50 points * 10.0 * 0.1 lots = 50.0
    p2 = pos(11, "EURUSD", 0, 0.1, 1.1000, 1.0950)
    v = open_risk_usd([p1, p2], SI_MAP)
    assert v["per_ticket"][10] == {"risk_usd": 50.0, "protected": True}
    assert v["per_ticket"][11] == {"risk_usd": 50.0, "protected": True}
    assert v["total_usd"] == 100.0
    assert v["unprotected"] == 0
    assert "2 позиций" in v["reason"]
    assert "риск $100.00" in v["reason"]
    assert "без стопа: 0" in v["reason"]


def test_sell_position_risk_correct_sign():
    # XAUUSD sell: distance = sl - entry = 10 → risk 50.0 (риск всегда положительный)
    p = pos(20, "XAUUSD", 1, 0.05, 2400.0, 2410.0)
    v = open_risk_usd([p], SI_MAP)
    assert v["per_ticket"][20] == {"risk_usd": 50.0, "protected": True}
    assert v["total_usd"] == 50.0


def test_open_risk_empty_positions():
    v = open_risk_usd([], SI_MAP)
    assert v == {"total_usd": 0.0, "unprotected": 0, "per_ticket": {},
                 "reason": "0 позиций, риск $0.00, без стопа: 0"}


def test_open_risk_symbol_missing_from_map_raises():
    p = pos(30, "GBPUSD", 0, 0.1, 1.30, 1.29)
    with pytest.raises(KeyError):
        open_risk_usd([p], {})


def test_open_risk_unknown_position_type_raises():
    # только 0 (BUY) и 1 (SELL) — открытая позиция других типов не бывает;
    # неизвестное значение — ошибка вызывающего, должна падать громко
    p = pos(31, "XAUUSD", 7, 0.05, 2400.0, 2390.0)
    with pytest.raises(ValueError):
        open_risk_usd([p], SI_MAP)


# ---------- net_currency_exposure ----------

def test_xau_and_eurusd_long_net_usd_short():
    p1 = pos(10, "XAUUSD", 0, 0.05, 2400.0, 2390.0)  # risk 50.0
    p2 = pos(11, "EURUSD", 0, 0.1, 1.1000, 1.0950)   # risk 50.0
    v = net_currency_exposure([p1, p2], SI_MAP)
    assert v["net"]["XAU"] == pytest.approx(50.0)
    assert v["net"]["EUR"] == pytest.approx(50.0)
    assert v["net"]["USD"] == pytest.approx(-100.0)
    assert v["unprotected"] == 0
    assert "по 3 валютам" in v["reason"]  # XAU, EUR, USD
    assert "без стопа: 0" in v["reason"]


def test_hedged_pair_nets_out():
    p_buy = pos(40, "XAUUSD", 0, 0.05, 2400.0, 2390.0)   # risk 50.0, XAU+/USD-
    p_sell = pos(41, "XAUUSD", 1, 0.05, 2400.0, 2410.0)  # risk 50.0, XAU-/USD+
    v = net_currency_exposure([p_buy, p_sell], SI_MAP)
    assert abs(v["net"].get("XAU", 0.0)) < 0.01
    assert abs(v["net"].get("USD", 0.0)) < 0.01


def test_net_sell_signs_mirror_buy():
    p = pos(50, "EURUSD", 1, 0.1, 1.1000, 1.1050)  # sell, risk 50.0
    v = net_currency_exposure([p], SI_MAP)
    assert v["net"]["EUR"] == pytest.approx(-50.0)
    assert v["net"]["USD"] == pytest.approx(50.0)


def test_net_excludes_unprotected_but_counts_them():
    p_protected = pos(60, "XAUUSD", 0, 0.05, 2400.0, 2390.0)  # risk 50.0
    p_naked = pos(61, "EURUSD", 0, 0.1, 1.1000, 0.0)  # no sl
    v = net_currency_exposure([p_protected, p_naked], SI_MAP)
    assert v["unprotected"] == 1
    assert "EUR" not in v["net"]
    assert v["net"]["XAU"] == pytest.approx(50.0)


def test_net_empty_positions():
    v = net_currency_exposure([], SI_MAP)
    assert v == {"net": {}, "unprotected": 0,
                 "reason": "нетто по 0 валютам, без стопа: 0"}


def test_net_unparseable_symbol_raises():
    si_map = dict(SI_MAP, **{"XAUUSD.m": SI_XAU})
    p = pos(70, "XAUUSD.m", 0, 0.05, 2400.0, 2390.0)
    with pytest.raises(ValueError):
        net_currency_exposure([p], si_map)


def test_net_currency_exposure_rejects_non_usd_account():
    # тихая неверная конвертация хуже исключения — account_currency, для
    # которого нет кросс-курса, должен падать громко, а не молча выдавать
    # те же USD-числа под чужим ярлыком
    p = pos(80, "EURUSD", 0, 0.1, 1.1000, 1.0950)
    with pytest.raises(ValueError):
        net_currency_exposure([p], SI_MAP, account_currency="EUR")
