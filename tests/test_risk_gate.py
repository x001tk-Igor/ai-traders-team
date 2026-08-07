import datetime as dt
from types import SimpleNamespace

import pytest

from trader_lib.config import load_config
from trader_lib.risk_gate import (
    BLOCKED_CODES,
    TERM_KEYS,
    blocked_response,
    evaluate_gate,
    safe_evaluate_gate,
)

BASE = dict(daily_limit_pct=3, total_limit_pct=6, flatten_buffer_pct=0.3, K=3, cap_pct=1.0)

# test_ok_budget удалён: строгое подмножество test_backward_compat_old_kwargs
# (тот же вызов, те же два ассерта плюс проверка всех новых полей).


def test_throttle_near_wall():
    # equity 9750 → дневной убыток 2.5% (в зоне [2.0,2.7) → THROTTLE, не FORCE_FLAT)
    v = evaluate_gate(equity=9750, day_start_equity=10000, initial_balance=10000, **BASE)
    assert v["verdict"] == "THROTTLE"


def test_force_flat_on_buffer():
    # дневной убыток 2.7% == стена(3%)-буфер(0.3%) → FORCE_FLAT
    v = evaluate_gate(equity=9730, day_start_equity=10000, initial_balance=10000, **BASE)
    assert v["verdict"] == "FORCE_FLAT"


def test_total_wall_dominates():
    # общий убыток 5.7% == 6%-0.3% → FORCE_FLAT (даже при малом дневном)
    v = evaluate_gate(equity=9430, day_start_equity=9500, initial_balance=10000, **BASE)
    assert v["verdict"] == "FORCE_FLAT"


def test_fail_closed_on_error():
    # некорректные аргументы → HALT_NEW, никогда не fail-open
    v = safe_evaluate_gate(equity=None, day_start_equity=10000, initial_balance=10000, **BASE)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_loss_sequence_never_breaches_daily():
    # Каждая сделка рискует max_risk от гейта и ВСЯ проигрывает.
    equity = 10000.0
    day_start = 10000.0
    for _ in range(200):
        v = evaluate_gate(equity=equity, day_start_equity=day_start,
                          initial_balance=10000, **BASE)
        if v["verdict"] in ("FORCE_FLAT", "HALT_NEW"):
            break
        equity -= v["max_risk_per_trade_usd"]  # полный стоп
    assert (day_start - equity) / day_start * 100 < 3.0


# ======================= v2: новый контракт (limits) =======================

# Зеркало блока risk из config/trader.config.json (только ключи, нужные гейту).
# ОТЛИЧИЕ от реального конфига: добавлена ступень total 5.0% → 0.25. В конфиге
# все ступени имеют risk_mult 0.5, поэтому min ≡ max ≡ 0.5 и решение «берём
# минимальный множитель» было бы непроверяемым (мутант min→max выжил бы).
# Совместимость имён ключей с настоящим cfg.risk проверяет отдельный тест.
LIMITS = {
    "daily_loss_limit_pct": 3.0,
    "total_loss_limit_pct": 6.0,
    "flatten_buffer_pct": 0.3,
    "risk_budget_divisor_K": 3,
    "per_trade_risk_cap_pct": 0.5,
    "max_open_positions": 3,
    "max_open_risk_pct": 1.5,
    "max_new_trades_per_day": 10,
    "max_unplanned_trades_per_day": 1,
    "near_wall_pct": 1.0,
    "near_wall_mult": 0.5,
    "ladder": {
        "daily": [{"loss_pct": 1.5, "risk_mult": 0.5, "require_status": "confirmed"}],
        "total": [{"loss_pct": 3.0, "risk_mult": 0.5},
                  {"loss_pct": 4.5, "risk_mult": 0.5, "require_status": "confirmed"},
                  {"loss_pct": 5.0, "risk_mult": 0.25}],
    },
}

NOW = dt.datetime(2026, 7, 25, 10, 0, tzinfo=dt.timezone.utc)


def gate(**kw):
    """Гейт на новом контракте: депозит 10 000, без убытка, лимиты из конфига."""
    args = dict(equity=10000.0, day_start_equity=10000.0, initial_balance=10000.0,
                limits=LIMITS, now=NOW)
    args.update(kw)
    return evaluate_gate(**args)


def test_new_contract_ok_reports_all_budget_fields():
    v = gate()
    assert v["verdict"] == "OK"
    # min(бюджет/K = 300/3 = 100, кап 0.5% = 50, остаток открытого риска 1.5% = 150)
    assert v["max_risk_per_trade_usd"] == 50.0
    assert v["daily_loss_pct"] == 0.0
    assert v["total_loss_pct"] == 0.0
    assert v["daily_budget"] == 300.0
    assert v["total_budget"] == 600.0
    assert v["budget"] == 300.0
    assert v["open_risk_usd"] == 0.0
    assert v["open_risk_room_usd"] == 150.0
    assert v["require_setup_status"] == "any"
    assert v["planned_only"] is False
    assert v["unplanned_allowed"] is True
    assert v["risk_mult_applied"] == 1.0
    assert v["limits_source"] == "limits"
    # машинночитаемые коды решения (задача 2.2 сегментирует статистику по ним)
    assert v["blocked_by"] is None
    # с кэпом 0.5% связывает именно он (50 < 100): при прошлой конституции
    # кэп и бюджет/K совпадали на 100, и выбор связывающего терма был неразличим
    assert v["binding_term"] == "per_trade_cap"
    assert v["terms"] == {"budget_div_k": 100.0, "per_trade_cap": 50.0,
                          "open_risk_room": 150.0, "budget_minus_open_risk": 300.0}
    assert v["daily_risk_remaining_usd"] == 300.0
    # ни одного сработавшего ограничения — только строка-расшифровка выданного риска
    assert len(v["reasons"]) == 1
    assert v["reasons"][0].startswith(
        "риск $50.00 = связывающий терм «кап 0.5% equity» ($50.00) × общий множитель ×1.0")
    assert "бюджет/K $100.00" in v["reasons"][0]
    assert "остаток открытого риска $150.00" in v["reasons"][0]
    assert "бюджет − открытый риск $300.00" in v["reasons"][0]


