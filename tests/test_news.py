"""Новостные окна (задача 5.1). Всё офлайн: XML-фикстура вместо сети.

Главная ловушка этого модуля — таймзона: ForexFactory отдаёт время в Eastern
Time, и наивное `.replace(tzinfo=utc)` сдвигает каждое событие на 4–5 часов.
Проверка `test_eastern_time_converted_to_utc` стоит первой не случайно: она
единственная отличает работающий модуль от такого, который блокирует не те часы
и потому «почти работает».
"""
import dataclasses
import datetime as dt
import json

import pytest

from trader_lib.config import load_config
from trader_lib.news import (
    load_windows,
    news_state,
    parse_ff_xml,
    symbol_currencies,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _cfg(tmp_path, **news_over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if news_over:
        cfg = dataclasses.replace(cfg, news=dataclasses.replace(cfg.news, **news_over))
    return cfg


def _xml(*events):
    """Фикстура в формате ff_calendar_thisweek.xml — с тегом <country>, как в
    НАСТОЯЩЕМ ответе ForexFactory. Тега <currency> там нет; фикстура, которая
    его подсовывала, скрыла реальный дефект (см. test_real_feed_shape)."""
    items = "".join(
        f"<event><title>{t}</title><country>{c}</country>"
        f"<date>{d}</date><time>{tm}</time><impact>{imp}</impact></event>"
        for t, c, d, tm, imp in events)
    return f"<?xml version='1.0'?><weeklyevents>{items}</weeklyevents>".encode("utf-8")


# Фрагмент НАСТОЯЩЕГО ответа ff_calendar_thisweek.xml (2026-07-26): CDATA,
# кодировка windows-1252, валюта в <country>, тега <currency> нет.
REAL_FEED = b"""<?xml version="1.0" encoding="windows-1252"?>
<weeklyevents>
\t<event>
\t\t<title>SPPI y/y</title>
\t\t<country>JPY</country>
\t\t<date><![CDATA[07-26-2026]]></date>
\t\t<time><![CDATA[11:50pm]]></time>
\t\t<impact><![CDATA[Low]]></impact>
\t\t<forecast><![CDATA[3.4%]]></forecast>
\t\t<previous><![CDATA[3.3%]]></previous>
\t</event>
\t<event>
\t\t<title>Federal Funds Rate</title>
\t\t<country>USD</country>
\t\t<date><![CDATA[07-29-2026]]></date>
\t\t<time><![CDATA[6:00pm]]></time>
\t\t<impact><![CDATA[High]]></impact>
\t</event>
</weeklyevents>"""


NFP = ("Non-Farm Employment Change", "USD", "07-27-2026", "8:30am", "High")
CPI = ("CPI m/m", "USD", "07-27-2026", "8:30am", "High")
ECB = ("ECB Press Conference", "EUR", "07-27-2026", "8:45am", "High")
RETAIL = ("Retail Sales m/m", "USD", "07-27-2026", "10:00am", "High")
MINOR = ("Housing Starts", "USD", "07-27-2026", "10:00am", "Low")


# --------------------------------------------------------------------------
# таймзона: главная ловушка
# --------------------------------------------------------------------------

def test_real_feed_shape(tmp_path):
    """РЕГРЕССИЯ НА НАСТОЯЩИЙ ФИД. Валюта в ForexFactory лежит в <country>, а
    тега <currency> нет вовсе. Парсер, читавший только <currency>, отдавал
    пустую валюту у КАЖДОГО события — и новостной гейт не блокировал ничего,
    выглядя при этом полностью исправным (события разобраны, окна построены,
    ошибок нет). Найдено первым живым обращением к фиду 2026-07-26.
    """
    events = parse_ff_xml(REAL_FEED)
    assert [e["currency"] for e in events] == ["JPY", "USD"]
    assert events[1]["impact"] == "high"
    # 6:00pm ET в июле (EDT) = 22:00 UTC
    assert events[1]["ts_utc"] == dt.datetime(2026, 7, 29, 22, 0, tzinfo=UTC)


def test_real_feed_actually_blocks_gold(tmp_path):
    """Сквозная проверка того же на уровне решения: USD-событие из настоящего
    фида обязано закрыть вход по золоту."""
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path),
                     now=dt.datetime(2026, 7, 29, 21, 30, tzinfo=UTC),
                     loader=lambda: REAL_FEED)
    st = news_state(w, now=dt.datetime(2026, 7, 29, 21, 30, tzinfo=UTC), symbol="XAUUSD")
    assert st["blocked"] is True and "Federal Funds Rate" in st["reason"]


