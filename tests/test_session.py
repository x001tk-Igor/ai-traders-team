"""Серверное время, фазы дня и сессионный гейт (задача 5.3).

ЭТО ФИКС БАГА, А НЕ НОВАЯ ФИЧА. До неё граница торгового дня считалась по
UTC-полуночи в трёх местах (risk_gate_cli, perceive, alert_watch), а брокер
живёт на UTC+3: с 21:00 до 24:00 UTC у него уже следующий день. Дневной лимит
−3% отмерялся не от того нуля — то есть главное защитное число считалось от
неверной точки отсчёта три часа в сутки.

ЧТО В КАКОМ ВРЕМЕНИ (иначе путаница неизбежна):
  * ГРАНИЦА ДНЯ — серверная. Это точка отсчёта дневного лимита и день, за
    который брокер начисляет свопы.
  * ОКНА И ФАЗЫ — UTC. Так они и записаны в конституции (ключи *_utc), и так
    их удобно сверять с расписанием сессий: LONDON 07:00, NY 12:15 — это UTC.
"""
import dataclasses
import datetime as dt
import io
import json

import pytest

from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket
from trader_lib.session import (
    current_phase,
    server_day_key,
    server_now,
    session_gate,
)

UTC = dt.timezone.utc


def _cfg(**over):
    cfg = load_config("config/trader.config.json")
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _at(y=2026, mo=7, d=27, h=12, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


# --------------------------------------------------------------------------
# граница дня
# --------------------------------------------------------------------------

def test_server_now_shifts_by_offset():
    assert server_now(utc_now=_at(h=12), offset_hours=3).hour == 15


def test_server_day_key_uses_offset():
    """23:30 UTC при +3 — это уже 02:30 СЛЕДУЮЩЕГО серверного дня. Ровно этот
    случай и был багом: дневной лимит считался от UTC-полуночи."""
    assert server_day_key(utc_now=_at(d=27, h=23, mi=30), offset_hours=3) == "2026-07-28"
    assert server_day_key(utc_now=_at(d=27, h=20, mi=59), offset_hours=3) == "2026-07-27"
    assert server_day_key(utc_now=_at(d=27, h=21, mi=0), offset_hours=3) == "2026-07-28"


def test_server_day_key_without_offset_is_utc_day():
    assert server_day_key(utc_now=_at(d=27, h=23, mi=30), offset_hours=0) == "2026-07-27"


def test_server_day_key_respects_reset_hour():
    """Если брокер сбрасывает день не в полночь, а, скажем, в 22:00 своего
    времени — точка отсчёта сдвигается вместе с ним."""
    # 21:30 server (= 18:30 UTC при +3) при reset_hour=22 — ещё «вчера»
    assert server_day_key(utc_now=_at(d=27, h=18, mi=30), offset_hours=3,
                          reset_hour=22) == "2026-07-26"
    assert server_day_key(utc_now=_at(d=27, h=19, mi=30), offset_hours=3,
                          reset_hour=22) == "2026-07-27"


def test_negative_offset():
    assert server_day_key(utc_now=_at(d=27, h=1, mi=0), offset_hours=-5) == "2026-07-26"


# --------------------------------------------------------------------------
# фазы дня
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hour,minute,expected", [
    (5, 45, "BRIEF"), (7, 0, "LONDON"), (10, 59, "LONDON"),
    (11, 0, "LULL"), (12, 15, "NY"), (15, 59, "NY"),
    (16, 0, "WINDDOWN"), (19, 0, "REVIEW"), (19, 40, None), (3, 0, None),
])
def test_phase_boundaries(hour, minute, expected):
    """Границы полуинтервальные: начало включено, конец исключён — иначе на
    стыке фаза зависела бы от порядка ключей в конфиге."""
    assert current_phase(utc_now=_at(h=hour, mi=minute), cfg=_cfg())["phase"] == expected


def test_phase_reports_window():
    st = current_phase(utc_now=_at(h=13), cfg=_cfg())
    assert st["phase"] == "NY" and st["from"] == "12:15" and st["to"] == "16:00"
    assert st["minutes_left"] == 180


# --------------------------------------------------------------------------
# сессионный гейт
# --------------------------------------------------------------------------

def test_inside_window_allows():
    st = session_gate(utc_now=_at(d=27, h=13), cfg=_cfg())
    assert st["allow_new"] is True and st["flat_required"] is False


def test_outside_window_blocks():
    """До открытия и после закрытия торгового окна новых входов нет."""
    assert session_gate(utc_now=_at(h=6, mi=30), cfg=_cfg())["allow_new"] is False
    assert session_gate(utc_now=_at(h=20, mi=30), cfg=_cfg())["allow_new"] is False


def test_no_new_after_blocks_before_window_close():
    """Окно ещё открыто (до 20:00), но новых входов уже нет: сделке нужно
    время, чтобы отработать, а не чтобы попасть в закрытие."""
    st = session_gate(utc_now=_at(h=19, mi=30), cfg=_cfg())
    assert st["allow_new"] is False and "no_new_after" in " ".join(st["reasons"])