def test_decision_codes_are_from_published_vocabulary():
    # blocked_by/binding_term — контракт для аналитики 2.2, а не свободный текст
    for v in (gate(), gate(equity=9730.0), gate(halt_rest_of_day=True),
              gate(positions_count=3), gate(trades_today=10), gate(unprotected_positions=1),
              gate(open_risk_usd=200.0), gate(paused_until=NOW + dt.timedelta(minutes=5)),
              gate(loss_streak_mult=0.0001, profile_mult=0.0001),
              safe_evaluate_gate(equity=None, day_start_equity=1, initial_balance=1,
                                 limits=LIMITS, now=NOW)):
        assert v["blocked_by"] is None or v["blocked_by"] in BLOCKED_CODES, v["blocked_by"]
        assert v["binding_term"] is None or v["binding_term"] in TERM_KEYS
        assert v["terms"] is None or set(v["terms"]) == set(TERM_KEYS)
    # коды конкретных запретов
    assert gate(equity=9730.0)["blocked_by"] == "wall_daily"
    assert gate(equity=9430.0, day_start_equity=9500.0)["blocked_by"] == "wall_total"
    assert gate(halt_rest_of_day=True)["blocked_by"] == "halt_rest_of_day"
    assert gate(paused_until=NOW + dt.timedelta(minutes=5))["blocked_by"] == "paused"
    assert gate(unprotected_positions=1)["blocked_by"] == "unprotected_positions"
    assert gate(positions_count=3)["blocked_by"] == "max_open_positions"
    assert gate(trades_today=10)["blocked_by"] == "max_new_trades"
    assert gate(open_risk_usd=200.0)["blocked_by"] == "open_risk_exhausted"
    assert gate(loss_streak_mult=0.0001,
                profile_mult=0.0001)["blocked_by"] == "risk_rounds_to_zero"
    assert safe_evaluate_gate(equity=None, day_start_equity=1, initial_balance=1,
                              limits=LIMITS, now=NOW)["blocked_by"] == "gate_error"


def test_daily_risk_remaining_is_daily_not_min_of_walls():
    """daily_risk_remaining_usd — то, что DECISION_REQUIRED в 2.1 называет
    остатком дневного риска: daily_budget − открытый риск. В формуле участвует
    terms['budget_minus_open_risk'] — то же за вычетом ОБЩЕЙ стены. Берём случай,
    где все три числа разные, иначе 2.1 не отличит их друг от друга.
    """
    v = gate(equity=9550.0, day_start_equity=9600.0, open_risk_usd=50.0)
    assert v["verdict"] == "OK"
    assert v["daily_budget"] == 238.0            # (3% − 0.52%) × 9600
    assert v["total_budget"] == 150.0            # (6% − 4.5%) × 10000
    assert v["budget"] == 150.0                  # min(дневного, общего)
    assert v["daily_risk_remaining_usd"] == 188.0            # 238 − 50, ДНЕВНАЯ стена
    assert v["terms"]["budget_minus_open_risk"] == 100.0     # 150 − 50, обе стены
    assert v["open_risk_room_usd"] == 93.25                  # 1.5% × 9550 − 50


def test_limits_accepts_cfg_risk_object():
    # limits можно передать как cfg.risk (dataclass), не только как dict —
    # заодно ловит расхождение имён ключей гейта и конфига
    cfg = load_config("config/trader.config.json")
    v = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=cfg.risk, now=NOW)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 50.0
    assert v["limits_source"] == "limits"


def test_fixture_limits_match_real_config_by_value():
    """Фикстура LIMITS обязана совпадать с конституцией ПО ЗНАЧЕНИЯМ, а не только
    по именам ключей: если владелец счёта потюнит config/trader.config.json, молча
    разошедшаяся фикстура оставит суиту зелёной, а арифметические комментарии
    в этом файле начнут врать. Проверяем ровно то, что заявлено в комментарии
    к LIMITS: всё совпадает, ladder отличается ровно одной лишней ступенью.
    """
    real = load_config("config/trader.config.json").risk
    for key, value in LIMITS.items():
        if key == "ladder":
            continue
        assert value == getattr(real, key), f"LIMITS[{key!r}] разошёлся с конфигом"
    assert LIMITS["ladder"]["daily"] == real.ladder["daily"]
    extra = [r for r in LIMITS["ladder"]["total"] if r not in real.ladder["total"]]
    assert extra == [{"loss_pct": 5.0, "risk_mult": 0.25}]
    assert [r for r in real.ladder["total"] if r not in LIMITS["ladder"]["total"]] == []


