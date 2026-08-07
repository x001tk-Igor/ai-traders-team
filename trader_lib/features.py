"""Фичи снимка: числа о форме рынка, без интерпретации.

ДИСЦИПЛИНА ОДНА НА ВЕСЬ МОДУЛЬ: недостаток данных даёт **null и причину**, а не
правдоподобное число. Модель читает снимок как факты; одно выдуманное значение
превращается в тезис, тезис — в сделку, и в журнале останется «объём
подтверждал» там, где объёма не было.

Из этого следует и форма ответа: набор ключей ОДИН И ТОТ ЖЕ на всех путях.
Пропущенный ключ хуже null — потребитель сделает `.get(...)`, получит None и не
узнает, что данных не было (а `reason` он не прочитает, потому что не ждал
проблемы).

ВРЕМЯ БАРОВ MT5 — СЕРВЕРНОЕ (наивный datetime). Всё, что зависит от часов
(VWAP дня, сессионные окна), обязано переводиться в UTC через
server_utc_offset_hours: иначе «азиатская сессия» съезжает на смещение брокера
и считает не те часы. Та же ловушка уже ловилась в брифинге (задача 6.1).
Нет колонки time или неизвестно смещение → эти фичи null, остальные считаются.
"""
import datetime as dt

import numpy as np
import pandas as pd

# Сессионные окна в UTC. Не в конфиге: это факт про рынок (когда торгуют
# Азия/Лондон), а не параметр риска, который владелец счёта мог бы захотеть менять.
ASIAN_UTC = (0, 7)
LONDON_UTC = (7, 12)

ADX_PERIOD = 14
SWING_FRACTAL_N = 2      # столько баров слева и справа должно быть ниже/выше

_NULL_KEYS = (
    "atr_price", "atr_points", "atr_pctile", "pos_in_range",
    "mom_short_atr", "mom_mid_atr", "mom_long_atr", "trend",
    "spread_atr", "spread_points", "dist_to_high_atr", "dist_to_low_atr",
    # структурные (задача 7.1)
    "adx", "vwap_day", "dist_to_vwap_atr",
    "asian_high", "asian_low", "asian_range_atr", "london_high", "london_low",
    "last_swing_high", "last_swing_low", "dist_to_last_swing_atr",
    "move_efficiency", "dist_to_ema_fast_atr", "dist_to_ema_slow_atr",
)


def _null_features(reason):
    return {**{k: None for k in _NULL_KEYS}, "reason": reason}


def _atr(h, l, c, period):
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _adx(h, l, c, period=ADX_PERIOD):
    """ADX по Уайлдеру. None, если баров не хватает на двойное сглаживание
    (сам ADX — среднее DX за период, то есть нужно ~2×period баров)."""
    if len(c) < period * 2 + 2:
        return None
    up = h.diff()
    down = -l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)

    alpha = 1.0 / period          # сглаживание Уайлдера
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=h.index).ewm(
        alpha=alpha, adjust=False).mean() / atr_w
    minus_di = 100 * pd.Series(minus_dm, index=h.index).ewm(
        alpha=alpha, adjust=False).mean() / atr_w
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx = dx.ewm(alpha=alpha, adjust=False).mean().iloc[-1]
    return None if pd.isna(adx) else float(adx)


def _times_utc(bars, offset_hours):
    """Время баров в UTC. None — колонки нет: фичи по часам считать не из чего,
    и подменять их «последними N барами» нельзя (это другое число)."""
    if "time" not in bars:
        return None
    times = pd.to_datetime(bars["time"])
    return times - dt.timedelta(hours=offset_hours or 0)


def _vwap_day(bars, times_utc):
    """VWAP текущего дня по типичной цене. None при нулевом объёме: средняя
    цена вместо VWAP — другое число под тем же именем."""
    today = times_utc.dt.date.iloc[-1]
    day = bars[times_utc.dt.date == today]
    volume = day["tick_volume"].astype(float)
    total = float(volume.sum())
    if total <= 0 or len(day) == 0:
        return None
    tp = (day["high"] + day["low"] + day["close"]) / 3.0
    return float((tp * volume).sum() / total)


def _session_range(bars, times_utc, window):
    """(high, low) окна сессии ТЕКУЩЕГО дня или (None, None), если баров этого
    окна в выборке нет — например, снимок снят до его начала."""
    start, end = window
    today = times_utc.dt.date.iloc[-1]
    mask = (times_utc.dt.date == today) & (times_utc.dt.hour >= start) & \
           (times_utc.dt.hour < end)
    win = bars[mask]
    if len(win) == 0:
        return None, None
    return float(win["high"].max()), float(win["low"].min())


