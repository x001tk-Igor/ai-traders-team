"""Структурный срез в снимке и бюджет контекста (задача 7.2).

ДВА ТРЕБОВАНИЯ, КОТОРЫЕ ТЯНУТ В РАЗНЫЕ СТОРОНЫ, И В ЭТОМ ВСЯ ЗАДАЧА.

1. Модель физически не видит форму рынка по одним агрегатам. ATR и моментум не
   отличают «плавный откат к уровню» от «свеча-шпилька туда-обратно» — для этого
   нужны сами бары. Поэтому в снимок кладутся последние закрытые бары OHLC.

2. Слабая модель живёт в 32k контекста. Раздутый снимок вытесняет план дня,
   опыт и открытое намерение — то есть именно то, из чего принимается решение.
   Поэтому есть жёсткий бюджет, и он проверяется тестом, а не пожеланием в
   промте.

Компромисс: бары округляются до digits инструмента и отдаются компактно.
"""
import dataclasses
import json

import pytest

import scripts.perceive as perceive
from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket


def _cfg(tmp_path, **perception):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if perception:
        cfg = dataclasses.replace(cfg, perception=dataclasses.replace(
            cfg.perception, **perception))
    return cfg


def _market():
    return FakeMarket(point=0.01, digits=2, spread_points=20,
                      account={"balance": 10000, "equity": 10000})


def _snap(tmp_path, monkeypatch, cfg=None, tfs=("M5", "H1")):
    monkeypatch.setattr(perceive, "state_dir", lambda cfg: str(tmp_path))
    return perceive.build_snapshot(_market(), cfg or _cfg(tmp_path), "XAUUSD", list(tfs))


# --------------------------------------------------------------------------
# структурный срез
# --------------------------------------------------------------------------

def test_snapshot_includes_recent_bars(tmp_path, monkeypatch):
    """Без самих баров модель не видит форму: агрегаты не отличают плавный
    откат от шпильки."""
    snap = _snap(tmp_path, monkeypatch)
    bars = snap["tf"]["M5"]["bars"]
    assert len(bars) == perceive.SNAPSHOT_BARS
    first = bars[0]
    assert set(first) == {"t", "o", "h", "l", "c"}
    assert all(isinstance(first[k], (int, float)) for k in ("o", "h", "l", "c"))


def test_bars_are_closed_only(tmp_path, monkeypatch):
    """Последний бар формируется прямо сейчас: показать его как закрытый
    значит дать модели цену, которой ещё не было."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(perceive, "state_dir", lambda c: str(tmp_path))
    market = _market()
    raw = market.copy_rates("XAUUSD", "M5", cfg.perception.atr_pctile_lookback + 50)
    snap = perceive.build_snapshot(market, cfg, "XAUUSD", ["M5"])
    last_shown = snap["tf"]["M5"]["bars"][-1]["c"]
    assert last_shown != pytest.approx(float(raw["close"].iloc[-1]))
    assert last_shown == pytest.approx(round(float(raw["close"].iloc[-2]), 2))


def test_bars_rounded_to_symbol_digits(tmp_path, monkeypatch):
    """Пятнадцать знаков после запятой — это байты контекста, а не точность."""
    snap = _snap(tmp_path, monkeypatch)
    for bar in snap["tf"]["M5"]["bars"]:
        for key in ("o", "h", "l", "c"):
            assert bar[key] == round(bar[key], 2)


def test_structural_features_present(tmp_path, monkeypatch):
    """Фичи задачи 7.1 обязаны доехать до снимка, а не остаться в модуле."""
    tf = _snap(tmp_path, monkeypatch)["tf"]["M5"]
    for key in ("adx", "move_efficiency", "dist_to_ema_fast_atr",
                "last_swing_high", "vwap_day", "asian_high"):
        assert key in tf, key


def test_session_features_use_server_offset(tmp_path, monkeypatch):
    """Смещение брокера обязано доехать из конституции до расчёта сессий:
    иначе окна съедут, а снимок этого не покажет."""
    calls = {}
    real = perceive.compute_tf_features

    def spy(bars, **kw):
        calls.update(kw)
        return real(bars, **kw)

    monkeypatch.setattr(perceive, "compute_tf_features", spy)
    cfg = _cfg(tmp_path)
    _snap(tmp_path, monkeypatch, cfg=cfg, tfs=("M5",))
    assert calls["server_utc_offset_hours"] == cfg.risk.server_utc_offset_hours


# --------------------------------------------------------------------------
# бюджет контекста
# --------------------------------------------------------------------------

def test_snapshot_token_budget(tmp_path, monkeypatch):
    """Порог из конституции, а не из головы. Оценка токенов грубая (символы/3),
    но её задача — поймать раздувание в разы, а не посчитать точно."""
    cfg = _cfg(tmp_path)
    snap = _snap(tmp_path, monkeypatch, cfg=cfg)
    size = len(json.dumps(snap, ensure_ascii=False))
    # Измерение самоссылочно: поля с размером сами занимают место, и после их
    # заполнения длина меняется на разряд самого числа. Допуск покрывает
    # ровно это и ничего больше — раздувание в разы он не пропустит.
    assert abs(snap["size_chars"] - size) <= 16, (snap["size_chars"], size)
    assert snap["est_tokens"] <= cfg.perception.snapshot_token_budget, \
        f"снимок раздут: {snap['est_tokens']} токенов"


def test_budget_overflow_is_reported_not_silent(tmp_path, monkeypatch):
    """Если снимок всё же перерос бюджет — это видно в нём самом, а не
    выясняется на слабой модели обрезанным контекстом."""
    cfg = _cfg(tmp_path, snapshot_token_budget=10)
    snap = _snap(tmp_path, monkeypatch, cfg=cfg)
    assert snap["budget_exceeded"] is True
    assert "бюджет" in snap["warnings"][0]


def test_three_timeframes_still_fit(tmp_path, monkeypatch):
    """Обычный рабочий случай — два-три ТФ. На трёх бюджет тоже обязан
    держаться, иначе правило «не тяни больше трёх» неисполнимо."""
    cfg = _cfg(tmp_path)
    snap = _snap(tmp_path, monkeypatch, cfg=cfg, tfs=("M5", "M15", "H1"))
    assert snap["budget_exceeded"] is False, snap["est_tokens"]