def test_open_risk_reduces_new_allocation():
    """Остаток открытого риска обязан СВЯЗЫВАТЬ, а не просто присутствовать.

    Сценарий подобран под конституцию с кэпом 0.5% ($50): при открытом риске
    $100 остаток тоже равен 50, терм совпал бы с кэпом, и тест не отличал бы
    работающий терм от неработающего. Берём $120 — остаток 30 < кэпа 50, и
    связывает именно он.
    """
    v = gate(open_risk_usd=120.0)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 30.0
    assert v["open_risk_usd"] == 120.0
    assert v["open_risk_room_usd"] == 30.0
    assert v["binding_term"] == "open_risk_room"
    assert any("остаток открытого риска" in r and "$30.00" in r for r in v["reasons"])


def test_open_risk_exhausted_halts_and_never_negative():
    # открытый риск $200 больше лимита $150 → остаток отрицательный,
    # но выданный риск обязан быть 0.0, а вердикт — HALT_NEW
    v = gate(open_risk_usd=200.0)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert v["open_risk_room_usd"] == -50.0
    assert any("открытый риск" in r and "исчерпал" in r for r in v["reasons"])
    # решение принято на шаге 7 → термы формулы в ответе есть
    assert v["binding_term"] == "open_risk_room"
    assert v["terms"]["open_risk_room"] == -50.0
    assert v["terms"]["budget_minus_open_risk"] == 100.0


def test_budget_minus_open_risk_binds_when_open_risk_limit_is_loose():
    # max_open_risk_pct=5% ШИРЕ дневного лимита 3%: терм остатка открытого риска
    # (500−250=250) уже не защищает бюджет, и суммарный риск пробил бы дневную
    # стену. Держит четвёртый терм: бюджет 300 − открытый риск 250 = 50.
    limits = dict(LIMITS, max_open_risk_pct=5.0)
    v = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=limits, open_risk_usd=250.0, now=NOW)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 50.0
    assert any("бюджет − открытый риск" in r and "$50.00" in r for r in v["reasons"])
    # весь бюджет уже в открытом риске → новых входов нет
    exhausted = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                              limits=limits, open_risk_usd=300.0, now=NOW)
    assert exhausted["verdict"] == "HALT_NEW"
    assert exhausted["max_risk_per_trade_usd"] == 0.0
    assert exhausted["blocked_by"] == "no_risk_left"
    assert exhausted["binding_term"] == "budget_minus_open_risk"
    assert any("риска для новой сделки не осталось" in r and "бюджет − открытый риск" in r
               for r in exhausted["reasons"])


def test_allocation_floors_to_cent_never_above_binding_term():
    # остаток бюджета $0.035: round дал бы $0.04 и суммарный открытый риск
    # $300.005 > бюджета $300.00. Округление вверх в риск-коде запрещено
    # независимо от величины (как math.floor к шагу лота в size_position.py)
    limits = dict(LIMITS, max_open_risk_pct=5.0)  # чтобы связывал именно бюджет
    v = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=limits, open_risk_usd=299.965, now=NOW)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 0.03
    assert 299.965 + v["max_risk_per_trade_usd"] <= v["budget"]
    # то же на остатке ЛИМИТА ОТКРЫТОГО РИСКА: 150 − 149.965 = 0.035 → 0.03
    room = gate(open_risk_usd=149.965)
    assert room["max_risk_per_trade_usd"] == 0.03
    assert 149.965 + room["max_risk_per_trade_usd"] <= 1.5 / 100 * 10000


def test_negative_inputs_fail_loud():
    # счётчики и суммы риска приходят из кода (exposure.py, журнал), как и
    # множители: отрицательное значение расширяет бюджет и отодвигает лимиты
    for extra in ({"open_risk_usd": -5000.0}, {"positions_count": -1},
                  {"trades_today": -1}, {"unplanned_today": -1},
                  {"unprotected_positions": -1}):
        with pytest.raises(ValueError):
            gate(**extra)
        v = safe_evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                               limits=LIMITS, now=NOW, **extra)
        assert v["verdict"] == "HALT_NEW", extra
        assert v["max_risk_per_trade_usd"] == 0.0
    # ноль — нормальный вход, не «отрицательный»
    assert gate(open_risk_usd=0.0, positions_count=0, trades_today=0,
                unplanned_today=0, unprotected_positions=0)["verdict"] == "OK"


def test_positions_limit_halts():
    assert gate(positions_count=2)["verdict"] == "OK"  # граница: 2 из 3 ещё можно
    v = gate(positions_count=3)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert any("открыто позиций 3 из 3" in r for r in v["reasons"])


def test_attempts_budget_halts():
    assert gate(trades_today=9)["verdict"] == "OK"  # граница: 9 из 10 ещё можно
    v = gate(trades_today=10)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert any("попыток за день 10 из 10" in r for r in v["reasons"])


def test_unprotected_position_halts_new():
    # позиция без стопа = неограниченный риск, который нельзя выразить числом
    v = gate(unprotected_positions=1)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert any("без стоп-лосса" in r for r in v["reasons"])


def test_halt_rest_of_day_blocks():
    v = gate(halt_rest_of_day=True)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert any("до конца дня" in r for r in v["reasons"])


