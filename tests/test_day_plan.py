"""План дня и открытое намерение (задача 6.2).

План дня — контракт, а не заметка. Из него растут три вещи: алерты, которые
разбудят модель; признак «плановый вход» в записи журнала; и разбор «план
против факта» вечером. Поэтому разбор строгий:

  * ГИПОТЕЗА БЕЗ РАЗБОРЧИВОГО АЛЕРТА — НЕ ГИПОТЕЗА. Условие, которое нельзя
    превратить в алерт, никого не разбудит: модель напишет красивый план и
    проспит его. Такие попадают в problems, а не в тихий пропуск.
  * НИЧЕГО НЕ ДОДУМЫВАЕТСЯ. Не указан уровень — алерта нет и в problems
    сказано почему; «примерно 2415» не подставляется.
  * НАМЕРЕНИЕ ЖИВЁТ В ФАЙЛЕ. Сессия прерывается, память не переживает
    перезапуск: то, что модель ведёт прямо сейчас, читается с диска.
"""
import datetime as dt

import pytest

from trader_lib.day_plan import (
    alerts_from_plan,
    hypothesis_by_id,
    is_planned,
    parse_day_plan,
    parse_open_intent,
    render_open_intent,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 7, 0, tzinfo=UTC)

PLAN_MD = """# План дня · 2026-07-27

Режим: тренд вверх на H1, ATR-перцентиль 0.6.

## H1 · XAUUSD · london-range-break-short
- **Условие:** возврат под 2415 после теста сверху, ATR-перцентиль < 0.8, вне окна новостей
- **Алерт:** price_below 2415 (id: h1-trigger)
- **Вход:** закрытие M15 под 2415
- **Стоп:** 2421 (за экстремум теста)
- **Цели:** TP1 1R ~50%, TP2 2R ~30%, остаток трейл — решаю по алерту
- **Убийца:** закрепление выше 2422 → гипотеза мертва на сегодня (алерт h1-dead)
- **Горизонт:** 120 мин (алерт pos-stall)

## H2 · EURUSD · trend-pullback-long
- **Условие:** откат к EMA20 M15 в тренде вверх
- **Алерт:** price_below 1.0850 (id: h2-trigger)
- **Вход:** пробой экстремума триггерной свечи
- **Стоп:** 1.0820
- **Цели:** TP1 1R, дальше по алерту
- **Убийца:** закрытие H1 ниже 1.0800 (алерт h2-dead)
- **Горизонт:** 240 мин
"""


def test_parse_hypotheses():
    plan = parse_day_plan(PLAN_MD)
    assert [h["id"] for h in plan["hypotheses"]] == ["H1", "H2"]
    h1 = plan["hypotheses"][0]
    assert h1["symbol"] == "XAUUSD"
    assert h1["setup_type"] == "london-range-break-short"
    assert "2415" in h1["condition"] and h1["stop"].startswith("2421")
    assert h1["horizon_minutes"] == 120
    assert plan["problems"] == []


def test_hypothesis_by_id():
    plan = parse_day_plan(PLAN_MD)
    assert hypothesis_by_id(plan, "H2")["symbol"] == "EURUSD"
    assert hypothesis_by_id(plan, "нет-такой") is None


def test_empty_plan_is_not_an_error():
    plan = parse_day_plan("# План дня\n\nСегодня не торгую: жду FOMC.\n")
    assert plan["hypotheses"] == [] and plan["problems"] == []


# --------------------------------------------------------------------------
# плановый / внеплановый вход
# --------------------------------------------------------------------------

def test_is_planned_matches():
    plan = parse_day_plan(PLAN_MD)
    ok, hid = is_planned(plan, symbol="XAUUSD", setup_type="london-range-break-short")
    assert ok is True and hid == "H1"


def test_is_planned_ignores_case_and_spaces():
    """Ярлык сетапа пишет модель руками — регистр и лишние пробелы не должны
    превращать плановый вход во внеплановый."""
    plan = parse_day_plan(PLAN_MD)
    ok, hid = is_planned(plan, symbol="xauusd", setup_type="  London-Range-Break-Short ")
    assert ok is True and hid == "H1"


def test_unplanned_flagged():
    plan = parse_day_plan(PLAN_MD)
    assert is_planned(plan, symbol="XAUUSD", setup_type="ny-reversal") == (False, None)
    assert is_planned(plan, symbol="GBPUSD",
                      setup_type="london-range-break-short") == (False, None)


# --------------------------------------------------------------------------
# алерты из плана
# --------------------------------------------------------------------------

def test_alerts_from_plan_generates_ids():
    alerts = alerts_from_plan(parse_day_plan(PLAN_MD))
    by_id = {a["id"]: a for a in alerts}
    assert {"h1-trigger", "h1-dead", "h2-trigger", "h2-dead"} <= set(by_id)

    trigger = by_id["h1-trigger"]
    assert trigger["type"] == "price_below" and trigger["level"] == 2415.0
    assert trigger["symbol"] == "XAUUSD" and trigger["hypothesis_id"] == "H1"

    dead = by_id["h1-dead"]
    assert dead["type"] == "price_above" and dead["level"] == 2422.0
    assert dead["priority"] == "critical", "гипотеза умерла — это важнее обычного"


def test_alerts_keep_symbol_of_their_hypothesis():
    alerts = alerts_from_plan(parse_day_plan(PLAN_MD))
    assert {a["symbol"] for a in alerts if a["hypothesis_id"] == "H2"} == {"EURUSD"}


