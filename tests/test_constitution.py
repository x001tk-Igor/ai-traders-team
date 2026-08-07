"""Защита конституции (задача 8.2).

Смысл проверки: агент не может ТИХО изменить свои лимиты. Не «не может
изменить» — эту формулировку модуль намеренно не использует: модель, имеющая
право писать файлы, может записать и подтверждение. Граница операционная —
расхождение попадает в гейт, уведомления и отчёт, то есть у человека остаётся
след.

Главный тест здесь — test_first_run_without_ack_blocks: защита, включающаяся
со второго раза, бесполезна ровно в тот момент, когда пакет только развернули
на новом ПК и никто ещё не смотрел, какие в нём лимиты.
"""
import dataclasses
import datetime as dt
import json

import pytest

from trader_lib.config import load_config
from trader_lib.constitution import (
    GUARDED_BLOCKS,
    HASH_FILE,
    check_config,
    config_hash,
    read_ack,
    write_ack,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)


def _raw():
    return json.loads(open("config/trader.config.json", encoding="utf-8").read())


def _cfg(tmp_path):
    cfg = load_config("config/trader.config.json")
    return dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})


# --------------------------------------------------------------------------
# хэш
# --------------------------------------------------------------------------

def test_hash_is_stable_and_key_order_independent():
    """Переформатирование конфига не должно выглядеть как изменение лимитов:
    иначе человек привыкает подтверждать «ложные срабатывания»."""
    raw = _raw()
    shuffled = {k: raw[k] for k in reversed(list(raw))}
    assert config_hash(raw) == config_hash(shuffled)


@pytest.mark.parametrize("block,field,value", [
    ("risk", "daily_loss_limit_pct", 10.0),
    ("risk", "per_trade_risk_cap_pct", 5.0),
    ("session", "no_new_after_utc", "23:00"),
    ("news", "fail_mode", "allow"),
    ("alerts", "max_events_per_day", 999),
    ("instruments", "spread_anomaly_mult", 99.0),
])
def test_hash_changes_on_any_risk_relevant_edit(block, field, value):
    raw = _raw()
    before = config_hash(raw)
    raw[block][field] = value
    assert config_hash(raw) != before, f"правка {block}.{field} прошла незаметно"


def test_model_block_is_not_guarded():
    """Смена модели — законное действие при переносе, и требовать подтверждения
    на каждую значило бы приучить подтверждать не глядя."""
    raw = _raw()
    before = config_hash(raw)
    raw["model"]["id"] = "другая-модель"
    assert config_hash(raw) == before


def test_guarded_blocks_cover_all_risk_limits():
    """Если в конфиге появится новый риск-блок, он обязан попасть под охрану —
    иначе защита тихо перестанет покрывать часть лимитов."""
    raw = _raw()
    unguarded = set(raw) - set(GUARDED_BLOCKS)
    assert unguarded == {"account", "goal", "model", "perception", "learning", "loop"}, \
        f"новый блок конфига вне охраны: {unguarded}"


# --------------------------------------------------------------------------
# подтверждение
# --------------------------------------------------------------------------

def test_first_run_without_ack_blocks(tmp_path):
    """Защита, включающаяся со второго раза, бесполезна: первый прогон на новом
    ПК шёл бы с любыми лимитами, какие оказались в файле."""
    verdict = check_config(_raw(), tmp_path / HASH_FILE)
    assert verdict["ok"] is False
    assert "не подтверждена" in verdict["reason"]


def test_hash_accepts_after_explicit_ack(tmp_path):
    raw = _raw()
    write_ack(tmp_path / HASH_FILE, config_dict=raw, now=NOW)
    verdict = check_config(raw, tmp_path / HASH_FILE)
    assert verdict["ok"] is True
    assert read_ack(tmp_path / HASH_FILE) == config_hash(raw)


def test_config_hash_mismatch_blocks_trading(tmp_path):
    raw = _raw()
    write_ack(tmp_path / HASH_FILE, config_dict=raw, now=NOW)
    raw["risk"]["daily_loss_limit_pct"] = 10.0
    verdict = check_config(raw, tmp_path / HASH_FILE)
    assert verdict["ok"] is False and "изменена" in verdict["reason"]


def test_ack_records_who_and_when(tmp_path):
    doc = write_ack(tmp_path / HASH_FILE, config_dict=_raw(), now=NOW,
                    note="поднял лимит по просьбе")
    assert doc["acked_utc"] == NOW.isoformat() and doc["acked_by"]
    assert doc["note"] == "поднял лимит по просьбе"
    assert doc["blocks"] == list(GUARDED_BLOCKS)


def test_corrupted_ack_requires_new_confirmation(tmp_path):
    p = tmp_path / HASH_FILE
    p.write_text("{битый", encoding="utf-8")
    assert read_ack(p) is None
    assert check_config(_raw(), p)["ok"] is False


# --------------------------------------------------------------------------
# связь с предвходовым гейтом
# --------------------------------------------------------------------------

def test_entry_gate_denies_on_changed_constitution(tmp_path, monkeypatch):
    """Расхождение обязано доходить до отказа во входе, а не оставаться
    диагностикой в отчёте."""
    from trader_lib.entry_gate import CHECKS, check_entry
    from trader_lib.mt5_client import FakeMarket

    assert "constitution" in CHECKS

    raw = _raw()
    write_ack(tmp_path / HASH_FILE, config_dict=raw, now=NOW)
    tampered = tmp_path / "tampered.json"
    raw["risk"]["daily_loss_limit_pct"] = 10.0
    tampered.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    res = check_entry(market=FakeMarket(), cfg=_cfg(tmp_path), symbol="XAUUSD",
                      side="buy", entry=2400.0, sl=2395.0, rr=2.0,
                      setup_status="подтверждён", now=NOW,
                      config_path=str(tampered))
    assert res["allow"] is False
    assert res["checks"]["constitution"]["ok"] is False
    assert any("конституция" in r.lower() for r in res["reasons"])


def test_entry_gate_passes_constitution_when_acked(tmp_path):
    from trader_lib.entry_gate import check_entry
    from trader_lib.mt5_client import FakeMarket

    write_ack(tmp_path / HASH_FILE, config_dict=_raw(), now=NOW)
    res = check_entry(market=FakeMarket(), cfg=_cfg(tmp_path), symbol="XAUUSD",
                      side="buy", entry=2400.0, sl=2395.0, rr=2.0,
                      setup_status="подтверждён", now=NOW)
    assert res["checks"]["constitution"]["ok"] is True