def test_blocked_verdict_reports_maximally_restrictive_fields():
    # вызывающий, который прочитает поля и проигнорирует verdict, не должен
    # собрать из них разрешение (fail-closed)
    for v in (gate(halt_rest_of_day=True), gate(unprotected_positions=1),
              evaluate_gate(equity=9730, day_start_equity=10000, initial_balance=10000,
                            limits=LIMITS, now=NOW)):
        assert v["verdict"] in ("HALT_NEW", "FORCE_FLAT")
        assert v["max_risk_per_trade_usd"] == 0.0
        assert v["require_setup_status"] == "confirmed"
        assert v["planned_only"] is True
        assert v["unplanned_allowed"] is False
        assert v["risk_mult_applied"] == 0.0


def test_force_flat_wins_over_halt_checks():
    # порядок проверок: стены (шаг 1) решают раньше каскада (шаг 2)
    v = gate(equity=9730.0, halt_rest_of_day=True, unprotected_positions=2,
             positions_count=9, trades_today=9)
    assert v["verdict"] == "FORCE_FLAT"
    assert any("стена" in r for r in v["reasons"])


def test_ladder_daily_15_halves_and_requires_confirmed():
    # дневной убыток ровно 1.5% → ступень daily: риск ×0.5 + только confirmed
    v = gate(equity=9850.0)
    assert v["verdict"] == "OK"  # до стены далеко, near-wall не сработал
    assert v["daily_loss_pct"] == 1.5
    # бюджет = (3−1.5)%×10000 = 150 → /3 = 50; кэп 0.5%×9850 = 49.25 — он меньше
    # и связывает; ×0.5 = 24.625, вниз до цента = 24.62
    assert v["max_risk_per_trade_usd"] == 24.62
    assert v["binding_term"] == "per_trade_cap"
    assert v["risk_mult_applied"] == 0.5
    assert v["require_setup_status"] == "confirmed"
    assert any("ступень daily" in r and "×0.5" in r and "confirmed" in r for r in v["reasons"])
    # на 1.49% ступень ещё не действует
    below = gate(equity=9851.0)
    assert below["risk_mult_applied"] == 1.0
    assert below["require_setup_status"] == "any"


def test_ladder_total_45_requires_confirmed():
    # общий убыток 4.5% при малом дневном → ступени total (3.0 и 4.5)
    v = gate(equity=9550.0, day_start_equity=9600.0)
    assert v["verdict"] == "OK"
    assert v["total_loss_pct"] == 4.5
    # бюджет = min(daily 238.0, total (6−4.5)%×10000 = 150) = 150 → /3 = 50;
    # кэп 0.5%×9550 = 47.75 меньше и связывает → ×0.5 = 23.875, вниз = 23.87
    assert v["max_risk_per_trade_usd"] == 23.87
    assert v["risk_mult_applied"] == 0.5
    assert v["require_setup_status"] == "confirmed"
    assert any("ступень total" in r and "4.5%" in r and "confirmed" in r for r in v["reasons"])


def test_both_ladders_active_takes_min_mult():
    # активны и daily (1.55%), и обе total (3.0/4.5): множители НЕ перемножаются
    # между ступенями — берётся минимальный (см. _active_ladder)
    v = gate(equity=9550.0, day_start_equity=9700.0)
    assert v["verdict"] == "OK"
    assert v["risk_mult_applied"] == 0.5  # не 0.5**3 = 0.125
    # бюджет = min(daily 3%*9700-150 = 141, total 150) = 141 → /3 = 47 → ×0.5 = 23.5
    assert v["max_risk_per_trade_usd"] == 23.5
    assert sum("ступень" in r for r in v["reasons"]) == 3


def test_ladder_takes_smallest_mult_not_largest():
    # общий убыток 5.0% → активны ступени total 3.0 (×0.5), 4.5 (×0.5) и
    # 5.0 (×0.25). Применяется НАИМЕНЬШИЙ: 0.25 (не 0.5 и не произведение)
    v = gate(equity=9500.0, day_start_equity=9550.0)
    assert v["verdict"] == "OK"
    assert v["total_loss_pct"] == 5.0
    assert v["risk_mult_applied"] == 0.25  # max дал бы 0.5, произведение 0.0625
    # бюджет = min(daily 236.5, total (6-5)%*10000 = 100) = 100 → /3 = 33.33 → ×0.25
    assert v["max_risk_per_trade_usd"] == 8.33
    assert v["require_setup_status"] == "confirmed"  # требует ступень 4.5
    assert sum("ступень total" in r for r in v["reasons"]) == 3


def test_streak_and_profile_mults_compose():
    # серия убытков и профиль модели перемножаются друг с другом
    v = gate(loss_streak_mult=0.5, profile_mult=0.5)
    assert v["verdict"] == "OK"
    assert v["risk_mult_applied"] == 0.25
    assert v["max_risk_per_trade_usd"] == 12.5  # кэп 50 × 0.5 × 0.5
    # …и со ступенью ladder: кэп 49.25 × 0.5(ladder) × 0.5(серия) × 0.5(профиль)
    with_ladder = gate(equity=9850.0, loss_streak_mult=0.5, profile_mult=0.5)
    assert with_ladder["risk_mult_applied"] == 0.125
    assert with_ladder["max_risk_per_trade_usd"] == 6.15


def test_mult_above_one_fails_loud():
    # множители — только деэскалация; >1 подняло бы риск выше лимитов
    with pytest.raises(ValueError):
        gate(profile_mult=1.5)
    with pytest.raises(ValueError):
        gate(loss_streak_mult=-0.1)
    assert safe_evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                              limits=LIMITS, profile_mult=1.5)["verdict"] == "HALT_NEW"