def test_currency_tag_still_supported():
    """Некоторые зеркала отдают <currency>. Принимаем оба тега — но приоритет
    у непустого значения, а не у порядка."""
    xml = (b"<?xml version='1.0'?><weeklyevents><event><title>CPI</title>"
           b"<currency>USD</currency><date>07-27-2026</date><time>8:30am</time>"
           b"<impact>High</impact></event></weeklyevents>")
    assert parse_ff_xml(xml)[0]["currency"] == "USD"


def test_eastern_time_converted_to_utc():
    """8:30 ET в июле (EDT, UTC−4) = 12:30 UTC. Наивная трактовка дала бы
    08:30 UTC — блокировка на 4 часа раньше события."""
    events = parse_ff_xml(_xml(NFP))
    assert len(events) == 1
    assert events[0]["ts_utc"] == dt.datetime(2026, 7, 27, 12, 30, tzinfo=UTC)


def test_winter_time_offset_differs():
    """Зимой Eastern = UTC−5: смещение НЕ константа, поэтому zoneinfo, а не
    вычитание пяти часов руками."""
    winter = ("CPI m/m", "USD", "01-15-2026", "8:30am", "High")
    assert parse_ff_xml(_xml(winter))[0]["ts_utc"] == \
        dt.datetime(2026, 1, 15, 13, 30, tzinfo=UTC)


def test_event_without_time_is_skipped_not_guessed():
    """«Tentative»/«All Day» без времени: блокировать по выдуманному моменту
    хуже, чем не блокировать — событие попадает в список ambiguous, и модель
    решает сама."""
    tentative = ("Bank Holiday", "USD", "07-27-2026", "All Day", "High")
    events = parse_ff_xml(_xml(tentative))
    assert events[0]["ts_utc"] is None
    assert events[0]["time_known"] is False


# --------------------------------------------------------------------------
# окна двух уровней
# --------------------------------------------------------------------------

def test_event_without_time_is_surfaced_not_dropped(tmp_path):
    """Окна по выдуманному моменту нет — но и молча выбросить событие нельзя:
    «сегодня заседание, время не объявлено» модель обязана видеть и решать
    сама. Поэтому такие события уходят в ambiguous."""
    tentative = ("FOMC Statement", "USD", "07-27-2026", "Tentative", "High")
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(tentative))
    assert w["windows"] == [], "окна по неизвестному времени быть не должно"
    assert w["ambiguous"] == [{"title": "FOMC Statement", "currency": "USD"}]


def test_top_event_window_60_30(tmp_path):
    """NFP — из cfg.news.top_events: окно 60 минут до и 30 после."""
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(NFP))
    win = w["windows"][0]
    assert win["level"] == "top"
    assert win["from"] == dt.datetime(2026, 7, 27, 11, 30, tzinfo=UTC)
    assert win["to"] == dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)


def test_normal_window_30_15(tmp_path):
    """Обычное high-событие: 30 до, 15 после."""
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(RETAIL))
    win = w["windows"][0]
    assert win["level"] == "normal"
    assert win["from"] == dt.datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
    assert win["to"] == dt.datetime(2026, 7, 27, 14, 15, tzinfo=UTC)


def test_low_impact_events_make_no_window(tmp_path):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(MINOR))
    assert w["windows"] == []


# --------------------------------------------------------------------------
# соответствие символ ↔ валюта
# --------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,expected", [
    ("XAUUSD", {"XAU", "USD"}), ("EURUSD", {"EUR", "USD"}),
    ("USDJPY", {"USD", "JPY"}), ("BTCUSD", {"BTC", "USD"}),
    ("USTEC", {"USD"}), ("SP500", {"USD"}), ("BRENT", {"USD"}),
])
def test_symbol_currency_matching(symbol, expected):
    assert symbol_currencies(symbol) == expected


