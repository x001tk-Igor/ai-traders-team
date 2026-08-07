"""Дневной разбор (задача 6.3). Всё офлайн.

ГЛАВНОЕ ЗДЕСЬ — МЕТРИКА «ПРОБУЖДЕНИЙ → РЕШЕНИЙ», и она принципиально не может
считаться через compute_stats: score._join оставляет только пары
decision+outcome и по построению отбрасывает skip и alert_event. Поэтому разбор
делает ВТОРОЙ независимый проход по сырому журналу. Тест
test_alert_efficiency_metric сторожит именно это: событие, которое разбудило
модель и не дало ни входа, ни осознанного пропуска, обязано считаться пустым.

Второе по важности — «план против факта»: вход, совпавший с гипотезой дня,
и вход вне плана — разные вещи с разными лимитами, и разбор обязан их
различать, а не считать сделки скопом.
"""
import dataclasses
import datetime as dt
import json

import pytest

from scripts.review import build_review
from trader_lib.config import load_config
from trader_lib.scorecard import render_daily_report

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 19, 30, tzinfo=UTC)      # вечер, фаза REVIEW
DAY = "2026-07-27"

PLAN_MD = """# План дня · 2026-07-27

## H1 · XAUUSD · london-range-break-short
- **Условие:** возврат под 2415
- **Алерт:** price_below 2415 (id: h1-trigger)
- **Стоп:** 2421
- **Горизонт:** 120 мин

## H2 · EURUSD · trend-pullback-long
- **Условие:** откат к EMA20
- **Алерт:** price_below 1.0850 (id: h2-trigger)
- **Стоп:** 1.0820
- **Горизонт:** 240 мин
"""


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _dec(trade_id, *, hours_ago=5, risk=100.0, planned=True, hyp="H1",
         setup="london-range-break-short", symbol="XAUUSD", confidence=0.6,
         alert_id=None, **over):
    rec = {"type": "decision", "ts": (NOW - dt.timedelta(hours=hours_ago)).isoformat(),
           "trade_id": trade_id, "symbol": symbol, "side": "sell", "regime": "тренд",
           "tactic": setup, "setup_type": setup, "setup_status": "подтверждён",
           "thesis": "тезис", "confidence": confidence, "technical_trigger": "M15",
           "entry": 2415.0, "sl": 2421.0, "tp_plan": 2400.0, "risk_usd": risk,
           "rr": 2.0, "costs_R": 0.02, "breakeven_p": 0.34, "p_win_journal": None,
           "news_check": "чисто", "spread_at_entry": 20.0, "correlation_check": "нет",
           "daily_risk_remaining_usd": 200.0, "planned": planned,
           "plan_hypothesis_id": hyp if planned else None, "gate_verdict": "OK",
           "session_phase": "LONDON", "model_id": "claude-opus-5",
           "model_profile": "strong"}
    if alert_id:
        rec["alert_id"] = alert_id
    rec.update(over)
    return rec


def _out(trade_id, R, *, hours_ago=3, profit=None):
    return {"type": "outcome", "trade_id": trade_id, "R": R,
            "profit": profit if profit is not None else R * 100.0,
            "exit": 2400.0, "exit_reason": "tp",
            "close_ts": (NOW - dt.timedelta(hours=hours_ago)).isoformat()}


def _skip(reason, *, hours_ago=6, alert_id=None, setup="london-range-break-short"):
    rec = {"type": "skip", "ts": (NOW - dt.timedelta(hours=hours_ago)).isoformat(),
           "setup_type": setup, "reason": reason, "confidence": 0.4,
           "regime": "тренд", "model_id": "claude-opus-5", "model_profile": "strong"}
    if alert_id:
        rec["alert_id"] = alert_id
    return rec


def _event(alert_id, *, hours_ago=6, delivered=True, alert_type="price_below"):
    return {"type": "alert_event", "ts": (NOW - dt.timedelta(hours=hours_ago)).isoformat(),
            "fired_utc": (NOW - dt.timedelta(hours=hours_ago)).isoformat(),
            "alert_id": alert_id, "alert_type": alert_type, "priority": "normal",
            "model_id": "claude-opus-5", "delivered": delivered}


