"""Мандаты директора (Ф4).

ЧТО ЭТО. Директор раздаёт на день: какой трейдер какие инструменты торгует и
какую долю дневного бюджета риска может израсходовать. Файл читает ГЕЙТ в
момент входа — не потому что так стройнее, а потому что иначе мандат остаётся
пожеланием. Окно входа схлопывается за секунды, спрашивать директора в этот
момент нельзя; его решение обязано быть уже скомпилировано в файл.

ГЛАВНОЕ СВОЙСТВО: аллокация умеет только ОГРАНИЧИВАТЬ. Директор не может
выдать риск больше того, что разрешила конституция, — только меньше. Иначе
оркестратор становился бы способом обойти лимиты, ради которых он и построен.

ОБРАТНАЯ СОВМЕСТИМОСТЬ. Нет файла или нет трейдера (одиночный режим) — работа
идёт как всю неделю. Команда не имеет права ломать одиночку.
"""
import datetime as dt
import json

import pytest

from trader_lib.allocation import (
    load_allocation,
    mandate_state,
    risk_cap_usd,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 3, 8, 0, tzinfo=UTC)

DOC = {
    "server_day": "2026-08-03",
    "written_by": "claude-opus-5",
    "written_utc": NOW.isoformat(),
    "traders": {
        "trend": {"instruments": ["XAUUSD"], "risk_share": 0.4, "active": True},
        "fade": {"instruments": ["EURUSD", "GBPUSD"], "risk_share": 0.35,
                 "active": True},
        "range": {"instruments": ["USDJPY"], "risk_share": 0.25, "active": False},
    },
}


