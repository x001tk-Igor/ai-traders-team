"""MFE/MAE: замер должен занижать, а не завышать, и честно говорить «не знаю»."""
import datetime as dt

import pandas as pd
import pytest

from trader_lib.excursion import excursion_R, measure

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def bars(rows, *, start=T0, freq="5min", offset_hours=0):
    """rows — список (high, low); время СЕРВЕРНОЕ, как отдаёт MT5."""
    t = pd.date_range(start.replace(tzinfo=None) + dt.timedelta(hours=offset_hours),
                      periods=len(rows), freq=freq)
    return pd.DataFrame({"time": t,
                         "high": [r[0] for r in rows],
                         "low": [r[1] for r in rows]})


def test_long_mfe_и_mae_в_единицах_R():
    # вход 100, стоп 98 → R = 2. Ход вверх до 103 = +1.5R, вниз до 99 = −0.5R
    b = bars([(101, 99.5), (103, 100), (102, 99)])
    mfe, mae = excursion_R(b, side="buy", entry=100, sl=98,
                           opened_utc=T0, closed_utc=T0 + dt.timedelta(minutes=15))
    assert mfe == 1.5
    assert mae == -0.5


def test_short_считает_зеркально():
    b = bars([(101, 97), (100, 96)])
    mfe, mae = excursion_R(b, side="sell", entry=100, sl=102,
                           opened_utc=T0, closed_utc=T0 + dt.timedelta(minutes=10))
    assert mfe == 2.0     # ушла вниз до 96 = +2R для шорта
    assert mae == -0.5    # сходила вверх до 101 = −0.5R


def test_серверное_время_приводится_к_UTC():
    """Забытое смещение сдвинуло бы окно на 3 часа и замерило чужой участок."""
    b = bars([(200, 100), (103, 99)], offset_hours=3)   # бары помечены server=UTC+3
    mfe, _ = excursion_R(b, side="buy", entry=100, sl=98,
                         opened_utc=T0 + dt.timedelta(minutes=5),
                         closed_utc=T0 + dt.timedelta(minutes=10),
                         server_utc_offset_hours=3)
    assert mfe == 1.5     # взят второй бар (high 103), а не выброс 200 из первого


def test_бар_начавшийся_до_входа_не_учитывается():
    """Его максимум — цена, которой сделка не видела: завысил бы MFE."""
    b = bars([(999, 99), (101, 99.5)])
    mfe, _ = excursion_R(b, side="buy", entry=100, sl=98,
                         opened_utc=T0 + dt.timedelta(minutes=2),
                         closed_utc=T0 + dt.timedelta(minutes=10))
    assert mfe == 0.5     # 999 из первого бара отброшен


def test_бар_не_закрывшийся_до_выхода_не_учитывается():
    b = bars([(101, 99.5), (999, 1)])
    mfe, mae = excursion_R(b, side="buy", entry=100, sl=98,
                           opened_utc=T0, closed_utc=T0 + dt.timedelta(minutes=7))
    assert mfe == 0.5 and mae == -0.25


def test_сделка_короче_бара_это_не_измерено_а_не_ноль():
    """Ноль означал бы «никуда не ходила» — статистика приняла бы его за факт."""
    b = bars([(105, 95)])
    assert excursion_R(b, side="buy", entry=100, sl=98, opened_utc=T0,
                       closed_utc=T0 + dt.timedelta(minutes=3)) == (None, None)


def test_история_не_покрывает_вход_это_не_измерено():
    """Иначе замерился бы ХВОСТ сделки под видом всей сделки."""
    b = bars([(101, 99)], start=T0 + dt.timedelta(minutes=30))
    assert excursion_R(b, side="buy", entry=100, sl=98, opened_utc=T0,
                       closed_utc=T0 + dt.timedelta(hours=2)) == (None, None)


@pytest.mark.parametrize("entry,sl", [(100, 100), (100, None), (None, 98)])
def test_без_расстояния_до_стопа_R_не_существует(entry, sl):
    b = bars([(101, 99), (102, 98)])
    assert excursion_R(b, side="buy", entry=entry, sl=sl, opened_utc=T0,
                       closed_utc=T0 + dt.timedelta(minutes=10)) == (None, None)


def test_время_строкой_из_журнала_разбирается():
    b = bars([(103, 99)])
    mfe, _ = excursion_R(b, side="buy", entry=100, sl=98,
                         opened_utc="2026-08-03T10:00:00+00:00",
                         closed_utc="2026-08-03T10:05:00Z")
    assert mfe == 1.5


def test_мусорные_времена_не_роняют_замер():
    b = bars([(103, 99)])
    assert excursion_R(b, side="buy", entry=100, sl=98, opened_utc="вчера",
                       closed_utc=T0) == (None, None)
    assert excursion_R(b, side="buy", entry=100, sl=98, opened_utc=T0,
                       closed_utc=T0) == (None, None)   # закрытие не позже входа


def test_гэп_мимо_входа_даёт_отрицательный_MFE_а_не_ноль():
    """Сделка, ни секунды не бывшая в плюсе, обязана выглядеть именно так."""
    b = bars([(99, 97), (98.5, 96)])
    mfe, mae = excursion_R(b, side="buy", entry=100, sl=95,
                           opened_utc=T0, closed_utc=T0 + dt.timedelta(minutes=10))
    assert mfe < 0 and mae < 0


def test_measure_не_падает_когда_рынок_недоступен():
    """Сорванный замер не должен мешать записи исхода закрытой сделки."""
    class Dead:
        def copy_rates(self, *a, **k):
            raise RuntimeError("MT5 недоступен")

    assert measure(Dead(), symbol="XAUUSD", side="buy", entry=100, sl=98,
                   opened_utc=T0, closed_utc=T0 + dt.timedelta(hours=1)) == (None, None)


def test_measure_на_живом_интерфейсе_возвращает_числа():
    class Market:
        def copy_rates(self, symbol, tf, count):
            return bars([(101, 99.5), (103, 100)])

    assert measure(Market(), symbol="XAUUSD", side="buy", entry=100, sl=98,
                   opened_utc=T0, closed_utc=T0 + dt.timedelta(minutes=10)) == (1.5, -0.25)
