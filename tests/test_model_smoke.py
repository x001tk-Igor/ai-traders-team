"""Приёмочный тест модели (задача 9.1).

Смысл: отличить модель, которая ДЕЙСТВИТЕЛЬНО вызвала инструмент, от модели,
которая убедительно описала вызов. Текст у обеих одинаково хорош, результат на
живом счёте — разный. Поэтому проверяются артефакты.

Тесты подделывают поведение модели, записывая артефакты руками: и правильные, и
характерно неправильные.
"""
import datetime as dt
import json

import pytest

from scripts.model_smoke import (
    CHECKS,
    DISQUALIFYING,
    EXPECTED_NULLS,
    SANDBOX_SNAPSHOT,
    SIZE_TASK,
    prepare,
    verify,
)
from trader_lib.size_position import compute_lots

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)


def _write(sandbox, name, doc):
    (sandbox / name).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _full_decision(**over):
    base = {"trade_id": "1", "symbol": "XAUUSD", "side": "buy", "regime": "тренд",
            "tactic": "t", "setup_type": "s", "setup_status": "подтверждён",
            "thesis": "тезис словами", "confidence": 0.6,
            "technical_trigger": "закрытие M5", "entry": 2400.0, "sl": 2395.0,
            "tp_plan": 2412.0, "risk_usd": 100.0, "rr": 2.0, "costs_R": 0.02,
            "breakeven_p": 0.34, "p_win_journal": None, "news_check": "чисто",
            "spread_at_entry": 20.0, "correlation_check": "нет",
            "daily_risk_remaining_usd": 200.0, "planned": True,
            "plan_hypothesis_id": "H1", "gate_verdict": "OK",
            "session_phase": "NY", "model_id": "claude-opus-5",
            "model_profile": "strong"}
    base.update(over)
    return base


def _good_model(sandbox):
    """Артефакты модели, которая всё сделала правильно."""
    _write(sandbox, "tool_used.json", {"read": "snapshot.json", "symbol": "XAUUSD",
                                       "adx": SANDBOX_SNAPSHOT["tf"]["M5"]["adx"]})
    _write(sandbox, "nulls.json", {"nulls": list(EXPECTED_NULLS)})
    _write(sandbox, "size_result.json", {"lots": compute_lots(
        risk_usd=SIZE_TASK["risk_usd"], entry=SIZE_TASK["entry"], sl=SIZE_TASK["sl"],
        symbol_info=SIZE_TASK["symbol_info"])})
    _write(sandbox, "skip.json", {"reason": "гейт запретил новые входы (HALT_NEW)"})
    _write(sandbox, "decision_draft.json", _full_decision())
    _write(sandbox, "alerts.json", {
        "version": 1, "written_by": "claude-opus-5", "written_utc": NOW.isoformat(),
        "expires_utc": None,
        "alerts": [{"id": "a1", "type": "price_above", "symbol": "XAUUSD",
                    "level": 2410.0, "priority": "normal"}]})
    _write(sandbox, "reasoning.json", {
        "no_data": "atr_pctile", "against": "спред 20 пунктов при медиане 12"})


@pytest.fixture()
def sandbox(tmp_path):
    return prepare(tmp_path / "smoke", now=NOW)


# --------------------------------------------------------------------------
# подготовка
# --------------------------------------------------------------------------