def test_paused_until_blocks():
    active = (NOW + dt.timedelta(minutes=30)).isoformat()
    v = gate(paused_until=active)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert any("пауза" in r and active in r for r in v["reasons"])
    # datetime вместо строки — тоже принимается
    assert gate(paused_until=NOW + dt.timedelta(minutes=30))["verdict"] == "HALT_NEW"
    # истёкшая пауза не блокирует
    expired = gate(paused_until=(NOW - dt.timedelta(minutes=1)).isoformat())
    assert expired["verdict"] == "OK"
    assert expired["max_risk_per_trade_usd"] == 50.0


def test_paused_until_naive_fails_closed():
    # без таймзоны нельзя сравнить с now → падаем громко, safe-обёртка даёт HALT_NEW
    naive = dt.datetime(2026, 7, 25, 10, 30)
    for bad in (naive, "не дата"):
        with pytest.raises(ValueError):
            gate(paused_until=bad)
        v = safe_evaluate_gate(equity=10000.0, day_start_equity=10000.0, initial_balance=10000.0,
                               limits=LIMITS, now=NOW, paused_until=bad)
        assert v["verdict"] == "HALT_NEW"  # fail-CLOSED, а не только fail-loud
        assert v["max_risk_per_trade_usd"] == 0.0


def test_unplanned_budget_sets_planned_only_but_allows_trading():
    # исчерпанный лимит внеплановых НЕ останавливает торговлю целиком —
    # он лишь требует, чтобы вход был из плана дня
    v = gate(unplanned_today=1)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 50.0  # риск не урезан
    assert v["unplanned_allowed"] is False
    assert v["planned_only"] is True
    assert any("внеплановые" in r and "1 из 1" in r for r in v["reasons"])
    # лимит не исчерпан → внеплановые разрешены
    free = gate(unplanned_today=0)
    assert free["unplanned_allowed"] is True
    assert free["planned_only"] is False


def test_near_wall_throttle_halves_risk():
    # дневной убыток 2.5%: до стены 0.5% (<1%) → THROTTLE, риск ×0.5 сверх ступени
    v = gate(equity=9750.0)
    assert v["verdict"] == "THROTTLE"
    # бюджет = (3-2.5)%*10000 = 50 → /3 = 16.667 → ×0.5(ladder daily) ×0.5(near-wall)
    # = 4.1667 → вниз до копейки 4.16 (round дал бы 4.17 — вверх нельзя)
    assert v["max_risk_per_trade_usd"] == 4.16
    assert v["risk_mult_applied"] == 0.25
    assert any("близко к стене" in r for r in v["reasons"])


def test_budget_exhausted_halts():
    # Шаг 5 (бюджет <= 0) — страховочная сеть: при положительном буфере стена
    # (шаг 1) срабатывает раньше, чем бюджет успевает уйти в 0. Достать шаг 5
    # можно только конфигом, где FORCE_FLAT наступает ПОЗЖЕ стены (буфер < 0).
    limits = dict(LIMITS, flatten_buffer_pct=-1.0)  # FORCE_FLAT только с 4.0%
    v = evaluate_gate(equity=9650, day_start_equity=10000, initial_balance=10000,
                      limits=limits, now=NOW)  # дневной убыток 3.5% → бюджет −50 → 0
    assert v["verdict"] == "HALT_NEW"
    assert v["budget"] == 0.0
    assert v["daily_budget"] == -50.0
    assert any("бюджет" in r and "исчерпан" in r for r in v["reasons"])


def test_backward_compat_old_kwargs():
    # старая форма продолжает работать: новые лимиты не заданы → не действуют
    v = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000, **BASE)
    assert v["verdict"] == "OK"
    assert v["max_risk_per_trade_usd"] == 100.0
    assert v["limits_source"] == "legacy_kwargs"
    assert v["require_setup_status"] == "any"
    assert v["planned_only"] is False
    assert v["unplanned_allowed"] is True
    assert v["open_risk_room_usd"] is None  # лимита открытого риска нет
    assert v["risk_mult_applied"] == 1.0
    # факт использования устаревшей формы виден в журнале
    assert any("устаревшая форма лимитов" in r for r in v["reasons"])


def test_legacy_form_rejects_v2_inputs():
    """Частично мигрировавший вызывающий («новые входы уже передаю, limits ещё
    нет») не должен получать разрешение с полным риском и без новых проверок.
    До появления этих kwargs такой вызов падал TypeError → HALT_NEW; поведение
    сохранено, но с внятным сообщением."""
    for extra in ({"open_risk_usd": 5000.0}, {"positions_count": 99},
                  {"trades_today": 99}, {"unplanned_today": 5}):
        with pytest.raises(ValueError):
            evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                          **extra, **BASE)
        v = safe_evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                               **extra, **BASE)
        assert v["verdict"] == "HALT_NEW", extra
        assert v["max_risk_per_trade_usd"] == 0.0
    # входы, которые проверяются БЕЗ лимитов из конфига, в старой форме работают
    assert evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                         unprotected_positions=1, **BASE)["verdict"] == "HALT_NEW"
    assert evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                         halt_rest_of_day=True, **BASE)["verdict"] == "HALT_NEW"


def test_mixing_limits_and_old_kwargs_fails_loud():
    # две формы лимитов одновременно — неоднозначность, а не «одна победит молча»
    with pytest.raises(ValueError):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=LIMITS, daily_limit_pct=3)


