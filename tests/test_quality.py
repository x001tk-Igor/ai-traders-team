import pytest

from trader_lib.config import Model
from trader_lib.quality import (
    breakeven_p,
    costs_R,
    profile_risk_mult,
    required_p_win,
    status_risk_mult,
)

# Совпадает с config/trader.config.json risk.status_risk_mult — держим тесты
# независимыми от файла на диске (как STREAK_CFG в test_streak.py), но
# значения синхронизированы с реальным конфигом.
STATUS_MULT_CFG = {"unknown": 0.2, "изучаю": 0.2, "карантин": 0.1, "подтверждён": 1.0}


def _model_cfg(weak_risk_mult=0.5):
    # Форма совпадает с config/trader.config.json risk.model.profile_rules.
    return Model(
        profile="strong",
        id="test-model",
        profile_rules={
            "weak": {
                "risk_mult": weak_risk_mult,
                "planned_only": True,
                "require_status": "confirmed",
                "force_dry_run": True,
            }
        },
    )


# ---------------------------------------------------------------------------
# costs_R
# ---------------------------------------------------------------------------


def test_costs_R_basic_calculation():
    # costs_usd = (20+5)*1.0*0.1 + 0.5 = 3.0 ; risk_usd = 100*1.0*0.1 = 10.0
    v = costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=100, lots=0.1, value_per_point=1.0)
    assert v == pytest.approx(0.3, abs=1e-9)


def test_costs_gate_rejects_tight_stop():
    # тот же спред/комиссия/проскальзывание, но узкий стоп (10 пунктов вместо
    # 100) — издержки съедают риск непропорционально. costs_R должен выйти
    # далеко за порог cfg.risk.max_costs_R = 0.10 (сам гейт — задача 5.4,
    # здесь только показываем, что число его нарушает).
    v = costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=10, lots=0.1, value_per_point=1.0)
    assert v > 0.10
    assert v == pytest.approx(3.0, abs=1e-9)


def test_costs_R_raises_on_nonpositive_sl_points():
    with pytest.raises(ValueError):
        costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=0, lots=0.1, value_per_point=1.0)
    with pytest.raises(ValueError):
        costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=-10, lots=0.1, value_per_point=1.0)


def test_costs_R_raises_on_nonpositive_lots():
    with pytest.raises(ValueError):
        costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=100, lots=0, value_per_point=1.0)
    with pytest.raises(ValueError):
        costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=100, lots=-0.1, value_per_point=1.0)


def test_costs_R_raises_on_nonpositive_value_per_point():
    with pytest.raises(ValueError):
        costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=100, lots=0.1, value_per_point=0)
    with pytest.raises(ValueError):
        costs_R(spread_points=20, commission_usd=0.5, slippage_points_est=5,
                sl_points=100, lots=0.1, value_per_point=-1.0)


# ---------------------------------------------------------------------------
# breakeven_p / required_p_win
# ---------------------------------------------------------------------------


def test_breakeven_p_reference_values():
    # Эталон из операционной инструкции: rr=1.8, costs_r=0.2.
    be = breakeven_p(rr=1.8, costs_r=0.2)
    assert be == pytest.approx(0.4286, abs=1e-4)

    req = required_p_win(rr=1.8, costs_r=0.2)
    assert req == pytest.approx(0.4786, abs=1e-4)


def test_required_p_win_uses_custom_margin():
    be = breakeven_p(rr=1.8, costs_r=0.2)
    req = required_p_win(rr=1.8, costs_r=0.2, margin=0.10)
    assert req == pytest.approx(be + 0.10, abs=1e-9)


def test_breakeven_p_raises_on_nonpositive_rr():
    with pytest.raises(ValueError):
        breakeven_p(rr=0, costs_r=0.2)
    with pytest.raises(ValueError):
        breakeven_p(rr=-1.0, costs_r=0.2)


def test_required_p_win_not_clipped_above_one():
    # rr маленький, издержки большие — честная арифметика говорит "сделка
    # недостижима", а не подменяет это правдоподобным числом.
    be = breakeven_p(rr=0.5, costs_r=1.0)
    assert be == pytest.approx(1.3333, abs=1e-4)
    req = required_p_win(rr=0.5, costs_r=1.0)
    assert req > 1.0
    assert req == pytest.approx(1.3833, abs=1e-4)


