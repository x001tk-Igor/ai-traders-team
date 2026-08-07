"""MFE/MAE — как далеко сделка ходила в прибыль и в убыток, в единицах R.

Поля `mfe_R`/`mae_R` объявлены в `OUTCOME_FIELDS` с самого начала и всё это
время писались `None`: место было размечено, заполняющего кода не было. Это тот
же дефект, что ловился в проекте семь раз («код написан, тестами покрыт, никем
не вызывается»), только в зеркальном виде — здесь не вызывается то, чего нет.

Цена пропуска конкретная. По одному R закрытия НЕРАЗЛИЧИМЫ две сделки:
та, что сразу пошла против и честно выбила стоп, и та, что дошла до +1.5R и
вернулась к стопу. Первая говорит «вход был плохой», вторая — «вход был верный,
прибыль отдало ведение». Лечатся они противоположным, а в журнале выглядят
одинаково: −1.0R.

Восстановление идёт ПО БАРАМ, а не по живым тикам: так замер идемпотентен и
одинаково работает для позиции, закрытой брокером, пока датчик спал.

## Замер сознательно ЗАНИЖАЮЩИЙ

Берутся только бары, целиком лежащие внутри [открытие; закрытие]. Бар, начавшийся
до входа, содержит цены ДО входа, и его максимум приписал бы сделке ход, которого
она не видела.

Асимметрия неслучайна. Этот замер будет решать судьбу метода наращивания позиции
(`docs/plan_team.md`), а там вопрос ровно один: сколько сделок дошло до +0.5R и
+1.0R. Завышенный MFE ответит «много» на выборке, где их не было, — то есть
подтвердит метод данными, которых нет. Занижение в худшем случае скажет «рано»;
цена ошибки несопоставима, поэтому округляем против себя.

Когда целых баров в окне нет (сделка короче бара) или история не покрывает
момент входа — возвращается `(None, None)`. Это ЧЕСТНОЕ «не измерено», а не
ноль: ноль означал бы «не ходила никуда», и статистика приняла бы его за факт.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

UTC = dt.timezone.utc

# Минуты в баре — для проверки, что бар целиком лежит внутри окна сделки.
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}

# ТФ замера по умолчанию. M5 мельче любого рабочего ТФ команды (H1) и при этом
# даёт точные экстремумы: high/low бара — настоящие максимум и минимум внутри
# него, а не приближение.
DEFAULT_TF = "M5"


def _as_utc(ts) -> dt.datetime | None:
    """Момент времени → aware UTC. None, если распарсить нечем."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(ts, dt.datetime):
        return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    return None


def excursion_R(bars, *, side, entry, sl, opened_utc, closed_utc,
                server_utc_offset_hours=0, tf=DEFAULT_TF):
    """→ (mfe_R, mae_R) в единицах R. (None, None), если замерить не на чем.

    `bars` — DataFrame с колонками time/high/low, время СЕРВЕРНОЕ (как отдаёт
    MT5), поэтому приводится к UTC по смещению из конституции. Забыть это
    приведение — значит сдвинуть окно сделки на три часа и замерить чужой
    участок графика; ровно этот класс ошибки уже стоил проекту разбора с
    календарём ForexFactory.
    """
    opened, closed = _as_utc(opened_utc), _as_utc(closed_utc)
    if opened is None or closed is None or closed <= opened:
        return None, None
    try:
        risk = abs(float(entry) - float(sl))
    except (TypeError, ValueError):
        return None, None
    if risk <= 0:
        return None, None            # без расстояния до стопа R не существует
    if bars is None or len(bars) == 0 or not {"time", "high", "low"} <= set(bars):
        return None, None

    times = pd.to_datetime(bars["time"]) - dt.timedelta(hours=server_utc_offset_hours or 0)
    times = times.dt.tz_localize(UTC) if times.dt.tz is None else times.dt.tz_convert(UTC)

    # История обязана покрывать момент входа целиком. Иначе замерился бы ХВОСТ
    # сделки под видом всей сделки — и «до +1R не доходила» означало бы лишь
    # «мы не смотрели туда, где доходила».
    if times.iloc[0] > opened:
        return None, None

    span = dt.timedelta(minutes=TF_MINUTES.get(tf, 5))
    inside = (times >= opened) & (times + span <= closed)
    if not inside.any():
        return None, None

    hi = float(pd.to_numeric(bars["high"])[inside].max())
    lo = float(pd.to_numeric(bars["low"])[inside].min())
    entry = float(entry)

    if str(side).lower() in ("buy", "long"):
        mfe, mae = (hi - entry) / risk, (lo - entry) / risk
    else:
        mfe, mae = (entry - lo) / risk, (entry - hi) / risk
    return round(mfe, 3), round(mae, 3)


def measure(market, *, symbol, side, entry, sl, opened_utc, closed_utc,
            server_utc_offset_hours=0, tf=DEFAULT_TF, bars=600):
    """То же по живому рынку. Никогда не бросает: замер — не критичный путь.

    Отдельная функция, потому что в `excursion_R` не должно быть ввода-вывода:
    вся арифметика проверяется на выдуманных барах без MT5, а сюда сводится
    единственное, что может упасть, — поход за историей.
    """
    try:
        return excursion_R(market.copy_rates(symbol, tf, bars), side=side, entry=entry,
                           sl=sl, opened_utc=opened_utc, closed_utc=closed_utc,
                           server_utc_offset_hours=server_utc_offset_hours, tf=tf)
    except Exception:  # noqa: BLE001 — молчим намеренно, см. ниже
        # Сорванный замер НЕ ДОЛЖЕН мешать записи исхода: журнал закрытой сделки
        # важнее аналитики о ней. Ошибка превращается в честное «не измерено».
        return None, None
