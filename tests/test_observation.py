"""Наблюдение — третий исход пробуждения (задача владельца счёта 2026-07-27).

До него различались только «вошла» и «отказалась от названного сетапа». Самый
частый случай — «увидела, но жду отката» — не оставлял следа: в журнале только
alert_event, и дневной разбор считал такое пробуждение ПУСТЫМ, помечая
разбудивший алерт мусорным. То есть система наказывала правильное ожидание
наравне с бесполезным будильником, а trader-reflect по её совету выбросил бы
работающее условие.

Здесь проверяется, что след появляется и что метрика перестала врать.
"""
import dataclasses
import datetime as dt
import json

import pytest

from scripts.review import build_review
from trader_lib.config import load_config
from trader_lib.journal import (
    OBSERVATION_REQUIRED,
    append_observation,
    read_records,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)


def _full(**over):
    base = {"reasoning": "уровень достигнут, но закрепления нет — жду закрытия M15",
            "regime": "тренд вниз", "model_id": "claude-opus-5",
            "symbol": "XAUUSD", "alert_id": "h1-trigger"}
    base.update(over)
    return base


def _cfg(tmp_path):
    cfg = load_config("config/trader.config.json")
    return dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})


# --------------------------------------------------------------------------
# запись
# --------------------------------------------------------------------------

def test_observation_written(tmp_path):
    j = tmp_path / "journal.jsonl"
    rec = append_observation(j, _full())
    assert rec["type"] == "observation" and rec["ts"]
    saved = read_records(j)[0]
    assert saved["reasoning"].startswith("уровень достигнут")
    assert saved["alert_id"] == "h1-trigger"


@pytest.mark.parametrize("missing", OBSERVATION_REQUIRED)
def test_missing_required_field_rejected(tmp_path, missing):
    rec = _full()
    del rec[missing]
    with pytest.raises(ValueError):
        append_observation(tmp_path / "journal.jsonl", rec)


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_empty_reasoning_rejected(tmp_path, empty):
    """«Посмотрела, ничего» не отличимо от отсутствия записи — смысл ровно в
    словах модели."""
    with pytest.raises(ValueError) as e:
        append_observation(tmp_path / "journal.jsonl", _full(reasoning=empty))
    assert "reasoning" in str(e.value)


def test_nothing_written_when_rejected(tmp_path):
    j = tmp_path / "journal.jsonl"
    with pytest.raises(ValueError):
        append_observation(j, _full(reasoning=""))
    assert not j.exists() or read_records(j) == []


# --------------------------------------------------------------------------
# метрика пробуждений
# --------------------------------------------------------------------------

def _event(alert_id, hours_ago=1):
    ts = (NOW - dt.timedelta(hours=hours_ago)).isoformat()
    return {"type": "alert_event", "ts": ts, "fired_utc": ts, "alert_id": alert_id,
            "alert_type": "price_below", "model_id": "claude-opus-5",
            "priority": "normal", "delivered": True}


def _state(tmp_path, *, journal=(), events=()):
    (tmp_path / "journal.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in journal),
        encoding="utf-8")
    (tmp_path / "alert_events.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in events),
        encoding="utf-8")
    (tmp_path / "news_cache.json").write_text(json.dumps(
        {"fetched_utc": NOW.isoformat(), "events": []}), encoding="utf-8")


def test_observation_counts_as_useful_wake(tmp_path):
    """ГЛАВНЫЙ ТЕСТ. Алерт разбудил, модель посмотрела и обосновала ожидание —
    пробуждение полезное, алерт не мусорный."""
    obs = {"type": "observation", "ts": (NOW - dt.timedelta(hours=1)).isoformat(),
           **_full()}
    _state(tmp_path, journal=[obs], events=[_event("h1-trigger")])

    a = build_review(_cfg(tmp_path), now=NOW)["alert_efficiency"]
    assert a["delivered"] == 1
    assert a["with_observation"] == 1 and a["ignored"] == 0
    assert a["usefulness"] == 1.0
    assert a["noisy_alerts"] == [], "полезный алерт не должен попадать в мусорные"


def test_wake_without_any_record_is_still_empty(tmp_path):
    """Обратная сторона: пробуждение, на которое модель не ответила ничем,
    по-прежнему считается пустым — иначе метрика потеряла бы смысл."""
    _state(tmp_path, events=[_event("noise-1")])
    a = build_review(_cfg(tmp_path), now=NOW)["alert_efficiency"]
    assert a["ignored"] == 1 and a["usefulness"] == 0.0
    assert a["noisy_alerts"] == [{"alert_id": "noise-1", "count": 1}]


def test_decision_wins_over_observation_for_the_same_alert(tmp_path):
    """Если по одному алерту есть и наблюдение, и вход — это вход, а не два
    разных ответа: иначе одно пробуждение считалось бы дважды."""
    obs = {"type": "observation", "ts": (NOW - dt.timedelta(hours=2)).isoformat(),
           **_full()}
    dec = {"type": "decision", "ts": (NOW - dt.timedelta(hours=1)).isoformat(),
           "trade_id": "1", "alert_id": "h1-trigger", "symbol": "XAUUSD",
           "setup_type": "s", "confidence": 0.6, "regime": "r",
           "model_id": "claude-opus-5", "planned": True, "risk_usd": 50.0}
    _state(tmp_path, journal=[obs, dec], events=[_event("h1-trigger")])

    a = build_review(_cfg(tmp_path), now=NOW)["alert_efficiency"]
    assert a["with_decision"] == 1 and a["with_observation"] == 0
    assert a["with_decision"] + a["with_skip"] + a["with_observation"] + a["ignored"] == 1


def test_report_shows_observation_line(tmp_path):
    from trader_lib.scorecard import render_daily_report

    obs = {"type": "observation", "ts": (NOW - dt.timedelta(hours=1)).isoformat(),
           **_full()}
    _state(tmp_path, journal=[obs], events=[_event("h1-trigger")])
    md = render_daily_report(build_review(_cfg(tmp_path), now=NOW))
    assert "наблюдение" in md