def _state(tmp_path, *, journal=(), plan=PLAN_MD, news=()):
    """Записи раскладываются ПО СВОИМ ФАЙЛАМ, как в бою: решения и пропуски —
    в journal.jsonl, события датчика — в alert_events.jsonl.

    Первая версия фикстуры клала всё в journal.jsonl, и тесты проходили при
    разборе, который читал только его. В бою метрика пробуждений всегда
    показывала ноль — найдено полным смоуком 2026-07-27."""
    rows = list(journal)
    events = [r for r in rows if r.get("type") == "alert_event"]
    others = [r for r in rows if r.get("type") != "alert_event"]
    (tmp_path / "journal.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in others),
        encoding="utf-8")
    (tmp_path / "alert_events.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in events),
        encoding="utf-8")
    if plan is not None:
        (tmp_path / "day_plan.md").write_text(plan, encoding="utf-8")
    (tmp_path / "news_cache.json").write_text(json.dumps(
        {"fetched_utc": NOW.isoformat(), "events": list(news)}), encoding="utf-8")


def _run(tmp_path, cfg=None):
    return build_review(cfg or _cfg(tmp_path), now=NOW)


# --------------------------------------------------------------------------
# числа дня
# --------------------------------------------------------------------------

def test_daily_report_numbers(tmp_path):
    journal = [_dec("1"), _out("1", 2.0, profit=200.0),
               _dec("2"), _out("2", -1.0, profit=-100.0),
               _dec("3"), _out("3", 1.0, profit=100.0)]
    _state(tmp_path, journal=journal)
    r = _run(tmp_path)
    t = r["trades"]
    assert t["closed"] == 3 and t["still_open"] == 0
    assert t["sum_R"] == pytest.approx(2.0)
    assert t["pnl_usd"] == pytest.approx(200.0)
    assert t["wr"] == pytest.approx(2 / 3, abs=0.001)
    assert t["avg_win_R"] == pytest.approx(1.5)
    assert t["avg_loss_R"] == pytest.approx(-1.0)
    # PF = сумма выигрышей / |сумма проигрышей| = 300 / 100
    assert t["profit_factor"] == pytest.approx(3.0)
    assert r["risk_used_usd"] == pytest.approx(300.0)


def test_open_trade_counted_separately(tmp_path):
    """Позиция без исхода ещё ведётся: она не выигрыш и не проигрыш."""
    _state(tmp_path, journal=[_dec("1"), _out("1", 1.0), _dec("2")])
    t = _run(tmp_path)["trades"]
    assert t["closed"] == 1 and t["still_open"] == 1


def test_drawdown_from_r_curve(tmp_path):
    """Внутридневная просадка считается по кривой накопленного R в порядке
    закрытия: тиковой кривой equity у нас нет, и делать вид, что есть, нельзя."""
    journal = [_dec("1"), _out("1", 2.0, hours_ago=6),
               _dec("2"), _out("2", -3.0, hours_ago=5),
               _dec("3"), _out("3", 1.0, hours_ago=4)]
    _state(tmp_path, journal=journal)
    t = _run(tmp_path)["trades"]
    assert t["max_drawdown_R"] == pytest.approx(-3.0)


def test_yesterday_trades_excluded(tmp_path):
    """Разбор — про СЕГОДНЯШНИЙ серверный день."""
    journal = [_dec("old", hours_ago=30), _out("old", 5.0, hours_ago=29),
               _dec("today"), _out("today", 1.0)]
    _state(tmp_path, journal=journal)
    t = _run(tmp_path)["trades"]
    assert t["closed"] == 1 and t["sum_R"] == pytest.approx(1.0)


def test_profit_factor_undefined_without_losses(tmp_path):
    """День без единого убытка: профит-фактор не определён. «inf» в отчёте,
    который читает человек, выглядит как поломка расчёта, а не как «делить не
    на что» — поэтому честное «н/д»."""
    _state(tmp_path, journal=[_dec("1"), _out("1", 2.0), _dec("2"), _out("2", 1.0)])
    r = _run(tmp_path)
    assert r["trades"]["profit_factor"] is None
    assert "н/д" in render_daily_report(r)


def test_empty_day_reports_no_trades(tmp_path):
    _state(tmp_path, journal=[])
    r = _run(tmp_path)
    t = r["trades"]
    assert t["closed"] == 0 and t["sum_R"] == 0.0
    assert t["wr"] is None and t["profit_factor"] is None
    assert "0" in render_daily_report(r)


# --------------------------------------------------------------------------
# план против факта
# --------------------------------------------------------------------------