def test_missing_limit_key_fails_loud():
    # опечатка/пропуск ключа лимита обязан падать, а не подменяться дефолтом
    # сообщение обязано объяснять себя: голый KeyError('max_open_risk_pct') не
    # говорит ни где искать, ни что это лимит из блока risk
    broken = {k: v for k, v in LIMITS.items() if k != "max_open_risk_pct"}
    with pytest.raises(KeyError, match="в limits нет лимита 'max_open_risk_pct'"):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=broken, now=NOW)


def test_incomplete_old_kwargs_fails_loud():
    with pytest.raises(ValueError):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      daily_limit_pct=3, total_limit_pct=6)


def test_bad_ladder_config_fails_loud():
    # незнакомый ключ ступени (опечатка require_statuss) тихо ослабил бы требование
    typo = dict(LIMITS, ladder={"daily": [{"loss_pct": 1.0, "risk_mult": 0.5,
                                           "require_statuss": "confirmed"}], "total": []})
    with pytest.raises(ValueError):
        evaluate_gate(equity=9800, day_start_equity=10000, initial_balance=10000,
                      limits=typo, now=NOW)
    # неизвестное значение require_status
    bad_value = dict(LIMITS, ladder={"daily": [{"loss_pct": 1.0, "risk_mult": 0.5,
                                                "require_status": "почти"}], "total": []})
    with pytest.raises(ValueError):
        evaluate_gate(equity=9800, day_start_equity=10000, initial_balance=10000,
                      limits=bad_value, now=NOW)
    # незнакомая ось ladder
    bad_axis = dict(LIMITS, ladder={"daily": [], "total": [], "weekly": []})
    with pytest.raises(ValueError):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=bad_axis, now=NOW)
    # отсутствующая ось: обе обязательны, отключённая — это пустой список
    with pytest.raises(ValueError, match="total"):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=dict(LIMITS, ladder={"daily": []}), now=NOW)


def test_ladder_risk_mult_above_one_fails_loud():
    # опечатка в конституции (risk_mult 5.0) поднимала бы выданный риск ВЫШЕ
    # кэпа per_trade_risk_cap_pct — последняя лазейка мимо «только деэскалация»
    loud = dict(LIMITS, ladder={"daily": [{"loss_pct": 1.0, "risk_mult": 5.0}], "total": []})
    with pytest.raises(ValueError, match="risk_mult"):
        evaluate_gate(equity=9850, day_start_equity=10000, initial_balance=10000,
                      limits=loud, now=NOW)
    v = safe_evaluate_gate(equity=9850, day_start_equity=10000, initial_balance=10000,
                           limits=loud, now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_broken_ladder_rung_fails_loud_before_it_triggers():
    """Ленивая валидация означала бы, что битый конфиг живёт неделями и падает
    ровно в тот момент, когда убыток дошёл до этой глубины. Ступени ниже
    текущего убытка (НЕ сработавшие) обязаны падать так же громко."""
    at_no_loss = dict(equity=10000, day_start_equity=10000, initial_balance=10000, now=NOW)
    broken_rungs = (
        {"loss_pct": 2.5, "risk_mult": 0.5, "require_status": "почти"},  # битое значение
        {"loss_pct": 2.5},                                              # нет risk_mult
        {"loss_pct": 2.5, "risk_mult": 5.0},                            # множитель > 1
        {"risk_mult": 0.5},                                             # нет loss_pct
    )
    for rung in broken_rungs:
        limits = dict(LIMITS, ladder={"daily": [rung], "total": []})
        with pytest.raises(ValueError):
            evaluate_gate(limits=limits, **at_no_loss)  # убыток 0% — ступень не сработала
    # исправная ступень, которая не сработала, вердикт не меняет
    ok = dict(LIMITS, ladder={"daily": [{"loss_pct": 2.5, "risk_mult": 0.5}], "total": []})
    v = evaluate_gate(limits=ok, **at_no_loss)
    assert v["verdict"] == "OK"
    assert v["risk_mult_applied"] == 1.0


def test_near_wall_rule_comes_from_config():
    # правило «×0.5 у стены» — риск-параметр, а не константа в коде: аудитор
    # конституции должен видеть его в cfg.risk и уметь изменить
    wide = dict(LIMITS, near_wall_pct=3.5, near_wall_mult=0.25)
    v = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=wide, now=NOW)  # порог шире всего лимита → ловит и нулевой убыток
    assert v["verdict"] == "THROTTLE"
    assert v["max_risk_per_trade_usd"] == 12.5  # кэп 50 × 0.25
    assert v["risk_mult_applied"] == 0.25
    assert any("×0.25" in r for r in v["reasons"])
    # порог 0 отключает правило целиком
    off = dict(LIMITS, near_wall_pct=0.0)
    assert evaluate_gate(equity=9750, day_start_equity=10000, initial_balance=10000,
                         limits=off, now=NOW)["verdict"] == "OK"
    # near_wall_mult — тоже множитель риска: > 1 недопустим
    with pytest.raises(ValueError, match="near_wall_mult"):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=dict(LIMITS, near_wall_mult=2.0), now=NOW)


