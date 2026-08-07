"""Утренний брифинг (задача 6.1). Всё офлайн.

Брифинг — первое, что модель читает в сессии, и единственное место, где она
узнаёт о том, что произошло, пока её не было. Поэтому главные утверждения
здесь не про красоту отчёта, а про честность:

  * НЕДОСТУПНОЕ ЗНАЧЕНИЕ = null + причина, никогда не догадка. Брифинг
    читается моделью как факты; «примерно такой гэп» превращается в её тезис,
    а потом в сделку.
  * ЧУЖАЯ ПОЗИЦИЯ ВСПЛЫВАЕТ ПЕРВОЙ. Оставшаяся с ночи позиция без записи в
    журнале — это либо чужая рука, либо потерянный след; и то и другое
    означает «не торговать, пока не разобрались».
  * КАЛИБРОВКА — ПО ТЕКУЩЕЙ МОДЕЛИ. Одна модель систематически переоценивает
    себя, другая недооценивает; смешанная калибровка не описывает ни одну.
"""
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.brief import build_brief
from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc
# 06:30 UTC — перед Лондоном, фаза BRIEF
NOW = dt.datetime(2026, 7, 27, 6, 30, tzinfo=UTC)
SERVER_OFFSET_H = 3


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _bars(n=400, *, tf_minutes=5, last_utc=NOW, base=2400.0, step=0.0,
          night_high=None, night_low=None, gap=None, spread=20,
          pre_asian_spike=None):
    """Бары в СЕРВЕРНОМ времени (наивные), как отдаёт MT5."""
    end = (last_utc + dt.timedelta(hours=SERVER_OFFSET_H)).replace(tzinfo=None)
    t = pd.date_range(end=end, periods=n, freq=f"{tf_minutes}min")
    close = base + np.arange(n) * step
    high = close + 1.0
    low = close - 1.0
    open_ = np.concatenate([[close[0]], close[:-1]]).astype(float)
    df = pd.DataFrame({"time": t, "open": open_, "high": high, "low": low,
                       "close": close, "tick_volume": 200, "spread": spread})
    # азиатская сессия 00:00–07:00 UTC = 03:00–10:00 серверного времени
    if night_high is not None:
        asian = (df["time"].dt.hour >= 3) & (df["time"].dt.hour < 10)
        df.loc[asian, "high"] = night_high
    if night_low is not None:
        asian = (df["time"].dt.hour >= 3) & (df["time"].dt.hour < 10)
        df.loc[asian, "low"] = night_low
    if pre_asian_spike is not None:
        # серверные часы 0–3 = UTC 21–24 ПРЕДЫДУЩЕГО дня: в азиатскую сессию
        # (UTC 0–7) они не входят. Всплеск здесь ловит отсутствие перевода
        # серверного времени в UTC — без перевода эти бары попадут в диапазон.
        pre = df["time"].dt.hour < 3
        df.loc[pre, "high"] = pre_asian_spike
    if gap is not None:
        df.loc[df.index[-1], "open"] = df["close"].iloc[-2] + gap
    return df


class Market(FakeMarket):
    def __init__(self, *, bars=None, positions=None, deals=None, equity=10000.0):
        super().__init__(bars=bars if bars is not None else _bars(),
                         account={"balance": 10000.0, "equity": equity},
                         positions=list(positions or []), deals=deals or [])

    def tick(self, symbol):
        return {"bid": 2399.9, "ask": 2400.1}


def _state(tmp_path, *, journal=(), heartbeat_age_s=5, intent=None, now=NOW):
    (tmp_path / "journal.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in journal),
        encoding="utf-8")
    (tmp_path / "news_cache.json").write_text(json.dumps(
        {"fetched_utc": now.isoformat(), "events": []}), encoding="utf-8")
    if heartbeat_age_s is not None:
        (tmp_path / "watch_heartbeat.json").write_text(json.dumps({
            "ts": (now - dt.timedelta(seconds=heartbeat_age_s)).isoformat(),
            "walls_checked": True, "pending_undelivered": 0,
            # здоровый пульс обязан подтверждать не только «я жив», но и «я той
            # версии»: живой процесс не видит правок кода на диске
            "code_mtime": _fresh_code_mtime(),
            "silence_rule_minutes": 180}), encoding="utf-8")
    if intent is not None:
        (tmp_path / "open_intent.md").write_text(intent, encoding="utf-8")


