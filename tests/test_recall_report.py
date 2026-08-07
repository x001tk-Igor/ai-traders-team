"""Два скрипта, оставшихся без тестов (аудит 2026-07-27).

recall.py — то, чем модель поднимает свой трек-рекорд перед решением. Аудит
нашёл в нём расхождение с собственным скиллом: trader-recall велит брать
калибровку СВОЕЙ модели, а инструмент возвращал общую. Документ говорил одно,
код делал другое, и haircut считался бы по чужим данным.

report.py — точка, где действие модели оставляет три следа сразу: журнал, лог
по времени машины и Telegram. Она новая и была непокрыта: если бы отказ
мессенджера ронял запись наблюдения, метрика пробуждений врала бы, а узнали бы
мы об этом по пустому отчёту через неделю.
"""
import dataclasses
import datetime as dt
import json

import pytest

import scripts.report as report
from scripts.recall import pull
from trader_lib.config import load_config
from trader_lib.eventlog import read_log
from trader_lib.journal import read_records

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)

STATS = {
    "overall": {"n": 10, "wr": 0.6, "avg_R": 0.3, "sum_R": 3.0},
    "by_symbol": {"XAUUSD": {"n": 7, "wr": 0.57}},
    "by_setup": {"ema_pullback": {"n": 5, "wr": 0.6, "insufficient": True}},
    "calibration": [{"conf_bucket": "0.6-0.7", "n": 10, "realized_wr": 0.5}],
    "calibration_by_model": {
        "claude-opus-5": [{"conf_bucket": "0.6-0.7", "n": 6, "realized_wr": 0.83}],
        "слабая": [{"conf_bucket": "0.6-0.7", "n": 4, "realized_wr": 0.0}],
    },
}


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


# --------------------------------------------------------------------------
# recall
# --------------------------------------------------------------------------

def test_calibration_is_scoped_to_current_model():
    """Главное расхождение, найденное аудитом: у своей модели реальный WR 0.83,
    у смеси — 0.5. Дисконтировать уверенность по смеси значит взять чужой
    haircut."""
    got = pull(STATS, "XAUUSD", ["ema_pullback"], model_id="claude-opus-5")
    assert got["calibration"] == STATS["calibration_by_model"]["claude-opus-5"]
    assert got["calibration"] != STATS["calibration"]
    assert got["model_id"] == "claude-opus-5"


def test_global_calibration_kept_but_named_differently():
    got = pull(STATS, "XAUUSD", [], model_id="claude-opus-5")
    assert got["calibration_all_models"] == STATS["calibration"]


def test_unknown_model_gets_none_not_someone_elses_numbers():
    """«Данных по тебе нет» и «вот данные по всем» — разные ответы."""
    got = pull(STATS, "XAUUSD", [], model_id="совсем-новая")
    assert got["calibration"] is None
    assert "чужую калибровку не бери" in got["calibration_note"]


def test_pull_without_model_id_does_not_leak_global():
    got = pull(STATS, "XAUUSD", [])
    assert got["calibration"] is None


def test_setups_and_symbol_slices():
    got = pull(STATS, "XAUUSD", ["ema_pullback", "нет-такого"],
               model_id="claude-opus-5")
    assert got["symbol"]["n"] == 7
    assert got["setups"]["ema_pullback"]["insufficient"] is True
    assert got["setups"]["нет-такого"] is None


def test_empty_stats_do_not_crash():
    got = pull({}, "XAUUSD", ["s"], model_id="m")
    assert got["overall"] is None and got["calibration"] is None


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

class Sender:
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail

    def __call__(self, text):
        if self._fail:
            raise RuntimeError("телеграм недоступен")
        self.sent.append(text)
        return {"ok": True}


def _tg_settings(tmp_path):
    (tmp_path / "telegram.json").write_text(json.dumps(
        {"enabled": True, "token": "T", "chat_id": 1}), encoding="utf-8")