def test_blocked_response_factory_is_reusable_and_complete():
    # вызывающие (CLI, entry_gate) отказывают ещё до гейта — им нужен готовый
    # ответ полной схемы, а не словарь из трёх ключей, собранный руками
    manual = blocked_response(error="market unavailable")
    assert set(manual) - {"error"} == set(gate(halt_rest_of_day=True))
    assert manual["verdict"] == "HALT_NEW"
    assert manual["blocked_by"] == "gate_error"
    assert manual["max_risk_per_trade_usd"] == 0.0
    assert manual["require_setup_status"] == "confirmed"
    assert manual["planned_only"] is True
    assert manual["error"] == "market unavailable"
    # своя формулировка и свой код причины
    custom = blocked_response(reason="устаревший heartbeat датчика", blocked_by="gate_error")
    assert custom["reasons"] == ["устаревший heartbeat датчика"]
    assert "error" not in custom  # причина не исключение — ключа error нет


def test_error_response_has_same_schema_as_blocked_verdict():
    # журнал (2.1) пишет полную схему решения; на неполном аварийном ответе
    # v.get("require_setup_status", "any") дал бы разрешительное значение,
    # а v["planned_only"] бросил бы KeyError
    err = safe_evaluate_gate(equity=None, day_start_equity=10000, initial_balance=10000,
                             limits=LIMITS, now=NOW)
    blocked = gate(halt_rest_of_day=True)
    assert set(err) - {"error"} == set(blocked)
    assert err["verdict"] == "HALT_NEW"
    assert err["max_risk_per_trade_usd"] == 0.0
    assert err["require_setup_status"] == "confirmed"
    assert err["planned_only"] is True
    assert err["unplanned_allowed"] is False
    assert err["risk_mult_applied"] == 0.0
    assert err["daily_loss_pct"] is None  # посчитать нечем, но ключ есть
    assert err["limits_source"] is None
    assert err["error"]
    assert any("ошибка гейта" in r for r in err["reasons"])


def test_force_flat_not_lost_to_argument_validation():
    # падение = HALT_NEW, а HALT_NEW не закрывает открытые позиции: при пробитой
    # стене вердикт «закрыть всё» важнее придирок к аргументам
    assert gate(equity=9730.0, profile_mult=1.5)["verdict"] == "FORCE_FLAT"
    assert gate(equity=9730.0, loss_streak_mult=-1.0)["verdict"] == "FORCE_FLAT"
    bad_k = dict(LIMITS, risk_budget_divisor_K=0)
    assert evaluate_gate(equity=9730.0, day_start_equity=10000, initial_balance=10000,
                         limits=bad_k, now=NOW)["verdict"] == "FORCE_FLAT"
    assert evaluate_gate(equity=9730.0, day_start_equity=10000, initial_balance=10000,
                         open_risk_usd=5000.0, **BASE)["verdict"] == "FORCE_FLAT"
    assert gate(equity=9730.0, paused_until="не дата")["verdict"] == "FORCE_FLAT"
    assert gate(equity=9730.0, open_risk_usd=-5000.0)["verdict"] == "FORCE_FLAT"


def test_zero_divisor_fails_loud():
    bad_k = dict(LIMITS, risk_budget_divisor_K=0)
    with pytest.raises(ValueError):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=bad_k, now=NOW)
    assert safe_evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                              limits=bad_k, now=NOW)["verdict"] == "HALT_NEW"


def test_limits_object_missing_attribute_fails_loud():
    # объект-лимиты (как cfg.risk) без нужного поля падает так же громко, как dict
    incomplete = SimpleNamespace(**{k: v for k, v in LIMITS.items() if k != "ladder"})
    with pytest.raises(KeyError, match="в limits нет лимита 'ladder'"):
        evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                      limits=incomplete, now=NOW)


def test_near_wall_on_total_axis_throttles():
    # дневной убыток мал (0.21%), но до ОБЩЕЙ стены 0.8% (<1%) → THROTTLE
    v = gate(equity=9480.0, day_start_equity=9500.0)
    assert v["verdict"] == "THROTTLE"
    assert v["total_loss_pct"] == 5.2
    # бюджет = min(daily 265, total (6-5.2)%*10000 = 80) = 80 → /3 = 26.67
    # ×0.25 (ступень total 5.0) ×0.5 (near-wall) = 3.33
    assert v["max_risk_per_trade_usd"] == 3.33
    assert v["risk_mult_applied"] == 0.125
    assert any("до общей стены 0.80%" in r for r in v["reasons"])
    assert not any("до дневной стены" in r for r in v["reasons"])


def test_risk_rounding_to_zero_refuses_trade():
    # все термы положительны, но множители сжимают риск ниже цента —
    # сделка с риском $0.00 невозможна, это HALT_NEW, а не OK с нулём
    v = gate(loss_streak_mult=0.0001, profile_mult=0.0001)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert v["open_risk_room_usd"] == 150.0  # остаток открытого риска не при чём
    assert v["blocked_by"] == "risk_rounds_to_zero"
    assert any("округляется до нуля" in r for r in v["reasons"])


