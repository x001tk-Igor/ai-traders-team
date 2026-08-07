import numpy as np
import pandas as pd
import pytest

from trader_lib.journal import CONFIRMED_SETUP_STATUS, DECISION_REQUIRED


@pytest.fixture
def make_decision():
    """Фабрика полных decision-записей с дефолтами по всем DECISION_REQUIRED.

    Тесты запрашивают фикстуру аргументом и переопределяют только те поля,
    которые проверяют — единственная фабрика полных записей в проекте
    (test_journal.py/test_e2e_dryrun.py/test_close_watch.py используют её
    же, дублировать дефолты в нескольких местах не нужно). Все decision-
    записи в тестах идут через настоящий append_decision (строгую
    валидацию) — второго, "облегчённого" пути записи в trader_lib/journal.py
    нет, поэтому тестам, которым не важна полная схема, нужен способ дёшево
    получить полную запись, а не способ обойти проверку.

    Фикстура, а не обычная импортируемая функция: под pytest
    --import-mode=importlib (и вообще без явной зависимости от того, есть
    ли у tests/ __init__.py) прямой `from conftest import make_decision` из
    другого тестового модуля не гарантирован — pytest грузит conftest.py
    как плагин независимо от режима импорта, и фикстуры из него всегда
    доступны любому тесту в этой директории по имени параметра.
    """
    def _make(**overrides):
        base = {
            "trade_id": "t1", "symbol": "XAUUSD", "side": "buy",
            "regime": "trend_up",
            "tactic": "pullback", "setup_type": "ema_pullback",
            "setup_status": CONFIRMED_SETUP_STATUS,
            "thesis": "пробой уровня с ретестом на растущем тренде",
            "confidence": 0.62,
            "technical_trigger": "закрытие H1 выше EMA21 после ретеста",
            "entry": 2410.5, "sl": 2405.0, "tp_plan": 2421.5,
            "risk_usd": 40.0, "rr": 2.0, "costs_R": 0.08, "breakeven_p": 0.36,
            "p_win_journal": 0.55,
            "news_check": "нет high-impact в ближайшие 2ч",
            "spread_at_entry": 2.1,
            "correlation_check": "нет открытых коррелирующих позиций",
            "daily_risk_remaining_usd": 160.0,
            "planned": True,
            "plan_hypothesis_id": "h-2026-07-24-01",
            "gate_verdict": "OK",
            "session_phase": "LONDON",
            "model_id": "claude-sonnet-5",
            "model_profile": "strong",
        }
        # страховка от дрейфа: если DECISION_REQUIRED вырастет, а дефолты
        # здесь не обновят — фабрика обязана упасть громко, а не тихо
        # отдавать неполную запись, которую append_decision потом отклонит
        # с непонятной причины
        missing = set(DECISION_REQUIRED) - set(base)
        assert not missing, f"make_decision не покрывает DECISION_REQUIRED: {missing}"
        base.update(overrides)
        return base
    return _make


@pytest.fixture
def uptrend_bars():
    n = 600
    t = pd.date_range("2026-01-01", periods=n, freq="5min")
    px = 2600 + np.linspace(0, 60, n) + np.sin(np.arange(n) / 8) * 0.8
    return pd.DataFrame({"time": t, "open": px, "high": px + 0.6, "low": px - 0.6,
                         "close": px + 0.1, "tick_volume": 200, "spread": 20})


@pytest.fixture
def chop_bars():
    n = 600
    t = pd.date_range("2026-01-01", periods=n, freq="5min")
    rng = np.random.default_rng(7)
    px = 2600 + np.cumsum(rng.normal(0, 0.4, n))
    return pd.DataFrame({"time": t, "open": px, "high": px + 0.5, "low": px - 0.5,
                         "close": px, "tick_volume": 150, "spread": 20})
