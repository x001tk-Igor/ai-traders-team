"""Структурные фичи (задача 7.1): ADX, VWAP, сессионные диапазоны, свинги,
эффективность движения.

Эти фичи отвечают на вопрос «какая сейчас форма рынка», а не «куда он пойдёт».
Дисциплина та же, что у всего снимка: **недостаток данных даёт null и причину,
а не правдоподобное число**. Проверка на это здесь главная — test_all_new_
features_null_on_insufficient_bars: модель читает снимок как факты, и одно
выдуманное значение превращается в тезис, а тезис в сделку.

Второе по важности — окна сессий. Время баров MT5 СЕРВЕРНОЕ; без перевода в UTC
«азиатская сессия» съезжает на смещение брокера и считает не те часы. Это уже
ловилось в брифинге (задача 6.1), здесь та же ловушка на уровне фич.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from trader_lib.features import compute_tf_features

KW = dict(point=0.01, atr_period=14, momentum_bars=[5, 15, 30], range_bars=30,
          ema_fast=12, ema_slow=26, atr_pctile_lookback=500,
          server_utc_offset_hours=3)


def _bars(n=300, *, start=2400.0, step=0.0, noise=0.0, seed=0,
          last_utc=dt.datetime(2026, 7, 27, 12, 0), tf_minutes=5, volume=100,
          hl=0.5):
    """Бары в СЕРВЕРНОМ времени (наивные), как отдаёт MT5.

    hl — половина размаха свечи. Через него задаётся ATR: при hl=0.5 он выходит
    около 1.0, и тогда деление на ATR численно неотличимо от его отсутствия —
    на этом выживала мутация «расстояние до EMA в цене вместо ATR».
    """
    rng = np.random.default_rng(seed)
    close = start + np.arange(n) * step
    if noise:
        close = close + rng.normal(0, noise, n)
    high = close + hl
    low = close - hl
    open_ = np.concatenate([[close[0]], close[:-1]])
    end_server = last_utc + dt.timedelta(hours=KW["server_utc_offset_hours"])
    times = pd.date_range(end=end_server, periods=n, freq=f"{tf_minutes}min")
    return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low,
                         "close": close, "tick_volume": volume, "spread": 20})


# --------------------------------------------------------------------------
# ADX
# --------------------------------------------------------------------------

def test_adx_flat_below_20_trend_above_25():
    """ADX отличает направленное движение от болтанки — это и есть его
    единственная работа."""
    trend = compute_tf_features(_bars(step=0.6, noise=0.05), **KW)
    flat = compute_tf_features(_bars(step=0.0, noise=1.2, seed=7), **KW)
    assert trend["adx"] > 25, f"тренд не опознан: adx={trend['adx']}"
    assert flat["adx"] < 20, f"флет не опознан: adx={flat['adx']}"


def test_adx_is_bounded():
    f = compute_tf_features(_bars(step=0.6), **KW)
    assert 0.0 <= f["adx"] <= 100.0


def test_adx_null_on_motionless_series():
    """Идеально ровный ряд: направленного движения нет вовсе, ADX не определён.
    Ноль здесь был бы враньём — ноль означает «движение есть, но
    разнонаправленное», а не «движения не было»."""
    flat = _bars(n=300, step=0.0, noise=0.0)
    assert compute_tf_features(flat, **KW)["adx"] is None


# --------------------------------------------------------------------------
# VWAP
# --------------------------------------------------------------------------

def test_vwap_matches_manual():
    """VWAP считается по барам ТЕКУЩЕГО серверного дня, вручную проверяемо."""
    bars = _bars(n=200, step=0.1, volume=100)
    f = compute_tf_features(bars, **KW)

    times_utc = bars["time"] - dt.timedelta(hours=KW["server_utc_offset_hours"])
    today = times_utc.dt.date.iloc[-1]
    day = bars[times_utc.dt.date == today]
    tp = (day["high"] + day["low"] + day["close"]) / 3
    manual = float((tp * day["tick_volume"]).sum() / day["tick_volume"].sum())
    assert f["vwap_day"] == pytest.approx(manual, rel=1e-6)
    assert f["dist_to_vwap_atr"] == pytest.approx(
        (float(bars["close"].iloc[-1]) - manual) / f["atr_price"], rel=1e-3)


def test_vwap_null_without_volume():
    """Нулевой объём — не повод делить на ноль и не повод выдать среднюю цену
    вместо VWAP: это разные числа."""
    f = compute_tf_features(_bars(volume=0), **KW)
    assert f["vwap_day"] is None and f["dist_to_vwap_atr"] is None


# --------------------------------------------------------------------------
# сессионные диапазоны
# --------------------------------------------------------------------------

def test_asian_range_window_utc():
    """Азиатская сессия — 00:00–07:00 UTC. Время баров серверное (+3), значит
    это 03:00–10:00 у брокера: без перевода окно съедет на три часа."""
    bars = _bars(n=400, step=0.0, last_utc=dt.datetime(2026, 7, 27, 12, 0))
    times_utc = bars["time"] - dt.timedelta(hours=3)
    asian = (times_utc.dt.hour >= 0) & (times_utc.dt.hour < 7) & \
            (times_utc.dt.date == times_utc.dt.date.iloc[-1])
    bars.loc[asian, "high"] = 2450.0
    bars.loc[asian, "low"] = 2350.0

    f = compute_tf_features(bars, **KW)
    assert f["asian_high"] == 2450.0 and f["asian_low"] == 2350.0
    assert f["asian_range_atr"] == pytest.approx(100.0 / f["atr_price"], rel=1e-3)


def test_london_window_does_not_leak_into_asian():
    """Окна не должны перетекать друг в друга. Лондонский максимум взят ВЫШЕ
    азиатского намеренно: на равных значениях тест не отличил бы правильное
    окно от расширенного (мутация «азиатское окно до 12:00» выживала)."""
    bars = _bars(n=400, step=0.0, last_utc=dt.datetime(2026, 7, 27, 15, 0))
    times_utc = bars["time"] - dt.timedelta(hours=3)
    today = times_utc.dt.date.iloc[-1]
    asian = (times_utc.dt.hour >= 0) & (times_utc.dt.hour < 7) & (times_utc.dt.date == today)
    london = (times_utc.dt.hour >= 7) & (times_utc.dt.hour < 12) & (times_utc.dt.date == today)
    bars.loc[asian, "high"] = 2450.0
    bars.loc[london, "high"] = 2470.0

    f = compute_tf_features(bars, **KW)
    assert f["london_high"] == 2470.0
    assert f["asian_high"] == 2450.0, "лондонский максимум попал в азиатское окно"


def test_session_ranges_null_without_time_column():
    """Нет времени — нет сессий. Считать «последние N баров» вместо окна
    значило бы выдать другое число под тем же именем."""
    # ряд с движением: на идеально ровном ADX не определён математически
    # (направленного движения нет вовсе), и тест не отличал бы «нет времени»
    # от «нет движения»
    bars = _bars(step=0.3).drop(columns=["time"])
    f = compute_tf_features(bars, **KW)
    assert f["asian_high"] is None and f["london_high"] is None
    assert f["vwap_day"] is None
    assert f["adx"] is not None, "фичи без времени обязаны считаться"
    assert f["move_efficiency"] is not None


# --------------------------------------------------------------------------
# свинги
# --------------------------------------------------------------------------

def test_swings_detected():
    """Фрактал: экстремум, вокруг которого N баров ниже (выше). Берём ПОСЛЕДНИЙ
    подтверждённый — незакрытый экстремум справа ещё не свинг."""
    bars = _bars(n=120, step=0.0, noise=0.0)
    bars.loc[60, "high"] = 2500.0     # локальный максимум
    bars.loc[90, "low"] = 2300.0      # локальный минимум
    f = compute_tf_features(bars, **KW)
    assert f["last_swing_high"] == 2500.0
    assert f["last_swing_low"] == 2300.0
    assert f["dist_to_last_swing_atr"] is not None


def test_swing_ignores_unconfirmed_edge():
    """Экстремум в самом конце ряда не подтверждён: справа нет N баров."""
    bars = _bars(n=120, step=0.0, noise=0.0)
    bars.loc[119, "high"] = 2600.0
    f = compute_tf_features(bars, **KW)
    assert f["last_swing_high"] != 2600.0


def test_swings_null_when_no_extremes():
    """Идеально ровный ряд свингов не имеет — это null, а не «последний бар»."""
    flat = _bars(n=120, step=0.0, noise=0.0)
    flat["high"] = 2400.5
    flat["low"] = 2399.5
    f = compute_tf_features(flat, **KW)
    assert f["last_swing_high"] is None and f["last_swing_low"] is None


# --------------------------------------------------------------------------
# эффективность движения и расстояния до EMA
# --------------------------------------------------------------------------

def test_move_efficiency_bounds_0_1():
    straight = compute_tf_features(_bars(step=0.5, noise=0.0), **KW)
    choppy = compute_tf_features(_bars(step=0.0, noise=1.5, seed=3), **KW)
    assert straight["move_efficiency"] == pytest.approx(1.0, abs=0.01)
    assert 0.0 <= choppy["move_efficiency"] <= 1.0
    assert choppy["move_efficiency"] < 0.5


def test_dist_to_emas_in_atr():
    """Расстояние ОБЯЗАНО быть в ATR, а не в цене: иначе числа несравнимы между
    инструментами, а модель читает их как «далеко/близко». Сравнение
    «медленная дальше быстрой» верно и в цене, и в ATR — поэтому проверяется
    само значение."""
    bars = _bars(step=0.5, hl=3.0)          # ATR ≈ 6, а не ≈ 1
    f = compute_tf_features(bars, **KW)
    assert f["atr_price"] > 3.0, "фикстура обязана дать ATR, заметно отличный от 1"
    close = float(bars["close"].iloc[-1])
    ema_f = float(bars["close"].ewm(span=KW["ema_fast"], adjust=False).mean().iloc[-1])
    expected = (close - ema_f) / f["atr_price"]
    assert f["dist_to_ema_fast_atr"] == pytest.approx(expected, abs=0.01)
    assert f["dist_to_ema_slow_atr"] > f["dist_to_ema_fast_atr"]


def test_adx_null_when_history_too_short_for_it():
    """ADX требует двойного сглаживания (~2×период). Бывает, что баров хватает
    на остальные фичи, но не на него: тогда null, а не число из трёх свечей."""
    short_kw = dict(KW, atr_period=14, momentum_bars=[2, 3, 5], range_bars=5,
                    ema_fast=3, ema_slow=5)
    f = compute_tf_features(_bars(n=20, step=0.3), **short_kw)
    assert f["atr_price"] is not None, "остальные фичи обязаны посчитаться"
    assert f["adx"] is None


# --------------------------------------------------------------------------
# дисциплина null
# --------------------------------------------------------------------------

NEW_KEYS = ("adx", "vwap_day", "dist_to_vwap_atr", "asian_high", "asian_low",
            "asian_range_atr", "london_high", "london_low", "last_swing_high",
            "last_swing_low", "dist_to_last_swing_atr", "move_efficiency",
            "dist_to_ema_fast_atr", "dist_to_ema_slow_atr")


def test_all_new_features_null_on_insufficient_bars():
    """Мало баров — КАЖДОЕ поле null и причина. Пропущенный ключ хуже null:
    потребитель сделает .get(...) и получит None молча, без причины."""
    f = compute_tf_features(_bars(n=5), **KW)
    assert f["reason"].startswith("insufficient")
    for key in NEW_KEYS:
        assert key in f, f"ключ {key} пропал из null-набора"
        assert f[key] is None, f"{key} не null при нехватке баров"


def test_new_keys_present_on_normal_path():
    f = compute_tf_features(_bars(step=0.3), **KW)
    for key in NEW_KEYS:
        assert key in f, f"ключ {key} отсутствует в обычном ответе"