def _last_swings(h, l, n=SWING_FRACTAL_N):
    """Последние ПОДТВЕРЖДЁННЫЕ фрактальные экстремумы.

    Подтверждённый — тот, справа от которого уже есть n баров: экстремум на
    краю ряда ещё может быть пробит следующим баром, и называть его свингом
    значит выдавать незавершённое за факт.
    """
    high_val = low_val = None
    high_idx = low_idx = None
    highs, lows = h.to_numpy(), l.to_numpy()
    # Верхняя граница len-n-1 избыточна и оставлена намеренно, как объявление
    # намерения: у бара ближе к концу срез окна обрезается длиной массива, и
    # условие «ровно 2n баров строго ниже» для него недостижимо в принципе.
    # То есть подтверждённость держится на СЧЁТЧИКЕ, а не на границе цикла —
    # проверено мутацией (снятие границы ничего не меняет).
    for i in range(len(highs) - n - 1, n - 1, -1):
        window_h = highs[i - n:i + n + 1]
        if high_val is None and highs[i] == window_h.max() and \
                (window_h < highs[i]).sum() == 2 * n:
            high_val, high_idx = float(highs[i]), i
        window_l = lows[i - n:i + n + 1]
        if low_val is None and lows[i] == window_l.min() and \
                (window_l > lows[i]).sum() == 2 * n:
            low_val, low_idx = float(lows[i]), i
        if high_val is not None and low_val is not None:
            break
    last = None
    if high_idx is not None or low_idx is not None:
        last = high_val if (low_idx is None or (high_idx or -1) > low_idx) else low_val
    return high_val, low_val, last


def _move_efficiency(c, bars_back):
    """Чистое перемещение / сумма модулей шагов на том же окне: 1.0 — прямая
    линия, около нуля — болтанка. None, если ходов не было вовсе."""
    window = c.tail(bars_back + 1)
    if len(window) < 2:
        return None
    gross = float(window.diff().abs().sum())
    if gross <= 0:
        return None
    net = abs(float(window.iloc[-1] - window.iloc[0]))
    return net / gross


def compute_tf_features(bars, *, point, atr_period, momentum_bars, range_bars,
                        ema_fast, ema_slow, atr_pctile_lookback,
                        server_utc_offset_hours=0):
    """bars: DataFrame time,open,high,low,close,tick_volume,spread. Закрытые бары.
    Возвращает dict фич. Недостаток данных → значения None + reason."""
    need = max(atr_period, max(momentum_bars), range_bars, ema_slow) + 2
    if len(bars) < need:
        return _null_features(f"insufficient bars: {len(bars)}<{need}")
    h, l, c = bars["high"], bars["low"], bars["close"]
    atr = _atr(h, l, c, atr_period)
    atr_price = float(atr.iloc[-1])
    if not (atr_price > 0):
        return _null_features("atr unavailable (<=0 or NaN)")
    hist = atr.dropna().tail(atr_pctile_lookback)
    atr_pctile = float((hist < atr_price).mean()) if len(hist) >= 20 else None
    win = bars.tail(range_bars)
    rng = float(win["high"].max() - win["low"].min())
    pos = float((c.iloc[-1] - win["low"].min()) / rng) if rng > 0 else 0.5
    ms, mm, ml = momentum_bars
    last_close = float(c.iloc[-1])

    def mom(k):
        return float((c.iloc[-1] - c.iloc[-1 - k]) / atr_price)

    ema_f = c.ewm(span=ema_fast, adjust=False).mean().iloc[-1]
    ema_s = c.ewm(span=ema_slow, adjust=False).mean().iloc[-1]
    slope = (ema_f - ema_s) / atr_price
    trend = "up" if slope > 0.1 else "down" if slope < -0.1 else "flat"
    spread_pts = float(bars["spread"].iloc[-1])
    spread_price = spread_pts * point

    # --- структурные фичи (задача 7.1) ---
    times_utc = _times_utc(bars, server_utc_offset_hours)
    vwap = _vwap_day(bars, times_utc) if times_utc is not None else None
    if times_utc is not None:
        asian_high, asian_low = _session_range(bars, times_utc, ASIAN_UTC)
        london_high, london_low = _session_range(bars, times_utc, LONDON_UTC)
    else:
        asian_high = asian_low = london_high = london_low = None
    swing_high, swing_low, last_swing = _last_swings(h, l)
    efficiency = _move_efficiency(c, range_bars)

    return {
        "atr_price": round(atr_price, 5), "atr_points": round(atr_price / point, 1),
        "atr_pctile": None if atr_pctile is None else round(atr_pctile, 2),
        "pos_in_range": round(min(max(pos, 0.0), 1.0), 2),
        "mom_short_atr": round(mom(ms), 2), "mom_mid_atr": round(mom(mm), 2),
        "mom_long_atr": round(mom(ml), 2), "trend": trend,
        "spread_atr": round(spread_price / atr_price, 3),
        "spread_points": spread_pts,
        "dist_to_high_atr": round((c.iloc[-1] - win["high"].max()) / atr_price, 2),
        "dist_to_low_atr": round((c.iloc[-1] - win["low"].min()) / atr_price, 2),

        "adx": _round_or_none(_adx(h, l, c), 1),
        "vwap_day": _round_or_none(vwap, 5),
        "dist_to_vwap_atr": (round((last_close - vwap) / atr_price, 2)
                             if vwap is not None else None),
        "asian_high": asian_high, "asian_low": asian_low,
        "asian_range_atr": (round((asian_high - asian_low) / atr_price, 2)
                            if asian_high is not None else None),
        "london_high": london_high, "london_low": london_low,
        "last_swing_high": swing_high, "last_swing_low": swing_low,
        "dist_to_last_swing_atr": (round((last_close - last_swing) / atr_price, 2)
                                   if last_swing is not None else None),
        "move_efficiency": _round_or_none(efficiency, 3),
        "dist_to_ema_fast_atr": round((last_close - float(ema_f)) / atr_price, 2),
        "dist_to_ema_slow_atr": round((last_close - float(ema_s)) / atr_price, 2),
        "reason": None,
    }


def _round_or_none(value, digits):
    return None if value is None else round(float(value), digits)