# ---------------------------------------------------------------------------
# status_risk_mult
# ---------------------------------------------------------------------------


def test_status_risk_mult_all_four_known_statuses():
    for status, expected_mult in STATUS_MULT_CFG.items():
        v = status_risk_mult(status, STATUS_MULT_CFG)
        assert v["mult"] == expected_mult, status
        assert v["recognized"] is True, status
        assert status in v["reason"], status


def test_status_risk_mult_unrecognized_status_gets_minimum():
    # Модель выдумала статус ("полу-подтверждён") — не должен упасть, не
    # должен молча стать recognized=True, и не должен совпасть с mult
    # у ключа "unknown" (0.2): fallback обязан быть МИНИМУМОМ по словарю
    # (карантин=0.1), а не синонимом слова "unknown".
    v = status_risk_mult("полу-подтверждён", STATUS_MULT_CFG)
    assert v["mult"] == 0.1
    assert v["mult"] != STATUS_MULT_CFG["unknown"]
    assert v["recognized"] is False
    assert "не распознан" in v["reason"] or "не найден" in v["reason"]
    assert "минимум" in v["reason"] or "минимальный" in v["reason"]
    # reason читает человек при разборе плохого дня — список известных
    # статусов обязан быть человекочитаемой фразой ("a, b, c"), а не
    # python-repr списка (с квадратными скобками и кавычками).
    assert ", ".join(sorted(STATUS_MULT_CFG)) in v["reason"]
    assert "[" not in v["reason"]  # не python-repr списка ("['a', 'b']")


def test_status_risk_mult_reason_mentions_the_bad_value():
    # Проверка содержательности reason: должно быть видно, ЧТО именно не
    # распознано, не только факт "что-то не так".
    v = status_risk_mult("полу-подтверждён", STATUS_MULT_CFG)
    assert "полу-подтверждён" in v["reason"]


# ---------------------------------------------------------------------------
# profile_risk_mult
# ---------------------------------------------------------------------------


def test_weak_profile_halves_risk():
    cfg_model = _model_cfg(weak_risk_mult=0.5)
    v = profile_risk_mult("weak", cfg_model)
    assert v["mult"] == 0.5
    assert v["recognized"] is True
    assert "weak" in v["reason"]


def test_strong_profile_is_full_risk():
    cfg_model = _model_cfg()
    v = profile_risk_mult("strong", cfg_model)
    assert v["mult"] == 1.0
    assert v["recognized"] is True
    assert "strong" in v["reason"]


def test_profile_risk_mult_unrecognized_profile_gets_minimum():
    # "medium" не существует ни как "strong" (1.0), ни в profile_rules
    # (только "weak") — fallback должен быть минимумом из известных (0.5),
    # а не дефолтом 1.0 (что отменило бы весь смысл защиты).
    cfg_model = _model_cfg(weak_risk_mult=0.5)
    v = profile_risk_mult("medium", cfg_model)
    assert v["mult"] == 0.5
    assert v["mult"] != 1.0
    assert v["recognized"] is False
    assert "medium" in v["reason"]
    assert "минимум" in v["reason"] or "минимальный" in v["reason"]
    # тот же человекочитаемый формат списка известных значений, что и у
    # status_risk_mult — не python-repr списка.
    assert "strong, weak" in v["reason"]
    assert "[" not in v["reason"]  # не python-repr списка ("['a', 'b']")


def test_profile_risk_mult_fallback_tracks_configured_weak_value():
    # Если бы fallback был захардкожен как константа, а не реально взят как
    # min() известных множителей, эта проверка поймала бы дрейф: делаем
    # "weak" ещё жёстче (0.2) — минимум известных должен стать 0.2, а не
    # остаться на старом 0.5.
    cfg_model = _model_cfg(weak_risk_mult=0.2)
    v = profile_risk_mult("medium", cfg_model)
    assert v["mult"] == 0.2
    assert v["recognized"] is False