def test_plan_vs_fact_split(tmp_path):
    journal = [_dec("1", planned=True, hyp="H1"), _out("1", 1.0),
               _dec("2", planned=False, hyp=None, setup="ny-reversal"), _out("2", -1.0)]
    _state(tmp_path, journal=journal)
    p = _run(tmp_path)["plan_vs_fact"]
    assert p["planned"] == 1 and p["unplanned"] == 1
    assert p["hypotheses_total"] == 2 and p["hypotheses_traded"] == 1
    assert p["untouched"] == ["H2"]
    assert p["off_plan_setups"] == ["ny-reversal"]


def test_plan_missing_is_not_an_error(tmp_path):
    """Плана нет (модель решила не торговать или не успела) — разбор всё равно
    должен собраться, а не упасть."""
    _state(tmp_path, journal=[_dec("1"), _out("1", 1.0)], plan=None)
    p = _run(tmp_path)["plan_vs_fact"]
    assert p["hypotheses_total"] == 0 and p["planned"] == 1
    assert p["untouched"] == []


def test_unplanned_limit_reported(tmp_path):
    _state(tmp_path, journal=[])
    p = _run(tmp_path)["plan_vs_fact"]
    assert p["unplanned_limit"] == 1


# --------------------------------------------------------------------------
# эффективность пробуждений
# --------------------------------------------------------------------------

def test_alert_efficiency_metric(tmp_path):
    """Пробуждение, не давшее ни входа, ни осознанного пропуска, — пустое.
    Считается вторым проходом по сырому журналу: compute_stats эти записи
    отбрасывает по построению."""
    journal = [
        _event("h1-trigger"),                       # разбудил → вошли
        _dec("1", alert_id="h1-trigger"), _out("1", 1.0),
        _event("h2-trigger"),                       # разбудил → осознанный пропуск
        _skip("спред выше порога", alert_id="h2-trigger"),
        _event("noise-1"), _event("noise-1", hours_ago=5),   # разбудил впустую дважды
        _event("suppressed-1", delivered=False),    # придушен бюджетом, не будил
    ]
    _state(tmp_path, journal=journal)
    a = _run(tmp_path)["alert_efficiency"]
    assert a["delivered"] == 4 and a["suppressed"] == 1
    assert a["with_decision"] == 1 and a["with_skip"] == 1
    assert a["ignored"] == 2
    assert a["usefulness"] == pytest.approx(0.5)
    assert a["noisy_alerts"] == [{"alert_id": "noise-1", "count": 2}]


def test_events_are_read_from_the_sensor_file(tmp_path):
    """РЕГРЕССИЯ. События датчика лежат в alert_events.jsonl — их пишет
    append_alert_event, и в journal.jsonl их нет вовсе. Разбор, читавший только
    журнал решений, всегда показывал ноль пробуждений и выглядел исправным."""
    _state(tmp_path, journal=[_event("h1-trigger"),
                              _dec("1", alert_id="h1-trigger"), _out("1", 1.0)])
    # проверяем именно раскладку по файлам, а не только результат
    assert "alert_event" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "h1-trigger" in (tmp_path / "alert_events.jsonl").read_text(encoding="utf-8")

    a = _run(tmp_path)["alert_efficiency"]
    assert a["delivered"] == 1 and a["with_decision"] == 1


def test_suppressed_events_are_not_counted_as_wakeups(tmp_path):
    """Придушенное бюджетом событие модель НЕ будило: считать его пустым
    пробуждением значило бы винить модель за то, чего она не видела."""
    _state(tmp_path, journal=[_event("x", delivered=False)])
    a = _run(tmp_path)["alert_efficiency"]
    assert a["delivered"] == 0 and a["ignored"] == 0 and a["usefulness"] is None


def test_stop_valve_events_counted_apart(tmp_path):
    """События стоп-крана — не «пустые пробуждения»: это отчёт кода о своих
    действиях, решения модели он не требует."""
    journal = [_event("stop-valve-wall_breach", alert_type="wall_breach")]
    _state(tmp_path, journal=journal)
    a = _run(tmp_path)["alert_efficiency"]
    assert a["ignored"] == 0
    assert a["stop_valve"] == 1


# --------------------------------------------------------------------------
# калибровка и блокировки
# --------------------------------------------------------------------------

def test_calibration_block(tmp_path):
    journal = []
    for i in range(4):
        journal += [_dec(f"c{i}", confidence=0.8), _out(f"c{i}", 1.0 if i < 3 else -1.0)]
    _state(tmp_path, journal=journal)
    calib = _run(tmp_path)["calibration"]
    assert calib and calib[0]["n"] == 4
    assert calib[0]["realized_wr"] == pytest.approx(0.75)