def _write(tmp_path, doc=None):
    (tmp_path / "allocation.json").write_text(
        json.dumps(doc if doc is not None else DOC, ensure_ascii=False),
        encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# мандат: что торгуешь и торгуешь ли вообще
# --------------------------------------------------------------------------

def test_symbol_inside_the_mandate_is_allowed(tmp_path):
    st = mandate_state(load_allocation(_write(tmp_path) / "allocation.json"),
                       trader="trend", symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True


def test_symbol_outside_the_mandate_is_denied(tmp_path):
    """Прямой ответ на страх скучивания: трейдер не может уйти на чужой
    инструмент, даже если там «явное движение». Проверка в коде, а не
    указание директора — указания нарушаются, проверки нет."""
    st = mandate_state(load_allocation(_write(tmp_path) / "allocation.json"),
                       trader="trend", symbol="EURUSD", now=NOW)
    assert st["allowed"] is False
    assert "мандат" in st["reason"]


def test_benched_trader_cannot_enter(tmp_path):
    """Директор вправе снять трейдера с торговли на день (его режима сегодня
    нет). Снятие обязано быть исполнимым, а не рекомендательным."""
    st = mandate_state(load_allocation(_write(tmp_path) / "allocation.json"),
                       trader="range", symbol="USDJPY", now=NOW)
    assert st["allowed"] is False
    assert "не активен" in st["reason"]


def test_unknown_trader_is_denied(tmp_path):
    """Трейдера нет в аллокации — значит директор его сегодня не заводил.
    Пропустить значило бы позволить неучтённому участнику тратить общий риск."""
    st = mandate_state(load_allocation(_write(tmp_path) / "allocation.json"),
                       trader="ghost", symbol="XAUUSD", now=NOW)
    assert st["allowed"] is False


def test_solo_mode_is_untouched(tmp_path):
    """Одиночный режим: трейдера нет — мандат не проверяется."""
    st = mandate_state(load_allocation(_write(tmp_path) / "allocation.json"),
                       trader=None, symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True


def test_missing_file_does_not_block_trading(tmp_path):
    """Аллокации ещё нет (директор не отработал) — торговлю это не
    останавливает: риск в этот момент держат остальные проверки гейта."""
    st = mandate_state(load_allocation(tmp_path / "нет.json"),
                       trader="trend", symbol="XAUUSD", now=NOW)
    assert st["allowed"] is True
    assert "аллокаци" in st["reason"]


def test_stale_allocation_is_refused(tmp_path):
    """Вчерашние мандаты сегодня недействительны: инструменты назначались под
    вчерашнюю структуру рынка. Работать по ним — то же самое, что торговать по
    вчерашнему календарю новостей."""
    doc = dict(DOC, server_day="2026-07-31")
    st = mandate_state(load_allocation(_write(tmp_path, doc) / "allocation.json"),
                       trader="trend", symbol="XAUUSD", now=NOW,
                       server_day="2026-08-03")
    assert st["allowed"] is False
    assert "устарел" in st["reason"]


# --------------------------------------------------------------------------
# доля риска: только вниз от конституции
# --------------------------------------------------------------------------

def test_share_caps_the_constitutional_maximum(tmp_path):
    alloc = load_allocation(_write(tmp_path) / "allocation.json")
    cap = risk_cap_usd(alloc, trader="trend", constitution_max=471.83,
                       daily_budget=1000.0, spent_today=0.0)
    assert cap == pytest.approx(400.0)          # 0.4 × 1000


def test_share_can_never_raise_the_constitutional_maximum(tmp_path):
    """ГЛАВНЫЙ ИНВАРИАНТ. Директор с долей 0.9 от бюджета 1000 не имеет права
    выдать 900, если конституция разрешает 471.83. Оркестратор, способный
    повысить лимит, — способ обойти защиту, ради которой он и построен."""
    doc = {"server_day": "2026-08-03",
           "traders": {"trend": {"instruments": ["XAUUSD"], "risk_share": 0.9,
                                 "active": True}}}
    alloc = load_allocation(_write(tmp_path, doc) / "allocation.json")
    cap = risk_cap_usd(alloc, trader="trend", constitution_max=471.83,
                       daily_budget=1000.0, spent_today=0.0)
    assert cap == pytest.approx(471.83)


def test_spent_today_reduces_what_is_left(tmp_path):
    alloc = load_allocation(_write(tmp_path) / "allocation.json")
    cap = risk_cap_usd(alloc, trader="trend", constitution_max=471.83,
                       daily_budget=1000.0, spent_today=250.0)
    assert cap == pytest.approx(150.0)          # 400 − 250


def test_exhausted_share_gives_zero_not_negative(tmp_path):
    alloc = load_allocation(_write(tmp_path) / "allocation.json")
    cap = risk_cap_usd(alloc, trader="trend", constitution_max=471.83,
                       daily_budget=1000.0, spent_today=600.0)
    assert cap == 0.0


def test_solo_mode_keeps_the_constitutional_maximum(tmp_path):
    alloc = load_allocation(_write(tmp_path) / "allocation.json")
    cap = risk_cap_usd(alloc, trader=None, constitution_max=471.83,
                       daily_budget=1000.0, spent_today=0.0)
    assert cap == pytest.approx(471.83)


def test_shares_summing_above_one_are_refused(tmp_path):
    """Сумма долей больше единицы означала бы, что команда вправе израсходовать
    больше дневного бюджета счёта. Такой файл — ошибка директора, и читать его
    как рабочий нельзя."""
    doc = {"server_day": "2026-08-03",
           "traders": {"a": {"instruments": ["XAUUSD"], "risk_share": 0.7,
                             "active": True},
                       "b": {"instruments": ["EURUSD"], "risk_share": 0.6,
                             "active": True}}}
    alloc = load_allocation(_write(tmp_path, doc) / "allocation.json")
    assert alloc["valid"] is False
    st = mandate_state(alloc, trader="a", symbol="XAUUSD", now=NOW)
    assert st["allowed"] is False
    assert "сумма долей" in st["reason"]


# --------------------------------------------------------------------------
# квота событий (Ф5): один трейдер не должен выесть бюджет команды
# --------------------------------------------------------------------------

def test_explicit_quota_is_respected(tmp_path):
    from trader_lib.allocation import events_quota

    doc = {"server_day": "2026-08-03",
           "traders": {"trend": {"instruments": ["XAUUSD"], "risk_share": 0.4,
                                 "active": True, "events_quota": 14}}}
    alloc = load_allocation(_write(tmp_path, doc) / "allocation.json")
    assert events_quota(alloc, "trend", total=40) == 14


def test_default_quota_splits_evenly_and_keeps_a_reserve(tmp_path):
    """Без явной квоты бюджет делится поровну, но НЕ весь: часть остаётся
    резервом. Директорские эскалации и стоп-кран не должны упираться в то, что
    трейдеры выбрали лимит подчистую."""
    from trader_lib.allocation import events_quota

    alloc = load_allocation(_write(tmp_path) / "allocation.json")   # трое
    q = events_quota(alloc, "trend", total=40)
    assert q == 10                                   # 40 // (3 + 1)
    assert q * 3 < 40, "резерв обязан остаться"


def test_solo_mode_gets_the_whole_budget(tmp_path):
    from trader_lib.allocation import events_quota

    alloc = load_allocation(_write(tmp_path) / "allocation.json")
    assert events_quota(alloc, None, total=40) == 40


def test_missing_allocation_gives_the_whole_budget(tmp_path):
    """Аллокации нет — делить не на кого и не на что."""
    from trader_lib.allocation import events_quota

    assert events_quota(load_allocation(tmp_path / "нет.json"), "trend",
                        total=40) == 40


# ================= имена трейдеров (просьба владельца 2026-08-03) =============

def test_имя_показывается_вместе_с_механизмом():
    """Направленность обязана быть В САМОМ отображении: читая тревожное
    сообщение, вспоминать, кто из них кто, некогда."""
    from trader_lib.allocation import display_name

    alloc = {"traders": {"trend": {"display_name": "Вэйран"},
                         "fade": {"display_name": "Шаэль"},
                         "range": {"display_name": "Оррин"}}}
    assert display_name(alloc, "trend") == "Вэйран · тренд"
    assert display_name(alloc, "fade") == "Шаэль · фейд"
    assert display_name(alloc, "range") == "Оррин · диапазон"


def test_без_имени_возвращается_идентификатор():
    """Выдумывать имя на лету нельзя: в канале появятся два разных обозначения
    одного трейдера, и связать их будет нечем."""
    from trader_lib.allocation import display_name

    assert display_name({"traders": {"trend": {}}}, "trend") == "trend"
    assert display_name({}, "range") == "range"
    assert display_name(None, "fade") == "fade"


def test_отсутствие_трейдера_не_роняет_отчёт():
    from trader_lib.allocation import display_name

    assert display_name({"traders": {}}, None) == "—"
    assert display_name({"traders": {}}, "чужой") == "чужой"


def test_идентификаторы_остались_прежними():
    """Имя — для человека, идентификатор — для кода. Путь traders/range/... в
    логе обязан читаться без словаря, иначе разбор требует держать
    соответствие в голове."""
    from trader_lib.allocation import MECHANISM_WORDS

    assert set(MECHANISM_WORDS) == {"trend", "fade", "range"}