def _decision(trade_id, **over):
    base = {"type": "decision", "ts": (NOW - dt.timedelta(days=1)).isoformat(),
            "trade_id": trade_id, "symbol": "XAUUSD", "side": "buy",
            "setup_type": "ema_pullback", "setup_status": "подтверждён",
            "confidence": 0.6, "regime": "тренд", "model_id": "claude-opus-5",
            "model_profile": "strong", "session_phase": "NY", "planned": True,
            "entry": 2400.0, "sl": 2395.0, "risk_usd": 50.0}
    base.update(over)
    return base


def _outcome(trade_id, R, **over):
    return {"type": "outcome", "trade_id": trade_id, "R": R,
            "close_ts": (NOW - dt.timedelta(days=1)).isoformat(),
            "exit_reason": "tp", **over}


def _run(tmp_path, market=None, cfg=None, at=NOW, **kw):
    return build_brief(market or Market(), cfg or _cfg(tmp_path), now=at,
                       symbols=["XAUUSD"], **kw)


# --------------------------------------------------------------------------
# состав
# --------------------------------------------------------------------------

SECTIONS = ("server_day", "account", "reconciliation", "orphans", "news",
            "symbols", "scorecard", "open_intent", "session", "watchdog",
            "allow_new", "warnings")


def test_brief_contains_all_sections(tmp_path):
    _state(tmp_path)
    b = _run(tmp_path)
    for section in SECTIONS:
        assert section in b, f"нет раздела {section}"


def test_server_day_and_baseline(tmp_path):
    _state(tmp_path)
    b = _run(tmp_path)
    assert b["server_day"] == "2026-07-27"
    assert b["account"]["equity"] == 10000.0
    assert b["account"]["day_start_equity"] == 10000.0


# --------------------------------------------------------------------------
# ночь: диапазон, экстремумы, гэп
# --------------------------------------------------------------------------

def test_asian_range_computed(tmp_path):
    _state(tmp_path)
    m = Market(bars=_bars(night_high=2410.0, night_low=2390.0))
    b = _run(tmp_path, market=m)
    a = b["symbols"]["XAUUSD"]["asian_range"]
    assert a["high"] == 2410.0 and a["low"] == 2390.0
    assert a["range_atr"] is not None and a["range_atr"] > 0


def test_night_extremes_reported(tmp_path):
    _state(tmp_path)
    m = Market(bars=_bars(night_high=2415.0, night_low=2385.0))
    b = _run(tmp_path, market=m)
    n = b["symbols"]["XAUUSD"]["night"]
    assert n["high"] == 2415.0 and n["low"] == 2385.0


def test_gap_measured_in_atr(tmp_path):
    """Гэп в пунктах ничего не говорит: 300 пунктов на золоте и на евро — это
    разные события. Меряем в ATR."""
    _state(tmp_path)
    m = Market(bars=_bars(gap=6.0))
    b = _run(tmp_path, market=m)
    g = b["symbols"]["XAUUSD"]["gap"]
    assert g["points"] == pytest.approx(600.0, rel=0.01)
    assert g["atr"] is not None and g["atr"] > 1.0


def test_missing_data_is_null_not_guessed(tmp_path):
    """Баров нет — раздел null с причиной, а не «примерно такой диапазон»."""
    _state(tmp_path)

    class NoBars(Market):
        def copy_rates(self, symbol, timeframe, count):
            raise RuntimeError("нет истории по символу")

    b = _run(tmp_path, market=NoBars())
    s = b["symbols"]["XAUUSD"]
    assert s["asian_range"] is None and s["gap"] is None
    assert "нет истории" in s["reason"]
    assert any("XAUUSD" in w for w in b["warnings"])


# --------------------------------------------------------------------------
# чужие позиции
# --------------------------------------------------------------------------

def test_orphans_surface_force_flat(tmp_path):
    """Позиция без записи в журнале — торговлю не начинаем.

    Время взято ВНУТРИ торгового окна (13:00 UTC): в 06:30 сессия запрещает
    входы сама, и тест не отличал бы «запретила чужая позиция» от «ещё не
    открылись» — на этом мутация «orphans не влияют» выживала.
    """
    at = NOW.replace(hour=13)
    _state(tmp_path, now=at)
    orphan = {"ticket": 999, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
              "price_open": 2400.0, "sl": 0.0, "tp": 0.0, "price_current": 2400.0,
              "profit": 0.0, "magic": 0}
    b = _run(tmp_path, market=Market(positions=[orphan]), at=at)
    assert b["session"]["allow_new"] is True, "мир должен разрешать вход по сессии"
    assert len(b["orphans"]) == 1 and b["orphans"][0]["ticket"] == 999
    assert b["allow_new"] is False
    assert any("999" in w for w in b["warnings"])