def test_observation_leaves_three_traces(tmp_path):
    """Журнал, лог и телеграм — за один вызов."""
    _tg_settings(tmp_path)
    s = Sender()
    report.observed(_cfg(tmp_path), symbol="XAUUSD", alert_id="h1", now=NOW,
                    alert_type="price_below", level=4087.0, price=4086.9,
                    regime="флет", reasoning="жду закрепления под уровнем",
                    equity=10000.0, wall_left_pct=2.99, positions=0, sender=s)

    recs = read_records(tmp_path / "journal.jsonl")
    assert [r["type"] for r in recs] == ["observation"]
    assert recs[0]["reasoning"] == "жду закрепления под уровнем"
    assert any("THINK" in line for line in read_log(tmp_path))
    assert s.sent and "жду закрепления" in s.sent[0]


def test_journal_is_written_even_if_telegram_dies(tmp_path):
    """Отказ мессенджера не имеет права стереть след рассуждения: без записи
    пробуждение считалось бы пустым, а полезный алерт — мусорным."""
    _tg_settings(tmp_path)
    res = report.observed(_cfg(tmp_path), symbol="XAUUSD", alert_id="h1", now=NOW,
                          alert_type="price_below", level=1, price=1, regime="флет",
                          reasoning="жду отката", sender=Sender(fail=True))
    assert res["sent"] is False
    assert read_records(tmp_path / "journal.jsonl")[0]["reasoning"] == "жду отката"


def test_empty_reasoning_refused_before_anything_is_sent(tmp_path):
    _tg_settings(tmp_path)
    s = Sender()
    with pytest.raises(ValueError):
        report.observed(_cfg(tmp_path), symbol="XAUUSD", alert_id="h1", now=NOW,
                        alert_type="price_below", level=1, price=1, regime="флет",
                        reasoning="   ", sender=s)
    assert s.sent == []


def test_entered_reports_thesis_and_logs(tmp_path):
    _tg_settings(tmp_path)
    s = Sender()
    draft = {"symbol": "XAUUSD", "side": "buy", "thesis": "откат к EMA20",
             "entry": 4091.35, "sl": 4085.18, "tp_plan": 4103.68, "rr": 2.0,
             "confidence": 0.5, "setup_type": "ema_pullback",
             "setup_status": "изучаю", "planned": True, "plan_hypothesis_id": "H1"}
    report.entered(_cfg(tmp_path), result={"ticket": 42, "lots": 0.05,
                                           "risk_usd": 94.45, "fill_price": 4091.36},
                   draft=draft, gate_verdict="OK", spread=21, now=NOW, sender=s)
    assert "откат к EMA20" in s.sent[0] and "тикет 42" in s.sent[0]
    assert any("ENTER" in line for line in read_log(tmp_path))


def test_exited_reports_result(tmp_path):
    _tg_settings(tmp_path)
    s = Sender()
    report.exited(_cfg(tmp_path), result={"ticket": 42, "R": -0.02, "profit": -1.04,
                                          "exit": 4091.23},
                  symbol="XAUUSD", reason="тезис не отработал", entry_price=4091.35,
                  now=NOW, sender=s)
    assert "ВЫХОД" in s.sent[0] and "тезис не отработал" in s.sent[0]
    assert any("EXIT" in line for line in read_log(tmp_path))


def test_critical_names_the_action(tmp_path):
    _tg_settings(tmp_path)
    s = Sender()
    report.critical(_cfg(tmp_path), title="СТОП-КРАН · стена пробита",
                    details=["закрыто 2 позиции"], action="торговля остановлена",
                    now=NOW, sender=s)
    assert "🚨" in s.sent[0] and "торговля остановлена" in s.sent[0]
    assert any("VALVE" in line for line in read_log(tmp_path))


def test_report_is_silent_without_telegram_settings(tmp_path):
    """Канал не настроен — журнал и лог всё равно пишутся."""
    res = report.observed(_cfg(tmp_path), symbol="XAUUSD", alert_id="h1", now=NOW,
                          alert_type="price_below", level=1, price=1, regime="флет",
                          reasoning="жду")
    assert res["sent"] is False and "не настроен" in res["reason"]
    assert read_records(tmp_path / "journal.jsonl")
    assert read_log(tmp_path)