def test_unparseable_alert_goes_to_problems_not_silence():
    """Условие, которое нельзя превратить в алерт, никого не разбудит: модель
    напишет план и проспит его. Молчаливый пропуск здесь — худший исход."""
    md = """## H9 · XAUUSD · нечто
- **Условие:** когда станет понятно
- **Алерт:** посмотреть глазами
- **Стоп:** 2400
"""
    plan = parse_day_plan(md)
    assert plan["hypotheses"][0]["alert_spec"] == "посмотреть глазами"
    assert any("H9" in p for p in plan["problems"])
    assert alerts_from_plan(plan) == []


def test_missing_level_is_not_guessed():
    md = """## H8 · XAUUSD · нечто
- **Условие:** возврат под уровень
- **Алерт:** price_below (id: h8-trigger)
- **Стоп:** 2421
"""
    plan = parse_day_plan(md)
    assert alerts_from_plan(plan) == []
    assert any("уровень" in p or "level" in p for p in plan["problems"])


def test_horizon_is_kept_but_not_armed_before_entry():
    """Горизонт гипотезы — про открытую позицию, а типы position_* требуют
    тикет. На момент планирования позиции нет, поэтому алерт не выпускается:
    датчик молча пропустил бы условие без тикета. Горизонт хранится в
    гипотезе и превращается в алерт при входе (scripts/enter.py)."""
    plan = parse_day_plan(PLAN_MD)
    assert hypothesis_by_id(plan, "H1")["horizon_minutes"] == 120
    alerts = alerts_from_plan(plan)
    assert all("ticket" not in a for a in alerts)
    assert not [a for a in alerts if a["type"].startswith("position_")]


def test_alerts_are_valid_for_the_watcher():
    """Черновик обязан проходить контракт alerts.json — иначе датчик его
    отвергнет, и план снова никого не разбудит."""
    from trader_lib.alerts import ALERT_TYPES

    for a in alerts_from_plan(parse_day_plan(PLAN_MD)):
        assert a["type"] in ALERT_TYPES, a
        assert a["id"] and a.get("symbol")


# --------------------------------------------------------------------------
# открытое намерение
# --------------------------------------------------------------------------

def test_intent_roundtrip():
    """Намерение пишется и читается с диска: сессия прерывается, память не
    переживает перезапуск."""
    intent = {"hypothesis_id": "H1", "symbol": "XAUUSD", "side": "sell",
              "state": "жду триггер", "entry": 2415.0, "sl": 2421.0,
              "note": "не входить до закрытия M15", "updated_utc": NOW.isoformat()}
    md = render_open_intent(intent)
    back = parse_open_intent(md)
    for key in ("hypothesis_id", "symbol", "side", "state", "note"):
        assert back[key] == intent[key]
    assert back["entry"] == pytest.approx(2415.0)
    assert back["sl"] == pytest.approx(2421.0)


def test_empty_intent_is_none():
    assert parse_open_intent("") is None
    assert parse_open_intent("## Открытое намерение\n\nНет.\n")["state"] is None


def test_intent_text_survives_free_form_note():
    md = render_open_intent({"hypothesis_id": "H2", "symbol": "EURUSD",
                             "side": "buy", "state": "в позиции",
                             "note": "стоп в безубытке; веду до 1.0900"})
    back = parse_open_intent(md)
    assert "1.0900" in back["note"] and back["state"] == "в позиции"


def test_killer_survives_a_wrapped_line():
    """РЕГРЕСС 2026-08-01, найден трейдером-субагентом на прогоне команды.

    Поле «Убийца» читалось только до конца первой строки. Модель, перенёсшая
    хвост на вторую (что естественно при длинной формулировке), теряла
    `(алерт <id>)` — и гипотеза оставалась БЕЗ условия отмены. Отказ тихий:
    problems пуст, план выглядит разобранным, а убийцы в alerts.json просто
    нет. Именно убийца снимает мёртвую гипотезу со стола, так что его потеря
    оставляет модель торговать по тезису, который уже опровергнут.
    """
    from trader_lib.day_plan import alerts_from_plan, parse_day_plan

    base = ("# План\n"
            "## H1 · XAUUSD · test\n"
            "- **Условие:** пробой\n"
            "- **Алерт:** price_below 4018 (id: tr-down)\n"
            "- **Стоп:** 4058\n")
    tail = "- **Горизонт:** 120 мин\n"

    one = base + "- **Убийца:** закрепление выше 4080 (алерт tr-down-dead)\n" + tail
    wrapped = base + ("- **Убийца:** закрепление выше 4080, разворот структуры\n"
                      "  (алерт tr-down-dead)\n") + tail

    assert [a["id"] for a in alerts_from_plan(parse_day_plan(one))] == \
        ["tr-down", "tr-down-dead"]
    assert [a["id"] for a in alerts_from_plan(parse_day_plan(wrapped))] == \
        ["tr-down", "tr-down-dead"], "перенос строки не должен терять убийцу"


def test_wrapped_continuation_joins_any_field():
    """Перенос — свойство markdown, а не одного поля: условие и цели пишутся
    длинно так же часто."""
    from trader_lib.day_plan import parse_day_plan

    plan = parse_day_plan(
        "# План\n"
        "## H1 · XAUUSD · test\n"
        "- **Условие:** возврат под уровень\n"
        "  после теста сверху\n"
        "- **Алерт:** price_below 4018 (id: tr-down)\n"
        "- **Стоп:** 4058\n"
        "- **Горизонт:** 120 мин\n")
    assert "после теста сверху" in plan["hypotheses"][0]["condition"]
