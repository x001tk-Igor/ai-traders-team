"""Кто именно торгует (задача владельца счёта 2026-07-27).

Раньше идентичность бралась из конституции — из строки, вписанной человеком
когда-то руками. Запустил на другой модели, забыл поправить конфиг: журнал
пишет чужое имя, две модели смешиваются в одном бакете, калибровка врёт обеим,
и заметить это нельзя ничем — ошибок нет, всё «работает».

Определить модель кодом невозможно: Claude Code не передаёт её имя дочерним
процессам (проверено — в окружении есть версия харнесса и SESSION_ID, имени
модели нет). Поэтому модель объявляет себя сама, а объявление привязывается к
идентификатору сессии. Проверить декларацию нельзя, но ПРОТУХШУЮ — можно, и
именно это здесь и проверяется.
"""
import dataclasses
import datetime as dt
import json

import pytest

from trader_lib.config import load_config
from trader_lib.model_session import (
    SESSION_ENV,
    current,
    declare,
    effective,
    read_declaration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
SID = "2bd18d8e-7483-4ff1-9b95-7e753414c456"
OTHER_SID = "ffffffff-0000-1111-2222-333333333333"


def _cfg(tmp_path, **model):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if model:
        cfg = dataclasses.replace(cfg, model=dataclasses.replace(cfg.model, **model))
    return cfg


# --------------------------------------------------------------------------
# объявление
# --------------------------------------------------------------------------

def test_declaration_is_stored_with_session(tmp_path):
    doc = declare(tmp_path, model_id="qwen-max", profile="weak", now=NOW,
                  session_id=SID)
    assert doc["model_id"] == "qwen-max" and doc["profile"] == "weak"
    assert doc["session_id"] == SID and doc["declared_utc"] == NOW.isoformat()
    assert read_declaration(tmp_path)["model_id"] == "qwen-max"


def test_empty_model_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        declare(tmp_path, model_id="  ", profile="strong", now=NOW)


def test_unknown_profile_downgraded_to_weak(tmp_path):
    """Опечатка в имени профиля не имеет права выдать полные права."""
    doc = declare(tmp_path, model_id="m", profile="стронг", now=NOW, session_id=SID)
    assert doc["profile"] == "weak" and "не распознан" in doc["profile_note"]


# --------------------------------------------------------------------------
# чтение: три источника идентичности
# --------------------------------------------------------------------------

def test_declared_by_this_session_is_ok(tmp_path):
    declare(tmp_path, model_id="claude-opus-5", profile="strong", now=NOW,
            session_id=SID)
    st = current(tmp_path, _cfg(tmp_path), session_id=SID)
    assert st["ok"] is True and st["source"] == "session"
    assert st["model_id"] == "claude-opus-5"


def test_no_declaration_falls_back_to_config_but_not_ok(tmp_path):
    """Записи всё равно подписываются (без подписи хуже), но идентичность
    считается неподтверждённой."""
    st = current(tmp_path, _cfg(tmp_path), session_id=SID)
    assert st["ok"] is False and st["source"] == "config"
    assert st["model_id"] == "claude-opus-5"
    assert "не объявлена" in st["reason"] and "session_start.py" in st["reason"]


def test_declaration_from_another_session_is_stale(tmp_path):
    """ГЛАВНЫЙ ТЕСТ. Другой ПК — другая сессия Claude Code, возможно другая
    модель. Объявление, сделанное там, здесь не действует."""
    declare(tmp_path, model_id="claude-opus-5", profile="strong", now=NOW,
            session_id=OTHER_SID)
    st = current(tmp_path, _cfg(tmp_path), session_id=SID)
    assert st["ok"] is False and st["source"] == "stale"
    assert "другой сессией" in st["reason"]


def test_same_session_after_restart_of_script_is_still_ok(tmp_path):
    """Скрипты — отдельные процессы; объявление живёт в файле и переживает их
    перезапуск, пока сессия та же."""
    declare(tmp_path, model_id="qwen-max", profile="weak", now=NOW, session_id=SID)
    for _ in range(3):
        assert current(tmp_path, _cfg(tmp_path), session_id=SID)["ok"] is True


def test_broken_declaration_is_treated_as_missing(tmp_path):
    (tmp_path / "model_session.json").write_text("{битый", encoding="utf-8")
    st = current(tmp_path, _cfg(tmp_path), session_id=SID)
    assert st["source"] == "config" and st["ok"] is False


def test_session_id_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv(SESSION_ENV, SID)
    declare(tmp_path, model_id="m", profile="strong", now=NOW)
    assert current(tmp_path, _cfg(tmp_path))["ok"] is True
    monkeypatch.setenv(SESSION_ENV, OTHER_SID)
    assert current(tmp_path, _cfg(tmp_path))["source"] == "stale"


def test_effective_always_returns_a_signature(tmp_path):
    """Писать записи без подписи хуже, чем подписать значением из конституции —
    о сомнительности сообщает current(), а не молчание."""
    assert effective(tmp_path, _cfg(tmp_path), session_id=SID) == \
        ("claude-opus-5", "strong")
    declare(tmp_path, model_id="qwen-max", profile="weak", now=NOW, session_id=SID)
    assert effective(tmp_path, _cfg(tmp_path), session_id=SID) == ("qwen-max", "weak")


# --------------------------------------------------------------------------
# связь с гейтом и записями
# --------------------------------------------------------------------------

def test_gate_denies_when_identity_is_not_confirmed(tmp_path, monkeypatch):
    """Неверный model_id не ломает сделку — он ломает ПАМЯТЬ: калибровка и
    by_model считаются по нему, а разделить перемешанные записи задним числом
    нечем."""
    from trader_lib.entry_gate import CHECKS, check_entry
    from trader_lib.mt5_client import FakeMarket

    assert "identity" in CHECKS
    monkeypatch.setenv(SESSION_ENV, SID)
    res = check_entry(market=FakeMarket(), cfg=_cfg(tmp_path), symbol="XAUUSD",
                      side="buy", entry=2400.0, sl=2395.0, rr=2.0,
                      setup_status="подтверждён", now=NOW)
    assert res["checks"]["identity"]["ok"] is False
    assert res["allow"] is False
    assert any("identity" in r for r in res["reasons"])


def test_gate_passes_after_declaration(tmp_path, monkeypatch):
    from trader_lib.entry_gate import check_entry
    from trader_lib.mt5_client import FakeMarket

    monkeypatch.setenv(SESSION_ENV, SID)
    declare(tmp_path, model_id="qwen-max", profile="weak", now=NOW)
    res = check_entry(market=FakeMarket(), cfg=_cfg(tmp_path), symbol="XAUUSD",
                      side="buy", entry=2400.0, sl=2395.0, rr=2.0,
                      setup_status="подтверждён", now=NOW)
    assert res["checks"]["identity"]["ok"] is True
    assert res["checks"]["identity"]["model_id"] == "qwen-max"


def test_records_are_signed_by_the_declared_model(tmp_path, monkeypatch):
    """Запись подписывается тем, кто объявился, а НЕ строкой из конституции:
    иначе на другом ПК статистика двух моделей смешается молча."""
    import scripts.report as report
    from trader_lib.journal import read_records

    monkeypatch.setenv(SESSION_ENV, SID)
    cfg = _cfg(tmp_path, id="claude-opus-5")
    declare(tmp_path, model_id="qwen-max", profile="weak", now=NOW)
    report.observed(cfg, symbol="XAUUSD", alert_id="a1", alert_type="price_below",
                    level=1, price=1, regime="флет", reasoning="жду", now=NOW)
    rec = read_records(tmp_path / "journal.jsonl")[0]
    assert rec["model_id"] == "qwen-max" and rec["model_profile"] == "weak"


def test_no_module_takes_identity_from_the_config():
    """Структурный запрет: имя модели берётся из объявления сеанса, а не из
    конституции.

    Аудит 2026-07-27 нашёл четыре места, где подпись бралась из конфига
    (exit.py, brief.py, recall.py, alert_watch.py). Симптом молчаливый: вход
    подписан Sonnet, выход — Opus из конфига; калибровка и by_model
    разъезжаются, и разделить их задним числом нечем. Единственное законное
    место обращения к конституции — сам model_session (fallback, когда
    объявления нет).
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    allowed = {root / "trader_lib" / "model_session.py"}
    offenders = []
    for p in [*(root / "scripts").glob("*.py"), *(root / "trader_lib").glob("*.py")]:
        if p in allowed:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bcfg\.model\.id\b", line) and "except" not in line:
                # допускается только как аварийный запасной вариант рядом с
                # effective_model — тогда в файле обязан быть его импорт
                if "effective_model" in p.read_text(encoding="utf-8"):
                    continue
                offenders.append(f"{p.name}:{i}")
    assert offenders == [], f"идентичность взята из конституции: {offenders}"