def test_asian_range_ignores_pre_session_bars(tmp_path):
    """Время баров MT5 — СЕРВЕРНОЕ. Без перевода в UTC «азиатская сессия»
    съезжает на смещение брокера и захватывает вечер предыдущего дня."""
    _state(tmp_path)
    m = Market(bars=_bars(night_high=2410.0, night_low=2390.0,
                          pre_asian_spike=2500.0))
    b = _run(tmp_path, market=m)
    assert b["symbols"]["XAUUSD"]["asian_range"]["high"] == 2410.0


def test_atr_null_on_short_history(tmp_path):
    """ATR по трём свечам — не мера волатильности. Меньше периода + 2 баров →
    None, и диапазон в ATR тоже None, а не выдуманное число."""
    _state(tmp_path)
    m = Market(bars=_bars(n=10, night_high=2410.0, night_low=2390.0))
    s = _run(tmp_path, market=m)["symbols"]["XAUUSD"]
    assert s["atr"] is None
    assert s["asian_range"] is None or s["asian_range"]["range_atr"] is None
    assert s["gap"] is None or s["gap"]["atr"] is None


def test_own_position_is_not_orphan(tmp_path):
    _state(tmp_path, journal=[_decision("555")])
    own = {"ticket": 555, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
           "price_open": 2400.0, "sl": 2395.0, "tp": 0.0, "price_current": 2400.0,
           "profit": 0.0, "magic": 0}
    b = _run(tmp_path, market=Market(positions=[own]))
    assert b["orphans"] == []


