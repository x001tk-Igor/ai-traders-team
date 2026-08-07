import datetime as dt
import json
import os
from pathlib import Path

import pytest

from trader_lib.alerts import (
    ALERT_TYPES,
    evaluate,
    event_budget,
    load_alerts,
    write_alerts_atomic,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 10, 0, 0, tzinfo=UTC)

# budget теперь трёхъярусный (normal / critical / абсолютный потолок в
# минуту, см. event_budget). Константы ниже — заранее собранные словари той
# же формы, что возвращает event_budget(), для тестов, которым не нужно
# гонять сам event_budget.
BUDGET_OK = {"allowed": True, "reason": "в пределах бюджета", "events_left": 10,
             "critical_allowed": True, "critical_reason": "в пределах интервала critical",
             "hard_cap_remaining": 10, "hard_cap_reason": "в пределах потолка"}
BUDGET_BLOCKED = {"allowed": False, "reason": "бюджет исчерпан (тест)", "events_left": 0,
                  "critical_allowed": False, "critical_reason": "интервал critical не прошёл (тест)",
                  "hard_cap_remaining": 0, "hard_cap_reason": "потолок исчерпан (тест)"}
# normal исчерпан, critical в пределах СВОЕГО яруса и в пределах потолка —
# ровно сценарий "critical обходит ТОЛЬКО дневной лимит normal"
BUDGET_NORMAL_BLOCKED_CRITICAL_OK = {
    "allowed": False, "reason": "дневной лимит normal исчерпан (тест)", "events_left": 0,
    "critical_allowed": True, "critical_reason": "в пределах интервала critical",
    "hard_cap_remaining": 10, "hard_cap_reason": "в пределах потолка"}

CFG_ALERTS = {"max_events_per_day": 40, "min_seconds_between_events": 60,
              "min_seconds_between_critical_events": 15, "max_events_per_minute": 6,
              "critical_types": ["wall_breach", "spread_anomaly", "gap", "sl_jumped", "data_stale"],
              "max_silence_minutes": 180, "poll_seconds": 1}


def _alerts_doc(*items):
    return {"version": 1, "written_by": "claude-opus-5", "written_utc": "2026-07-27T06:00:00Z",
            "expires_utc": None, "alerts": list(items)}


def _run(alert, ctx, *, now=NOW, budget=BUDGET_OK):
    return evaluate(_alerts_doc(alert), ctx, now=now, budget=budget)


# --------------------------------------------------------------------------
# load_alerts / write_alerts_atomic
# --------------------------------------------------------------------------

def test_roundtrip(tmp_path):
    p = tmp_path / "alerts.json"
    data = {
        "version": 1, "written_by": "claude-opus-5",
        "written_utc": "2026-07-27T06:02:11Z", "expires_utc": "2026-07-27T16:00:00Z",
        "alerts": [
            {"id": "h1-trigger", "symbol": "XAUUSD", "type": "price_below",
             "level": 2415.0, "once": True, "priority": "normal",
             "note": "гипотеза H1: возврат под уровень"},
            {"id": "spread", "symbol": "XAUUSD", "type": "spread_anomaly", "mult": 2.0,
             "priority": "critical"},
        ],
    }
    write_alerts_atomic(p, data)

    loaded = load_alerts(p, now=dt.datetime(2026, 7, 27, 7, 0, tzinfo=UTC))
    assert loaded["version"] == 1
    assert loaded["written_by"] == "claude-opus-5"
    assert loaded["expires_utc"] == "2026-07-27T16:00:00Z"
    assert loaded["alerts"] == data["alerts"]


def test_load_alerts_missing_file_returns_empty(tmp_path):
    loaded = load_alerts(tmp_path / "does_not_exist.json", now=NOW)
    assert loaded["alerts"] == []
    assert loaded["version"] == 1
    assert loaded["written_by"] is None


def test_expired_alerts_dropped(tmp_path):
    p = tmp_path / "alerts.json"
    data = {"version": 1, "written_by": "x", "written_utc": "2026-07-27T06:00:00Z",
            "expires_utc": "2026-07-27T07:00:00Z",
            "alerts": [{"id": "a", "type": "price_above", "symbol": "XAUUSD", "level": 1.0}]}
    write_alerts_atomic(p, data)

    # до истечения — алерты на месте
    before = load_alerts(p, now=dt.datetime(2026, 7, 27, 6, 30, tzinfo=UTC))
    assert len(before["alerts"]) == 1

    # после истечения — весь файл отброшен (не только "просроченный" алерт)
    after = load_alerts(p, now=dt.datetime(2026, 7, 27, 8, 0, tzinfo=UTC))
    assert after["alerts"] == []