def test_unknown_symbol_currencies_unknown():
    """Не угадываем: неизвестный символ → None, и news_state трактует это
    консервативно (см. test_unknown_symbol_is_blocked)."""
    assert symbol_currencies("WTF") is None


def test_gold_blocked_by_usd_event(tmp_path):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(NFP))
    st = news_state(w, now=dt.datetime(2026, 7, 27, 12, 30, tzinfo=UTC), symbol="XAUUSD")
    assert st["blocked"] is True and "Non-Farm" in st["reason"]


def test_euro_event_does_not_block_gold(tmp_path):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(ECB))
    at = dt.datetime(2026, 7, 27, 12, 45, tzinfo=UTC)
    assert news_state(w, now=at, symbol="XAUUSD")["blocked"] is False
    assert news_state(w, now=at, symbol="EURUSD")["blocked"] is True


def test_unknown_symbol_is_blocked(tmp_path):
    """Валюты символа не определены — торговать вслепую в новостях нельзя."""
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(NFP))
    st = news_state(w, now=NOW, symbol="WTF")
    assert st["blocked"] is True and "валюты" in st["reason"]


# --------------------------------------------------------------------------
# границы окна
# --------------------------------------------------------------------------

@pytest.mark.parametrize("minute,blocked", [
    (11 * 60 + 29, False),   # за минуту до начала окна
    (11 * 60 + 30, True),    # ровно на границе — блокируем
    (12 * 60 + 30, True),    # момент события
    (13 * 60 + 0, True),     # ровно конец окна
    (13 * 60 + 1, False),    # минутой позже
])
def test_window_boundaries_inclusive(tmp_path, minute, blocked):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(NFP))
    at = dt.datetime(2026, 7, 27, 0, 0, tzinfo=UTC) + dt.timedelta(minutes=minute)
    assert news_state(w, now=at, symbol="XAUUSD")["blocked"] is blocked


# --------------------------------------------------------------------------
# обратный отсчёт для алерта
# --------------------------------------------------------------------------

def test_next_event_countdown_feeds_alert(tmp_path):
    """Модель ставит алерт news_soon по этому числу — оно обязано считаться до
    НАЧАЛА окна, а не до момента события: войти за 5 минут до начала окна
    значит войти в блокировку."""
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(NFP))
    st = news_state(w, now=dt.datetime(2026, 7, 27, 11, 0, tzinfo=UTC), symbol="XAUUSD")
    assert st["blocked"] is False
    assert st["next_event_in_min"] == 30
    assert st["next_event"]["title"].startswith("Non-Farm")


def test_no_upcoming_events_gives_none(tmp_path):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: _xml(NFP))
    st = news_state(w, now=dt.datetime(2026, 7, 28, 12, 0, tzinfo=UTC), symbol="XAUUSD")
    assert st["blocked"] is False and st["next_event_in_min"] is None


# --------------------------------------------------------------------------
# кэш и fail-closed
# --------------------------------------------------------------------------

def test_cache_written_and_reused(tmp_path):
    """Сеть дёргается один раз: датчик зовёт это раз в секунду."""
    calls = []

    def loader():
        calls.append(1)
        return _xml(NFP)

    p = tmp_path / "news_cache.json"
    load_windows(p, cfg=_cfg(tmp_path), now=NOW, loader=loader)
    w2 = load_windows(p, cfg=_cfg(tmp_path), now=NOW + dt.timedelta(minutes=10),
                      loader=loader)
    assert len(calls) == 1, "второй раз должен читаться кэш"
    assert w2["windows"] and w2["stale"] is False
    assert json.loads(p.read_text(encoding="utf-8"))["events"]