def test_reconciliation_writes_outcomes(tmp_path):
    """Сделка закрылась ночью, пока модель спала: исход дописывается тут же,
    иначе разбор дня посчитает её открытой."""
    _state(tmp_path, journal=[_decision("777")])
    # у позиции ДВЕ сделки: вход (entry=0) и выход (entry=1) — reconcile
    # суммирует profit по обеим и берёт цену последнего выхода
    deals = [{"position_id": 777, "profit": 0.0, "price": 2400.0,
              "time": int((NOW - dt.timedelta(hours=9)).timestamp()), "entry": 0},
             {"position_id": 777, "profit": 120.0, "price": 2412.0,
              "time": int((NOW - dt.timedelta(hours=5)).timestamp()), "entry": 1}]
    b = _run(tmp_path, market=Market(deals=deals))
    assert b["reconciliation"]["written"] == 1
    recs = [json.loads(x) for x in
            (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(r["type"] == "outcome" and r["trade_id"] == "777" for r in recs)


# --------------------------------------------------------------------------
# статистика и калибровка
# --------------------------------------------------------------------------

def test_calibration_scoped_to_current_model(tmp_path):
    """Одна модель переоценивает себя, другая нет; смешанная калибровка не
    описывает ни одну."""
    journal = []
    for i in range(6):
        journal += [_decision(f"o{i}", model_id="claude-opus-5", confidence=0.8),
                    _outcome(f"o{i}", 1.0)]
    for i in range(6):
        journal += [_decision(f"w{i}", model_id="слабая-модель", confidence=0.8),
                    _outcome(f"w{i}", -1.0)]
    _state(tmp_path, journal=journal)

    b = _run(tmp_path, cfg=_cfg(tmp_path, model={"id": "claude-opus-5"}))
    calib = b["scorecard"]["calibration"]
    assert b["scorecard"]["model_id"] == "claude-opus-5"
    buckets = [x for x in calib if x.get("n")]
    assert buckets, "калибровка текущей модели пуста"
    assert all(x["realized_wr"] == 1.0 for x in buckets), \
        f"в калибровку попали чужие сделки: {calib}"


def test_setup_statuses_in_scorecard(tmp_path):
    journal = []
    for i in range(3):
        journal += [_decision(f"t{i}", setup_type="ema_pullback"), _outcome(f"t{i}", 1.0)]
    _state(tmp_path, journal=journal)
    b = _run(tmp_path)
    assert "ema_pullback" in b["scorecard"]["by_setup"]
    assert b["scorecard"]["by_setup"]["ema_pullback"]["insufficient"] is True


def test_empty_journal_does_not_crash(tmp_path):
    _state(tmp_path)
    b = _run(tmp_path)
    assert b["scorecard"]["by_setup"] == {} and b["scorecard"]["overall"]["n"] == 0


# --------------------------------------------------------------------------
# намерение, сессия, датчик
# --------------------------------------------------------------------------

def test_open_intent_read_from_file(tmp_path):
    """Намерение берётся из файла, а не из памяти модели: сессия могла
    прерваться, а память — не пережить перезапуск."""
    _state(tmp_path, intent="## Открытое намерение\nВеду XAUUSD от 2400, стоп 2395.")
    b = _run(tmp_path)
    assert "2400" in b["open_intent"]


def test_missing_intent_is_none(tmp_path):
    _state(tmp_path)
    assert _run(tmp_path)["open_intent"] is None


def test_watchdog_state_reported(tmp_path):
    _state(tmp_path, heartbeat_age_s=5)
    b = _run(tmp_path)
    assert b["watchdog"]["alive"] is True and b["watchdog"]["age_s"] < 90


def test_dead_watchdog_blocks_and_warns(tmp_path):
    """Тоже внутри торгового окна: иначе запрет обеспечивала бы сессия, и
    мутация «мёртвый датчик не влияет» выживала."""
    at = NOW.replace(hour=13)
    _state(tmp_path, heartbeat_age_s=None, now=at)   # файла нет вовсе
    b = _run(tmp_path, at=at)
    assert b["session"]["allow_new"] is True
    assert b["watchdog"]["alive"] is False and b["allow_new"] is False
    assert any("датчик" in w for w in b["warnings"])


def test_session_permissions_reported(tmp_path):
    _state(tmp_path)
    b = _run(tmp_path)
    assert b["session"]["phase"] == "BRIEF"
    # 06:30 UTC — торговое окно ещё закрыто (открывается в 07:00)
    assert b["session"]["allow_new"] is False
    assert b["allow_new"] is False


def test_news_windows_for_the_day(tmp_path):
    _state(tmp_path)
    (tmp_path / "news_cache.json").write_text(json.dumps({
        "fetched_utc": NOW.isoformat(),
        "events": [{"title": "Non-Farm Employment Change", "currency": "USD",
                    "impact": "high",
                    "ts_utc": (NOW + dt.timedelta(hours=6)).isoformat(),
                    "time_known": True}]}), encoding="utf-8")
    b = _run(tmp_path)
    assert len(b["news"]["windows"]) == 1
    w = b["news"]["windows"][0]
    assert w["level"] == "top" and "Non-Farm" in w["title"]


def _fresh_code_mtime():
    """Отпечаток кода тем же счётом, что и датчик. РЕГРЕСС 2026-08-01: фикстура
    брала mtime только alert_watch.py, а отпечаток покрывает и trader_lib —
    «здоровый» пульс выглядел устаревшим, потому что тест и код считали
    по-разному."""
    from scripts.alert_watch import _loaded_code_mtime
    return _loaded_code_mtime()


def _hb(tmp_path, **over):
    """Пульс датчика с полями по умолчанию — как у живого процесса."""
    import datetime as dt
    import json
    import os
    from pathlib import Path
    hb = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
          "tick_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
          "walls_checked": True, "pid": 1, "tick": 10,
          "code_mtime": _fresh_code_mtime(),
          "silence_rule_minutes": 180}
    hb.update(over)
    (Path(tmp_path) / "watch_heartbeat.json").write_text(
        json.dumps(hb), encoding="utf-8")
    return dt.datetime.now(dt.timezone.utc)


def test_watchdog_ok_when_process_runs_current_code(tmp_path):
    from scripts.brief import _watchdog
    now = _hb(tmp_path)
    w = _watchdog(tmp_path, now=now)
    assert w["alive"] is True and w["stale_code"] is None


def test_watchdog_flags_process_started_before_the_code_changed(tmp_path):
    """27.07.2026: датчик стартовал в 04:41, правило живости закоммичено в
    05:08 — шесть часов контур крутил код БЕЗ единственной защиты от «модель
    уснула навсегда», а пульс всё это время выглядел здоровым (tick растёт,
    errors пуст). Живой процесс держит модуль в памяти и правок на диске не
    видит; отличить это можно только сравнив отпечаток кода."""
    from scripts.brief import _watchdog
    now = _hb(tmp_path, code_mtime=1.0)          # процесс запущен давным-давно
    w = _watchdog(tmp_path, now=now)
    assert w["alive"] is False
    assert "перезапусти датчик" in w["reason"].lower()


def test_watchdog_flags_missing_liveness_rule(tmp_path):
    """Порог тишины пишется в пульс из ЗАГРУЖЕННОГО кода. Пусто — значит
    правила в процессе нет, и будить модель некому."""
    from scripts.brief import _watchdog
    now = _hb(tmp_path, silence_rule_minutes=None)
    w = _watchdog(tmp_path, now=now)
    assert w["alive"] is False and "живости" in w["reason"]