def test_blocks_from_skips(tmp_path):
    journal = [_skip("гейт: спред выше порога"), _skip("гейт: спред выше порога"),
               _skip("окно новости")]
    _state(tmp_path, journal=journal)
    blocks = _run(tmp_path)["blocks"]
    assert blocks[0] == {"reason": "гейт: спред выше порога", "count": 2}
    assert {"reason": "окно новости", "count": 1} in blocks


def test_news_tomorrow(tmp_path):
    tomorrow = NOW + dt.timedelta(hours=18)
    _state(tmp_path, journal=[], news=[{
        "title": "CPI m/m", "currency": "USD", "impact": "high",
        "ts_utc": tomorrow.isoformat(), "time_known": True}])
    r = _run(tmp_path)
    assert len(r["news_tomorrow"]) == 1
    assert "CPI" in r["news_tomorrow"][0]["title"]


# --------------------------------------------------------------------------
# рендеринг
# --------------------------------------------------------------------------

def test_report_renders_all_sections(tmp_path):
    journal = [_event("h1-trigger"), _dec("1", alert_id="h1-trigger"), _out("1", 2.0)]
    _state(tmp_path, journal=journal)
    md = render_daily_report(_run(tmp_path))
    for header in ("# Разбор дня", "## Числа", "## План против факта",
                   "## Пробуждения", "## Калибровка дня", "## Что мешало", "## Завтра"):
        assert header in md, header
    assert DAY in md


# --------------------------------------------------------------------------
# «что мешало» — по факту, а не по аккуратности модели
# --------------------------------------------------------------------------

def _gate_event(alert_id, verdict, reasons=(), fired="2026-07-27T14:00:00+00:00"):
    return {"type": "alert_event", "alert_id": alert_id, "fired_utc": fired,
            "delivered": True, "priority": "normal",
            "snapshot": {"gate": {"verdict": verdict, "reasons": list(reasons)}}}


def test_blocks_come_from_gate_verdicts_not_only_skips():
    """РЕГРЕСС 2026-07-27: торговля стояла с обеда по серии убытков, а разбор
    дня написал «ничего не блокировало» — потому что считал только записи
    skip, а модель фиксировала пропуски как observation. Отчёт, который врёт
    именно про то, ради чего его читают, хуже отсутствующего."""
    from scripts.review import _blocks

    events = [_gate_event("a1", "OK"),
              _gate_event("a2", "HALT_NEW", ["серия убытков: стоп торговли до конца дня"]),
              _gate_event("a3", "HALT_NEW", ["серия убытков: стоп торговли до конца дня"])]
    blocks = _blocks([], events)
    assert blocks == [{"reason": "гейт: серия убытков: стоп торговли до конца дня",
                       "count": 2}]


def test_blocks_still_include_model_skips():
    """Собственные отказы модели не теряются: гейт объясняет запреты, skip —
    решения не входить, когда никто не запрещал."""
    from scripts.review import _blocks

    records = [{"type": "skip", "reason": "спред шире нормы"},
               {"type": "skip", "reason": "спред шире нормы"}]
    blocks = _blocks(records, [_gate_event("a1", "OK")])
    assert blocks == [{"reason": "спред шире нормы", "count": 2}]


def test_clean_day_reports_nothing_blocking():
    from scripts.review import _blocks
    assert _blocks([], [_gate_event("a1", "OK"), _gate_event("a2", "OK")]) == []


def test_main_passes_explicit_now_to_build_review(monkeypatch, capsys):
    """Без --now разбор на позднем UTC-вечере уходит на уже наступивший
    серверный день (см. server_day_key: сутки начинаются в 21:00 UTC при
    offset+3) и считает пустой отчёт за неверный день. --now даёт способ
    указать день разбора явно, не дожидаясь полуночи по системным часам."""
    import scripts.review as review

    captured = {}

    def fake_build_review(cfg, *, now=None, trader=None):
        captured["now"] = now
        return {"server_day": "2026-07-29"}

    monkeypatch.setattr(review, "build_review", fake_build_review)
    review.main(["--now", "2026-07-29T20:55:00+00:00", "--json"])

    assert captured["now"] == dt.datetime(2026, 7, 29, 20, 55, tzinfo=UTC)


def test_main_defaults_now_to_none_without_flag(monkeypatch):
    import scripts.review as review

    captured = {}

    def fake_build_review(cfg, *, now=None, trader=None):
        captured["now"] = now
        return {"server_day": "2026-07-27"}

    monkeypatch.setattr(review, "build_review", fake_build_review)
    review.main(["--json"])

    assert captured["now"] is None
