"""Спред-гейт и авто-исключение инструмента (задача 5.2).

Смысл модуля: спред — единственная издержка, которая меняется на порядок за
секунды и делает нормальную сделку заведомо убыточной. Медиана считается по
истории и хранится на диске, потому что «нормально» у каждого инструмента и
брокера своё, а сравнивать не с чем в момент, когда спред уже разъехался.

Ключевая развилка здесь — ГИСТЕРЕЗИС. Инструмент исключается при выходе за
порог и возвращается только когда спред пришёл К МЕДИАНЕ, а не когда он
опустился чуть ниже порога: иначе на границе он мигал бы вход-выход каждую
секунду, и решения модели зависели бы от того, в какую секунду она посмотрела.
"""
import dataclasses
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket
from trader_lib.spread_gate import (
    load_medians,
    spread_state,
    update_medians,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)


def _cfg(tmp_path, **instr):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if instr:
        cfg = dataclasses.replace(cfg, instruments=dataclasses.replace(
            cfg.instruments, **instr))
    return cfg


def _bars(n=1500, *, spread=20, spread_empty=False):
    c = 2400.0 + np.arange(n) * 0.01
    sp = np.zeros(n) if spread_empty else np.full(n, spread, dtype=float)
    return pd.DataFrame({"time": pd.date_range("2026-07-20", periods=n, freq="5min"),
                         "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                         "tick_volume": 200, "spread": sp})


class Market(FakeMarket):
    """Спред можно менять между вызовами — как на живом рынке."""

    def __init__(self, *, current_spread=20, bars=None, **kw):
        super().__init__(bars=bars if bars is not None else _bars(), **kw)
        self.current_spread = current_spread
        self.rate_calls = 0

    def symbol_info(self, symbol):
        return {**super().symbol_info(symbol), "spread": self.current_spread}

    def copy_rates(self, symbol, timeframe, count):
        self.rate_calls += 1
        return super().copy_rates(symbol, timeframe, count)


def _medians(tmp_path, market=None, cfg=None, now=NOW, **kw):
    return update_medians(market or Market(), cfg or _cfg(tmp_path),
                          tmp_path / "spread_median.json", now=now, **kw)


# --------------------------------------------------------------------------
# медиана: расчёт, хранение, переиспользование
# --------------------------------------------------------------------------

def test_median_computed_from_bars(tmp_path):
    doc = _medians(tmp_path, Market(bars=_bars(spread=17)))
    assert doc["medians"]["XAUUSD"] == 17.0
    assert doc["source"]["XAUUSD"] == "bars"


def test_median_persisted_and_reused(tmp_path):
    """Пересчёт раз в сутки: датчик и гейт зовут это часто, а 1500 баров по
    семи символам — не та цена, которую стоит платить каждую секунду."""
    m = Market()
    p = tmp_path / "spread_median.json"
    _medians(tmp_path, m)
    calls_after_first = m.rate_calls
    assert calls_after_first > 0

    doc = update_medians(m, _cfg(tmp_path), p, now=NOW + dt.timedelta(hours=6))
    assert m.rate_calls == calls_after_first, "в течение суток пересчёта нет"
    assert doc["medians"]["XAUUSD"] == 20.0
    assert load_medians(p)["medians"]["XAUUSD"] == 20.0

    update_medians(m, _cfg(tmp_path), p, now=NOW + dt.timedelta(hours=30))
    assert m.rate_calls > calls_after_first, "через сутки медиана пересчитывается"


def test_bars_without_spread_fall_back_to_samples(tmp_path):
    """У части брокеров поле spread в барах пустое (это ловит bootstrap_env).
    Тогда медиана набирается ежедневными замерами текущего спреда."""
    p = tmp_path / "spread_median.json"
    m = Market(bars=_bars(spread_empty=True), current_spread=25)
    doc = update_medians(m, _cfg(tmp_path), p, now=NOW)
    assert doc["medians"]["XAUUSD"] is None, "одного замера мало для медианы"
    assert doc["source"]["XAUUSD"] == "samples"

    for i, sp in enumerate((25, 31, 27), start=1):
        m.current_spread = sp
        doc = update_medians(m, _cfg(tmp_path), p, now=NOW + dt.timedelta(days=i))
    # выборка [25 (первый день), 25, 31, 27] → медиана 26
    assert doc["medians"]["XAUUSD"] == 26.0, "медиана по накопленным замерам"
    assert doc["samples"]["XAUUSD"] == [25.0, 25.0, 31.0, 27.0]


def test_missing_file_is_not_an_error(tmp_path):
    assert load_medians(tmp_path / "нет.json") == {"medians": {}, "excluded": {},
                                                   "samples": {}, "source": {}}


def test_corrupted_file_recomputed(tmp_path):
    p = tmp_path / "spread_median.json"
    p.write_text("{битый", encoding="utf-8")
    doc = _medians(tmp_path)
    assert doc["medians"]["XAUUSD"] == 20.0


# --------------------------------------------------------------------------
# проверка входа
# --------------------------------------------------------------------------

def test_normal_spread_allowed(tmp_path):
    doc = _medians(tmp_path)
    st = spread_state(Market(current_spread=22), _cfg(tmp_path), doc,
                      symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True and st["ratio"] == pytest.approx(1.1)


def test_blocks_above_1_5x_median(tmp_path):
    doc = _medians(tmp_path)
    st = spread_state(Market(current_spread=31), _cfg(tmp_path), doc,
                      symbol="XAUUSD", now=NOW)
    assert st["allowed"] is False
    assert st["ratio"] == pytest.approx(1.55) and "спред" in st["reason"]


def test_exactly_at_threshold_allowed(tmp_path):
    """Ровно порог — ещё не аномалия: блокирует превышение, а не достижение."""
    doc = _medians(tmp_path)
    st = spread_state(Market(current_spread=30), _cfg(tmp_path), doc,
                      symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True and st["ratio"] == pytest.approx(1.5)


def test_not_in_whitelist_denied(tmp_path):
    """Инструмент вне белого списка не торгуется вообще — это решение
    конституции, а не спреда."""
    doc = _medians(tmp_path)
    st = spread_state(Market(), _cfg(tmp_path), doc, symbol="BTCUSD", now=NOW)
    assert st["allowed"] is False and "whitelist" in st["reason"]


def test_unknown_median_allows_with_flag(tmp_path):
    """Медианы ещё нет (первые дни). Блокировать всё до её накопления значит
    запретить ровно ту торговлю, которая её накапливает; реальная защита от
    дорогого входа — costs_R в scripts/enter.py, он считает по ФАКТИЧЕСКОМУ
    спреду. Поэтому здесь разрешено, но с явным флагом в записи журнала."""
    st = spread_state(Market(current_spread=99), _cfg(tmp_path),
                      {"medians": {}, "excluded": {}, "samples": {}, "source": {}},
                      symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True and st["median_unknown"] is True
    assert st["median"] is None


# --------------------------------------------------------------------------
# авто-исключение и гистерезис
# --------------------------------------------------------------------------

def test_instrument_excluded_until_normalized(tmp_path):
    doc = _medians(tmp_path)
    cfg = _cfg(tmp_path)

    st = spread_state(Market(current_spread=40), cfg, doc, symbol="XAUUSD", now=NOW)
    assert st["allowed"] is False and st["excluded"] is True
    assert "XAUUSD" in doc["excluded"]

    # спред опустился ниже ПОРОГА, но ещё выше медианы — гистерезис держит
    later = NOW + dt.timedelta(minutes=5)
    st = spread_state(Market(current_spread=26), cfg, doc, symbol="XAUUSD", now=later)
    assert st["allowed"] is False and st["excluded"] is True
    assert "нормализ" in st["reason"]

    # вернулся к медиане — исключение снято
    st = spread_state(Market(current_spread=20), cfg, doc, symbol="XAUUSD",
                      now=later + dt.timedelta(minutes=5))
    assert st["allowed"] is True and st["excluded"] is False
    assert "XAUUSD" not in doc["excluded"]


def test_exclusion_survives_restart(tmp_path):
    """Исключение живёт в файле: перезапуск процесса не возвращает инструмент
    в торговлю, пока спред не нормализовался."""
    p = tmp_path / "spread_median.json"
    cfg = _cfg(tmp_path)
    doc = _medians(tmp_path)
    spread_state(Market(current_spread=40), cfg, doc, symbol="XAUUSD", now=NOW,
                 path=p)

    reloaded = load_medians(p)
    assert "XAUUSD" in reloaded["excluded"]
    st = spread_state(Market(current_spread=26), cfg, reloaded, symbol="XAUUSD",
                      now=NOW + dt.timedelta(minutes=1))
    assert st["allowed"] is False


def test_exclusion_records_when_and_why(tmp_path):
    doc = _medians(tmp_path)
    spread_state(Market(current_spread=40), _cfg(tmp_path), doc, symbol="XAUUSD", now=NOW)
    ex = doc["excluded"]["XAUUSD"]
    assert ex["since"] == NOW.isoformat()
    assert ex["ratio"] == pytest.approx(2.0) and ex["median"] == 20.0


def test_other_symbols_not_affected(tmp_path):
    """Исключение адресное. Спред второго символа берётся ПОВЫШЕННЫМ, но ниже
    порога (×1.3): на спреде ровно по медиане проверка слепа — символ прошёл бы
    и через ветку возврата из исключения, то есть тест не отличал бы адресное
    исключение от глобального."""
    doc = _medians(tmp_path)
    cfg = _cfg(tmp_path)
    spread_state(Market(current_spread=40), cfg, doc, symbol="XAUUSD", now=NOW)
    st = spread_state(Market(current_spread=26), cfg, doc, symbol="EURUSD", now=NOW)
    assert st["allowed"] is True and st["ratio"] == pytest.approx(1.3)
    assert list(doc["excluded"]) == ["XAUUSD"]


# --------------------------------------------------------------------------
# ЖИВАЯ МЕДИАНА (Ф1). Барная медиана меряет спред на ЗАКРЫТИИ свечи — то есть
# в самый спокойный момент, — а решения принимаются в активные. За неделю
# 2026-07-27..31 это дало 9 отклонённых входов, 6 из них при ×1.05 (20 против
# медианы 19, разница в один пункт), и обе упущенные прибыльные сделки
# (MFE 3.99 и 4.89 ATR) заблокированы именно так. Чиним не порог, а базу.
# --------------------------------------------------------------------------

def test_live_window_median_over_rolling_hour():
    """Медиана считается по объединению поминутных корзин за час."""
    from trader_lib.spread_gate import LiveSpreadWindow

    w = LiveSpreadWindow(minutes=60)
    t = NOW
    for points in (19, 20, 20, 21, 60):
        w.observe("XAUUSD", points, now=t)
    assert w.median("XAUUSD", now=NOW) == 20.0
    assert w.samples("XAUUSD", now=NOW) == 5


def test_live_window_forgets_minutes_older_than_the_window():
    """Час назад спред был другим — он не должен тянуть медиану сегодня."""
    from trader_lib.spread_gate import LiveSpreadWindow

    w = LiveSpreadWindow(minutes=60)
    for _ in range(100):
        w.observe("XAUUSD", 19, now=NOW)
    assert w.median("XAUUSD", now=NOW) == 19.0

    later = NOW + dt.timedelta(minutes=61)
    for _ in range(3):
        w.observe("XAUUSD", 40, now=later)
    assert w.median("XAUUSD", now=later) == 40.0, "старые корзины обязаны выпасть из окна"
    assert w.samples("XAUUSD", now=later) == 3


def test_live_window_symbols_are_independent():
    from trader_lib.spread_gate import LiveSpreadWindow

    w = LiveSpreadWindow(minutes=60)
    w.observe("XAUUSD", 20, now=NOW)
    w.observe("BTCUSD", 1459, now=NOW)
    assert w.median("XAUUSD", now=NOW) == 20.0
    assert w.median("BTCUSD", now=NOW) == 1459.0
    assert w.median("EURUSD", now=NOW) is None


def test_live_window_survives_restart_through_disk(tmp_path):
    """Датчик перезапускается — накопленное окно не должно обнуляться."""
    from trader_lib.spread_gate import LiveSpreadWindow

    w = LiveSpreadWindow(minutes=60)
    for points in (19, 20, 21):
        w.observe("XAUUSD", points, now=NOW)
    path = tmp_path / "spread_live.json"
    w.save(path)

    restored = LiveSpreadWindow.load(path, minutes=60)
    assert restored.median("XAUUSD", now=NOW) == 20.0
    assert restored.samples("XAUUSD", now=NOW) == 3


def test_live_window_ignores_nonpositive_and_missing():
    """Закрытый рынок отдаёт ноль/None — такие замеры не должны портить базу."""
    from trader_lib.spread_gate import LiveSpreadWindow

    w = LiveSpreadWindow(minutes=60)
    w.observe("XAUUSD", 0, now=NOW)
    w.observe("XAUUSD", None, now=NOW)
    w.observe("XAUUSD", -5, now=NOW)
    assert w.median("XAUUSD", now=NOW) is None
    assert w.samples("XAUUSD", now=NOW) == 0


def test_live_median_unblocks_the_entries_that_bar_median_killed(tmp_path):
    """ГЛАВНАЯ ЛОВУШКА Ф1, воспроизводит боевой отказ 2026-07-28..31.

    Барная медиана 19 (спред на закрытии свечи — тихий момент). Живой спред в
    момент решения — 20, и так шесть раз подряд из шести независимых попыток.
    Инструмент сидит в исключении, порог возврата ratio ≤ 1.0 недостижим: чтобы
    его взять, живой спред должен опуститься до уровня тихого закрытия бара.
    Обе упущенные прибыльные сделки недели (MFE 3.99 и 4.89 ATR) умерли здесь.

    Барная и живая медианы здесь РАЗНЫЕ (19 против 20) — иначе тест не
    различал бы, какая из них применилась.
    """
    from trader_lib.spread_gate import LiveSpreadWindow

    cfg = _cfg(tmp_path)
    quiet_bars = Market(bars=_bars(spread=19))          # барная медиана 19
    doc = _medians(tmp_path, market=quiet_bars)
    assert doc["medians"]["XAUUSD"] == 19.0
    doc["excluded"]["XAUUSD"] = {"since": NOW.isoformat(), "ratio": 3.0,
                                 "median": 19.0, "spread_points": 57.0}

    live = LiveSpreadWindow(minutes=60)
    for _ in range(120):                                # живая база: типично 20
        live.observe("XAUUSD", 20, now=NOW)

    ok = spread_state(Market(current_spread=20), cfg, doc, symbol="XAUUSD",
                      now=NOW, live=live)
    assert ok["median"] == 20.0, "база обязана быть живой, а не барной"
    assert ok["allowed"] is True, ok["reason"]

    doc["excluded"]["XAUUSD"] = {"since": NOW.isoformat(), "ratio": 3.0,
                                 "median": 20.0, "spread_points": 61.0}
    spike = spread_state(Market(current_spread=61), cfg, doc, symbol="XAUUSD",
                         now=NOW, live=live)
    assert spike["allowed"] is False, "настоящий выброс обязан отклоняться"


def test_live_median_falls_back_to_bars_while_samples_are_few(tmp_path):
    """Первые минуты после старта датчика: живых замеров почти нет. Считать
    медиану по трём тикам — выдумывать базу, поэтому работает барная."""
    from trader_lib.spread_gate import LiveSpreadWindow

    live = LiveSpreadWindow(minutes=60)
    for _ in range(3):
        live.observe("XAUUSD", 45, now=NOW)

    doc = _medians(tmp_path, market=Market(bars=_bars(spread=19)))
    st = spread_state(Market(current_spread=20), _cfg(tmp_path), doc,
                      symbol="XAUUSD", now=NOW, live=live)
    assert st["median"] == 19.0, "мало замеров — база остаётся барной"


def test_exclusion_returns_at_1_1_not_only_at_exactly_median(tmp_path):
    """Порог возврата ≤ 1.0 требовал попасть в медиану ровно — при дискретности
    в один пункт это давало систематический промах: 2026-07-28..31 шесть
    попыток подряд показали 20 против медианы 19 и все шесть отклонены.
    Запас 1.1 сохраняет гистерезис (порог аномалии ×1.5 далеко)."""
    cfg = _cfg(tmp_path)
    doc = _medians(tmp_path, market=Market(bars=_bars(spread=19)))
    doc["excluded"]["XAUUSD"] = {"since": NOW.isoformat(), "ratio": 3.0,
                                 "median": 19.0, "spread_points": 57.0}
    st = spread_state(Market(current_spread=20), cfg, doc, symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True, "боевой случай: 20 против 19 обязан пройти"
    assert st["ratio"] == pytest.approx(20 / 19)


def test_exclusion_still_holds_above_return_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    doc = _medians(tmp_path, market=Market(bars=_bars(spread=19)))
    doc["excluded"]["XAUUSD"] = {"since": NOW.isoformat(), "ratio": 3.0,
                                 "median": 19.0, "spread_points": 57.0}
    st = spread_state(Market(current_spread=25), cfg, doc, symbol="XAUUSD", now=NOW)
    assert st["allowed"] is False and st["excluded"] is True


def test_замолчавший_символ_не_считается_живым():
    """БАГ, найденный трейдером `fade` 2026-08-03 на первом живом дне.

    Окно мерилось от последнего замера ЭТОГО ЖЕ символа, а не от «сейчас».
    Пока символ наблюдается — разницы нет; стоит ему замолчать, его старые
    замеры остаются свежими относительно самих себя НАВСЕГДА.

    Практически: датчик опрашивает только символы из чьих-то алертов, EURUSD не
    был упомянут ни у кого — и гейт сравнивал бы сегодняшний вход с пятничной
    базой, молча и правдоподобно (samples рапортует полную выборку, median —
    уверенное число).
    """
    from trader_lib.spread_gate import LiveSpreadWindow

    w = LiveSpreadWindow(minutes=60)
    for _ in range(100):
        w.observe("EURUSD", 19, now=NOW)          # пятница, широкий спред
    assert w.median("EURUSD", now=NOW) == 19.0    # тогда — законное число

    спустя_двое_суток = NOW + dt.timedelta(days=2)
    assert w.median("EURUSD", now=спустя_двое_суток) is None,         "протухшие замеры обязаны выпасть из окна, а не считаться живыми"
    assert w.samples("EURUSD", now=спустя_двое_суток) == 0,         "иначе выборка проходит MIN_LIVE_SAMPLES и вытесняет барную медиану"


def test_протухшее_живое_окно_возвращает_решение_барной_медиане(tmp_path):
    """Полный тракт: замолчавший символ не должен уводить гейт от барной базы.

    Барная медиана хотя бы имеет дату; протухшее живое окно выглядит свежим и
    именно поэтому опаснее отсутствия данных.
    """
    import json

    from trader_lib.spread_gate import LiveSpreadWindow, spread_state

    w = LiveSpreadWindow(minutes=60)
    for _ in range(200):
        w.observe("EURUSD", 40, now=NOW)          # старая широкая база

    path = tmp_path / "spread_median.json"
    path.write_text(json.dumps({"medians": {"EURUSD": 12.0}}), encoding="utf-8")
    doc = json.loads(path.read_text(encoding="utf-8"))

    market = Market(current_spread=13)
    поздно = NOW + dt.timedelta(days=2)
    st = spread_state(market, _cfg(tmp_path), doc, symbol="EURUSD", now=поздно,
                      path=path, live=w)
    # против протухшей живой базы (40) отношение вышло бы 0.33 — то есть
    # «спред необычно узкий, входи смело» там, где он на самом деле нормальный
    assert st["median"] == 12.0, "решение обязано вернуться барной медиане"
    assert round(st["ratio"], 3) == round(13 / 12.0, 3)