def test_swap_window_blocks():
    """Окно начисления свопов: спреды разъезжаются, исполнение непредсказуемо."""
    st = session_gate(utc_now=_at(h=21, mi=0), cfg=_cfg())
    assert st["allow_new"] is False and any("своп" in r for r in st["reasons"])


def test_friday_no_new_after_15():
    friday = _at(y=2026, mo=7, d=31, h=15, mi=30)   # пятница
    assert friday.weekday() == 4
    st = session_gate(utc_now=friday, cfg=_cfg())
    assert st["allow_new"] is False and any("пятниц" in r for r in st["reasons"])


def test_friday_before_15_still_allows():
    st = session_gate(utc_now=_at(y=2026, mo=7, d=31, h=13), cfg=_cfg())
    assert st["allow_new"] is True


def test_friday_flat_at_19():
    """После 19:00 в пятницу позиции не переносятся через выходные: гэп
    открытия не защищён стопом."""
    st = session_gate(utc_now=_at(y=2026, mo=7, d=31, h=19, mi=5), cfg=_cfg())
    assert st["flat_required"] is True and st["allow_new"] is False


def test_weekend_blocks():
    saturday = _at(y=2026, mo=8, d=1, h=13)
    assert saturday.weekday() == 5
    st = session_gate(utc_now=saturday, cfg=_cfg())
    assert st["allow_new"] is False and any("выходн" in r for r in st["reasons"])


def test_reasons_are_journalable():
    """Причины идут в журнал как есть — они обязаны быть строками, понятными
    человеку через месяц, а не кодами."""
    st = session_gate(utc_now=_at(h=6), cfg=_cfg())
    assert st["reasons"] and all(isinstance(r, str) and len(r) > 10 for r in st["reasons"])


def test_gate_reports_server_day():
    st = session_gate(utc_now=_at(d=27, h=23), cfg=_cfg())
    assert st["server_day"] == "2026-07-28", "гейт сообщает серверный день, не UTC"


# --------------------------------------------------------------------------
# погашение долга: три точки, которые считали день по UTC
# --------------------------------------------------------------------------

LATE = _at(d=27, h=23, mi=30)     # серверный день уже 28-е при смещении +3


def test_risk_gate_baselines_use_server_day(tmp_path):
    """ГЛАВНЫЙ ТЕСТ ФИКСА. В 23:30 UTC серверный день — 28-е. Baseline,
    записанный за 28-е, обязан читаться, а не отбрасываться как «не за
    сегодня»: иначе точкой отсчёта дневного лимита становится текущий equity,
    и просадка, набранная за день, обнуляется на три часа раньше срока.
    """
    import scripts.risk_gate_cli as cli

    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": "2026-07-28", "equity": 10000.0, "initial_balance": 10000.0}),
        encoding="utf-8")
    day_eq, init = cli._baselines(str(tmp_path), 9700.0, now=LATE, cfg=_cfg())
    assert (day_eq, init) == (10000.0, 10000.0)

    # а baseline за UTC-день (27-е) в этот момент уже НЕ актуален
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": "2026-07-27", "equity": 10000.0, "initial_balance": 10000.0}),
        encoding="utf-8")
    day_eq, _ = cli._baselines(str(tmp_path), 9700.0, now=LATE, cfg=_cfg())
    assert day_eq == 9700.0, "вчерашний baseline не должен подставляться как сегодняшний"


def test_risk_gate_day_start_is_server_midnight():
    """Начало дня для отбора сделок «за сегодня» — серверная полночь
    (21:00 UTC при +3), а не UTC-полночь."""
    import scripts.risk_gate_cli as cli

    assert cli._day_start_utc(LATE, _cfg()) == _at(d=27, h=21, mi=0)


def test_perceive_baseline_uses_server_day(tmp_path):
    """Та же граница в записи baseline: perceive пишет ключ серверного дня,
    иначе в 21:00–24:00 UTC он каждый раз переписывал бы файл текущим equity."""
    import scripts.perceive as perceive

    perceive._day_baseline(str(tmp_path), 9700.0, _cfg(), now=LATE)
    doc = json.loads((tmp_path / "day_baseline.json").read_text(encoding="utf-8"))
    assert doc["day"] == "2026-07-28"


def test_alert_watch_event_budget_day_is_server_day(tmp_path):
    """Дневной лимит СОБЫТИЙ обнуляется вместе с торговым днём: иначе с 21:00
    до полуночи UTC счётчик жил бы по вчерашнему дню и мог бы молча запретить
    пробуждения на весь вечер."""
    import scripts.alert_watch as aw

    class _Executor:
        def close_position(self, ticket):
            return {"ok": True}

        def modify_sl(self, ticket, new_sl):
            return {"ok": True}

    w = aw.AlertWatch(FakeMarket(), _cfg(), executor=_Executor(),
                      state_dir_path=str(tmp_path), out=io.StringIO(), log=io.StringIO())
    assert w._server_day(LATE) == "2026-07-28"
    assert w._server_day(_at(d=27, h=20, mi=59)) == "2026-07-27"
