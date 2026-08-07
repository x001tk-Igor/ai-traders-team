import ast
import inspect
import json

import pytest

import trader_lib.journal as journal_module
from trader_lib.journal import (
    ALERT_EVENT_REQUIRED,
    DECISION_REQUIRED,
    SKIP_REQUIRED,
    append_alert_event,
    append_decision,
    append_outcome,
    append_skip,
    read_records,
    validate_decision,
)
from trader_lib.risk_gate import BLOCKED_CODES, TERM_KEYS

EXPECTED_APPEND_WRITERS = {"append_decision", "append_outcome", "append_skip",
                          "append_alert_event", "append_intent", "append_trade_event",
                          "append_observation"}


def _calls_append_primitive(func_node) -> bool:
    """Обходит тело функции верхнего уровня (ast.FunctionDef) и проверяет,
    есть ли в нём вызов _append — единственного низкоуровневого примитива
    файловой записи journal.py. Общий хелпер для test_no_second_decision_
    write_path и test_guard_catches_renamed_bypass_functions — раньше это
    была одна и та же логика, скопированная в оба теста дословно, что давало
    им шанс молча разойтись при правке одного и забытой правке другого."""
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "_append"
              for node in ast.walk(func_node))


def _writers_in(source) -> set:
    """Множество имён функций верхнего уровня в исходнике source, которые
    вызывают _append где-то в своём теле (см. _calls_append_primitive)."""
    tree = ast.parse(source)
    return {node.name for node in tree.body
           if isinstance(node, ast.FunctionDef) and _calls_append_primitive(node)}


def _full_skip(**overrides):
    base = {
        "setup_type": "ema_pullback",
        "reason": "спред выше порога на входе",
        "confidence": 0.4,
        "regime": "flat",
        "model_id": "claude-sonnet-5",
        "model_profile": "strong",
    }
    base.update(overrides)
    return base


def _full_alert_event(**overrides):
    base = {
        "alert_id": "al-1",
        "alert_type": "spread_spike",
        "model_id": "claude-sonnet-5",
    }
    base.update(overrides)
    return base


# --- базовый round-trip, через настоящий append_decision + фикстуру make_decision ---