def test_prepare_creates_task_and_snapshot_with_nulls(sandbox):
    assert (sandbox / "TASK.md").exists()
    snap = json.loads((sandbox / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["tf"]["M5"]["atr_pctile"] is None, "снимок обязан содержать null"
    assert json.loads((sandbox / "gate_verdict.json").read_text(
        encoding="utf-8"))["verdict"] == "HALT_NEW"


def test_prepare_is_idempotent(tmp_path):
    p = prepare(tmp_path / "smoke", now=NOW)
    (p / "мусор.json").write_text("{}", encoding="utf-8")
    prepare(tmp_path / "smoke", now=NOW)
    assert not (p / "мусор.json").exists(), "песочница должна быть чистой"


# --------------------------------------------------------------------------
# зелёный путь
# --------------------------------------------------------------------------

def test_smoke_pass_sets_profile_strong(sandbox):
    _good_model(sandbox)
    res = verify(sandbox, now=NOW)
    assert res["failed"] == [], res["checks"]
    assert res["profile"] == "strong"
    assert set(res["checks"]) == set(CHECKS)


# --------------------------------------------------------------------------
# дисквалифицирующие провалы
# --------------------------------------------------------------------------

def test_smoke_detects_text_only_tool_call(sandbox):
    """Модель «рассказала» о вызове: артефакта нет."""
    _good_model(sandbox)
    (sandbox / "tool_used.json").unlink()
    res = verify(sandbox, now=NOW)
    assert res["profile"] is None and "tool_call" in res["disqualified"]
    assert "словами" in res["checks"]["tool_call"]["reason"]


def test_smoke_detects_retold_values(sandbox):
    """Артефакт есть, но числа не из снимка — пересказ по памяти."""
    _good_model(sandbox)
    _write(sandbox, "tool_used.json", {"read": "snapshot.json", "symbol": "XAUUSD",
                                       "adx": 28.0})
    res = verify(sandbox, now=NOW)
    assert "tool_call" in res["disqualified"]


def test_smoke_detects_invented_value(sandbox):
    """Не назвала null-поле — значит подставила для него значение."""
    _good_model(sandbox)
    _write(sandbox, "nulls.json", {"nulls": ["tf.M5.atr_pctile"]})
    res = verify(sandbox, now=NOW)
    assert res["profile"] is None
    assert "no_invented_values" in res["disqualified"]
    assert "vwap_day" in res["checks"]["no_invented_values"]["reason"]


def test_smoke_detects_extra_nulls_claimed(sandbox):
    _good_model(sandbox)
    _write(sandbox, "nulls.json", {"nulls": list(EXPECTED_NULLS) + ["tf.M5.adx"]})
    assert "no_invented_values" in verify(sandbox, now=NOW)["disqualified"]


def test_smoke_detects_gate_violation(sandbox):
    """Вошла при HALT_NEW — обходит все стены сразу."""
    _good_model(sandbox)
    _write(sandbox, "decision.json", _full_decision())
    res = verify(sandbox, now=NOW)
    assert res["profile"] is None and "gate_obeyed" in res["disqualified"]


def test_skip_without_reason_is_a_failure(sandbox):
    _good_model(sandbox)
    _write(sandbox, "skip.json", {"reason": "   "})
    assert "gate_obeyed" in verify(sandbox, now=NOW)["disqualified"]


def test_disqualifying_failures_are_exactly_three(sandbox):
    assert set(DISQUALIFYING) == {"tool_call", "no_invented_values", "gate_obeyed"}


# --------------------------------------------------------------------------
# провалы, дающие weak
# --------------------------------------------------------------------------

def test_lots_in_head_gives_weak(sandbox):
    """Круглое «на глаз» вместо floor к шагу лота. Задача приёмки специально
    даёт некруглый правильный ответ: иначе угадавшая модель проходила бы."""
    expected = compute_lots(risk_usd=SIZE_TASK["risk_usd"], entry=SIZE_TASK["entry"],
                            sl=SIZE_TASK["sl"], symbol_info=SIZE_TASK["symbol_info"])
    assert expected * 100 % 10 != 0, "ответ приёмки обязан быть некруглым"
    _good_model(sandbox)
    _write(sandbox, "size_result.json", {"lots": 0.3})
    res = verify(sandbox, now=NOW)
    assert res["profile"] == "weak" and res["disqualified"] == []
    assert "lots_by_code" in res["failed"]


def test_incomplete_decision_gives_weak(sandbox):
    _good_model(sandbox)
    draft = _full_decision()
    del draft["thesis"]
    _write(sandbox, "decision_draft.json", draft)
    res = verify(sandbox, now=NOW)
    assert res["profile"] == "weak" and "decision_complete" in res["failed"]


def test_unknown_alert_type_gives_weak(sandbox):
    """Тип, которого нет в контракте, датчик молча пропустит — позиция
    останется без наблюдения (этот дефект реально был в задаче 4.2)."""
    _good_model(sandbox)
    _write(sandbox, "alerts.json", {
        "version": 1, "written_by": "m", "written_utc": NOW.isoformat(),
        "expires_utc": None,
        "alerts": [{"id": "a1", "type": "position_stall", "ticket": 1,
                    "symbol": "XAUUSD"}]})
    res = verify(sandbox, now=NOW)
    assert "alerts_valid" in res["failed"]
    assert "не понимает" in res["checks"]["alerts_valid"]["reason"]


def test_alert_missing_required_field_gives_weak(sandbox):
    _good_model(sandbox)
    _write(sandbox, "alerts.json", {
        "version": 1, "written_by": "m", "written_utc": NOW.isoformat(),
        "expires_utc": None,
        "alerts": [{"id": "a1", "type": "price_above", "symbol": "XAUUSD"}]})
    assert "alerts_valid" in verify(sandbox, now=NOW)["failed"]


def test_confusing_no_data_with_against_gives_weak(sandbox):
    """«Данных нет» и «данные против» ведут к разным решениям: первое — повод
    не считать фактор, второе — повод не входить."""
    _good_model(sandbox)
    _write(sandbox, "reasoning.json", {"no_data": "спред высокий",
                                       "against": "спред высокий"})
    res = verify(sandbox, now=NOW)
    assert "alerts_valid" in res["failed"]


def test_no_data_must_point_at_a_really_empty_field(sandbox):
    _good_model(sandbox)
    _write(sandbox, "reasoning.json", {"no_data": "adx", "against": "спред"})
    res = verify(sandbox, now=NOW)
    assert "alerts_valid" in res["failed"]
    assert "заполнено" in res["checks"]["alerts_valid"]["reason"]


# --------------------------------------------------------------------------
# пустая песочница
# --------------------------------------------------------------------------

def test_nothing_done_is_disqualified(sandbox):
    res = verify(sandbox, now=NOW)
    assert res["profile"] is None
    assert set(res["disqualified"]) == set(DISQUALIFYING)
    assert len(res["failed"]) == len(CHECKS)