def test_stale_cache_fails_closed(tmp_path):
    """Кэш старше cache_max_age_hours, сеть недоступна → stale, и при
    fail_mode=halt_new любой символ заблокирован. Молча торговать по данным
    вчерашнего дня — худший исход: NFP не в кэше и не увиден."""
    p = tmp_path / "news_cache.json"
    load_windows(p, cfg=_cfg(tmp_path), now=NOW, loader=lambda: _xml(NFP))

    def dead():
        raise RuntimeError("нет сети")

    late = NOW + dt.timedelta(hours=30)
    w = load_windows(p, cfg=_cfg(tmp_path), now=late, loader=dead)
    assert w["stale"] is True
    st = news_state(w, now=late, symbol="XAUUSD")
    assert st["blocked"] is True and "устар" in st["reason"]


def test_stale_cache_allowed_when_configured(tmp_path):
    p = tmp_path / "news_cache.json"
    cfg = _cfg(tmp_path, fail_mode="allow")
    load_windows(p, cfg=cfg, now=NOW, loader=lambda: _xml(NFP))
    late = NOW + dt.timedelta(hours=30)
    w = load_windows(p, cfg=cfg, now=late, loader=lambda: (_ for _ in ()).throw(OSError()))
    assert news_state(w, now=late, symbol="XAUUSD")["blocked"] is False


def test_unknown_fail_mode_is_conservative(tmp_path):
    """Опечатка в конституции не имеет права открыть торговлю в новостях."""
    p = tmp_path / "news_cache.json"
    cfg = _cfg(tmp_path, fail_mode="как-нибудь")
    load_windows(p, cfg=cfg, now=NOW, loader=lambda: _xml(NFP))
    late = NOW + dt.timedelta(hours=30)
    w = load_windows(p, cfg=cfg, now=late, loader=lambda: (_ for _ in ()).throw(OSError()))
    assert news_state(w, now=late, symbol="XAUUSD")["blocked"] is True


def test_no_cache_and_no_network_is_stale(tmp_path):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: (_ for _ in ()).throw(RuntimeError("нет сети")))
    assert w["stale"] is True and w["windows"] == []
    assert news_state(w, now=NOW, symbol="XAUUSD")["blocked"] is True


def test_broken_xml_does_not_crash(tmp_path):
    w = load_windows(tmp_path / "news_cache.json", cfg=_cfg(tmp_path), now=NOW,
                     loader=lambda: b"<not xml")
    assert w["stale"] is True and w["windows"] == []


def test_corrupted_cache_is_refetched(tmp_path):
    p = tmp_path / "news_cache.json"
    p.write_text("{битый", encoding="utf-8")
    w = load_windows(p, cfg=_cfg(tmp_path), now=NOW, loader=lambda: _xml(NFP))
    assert w["windows"] and w["stale"] is False


def test_load_windows_force_refetches_even_when_cache_is_still_fresh(tmp_path):
    """РЕГРЕСС 2026-07-31: календарь протух ПОСРЕДИ торговой сессии и заблокировал
    живой вход (гейт: «календарь устарел и не обновился»).

    Кэш был взят накануне в 06:30:32 при лимите 24ч. Утренний brief.py прошёл в
    06:12 — кэшу было 23.7ч, то есть формально свежий, и сеть не дёргалась. Через
    18 минут кэш пересёк границу суток, а обновить его внутри дня стало нечем:
    гейт по сети принципиально не ходит, а цикл восприятия за день больше не
    вызывается. Утренняя подготовка обязана обновлять календарь БЕЗУСЛОВНО, иначе
    её результат зависит от того, на сколько минут она разминулась с границей.
    """
    cache = tmp_path / "news_cache.json"
    cfg = _cfg(tmp_path)
    calls = []

    def loader():
        calls.append(1)
        return _xml(NFP)

    # первый вызов наполняет кэш
    load_windows(cache, cfg=cfg, now=NOW, loader=loader)
    assert calls == [1]

    # кэш ещё свежий (прошло 23ч при лимите 24) — сеть не нужна
    later = NOW + dt.timedelta(hours=23)
    load_windows(cache, cfg=cfg, now=later, loader=loader)
    assert calls == [1], "без force свежий кэш не должен дёргать сеть"

    # force обязан обновить независимо от возраста
    load_windows(cache, cfg=cfg, now=later, loader=loader, force=True)
    assert calls == [1, 1], "force обязан обновить кэш независимо от его возраста"