@pytest.mark.parametrize("equity,max_open_risk_pct,start_open_risk,min_opens", [
    (10000.0, 1.5, 0.0, 2),        # остаток открытого риска связывает раньше бюджета
    (10000.0, 3.0, 0.0, 3),        # лимит открытого риска равен дневному бюджету
    (10000.0, 0.5, 0.0, 1),        # жёсткий лимит открытого риска
    (10000.0, 5.0, 0.0, 3),        # лимит открытого риска ШИРЕ дневного: держит бюджет
    (5000.0, 1.5, 0.0, 2),
    (5000.0, 9.0, 0.0, 3),         # лимит открытого риска втрое шире дневного
    (10000.0, 5.0, 299.965, 1),    # неудобный остаток БЮДЖЕТА $0.035: round дал бы $0.04
    (10000.0, 1.5, 149.965, 1),    # неудобный остаток ЛИМИТА открытого риска $0.035
    (10000.0, 1.5, 149.999, 0),    # остаток меньше копейки → выдавать нечего
], ids=["open_risk_limit_binds", "open_risk_limit_equals_daily", "tight_open_risk_limit",
        "open_risk_limit_wider_than_daily", "small_equity", "open_risk_limit_triple_daily",
        "awkward_budget_residue", "awkward_open_risk_residue", "residue_below_one_cent"])
def test_concurrent_positions_never_exceed_daily_budget(equity, max_open_risk_pct,
                                                        start_open_risk, min_opens):
    """Позиции открываются ПОДРЯД и НЕ закрываются: equity не меняется, поэтому
    дневной бюджет остаётся прежним, а единственный тормоз — учёт уже открытого
    риска. Инвариант держат два терма формулы: (max_open_risk_pct% × equity −
    open_risk_usd) и (бюджет − open_risk_usd). Без первого пробивается лимит
    открытого риска, без второго — дневной бюджет, когда max_open_risk_pct шире
    дневного лимита. Неудобные стартовые остатки проверяют, что выдача
    округляется ВНИЗ: round вверх пробил бы бюджет на последней сделке.
    """
    limits = dict(LIMITS, max_open_risk_pct=max_open_risk_pct)
    daily_budget = LIMITS["daily_loss_limit_pct"] / 100 * equity
    open_risk = start_open_risk
    opened = 0
    for _ in range(50):
        v = evaluate_gate(equity=equity, day_start_equity=equity, initial_balance=equity,
                          limits=limits, open_risk_usd=open_risk,
                          # positions_count/trades_today намеренно 0: иначе лимит
                          # позиций/попыток остановил бы цикл первым и замаскировал
                          # проверяемый механизм
                          positions_count=0, trades_today=0, now=NOW)
        if v["verdict"] in ("HALT_NEW", "FORCE_FLAT"):
            break
        risk = v["max_risk_per_trade_usd"]
        assert risk > 0.0
        open_risk += risk
        opened += 1
        assert open_risk <= daily_budget + 1e-9, (
            f"суммарный открытый риск ${open_risk:.2f} превысил дневной бюджет "
            f"${daily_budget:.2f} после {opened} позиций")
        assert open_risk <= max_open_risk_pct / 100 * equity + 1e-9
    else:  # цикл не прервался — гейт не остановил накопление риска
        pytest.fail(f"гейт разрешил 50 одновременных позиций, открытый риск ${open_risk:.2f}")
    # min_opens задан явно по кейсам: защита от вырожденного «зелёного» прогона,
    # в котором гейт не дал открыть ничего и инвариант выполнился сам собой
    assert opened >= min_opens, f"открыто {opened} позиций, ожидалось >= {min_opens}"
    assert open_risk <= min(daily_budget, max_open_risk_pct / 100 * equity) + 1e-9


# --------------------------------------------------------------------------
# смоук-прогоны не копятся в серию убытков
# --------------------------------------------------------------------------

def _dec(trade_id, **over):
    rec = {"type": "decision", "trade_id": trade_id}
    rec.update(over)
    return rec


def _out(trade_id, R, minute):
    return {"type": "outcome", "trade_id": trade_id, "R": R,
            "close_ts": f"2026-07-27T0{minute}:00:00+00:00"}


def test_smoke_runs_do_not_build_a_loss_streak():
    """РЕГРЕСС 2026-07-27: три технических прогона стека (открыть-закрыть,
    минус на спреде: −0.019, −0.042, −0.006) плюс одна реальная сделка дали
    серию из четырёх и остановили торговлю на весь день. Проверки
    работоспособности не являются торговыми решениями и защищать счёт от них
    правило не должно."""
    import datetime as dt
    from trader_lib.streak import compute_streak

    records = []
    for i, r in enumerate((-0.019, -0.042, -0.006), start=4):
        records += [_dec(f"s{i}", smoke=True), _out(f"s{i}", r, i)]
    records += [_dec("real1"), _out("real1", -0.398, 8)]

    res = compute_streak(
        records, now=dt.datetime(2026, 7, 27, 13, tzinfo=dt.timezone.utc),
        day_start_utc=dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc),
        streak_cfg=load_config("config/trader.config.json").risk.streak)
    assert res["loss_streak"] == 1
    assert res["halt_rest_of_day"] is False


def test_unmarked_losses_still_build_a_streak():
    """Обратная сторона: без явной пометки в момент входа убыток считается
    настоящим. Пометить его смоуком задним числом нельзя — иначе стена
    обходилась бы одним словом в записи."""
    import datetime as dt
    from trader_lib.streak import compute_streak

    records = []
    for i in range(4, 8):
        records += [_dec(f"t{i}"), _out(f"t{i}", -0.5, i)]

    res = compute_streak(
        records, now=dt.datetime(2026, 7, 27, 13, tzinfo=dt.timezone.utc),
        day_start_utc=dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc),
        streak_cfg=load_config("config/trader.config.json").risk.streak)
    assert res["loss_streak"] == 4