def test_load_alerts_malformed_json_raises(tmp_path):
    p = tmp_path / "alerts.json"
    p.write_text("{not valid json at all", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_alerts(p, now=NOW)


def test_atomic_write(tmp_path, monkeypatch):
    p = tmp_path / "alerts.json"
    p.write_text(json.dumps({"version": 1, "alerts": []}), encoding="utf-8")
    old_content = p.read_text(encoding="utf-8")

    real_replace = os.replace
    calls = []

    def spy_replace(src, dst):
        # структурная проверка "запись через временный файл": в момент
        # replace временный файл уже содержит НОВЫЕ данные, а целевой путь
        # ещё СТАРЫЕ — то есть запись реально шла не напрямую в целевой файл
        assert Path(src).read_text(encoding="utf-8") != old_content
        assert p.read_text(encoding="utf-8") == old_content
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    new_data = {"version": 1, "alerts": [{"id": "x", "type": "gap", "symbol": "XAUUSD"}]}
    write_alerts_atomic(p, new_data)

    assert len(calls) == 1
    tmp_src, dst = calls[0]
    assert dst == str(p)
    assert tmp_src != str(p)
    assert not Path(tmp_src).exists()  # replace переместил файл, временного не осталось

    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []

    loaded = load_alerts(p, now=NOW)
    assert loaded["alerts"] == new_data["alerts"]


# --------------------------------------------------------------------------
# once / rearm / disarm
# --------------------------------------------------------------------------

def test_once_disarms(tmp_path):
    p = tmp_path / "alerts.json"
    alert = {"id": "a", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0, "once": True}
    write_alerts_atomic(p, _alerts_doc(alert))
    ctx = {"symbols": {"XAUUSD": {"price": 2450.0}}}

    loaded1 = load_alerts(p, now=NOW)
    fired1, updated1 = evaluate(loaded1, ctx, now=NOW, budget=BUDGET_OK)
    assert len(fired1) == 1
    assert updated1["alerts"][0]["_state"]["armed"] is False
    write_alerts_atomic(p, updated1)

    # состояние пережило запись/чтение файла (не только in-memory)
    loaded2 = load_alerts(p, now=NOW)
    fired2, updated2 = evaluate(loaded2, ctx, now=NOW, budget=BUDGET_OK)
    assert fired2 == []
    assert any(s["id"] == "a" and "разоружён" in s["reason"] for s in updated2["skipped"])


def test_rearm_after_minutes():
    alert = {"id": "a", "type": "atr_pctile_above", "symbol": "XAUUSD", "level": 0.9,
             "rearm_after_minutes": 120}
    ctx = {"symbols": {"XAUUSD": {"atr_pctile": 0.95}}}

    t0 = NOW
    fired0, updated0 = evaluate(_alerts_doc(alert), ctx, now=t0, budget=BUDGET_OK)
    assert len(fired0) == 1
    alert_after0 = updated0["alerts"][0]
    assert alert_after0["_state"]["armed"] is False

    # +30 минут — ещё не перевооружён, условие всё ещё истинно
    t1 = t0 + dt.timedelta(minutes=30)
    fired1, updated1 = evaluate(_alerts_doc(alert_after0), ctx, now=t1, budget=BUDGET_OK)
    assert fired1 == []
    alert_after1 = updated1["alerts"][0]

    # +121 минута от первого срабатывания — перевооружён и срабатывает снова
    t2 = t0 + dt.timedelta(minutes=121)
    fired2, updated2 = evaluate(_alerts_doc(alert_after1), ctx, now=t2, budget=BUDGET_OK)
    assert len(fired2) == 1
    assert updated2["alerts"][0]["_state"]["armed"] is False  # снова сработал -> снова разоружён


def test_state_resets_when_model_rewrites_file(tmp_path):
    p = tmp_path / "alerts.json"
    alert = {"id": "a", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0, "once": True}
    ctx = {"symbols": {"XAUUSD": {"price": 2500.0}}}

    write_alerts_atomic(p, _alerts_doc(alert))
    fired1, updated1 = evaluate(load_alerts(p, now=NOW), ctx, now=NOW, budget=BUDGET_OK)
    assert len(fired1) == 1
    write_alerts_atomic(p, updated1)

    # датчик перечитывает — уже разоружён
    fired2, _ = evaluate(load_alerts(p, now=NOW), ctx, now=NOW, budget=BUDGET_OK)
    assert fired2 == []

    # модель заканчивает следующую сессию и перезаписывает файл ТЕМ ЖЕ id,
    # но свежим определением (без _state) — новый цикл наблюдения
    write_alerts_atomic(p, _alerts_doc(alert))
    fired3, _ = evaluate(load_alerts(p, now=NOW), ctx, now=NOW, budget=BUDGET_OK)
    assert len(fired3) == 1  # прошлое разоружение не унаследовано


# --------------------------------------------------------------------------
# event budget
# --------------------------------------------------------------------------

def test_critical_bypasses_budget():
    """critical обходит ТОЛЬКО дневной лимит normal, а не бюджет целиком —
    см. инцидент в docs/alerts_schema.md: полный обход превращал критическую
    аномалию, которая держится минутами, в десятки срабатываний в минуту при
    опросе раз в секунду, и Monitor останавливался от переизбытка событий
    ровно в момент, когда модель нужнее всего."""
    alert_critical = {"id": "c", "type": "spread_anomaly", "symbol": "XAUUSD", "mult": 2.0,
                      "priority": "critical"}
    alert_normal = {"id": "n", "type": "spread_anomaly", "symbol": "XAUUSD", "mult": 2.0,
                    "priority": "normal"}
    ctx = {"symbols": {"XAUUSD": {"spread_points": 30.0, "spread_median_points": 10.0}}}

    # normal-дневной лимит исчерпан, свой (critical) интервал и потолок — в порядке
    fired_c, _ = _run(alert_critical, ctx, budget=BUDGET_NORMAL_BLOCKED_CRITICAL_OK)
    assert len(fired_c) == 1

    fired_n, updated_n = _run(alert_normal, ctx, budget=BUDGET_NORMAL_BLOCKED_CRITICAL_OK)
    assert fired_n == []
    assert any(s["id"] == "n" and "бюджет" in s["reason"] for s in updated_n["skipped"])


def test_critical_respects_own_interval_not_whole_budget():
    """Обратная сторона предыдущего теста: critical НЕ обходит бюджет
    целиком. Если исчерпан именно ЕГО ярус (свой интервал), critical тоже не
    срабатывает — обходит он только дневной лимит normal."""
    alert_critical = {"id": "c", "type": "spread_anomaly", "symbol": "XAUUSD", "mult": 2.0,
                      "priority": "critical"}
    ctx = {"symbols": {"XAUUSD": {"spread_points": 30.0, "spread_median_points": 10.0}}}

    fired, updated = _run(alert_critical, ctx, budget=BUDGET_BLOCKED)
    assert fired == []
    assert any(s["id"] == "c" and "critical" in s["reason"] for s in updated["skipped"])


def test_normal_respects_min_interval():
    cfg = {"max_events_per_day": 100, "min_seconds_between_events": 60,
           "min_seconds_between_critical_events": 15, "max_events_per_minute": 6}

    recent = event_budget(5, cfg, now=NOW, last_event_ts=NOW - dt.timedelta(seconds=30),
                          last_critical_event_ts=None, recent_event_ts=())
    assert recent["allowed"] is False
    assert "интервал" in recent["reason"]

    old_enough = event_budget(5, cfg, now=NOW, last_event_ts=NOW - dt.timedelta(seconds=90),
                              last_critical_event_ts=None, recent_event_ts=())
    assert old_enough["allowed"] is True

    # то же самое через evaluate() для normal-приоритета
    alert = {"id": "n", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0}
    ctx = {"symbols": {"XAUUSD": {"price": 2450.0}}}
    fired, _ = _run(alert, ctx, budget=recent)
    assert fired == []
    fired2, _ = _run(alert, ctx, budget=old_enough)
    assert len(fired2) == 1


def test_critical_respects_own_shorter_interval():
    """Симметрично normal: critical подчиняется СВОЕМУ (короче обычного)
    интервалу min_seconds_between_critical_events, а не общему."""
    cfg = {"max_events_per_day": 100, "min_seconds_between_events": 60,
           "min_seconds_between_critical_events": 15, "max_events_per_minute": 6}

    too_soon = event_budget(0, cfg, now=NOW, last_event_ts=None,
                            last_critical_event_ts=NOW - dt.timedelta(seconds=5),
                            recent_event_ts=())
    assert too_soon["critical_allowed"] is False
    assert "critical" in too_soon["critical_reason"]

    old_enough = event_budget(0, cfg, now=NOW, last_event_ts=None,
                              last_critical_event_ts=NOW - dt.timedelta(seconds=20),
                              recent_event_ts=())
    assert old_enough["critical_allowed"] is True

    # 20с достаточно для critical (порог 15с), но НЕ достаточно для normal
    # (порог 60с) — интервалы разных ярусов не должны путаться местами
    also_as_normal_interval = event_budget(0, cfg, now=NOW,
                                           last_event_ts=NOW - dt.timedelta(seconds=20),
                                           last_critical_event_ts=NOW - dt.timedelta(seconds=20),
                                           recent_event_ts=())
    assert also_as_normal_interval["critical_allowed"] is True
    assert also_as_normal_interval["allowed"] is False


def test_daily_event_cap():
    # max_events_per_day и min_seconds_between_events намеренно разные и не
    # кратные друг другу — инвариант не должен держаться на совпадении чисел
    cfg = {"max_events_per_day": 2, "min_seconds_between_events": 7,
           "min_seconds_between_critical_events": 3, "max_events_per_minute": 6}

    at_cap = event_budget(2, cfg, now=NOW, last_event_ts=None,
                          last_critical_event_ts=None, recent_event_ts=())
    assert at_cap["allowed"] is False
    assert at_cap["events_left"] == 0
    assert "2/2" in at_cap["reason"] or "дневн" in at_cap["reason"]
    # дневной лимит — только ярус normal, critical он не задевает
    assert at_cap["critical_allowed"] is True

    under_cap = event_budget(1, cfg, now=NOW, last_event_ts=None,
                             last_critical_event_ts=None, recent_event_ts=())
    assert under_cap["allowed"] is True
    assert under_cap["events_left"] == 1

    over_cap = event_budget(5, cfg, now=NOW, last_event_ts=None,
                            last_critical_event_ts=None, recent_event_ts=())
    assert over_cap["allowed"] is False
    assert over_cap["events_left"] == 0


def test_event_budget_interval_boundary_inclusive():
    cfg = {"max_events_per_day": 100, "min_seconds_between_events": 60,
           "min_seconds_between_critical_events": 15, "max_events_per_minute": 6}
    exactly_at = event_budget(0, cfg, now=NOW, last_event_ts=NOW - dt.timedelta(seconds=60),
                              last_critical_event_ts=None, recent_event_ts=())
    assert exactly_at["allowed"] is True

    exactly_at_critical = event_budget(0, cfg, now=NOW, last_event_ts=None,
                                       last_critical_event_ts=NOW - dt.timedelta(seconds=15),
                                       recent_event_ts=())
    assert exactly_at_critical["critical_allowed"] is True


def test_event_budget_requires_last_critical_event_ts():
    """last_critical_event_ts — обязательный keyword-only параметр, БЕЗ
    дефолта (см. шапку event_budget). Коллега вызвал event_budget(...) без
    него при проверке фикса critical-бюджета: прежний дефолт None молча
    читался как "критических событий ещё не было" → интервал не применялся
    → тот самый дефект (10 срабатываний за 10 секунд), который фикс должен
    был закрыть. Без дефолта неполный вызов обязан падать TypeError на
    первом же прогоне, а не тихо отключать защиту."""
    cfg = {"max_events_per_day": 40, "min_seconds_between_events": 60,
           "min_seconds_between_critical_events": 15, "max_events_per_minute": 6}
    with pytest.raises(TypeError):
        event_budget(0, cfg, now=NOW, last_event_ts=None, recent_event_ts=())


def test_event_budget_requires_recent_event_ts():
    """Симметрично last_critical_event_ts: recent_event_ts тоже обязателен
    без дефолта — прежний дефолт () молча читался как "за минуту событий не
    было" → hard_cap_remaining всегда полный → абсолютный потолок никогда
    не срабатывал бы, если вызывающий забыл его передать."""
    cfg = {"max_events_per_day": 40, "min_seconds_between_events": 60,
           "min_seconds_between_critical_events": 15, "max_events_per_minute": 6}
    with pytest.raises(TypeError):
        event_budget(0, cfg, now=NOW, last_event_ts=None, last_critical_event_ts=None)


def test_unknown_priority_does_not_bypass_budget():
    alert = {"id": "a", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0,
             "priority": "urgent"}
    ctx = {"symbols": {"XAUUSD": {"price": 2500.0}}}
    fired, updated = _run(alert, ctx, budget=BUDGET_BLOCKED)
    assert fired == []
    assert any(s["id"] == "a" and "priority" in s["reason"] for s in updated["skipped"])


# --------------------------------------------------------------------------
# абсолютный потолок событий в минуту (не обходит никто, включая critical)
# --------------------------------------------------------------------------

def test_hard_cap_blocks_even_critical():
    """Свой интервал critical в порядке, но абсолютный потолок исчерпан —
    critical тоже не срабатывает. Потолок защищает механизм пробуждения, а
    не квоту, и его не обходит ни один приоритет."""
    alert = {"id": "c", "type": "spread_anomaly", "symbol": "XAUUSD", "mult": 2.0,
             "priority": "critical"}
    ctx = {"symbols": {"XAUUSD": {"spread_points": 30.0, "spread_median_points": 10.0}}}
    budget = {**BUDGET_OK, "hard_cap_remaining": 0, "hard_cap_reason": "потолок исчерпан (тест)"}

    fired, updated = _run(alert, ctx, budget=budget)
    assert fired == []
    assert any(s["id"] == "c" and "потолок" in s["reason"] for s in updated["skipped"])


def test_hard_cap_shared_across_simultaneous_alerts_in_one_tick():
    """Несколько РАЗНЫХ critical-алертов, истинных ОДНОВРЕМЕННО (в одном
    вызове evaluate) — ни дневной лимит, ни интервал (оба меряют время ОТ
    ПРЕДЫДУЩЕГО события) не защищают от такой пачки, когда предыдущего
    события ещё не было. Абсолютный потолок обязан ограничить именно её:
    из 4 одновременно истинных условий с hard_cap_remaining=2 сработать
    должны ровно 2, а не все 4."""
    alerts = [{"id": f"c{i}", "type": "gap", "symbol": "XAUUSD", "priority": "critical"}
             for i in range(4)]
    ctx = {"symbols": {"XAUUSD": {"bar_gap": True}}}
    budget = {**BUDGET_OK, "hard_cap_remaining": 2}

    fired, updated = evaluate(_alerts_doc(*alerts), ctx, now=NOW, budget=budget)

    assert len(fired) == 2
    assert len(updated["skipped"]) == 2
    assert all("потолок" in s["reason"] for s in updated["skipped"])


def test_critical_repeated_ticks_bounded_by_interval_and_hard_cap():
    """Зонд из ревью: critical-алерт БЕЗ once/rearm, десять тиков раз в
    секунду, условие истинно каждый раз. До фикса budget['allowed']
    целиком игнорировался для critical — 10 срабатываний за 10 секунд.
    После фикса critical подчиняется своему интервалу (15с) и общему
    потолку (6/мин) — за 10 тиков по 1с (10-секундное окно, короче
    интервала) физически не может сработать больше одного раза.

    Тест гоняет реальную последовательность тиков, как зонд коллеги (не
    один вызов) — это единственная форма, которая ловит регресс: одиночный
    вызов evaluate() с уже собранным budget не отличает "интервал применён"
    от "интервал проигнорирован", если событие только одно."""
    cfg = {"max_events_per_day": 40, "min_seconds_between_events": 60,
           "min_seconds_between_critical_events": 15, "max_events_per_minute": 6}
    alert = {"id": "c", "type": "spread_anomaly", "symbol": "XAUUSD", "mult": 2.0,
             "priority": "critical"}
    ctx = {"symbols": {"XAUUSD": {"spread_points": 30.0, "spread_median_points": 10.0}}}

    doc = _alerts_doc(alert)
    events_today = 0
    last_critical_ts = None
    recent_ts = []
    total_fired = 0

    for i in range(10):
        tick_now = NOW + dt.timedelta(seconds=i)
        budget = event_budget(events_today, cfg, now=tick_now, last_event_ts=None,
                              last_critical_event_ts=last_critical_ts, recent_event_ts=recent_ts)
        fired, doc = evaluate(doc, ctx, now=tick_now, budget=budget)
        for f in fired:
            total_fired += 1
            last_critical_ts = tick_now
            recent_ts.append(tick_now)

    # интервал 15с > 10-секундное окно тиков => не более одного срабатывания
    assert total_fired <= 1
    assert total_fired >= 1  # но и не ноль — первый тик обязан сработать
    # и в любом случае не превышает абсолютный потолок в минуту
    assert total_fired <= cfg["max_events_per_minute"]


def test_event_budget_hard_cap_counts_only_last_60_seconds():
    cfg = {"max_events_per_day": 100, "min_seconds_between_events": 1,
           "min_seconds_between_critical_events": 1, "max_events_per_minute": 3}
    recent = [NOW - dt.timedelta(seconds=90),   # за окном — не считается
             NOW - dt.timedelta(seconds=50),
             NOW - dt.timedelta(seconds=10)]

    budget = event_budget(0, cfg, now=NOW, last_event_ts=None,
                          last_critical_event_ts=None, recent_event_ts=recent)
    assert budget["hard_cap_remaining"] == 1  # 3 - 2 в окне (90с исключена)

    budget_full = event_budget(0, cfg, now=NOW, last_event_ts=None,
                               last_critical_event_ts=None,
                               recent_event_ts=recent + [NOW - dt.timedelta(seconds=5)])
    assert budget_full["hard_cap_remaining"] == 0


# --------------------------------------------------------------------------
# устойчивость к плохим данным
# --------------------------------------------------------------------------

def test_unknown_type_ignored_not_crash():
    good = {"id": "g", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0}
    bad = {"id": "b", "type": "totally_made_up_type", "symbol": "XAUUSD"}
    ctx = {"symbols": {"XAUUSD": {"price": 2450.0}}}

    fired, updated = evaluate(_alerts_doc(good, bad), ctx, now=NOW, budget=BUDGET_OK)

    assert {f["id"] for f in fired} == {"g"}
    assert any(s["id"] == "b" and "неизвестный тип" in s["reason"] for s in updated["skipped"])
    assert len(updated["alerts"]) == 2  # обе записи сохранены, ни одна не выброшена


def test_alert_without_type_field_skipped_not_crash():
    alert = {"id": "a", "symbol": "XAUUSD", "level": 2400.0}  # нет "type"
    fired, updated = _run(alert, {"symbols": {"XAUUSD": {"price": 2500.0}}})
    assert fired == []
    assert updated["skipped"][0]["id"] == "a"


def test_malformed_alert_missing_required_field_skipped_not_crash():
    alert = {"id": "a", "type": "price_above", "symbol": "XAUUSD"}  # нет "level"
    fired, updated = _run(alert, {"symbols": {"XAUUSD": {"price": 2450.0}}})
    assert fired == []
    assert "level" in updated["skipped"][0]["reason"]


def test_condition_error_skips_not_crashes():
    # level — не число: сравнение упадёт внутри обработчика; evaluate не должна упасть
    alert = {"id": "a", "type": "price_above", "symbol": "XAUUSD", "level": "not-a-number"}
    fired, updated = _run(alert, {"symbols": {"XAUUSD": {"price": 2450.0}}})
    assert fired == []
    assert "ошибка вычисления условия" in updated["skipped"][0]["reason"]


# --------------------------------------------------------------------------
# ctx без нужных данных — skip, не срабатывание
# --------------------------------------------------------------------------

def test_ctx_missing_data_skips_not_fires():
    alert = {"id": "a", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0}
    fired, updated = _run(alert, {})  # пустой ctx — символа вообще нет
    assert fired == []
    assert len(updated["skipped"]) == 1
    skip = updated["skipped"][0]
    assert skip["id"] == "a"
    assert "XAUUSD" in skip["reason"]
    assert len(updated["alerts"]) == 1  # алерт не выброшен, просто не сработал


TYPE_CASES = {
    # type: (extra_fires, extra_not_fires, ctx_fires, ctx_not_fires)
    "price_above": (
        {"symbol": "XAUUSD", "level": 2400.0}, {"symbol": "XAUUSD", "level": 2400.0},
        {"symbols": {"XAUUSD": {"price": 2450.0}}}, {"symbols": {"XAUUSD": {"price": 2350.0}}}),
    "price_below": (
        {"symbol": "XAUUSD", "level": 2400.0}, {"symbol": "XAUUSD", "level": 2400.0},
        {"symbols": {"XAUUSD": {"price": 2350.0}}}, {"symbols": {"XAUUSD": {"price": 2450.0}}}),
    "price_touch": (
        {"symbol": "XAUUSD", "level": 2400.0, "tolerance_atr": 0.5},
        {"symbol": "XAUUSD", "level": 2400.0, "tolerance_atr": 0.5},
        {"symbols": {"XAUUSD": {"price": 2400.4, "atr": 1.0}}},
        {"symbols": {"XAUUSD": {"price": 2410.0, "atr": 1.0}}}),
    "atr_pctile_above": (
        {"symbol": "XAUUSD", "level": 0.8}, {"symbol": "XAUUSD", "level": 0.8},
        {"symbols": {"XAUUSD": {"atr_pctile": 0.95}}}, {"symbols": {"XAUUSD": {"atr_pctile": 0.3}}}),
    "atr_pctile_below": (
        {"symbol": "XAUUSD", "level": 0.2}, {"symbol": "XAUUSD", "level": 0.2},
        {"symbols": {"XAUUSD": {"atr_pctile": 0.05}}}, {"symbols": {"XAUUSD": {"atr_pctile": 0.9}}}),
    "position_R_reaches": (
        {"ticket": 123, "level": 1.0}, {"ticket": 123, "level": 1.0},
        {"positions": {123: {"r_multiple": 1.5}}}, {"positions": {123: {"r_multiple": 0.2}}}),
    "position_R_drops_to": (
        {"ticket": 123, "level": 1.0}, {"ticket": 123, "level": 1.0},
        {"positions": {123: {"r_multiple": -1.5}}}, {"positions": {123: {"r_multiple": -0.2}}}),
    "position_time_elapsed": (
        {"ticket": 123, "minutes": 30, "min_progress_R": 0.3},
        {"ticket": 123, "minutes": 30, "min_progress_R": 0.3},
        {"positions": {123: {"opened_utc": NOW - dt.timedelta(minutes=40), "r_multiple": 0.1}}},
        {"positions": {123: {"opened_utc": NOW - dt.timedelta(minutes=5), "r_multiple": 0.1}}}),
    "news_window_opens": (
        {"minutes_before": 30}, {"minutes_before": 30},
        {"news_windows": [{"name": "NFP", "minutes_until": 10}]},
        {"news_windows": [{"name": "NFP", "minutes_until": 120}]}),
    "spread_anomaly": (
        {"symbol": "XAUUSD", "mult": 2.0}, {"symbol": "XAUUSD", "mult": 2.0},
        {"symbols": {"XAUUSD": {"spread_points": 25.0, "spread_median_points": 10.0}}},
        {"symbols": {"XAUUSD": {"spread_points": 15.0, "spread_median_points": 10.0}}}),
    # зеркало spread_anomaly: срабатывает на ВОЗВРАТЕ спреда к норме — это
    # момент входа возвратной тактики (книга восстановилась)
    "spread_normalizes": (
        {"symbol": "XAUUSD", "mult": 1.5}, {"symbol": "XAUUSD", "mult": 1.5},
        {"symbols": {"XAUUSD": {"spread_points": 12.0, "spread_median_points": 10.0}}},
        {"symbols": {"XAUUSD": {"spread_points": 40.0, "spread_median_points": 10.0}}}),
    "gap": (
        {"symbol": "XAUUSD"}, {"symbol": "XAUUSD"},
        {"symbols": {"XAUUSD": {"bar_gap": True}}}, {"symbols": {"XAUUSD": {"bar_gap": False}}}),
    "sl_jumped": (
        {"ticket": 123}, {"ticket": 123},
        {"positions": {123: {"beyond_sl": True}}}, {"positions": {123: {"beyond_sl": False}}}),
    "data_stale": (
        {"symbol": "XAUUSD"}, {"symbol": "XAUUSD"},
        {"symbols": {"XAUUSD": {"tick_stale": True}}}, {"symbols": {"XAUUSD": {"tick_stale": False}}}),
    "time_at_utc": (
        {"at": "2026-07-27T09:00:00Z"}, {"at": "2026-07-27T11:00:00Z"}, {}, {}),
    "silence_timeout": (
        {"minutes": 30}, {"minutes": 30},
        {"last_event_utc": NOW - dt.timedelta(minutes=45)},
        {"last_event_utc": NOW - dt.timedelta(minutes=5)}),
}
# три типа с памятью (trend_flips/gate_verdict_changes/session_phase_changes)
# требуют двух последовательных вызовов evaluate — не укладываются в
# fires/not-fires-за-один-вызов и протестированы отдельными функциями ниже.
_MEMORY_TYPES = {"trend_flips", "gate_verdict_changes", "session_phase_changes"}


def test_all_alert_types_covered_by_a_test():
    assert set(TYPE_CASES) | _MEMORY_TYPES == set(ALERT_TYPES)


@pytest.mark.parametrize("atype", sorted(TYPE_CASES))
def test_type_fires_and_not_fires(atype):
    extra_fires, extra_not, ctx_fires, ctx_not = TYPE_CASES[atype]

    fired, _ = _run({"id": "a", "type": atype, **extra_fires}, ctx_fires)
    assert len(fired) == 1, f"{atype} должен был сработать"
    assert fired[0]["type"] == atype

    fired2, _ = _run({"id": "a", "type": atype, **extra_not}, ctx_not)
    assert fired2 == [], f"{atype} НЕ должен был сработать"


def test_type_trend_flips():
    alert = {"id": "a", "type": "trend_flips", "symbol": "XAUUSD", "tf": "H1"}

    fired1, updated1 = _run(alert, {"symbols": {"XAUUSD": {"trend": {"H1": "up"}}}})
    assert fired1 == []  # первое наблюдение — эталон, сравнивать не с чем

    alert2 = updated1["alerts"][0]
    fired2, updated2 = evaluate(_alerts_doc(alert2), {"symbols": {"XAUUSD": {"trend": {"H1": "down"}}}},
                                now=NOW, budget=BUDGET_OK)
    assert len(fired2) == 1
    assert fired2[0]["detail"] == {"trend_prev": "up", "trend_now": "down"}

    alert3 = updated2["alerts"][0]
    fired3, _ = evaluate(_alerts_doc(alert3), {"symbols": {"XAUUSD": {"trend": {"H1": "down"}}}},
                         now=NOW, budget=BUDGET_OK)
    assert fired3 == []  # тренд не менялся (down -> down)


def test_type_gate_verdict_changes():
    alert = {"id": "a", "type": "gate_verdict_changes"}

    fired1, updated1 = _run(alert, {"gate_verdict": "OK"})
    assert fired1 == []

    alert2 = updated1["alerts"][0]
    fired2, updated2 = evaluate(_alerts_doc(alert2), {"gate_verdict": "HALT_NEW"}, now=NOW, budget=BUDGET_OK)
    assert len(fired2) == 1
    assert fired2[0]["detail"] == {"verdict_prev": "OK", "verdict_now": "HALT_NEW"}

    alert3 = updated2["alerts"][0]
    fired3, _ = evaluate(_alerts_doc(alert3), {"gate_verdict": "HALT_NEW"}, now=NOW, budget=BUDGET_OK)
    assert fired3 == []


def test_type_session_phase_changes():
    alert = {"id": "a", "type": "session_phase_changes"}

    fired1, updated1 = _run(alert, {"session_phase": "LONDON"})
    assert fired1 == []

    alert2 = updated1["alerts"][0]
    fired2, _ = evaluate(_alerts_doc(alert2), {"session_phase": "NY"}, now=NOW, budget=BUDGET_OK)
    assert len(fired2) == 1


MISSING_CTX_EXTRA = {t: TYPE_CASES[t][0] for t in TYPE_CASES}
MISSING_CTX_EXTRA["trend_flips"] = {"symbol": "XAUUSD", "tf": "H1"}
MISSING_CTX_EXTRA["gate_verdict_changes"] = {}
MISSING_CTX_EXTRA["session_phase_changes"] = {}


@pytest.mark.parametrize("atype", sorted(set(ALERT_TYPES) - {"time_at_utc"}))
def test_ctx_missing_data_all_types_skip(atype):
    """time_at_utc исключён: он не зависит от ctx вовсе (сравнивает 'at' с
    now), поэтому пустой ctx для него не является "нехваткой данных"."""
    extra = MISSING_CTX_EXTRA[atype]
    fired, updated = _run({"id": "a", "type": atype, **extra}, {})
    assert fired == [], f"{atype} не должен был сработать на пустом ctx"
    assert updated["skipped"], f"{atype} должен был попасть в skipped на пустом ctx"
    assert updated["skipped"][0]["id"] == "a"


def test_position_time_elapsed_progress_sufficient_no_fire():
    alert = {"id": "a", "type": "position_time_elapsed", "ticket": 123,
             "minutes": 30, "min_progress_R": 0.3}
    ctx = {"positions": {123: {"opened_utc": NOW - dt.timedelta(minutes=40), "r_multiple": 0.9}}}
    fired, _ = _run(alert, ctx)
    assert fired == []  # время прошло, но прогресс уже достаточный


def test_position_time_elapsed_unknown_progress_skips():
    alert = {"id": "a", "type": "position_time_elapsed", "ticket": 123,
             "minutes": 30, "min_progress_R": 0.3}
    ctx = {"positions": {123: {"opened_utc": NOW - dt.timedelta(minutes=40), "r_multiple": None}}}
    fired, updated = _run(alert, ctx)
    assert fired == []
    assert updated["skipped"][0]["id"] == "a"


def test_ticket_not_found_skips_not_crashes():
    alert = {"id": "a", "type": "sl_jumped", "ticket": 999999}
    fired, updated = _run(alert, {"positions": {123: {"beyond_sl": True}}})  # другой тикет
    assert fired == []
    assert "999999" in updated["skipped"][0]["reason"]


# --------------------------------------------------------------------------
# несколько срабатываний одновременно
# --------------------------------------------------------------------------

def test_multiple_alerts_fire_simultaneously():
    a1 = {"id": "p1", "type": "price_above", "symbol": "XAUUSD", "level": 2400.0}
    a2 = {"id": "p2", "type": "gap", "symbol": "XAUUSD"}
    a3 = {"id": "p3", "type": "price_below", "symbol": "XAUUSD", "level": 5000.0}  # тоже сработает
    ctx = {"symbols": {"XAUUSD": {"price": 2450.0, "bar_gap": True}}}

    fired, updated = evaluate(_alerts_doc(a1, a2, a3), ctx, now=NOW, budget=BUDGET_OK)

    assert {f["id"] for f in fired} == {"p1", "p2", "p3"}
    assert updated["skipped"] == []
    assert len(updated["alerts"]) == 3


# --------------------------------------------------------------------------
# ПРОТУХШИЙ ТИК (регресс 2026-08-01, найден первой обкаткой команды)
# --------------------------------------------------------------------------

def test_price_alerts_do_not_fire_on_a_stale_tick():
    """Цена на закрытом рынке заморожена — это не движение, а его отсутствие.

    БОЕВОЙ СЛУЧАЙ. Суббота, последний тик пятничный (возраст 15.4 часа), цена
    в нём 4046.38. Трейдер спланировал уровни от закрытия H1-бара (4051) и
    взвёл price_below 4050.11. Условие «цена ниже уровня» оказалось истинным
    мгновенно — два алерта из четырёх сгорели в первую же секунду, ещё до
    открытия рынка.

    Датчик ЗНАЛ, что тик протух (tick_stale в снимке, есть даже отдельный тип
    условия data_stale), но ценовые обработчики этот флаг не смотрели. За
    выходные так сгорит любой уровень, оказавшийся не с той стороны от
    замёрзшей цены, и в понедельник трейдер проснётся без будильников.

    Тип data_stale при этом обязан продолжать работать: он существует ровно
    затем, чтобы СООБЩИТЬ о протухших данных.
    """
    from trader_lib.alerts import evaluate

    ctx = {"symbols": {"XAUUSD": {"price": 4046.38, "atr": 15.8,
                                  "tick_stale": True}}}
    doc = {"version": 1, "alerts": [
        {"id": "dn", "type": "price_below", "symbol": "XAUUSD", "level": 4050.11,
         "once": True, "_state": {"armed": True}},
        {"id": "up", "type": "price_above", "symbol": "XAUUSD", "level": 4000.0,
         "once": True, "_state": {"armed": True}},
        {"id": "stale", "type": "data_stale", "symbol": "XAUUSD",
         "once": True, "_state": {"armed": True}},
    ]}
    fired, _ = evaluate(doc, ctx, now=NOW, budget=BUDGET_OK)
    ids = {f["id"] for f in fired}
    assert "dn" not in ids and "up" not in ids, \
        "ценовые условия не должны срабатывать на замёрзшей цене"
    assert "stale" in ids, "сообщить о протухших данных — как раз задача data_stale"


def test_price_alerts_fire_normally_on_a_fresh_tick():
    from trader_lib.alerts import evaluate

    ctx = {"symbols": {"XAUUSD": {"price": 4046.38, "atr": 15.8,
                                  "tick_stale": False}}}
    doc = {"version": 1, "alerts": [
        {"id": "dn", "type": "price_below", "symbol": "XAUUSD", "level": 4050.11,
         "once": True, "_state": {"armed": True}}]}
    fired, _ = evaluate(doc, ctx, now=NOW, budget=BUDGET_OK)
    assert [f["id"] for f in fired] == ["dn"]


def test_unknown_staleness_is_treated_as_stale():
    """Свежесть неизвестна — значит неизвестна. Трактовать «не знаю» как
    «свежий» означало бы читать незнание как разрешение, а весь пакет устроен
    наоборот."""
    from trader_lib.alerts import evaluate

    ctx = {"symbols": {"XAUUSD": {"price": 4046.38, "atr": 15.8,
                                  "tick_stale": None}}}
    doc = {"version": 1, "alerts": [
        {"id": "dn", "type": "price_below", "symbol": "XAUUSD", "level": 4050.11,
         "once": True, "_state": {"armed": True}}]}
    fired, _ = evaluate(doc, ctx, now=NOW, budget=BUDGET_OK)
    assert fired == []


# --------------------------------------------------------------------------
# spread_normalizes — запрошен трейдером-субагентом 2026-08-01
# --------------------------------------------------------------------------

def test_spread_normalizes_fires_when_the_book_comes_back():
    """Контур умел будить на «книга опустела» и не умел на «книга вернулась».

    Для трендовой тактики этого достаточно: расширение спреда — повод НЕ
    входить. Для возвратной наоборот: разъезд спреда лишь повод ЗАМЕТИТЬ, а
    вход происходит на возврате к норме — цена возвращается тогда, когда
    маркет-мейкер вернул котировки. Момент входа фейда был физически
    невооружаем, и трейдеру оставалось опрашивать спред руками по каждому
    ценовому пробуждению.

    Найдено 2026-08-01 трейдером-субагентом на прогоне команды: он отказался
    от входа именно потому, что спред стоял на пике (2.58× при пороге распада
    1.5×), и назвал отсутствие обратного условия профильным пробелом.
    """
    from trader_lib.alerts import evaluate

    doc = {"version": 1, "alerts": [
        {"id": "back", "type": "spread_normalizes", "symbol": "EURUSD",
         "mult": 1.5, "once": True, "_state": {"armed": True}}]}

    wide = {"symbols": {"EURUSD": {"spread_points": 31.0,
                                   "spread_median_points": 12.0}}}
    assert evaluate(doc, wide, now=NOW, budget=BUDGET_OK)[0] == [], \
        "книга ещё пуста — входить рано"

    calm = {"symbols": {"EURUSD": {"spread_points": 17.0,
                                   "spread_median_points": 12.0}}}
    fired, _ = evaluate(doc, calm, now=NOW, budget=BUDGET_OK)
    assert [f["id"] for f in fired] == ["back"]


def test_spread_normalizes_needs_data_like_its_twin():
    """Нет медианы — нет суждения. Незнание не равно норме."""
    from trader_lib.alerts import evaluate

    doc = {"version": 1, "alerts": [
        {"id": "back", "type": "spread_normalizes", "symbol": "EURUSD",
         "mult": 1.5, "once": True, "_state": {"armed": True}}]}
    ctx = {"symbols": {"EURUSD": {"spread_points": 17.0,
                                  "spread_median_points": None}}}
    fired, updated = evaluate(doc, ctx, now=NOW, budget=BUDGET_OK)
    assert fired == []
    assert updated.get("skipped"), "причина пропуска обязана быть названа"


# ============ atr_pctile: явный таймфрейм (инцидент 2026-08-03) ============
# Контекст датчика хранит ОДИН перцентиль на символ, эталон — DEFAULT_TIMEFRAMES[0]
# = M5, выбранный до принятия командой стандарта H1. Трейдер видит в снимке
# H1-число, ставит от него порог, датчик сравнивает с M5.
#
# Цена ошибки в тот день была предметной: трейдер `range` нашла на USDCHF
# подтверждённый бокс и завела ЕДИНСТВЕННОЕ за день условие, способное привести
# к сделке — «ATR сожмётся → тот же бокс даст R:R >= 1.5 без движения цены».
# Порог 0.75 от H1-перцентиля 0.94 сгорел в ту же секунду: на M5 было 0.31.

def _ctx_pctile(*, ref, by_tf):
    return {"symbols": {"USDCHF": {"atr_pctile": ref, "atr_pctile_by_tf": by_tf}}}


def test_без_tf_поведение_прежнее():
    """Уже взведённые алерты не должны молча сменить смысл."""
    from trader_lib.alerts import evaluate

    ctx = _ctx_pctile(ref=0.31, by_tf={"M5": 0.31, "H1": 0.94})
    a = {"id": "x", "type": "atr_pctile_below", "symbol": "USDCHF", "level": 0.75}
    fired, _ = _run(a, ctx)
    assert [f["id"] for f in fired] == ["x"], "эталонный ТФ (0.31 < 0.75) обязан сработать"


def test_с_tf_H1_смотрит_на_H1_а_не_на_эталон():
    """Тот самый случай: на M5 условие истинно, на H1 — нет."""
    from trader_lib.alerts import evaluate

    ctx = _ctx_pctile(ref=0.31, by_tf={"M5": 0.31, "H1": 0.94})
    a = {"id": "chf-vol-decay", "type": "atr_pctile_below", "symbol": "USDCHF",
         "level": 0.75, "tf": "H1"}
    fired, _ = _run(a, ctx)
    assert not fired, "0.94 не ниже 0.75 — алерт не имел права сгореть"


def test_с_tf_H1_срабатывает_когда_сжался_именно_H1():
    from trader_lib.alerts import evaluate

    ctx = _ctx_pctile(ref=0.90, by_tf={"M5": 0.90, "H1": 0.40})
    a = {"id": "chf-vol-decay", "type": "atr_pctile_below", "symbol": "USDCHF",
         "level": 0.75, "tf": "H1"}
    fired, _ = _run(a, ctx)
    assert [f["id"] for f in fired] == ["chf-vol-decay"]
    assert fired[0]["detail"]["tf"] == "H1", "в событии обязан быть виден ТФ"
    assert fired[0]["detail"]["atr_pctile"] == 0.40


def test_запрошенного_tf_нет_это_отказ_а_не_подстановка_эталона():
    """Молча ответить числом ДРУГОГО таймфрейма — ровно тот отказ, ради
    которого поле и заводилось."""
    from trader_lib.alerts import evaluate

    ctx = _ctx_pctile(ref=0.31, by_tf={"M5": 0.31})
    a = {"id": "x", "type": "atr_pctile_below", "symbol": "USDCHF",
         "level": 0.75, "tf": "H4"}
    fired, _ = _run(a, ctx)
    assert not fired, "неизвестный ТФ обязан быть отказом, а не подстановкой эталона"


def test_atr_pctile_above_тоже_понимает_tf():
    from trader_lib.alerts import evaluate

    ctx = _ctx_pctile(ref=0.20, by_tf={"M5": 0.20, "H1": 0.95})
    a = {"id": "x", "type": "atr_pctile_above", "symbol": "USDCHF",
         "level": 0.60, "tf": "H1"}
    fired, _ = _run(a, ctx)
    assert [f["id"] for f in fired] == ["x"]