def test_append_and_read(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    append_decision(j, make_decision(trade_id="a1"))
    append_outcome(j, {"trade_id": "a1", "R": 1.8, "exit_reason": "tp"})
    recs = read_records(j)
    assert len(recs) == 2
    assert recs[0]["type"] == "decision" and recs[1]["type"] == "outcome"
    assert recs[0]["trade_id"] == recs[1]["trade_id"]


def test_read_missing_returns_empty(tmp_path):
    assert read_records(tmp_path / "nope.jsonl") == []


# --- обязательные тесты задачи 2.1 ---

def test_incomplete_decision_rejected(tmp_path):
    j = tmp_path / "journal.jsonl"
    rec = {"trade_id": "t1", "symbol": "XAUUSD", "side": "buy"}
    assert validate_decision(rec) != []
    with pytest.raises(ValueError):
        append_decision(j, rec)
    assert read_records(j) == []  # отказ ничего не пишет на диск


def test_full_decision_written(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    full = make_decision()
    assert validate_decision(full) == []
    out = append_decision(j, full)
    assert out["type"] == "decision"
    assert "ts" in out

    recs = read_records(j)
    assert len(recs) == 1
    rec = recs[0]
    for field in DECISION_REQUIRED:
        assert rec[field] == full[field], f"поле {field} не пережило запись/чтение"


def test_skip_record_allowed(tmp_path):
    j = tmp_path / "journal.jsonl"
    rec = _full_skip()
    out = append_skip(j, rec)
    assert out["type"] == "skip"
    assert "entry" not in out and "sl" not in out and "risk_usd" not in out

    recs = read_records(j)
    assert len(recs) == 1
    assert recs[0]["type"] == "skip"
    for field in SKIP_REQUIRED:
        assert recs[0][field] == rec[field]


def test_model_id_required(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    rec = make_decision()
    del rec["model_id"]
    assert "model_id" in validate_decision(rec)
    with pytest.raises(ValueError):
        append_decision(j, rec)


def test_alert_event_appends(tmp_path):
    j = tmp_path / "journal.jsonl"
    rec = _full_alert_event(snapshot={"spread_points": 42}, response="skip_setup")
    out = append_alert_event(j, rec)
    assert out["type"] == "alert_event"

    recs = read_records(j)
    assert len(recs) == 1
    assert recs[0]["alert_id"] == "al-1"
    assert recs[0]["alert_type"] == "spread_spike"
    assert recs[0]["model_id"] == "claude-sonnet-5"
    assert recs[0]["snapshot"] == {"spread_points": 42}
    assert recs[0]["response"] == "skip_setup"


# --- единственный путь записи (см. шапку journal.py) ---

def test_no_second_decision_write_path():
    """Инвариант: _append — единственный низкоуровневый примитив файловой
    записи в модуле, и ровно четыре функции его вызывают:
    append_decision/append_outcome/append_skip/append_alert_event.

    Проверка СТРУКТУРНАЯ, по поведению, а не по имени: разбираем исходник
    journal.py через ast, обходим все функции верхнего уровня и находим те,
    в чьём теле встречается вызов _append. Первая версия этого guard'а
    фильтровала по имени (`append_*decision*`) и не заметила ни
    `write_decision`, ни `append_dec` — обе пишут через _append без всякой
    валидации, но не совпадают с шаблоном имени. Текущая версия ловит любую
    функцию независимо от названия, потому что смотрит на факт вызова
    _append, а не на подстроку в имени — попытка добавить писателя под
    любым именем меняет множество writers и роняет assert, пока её не
    внесут в EXPECTED_APPEND_WRITERS осознанно.

    Граница гарантии (см. подробно в шапке journal.py, включая самый
    дешёвый обход — импорт _append напрямую в другом модуле, которого этот
    тест НЕ видит вовсе, потому что разбирает только journal.py): функция,
    которая реализует запись в файл заново (свой open()/write()) или
    вызывает _append из другого модуля, этим тестом не ловится — тест
    доказывает "нет второго пути ЧЕРЕЗ _append ВНУТРИ journal.py", а не "нет
    никакого другого способа дописать в файл".
    """
    writers = _writers_in(inspect.getsource(journal_module))
    assert writers == EXPECTED_APPEND_WRITERS


def test_guard_catches_renamed_bypass_functions():
    """Мутационная проверка самого guard'а: воспроизводит обе дыры, которые
    нашёл ревьюер у предыдущей (по имени) версии — write_decision и
    append_dec, функционально идентичные обходу валидации, но не совпадающие
    с шаблоном `append_*decision*`. Обе обязаны появиться в writers, если их
    добавить в модуль, — иначе guard снова ловит только имя, а не поведение."""
    poc_writers = '''
def write_decision(path, rec):
    out = {"type": "decision", **rec}
    _append(path, out)

def append_dec(path, rec):
    out = {"type": "decision", **rec}
    _append(path, out)
'''
    writers = _writers_in(inspect.getsource(journal_module) + poc_writers)

    assert "write_decision" in writers
    assert "append_dec" in writers
    assert writers != EXPECTED_APPEND_WRITERS  # guard бы упал на этом исходнике


# --- каждое обязательное поле проверено индивидуально (урок №1: дефект живёт
# в поле, которое есть в контракте, но не проверено ни одним тестом) ---

@pytest.mark.parametrize("field", DECISION_REQUIRED)
def test_decision_each_required_field_enforced(field, tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    rec = make_decision()
    del rec[field]
    assert field in validate_decision(rec)
    with pytest.raises(ValueError):
        append_decision(j, rec)
    assert read_records(j) == []


@pytest.mark.parametrize("field", SKIP_REQUIRED)
def test_skip_each_required_field_enforced(field, tmp_path):
    j = tmp_path / "journal.jsonl"
    rec = _full_skip()
    del rec[field]
    with pytest.raises(ValueError):
        append_skip(j, rec)
    assert read_records(j) == []


@pytest.mark.parametrize("bad_confidence", [1.5, -0.2, 1.0001, "high"])
def test_skip_confidence_out_of_range_rejected(bad_confidence, tmp_path):
    """Тот же инвариант, что у decision (test_confidence_out_of_range_
    rejected), покрытый одинаково для skip: confidence фиксирует уверенность
    модели и при осознанном отказе от входа, не только при входе, поэтому
    испорченное значение так же нельзя молча пропустить в журнал (см.
    _check_confidence в journal.py)."""
    j = tmp_path / "journal.jsonl"
    rec = _full_skip(confidence=bad_confidence)
    with pytest.raises(ValueError):
        append_skip(j, rec)
    assert read_records(j) == []


def test_skip_confidence_boundary_values_accepted(tmp_path):
    j = tmp_path / "journal.jsonl"
    for edge in (0.0, 1.0):
        rec = _full_skip(confidence=edge, setup_type=f"s-{edge}")
        out = append_skip(j, rec)
        assert out["confidence"] == edge


@pytest.mark.parametrize("field", ALERT_EVENT_REQUIRED)
def test_alert_event_each_required_field_enforced(field, tmp_path):
    j = tmp_path / "journal.jsonl"
    rec = _full_alert_event()
    del rec[field]
    with pytest.raises(ValueError):
        append_alert_event(j, rec)
    assert read_records(j) == []


def test_validate_decision_lists_all_missing_fields_not_just_first():
    rec = {"trade_id": "t1", "symbol": "XAUUSD"}
    problems = validate_decision(rec)
    expected_missing = [f for f in DECISION_REQUIRED if f not in rec]
    assert problems == expected_missing
    assert len(problems) > 5  # много полей сразу, не одно за цикл


def test_read_legacy_format_without_new_fields(tmp_path):
    """Запись старого формата (до этой схемы) — без regime/model_id и прочих
    новых полей — должна читаться без исключений. Валидация — только на
    запись, не на чтение."""
    j = tmp_path / "journal.jsonl"
    legacy = {"type": "decision", "trade_id": "legacy1", "symbol": "XAUUSD",
              "setup_type": "s1", "confidence": 0.6, "action": "buy", "risk_usd": 40}
    j.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

    recs = read_records(j)
    assert len(recs) == 1
    assert recs[0]["trade_id"] == "legacy1"
    assert "regime" not in recs[0]
    assert "model_id" not in recs[0]


@pytest.mark.parametrize("bad_confidence", [1.5, -0.2, 1.0001, "high"])
def test_confidence_out_of_range_rejected(bad_confidence, tmp_path, make_decision):
    """Решение: confidence вне [0,1] ОТКЛОНЯЕТСЯ (не пропускается). Это число
    со строгой математической областью, напрямую питающее калибровку
    (score.py) — испорченное значение молча ломает калибровку навсегда без
    возможности исправить задним числом. См. обоснование в шапке journal.py."""
    j = tmp_path / "journal.jsonl"
    rec = make_decision(confidence=bad_confidence)
    problems = validate_decision(rec)
    assert any("confidence" in p for p in problems)
    with pytest.raises(ValueError):
        append_decision(j, rec)


def test_confidence_boundary_values_accepted(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    for edge, tid in [(0.0, "t-low"), (1.0, "t-high")]:
        rec = make_decision(confidence=edge, trade_id=tid)
        assert validate_decision(rec) == []
        out = append_decision(j, rec)
        assert out["confidence"] == edge


def test_p_win_journal_none_accepted(tmp_path, make_decision):
    """p_win_journal обязателен как КЛЮЧ, но None — легитимное значение,
    когда по сетапу ещё нет выборки (см. комментарий поля в journal.py) —
    структурно тот же случай, что и plan_hypothesis_id=None."""
    j = tmp_path / "journal.jsonl"
    rec = make_decision(p_win_journal=None)
    assert validate_decision(rec) == []
    out = append_decision(j, rec)
    assert out["p_win_journal"] is None


def test_planned_false_without_hypothesis_id_allowed(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    rec = make_decision(planned=False, plan_hypothesis_id=None)
    assert validate_decision(rec) == []
    out = append_decision(j, rec)
    assert out["planned"] is False
    assert out["plan_hypothesis_id"] is None


def test_planned_true_without_hypothesis_id_rejected(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"
    rec = make_decision(planned=True, plan_hypothesis_id=None)
    problems = validate_decision(rec)
    assert any("plan_hypothesis_id" in p for p in problems)
    with pytest.raises(ValueError):
        append_decision(j, rec)


def test_blocked_by_and_binding_term_validated_against_gate_codes(tmp_path, make_decision):
    j = tmp_path / "journal.jsonl"

    bad_blocked = make_decision(blocked_by="totally_made_up_code")
    assert any("blocked_by" in p for p in validate_decision(bad_blocked))
    with pytest.raises(ValueError):
        append_decision(j, bad_blocked)

    bad_term = make_decision(binding_term="totally_made_up_term")
    assert any("binding_term" in p for p in validate_decision(bad_term))
    with pytest.raises(ValueError):
        append_decision(j, bad_term)

    ok = make_decision(blocked_by=None, binding_term=TERM_KEYS[0])
    assert validate_decision(ok) == []
    out = append_decision(j, ok)
    assert out["binding_term"] == TERM_KEYS[0]

    # blocked_by реально из словаря — тоже допустимо (запись контекста
    # заблокированного решения, не обязательно вошедшей сделки)
    ok2 = make_decision(blocked_by=BLOCKED_CODES[0], trade_id="t-blocked")
    assert validate_decision(ok2) == []
