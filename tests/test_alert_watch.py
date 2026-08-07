"""Датчик пробуждения и стоп-кран (задача 3.2). Всё офлайн на FakeMarket.

ГЛАВНЫЙ ТЕСТ ЗДЕСЬ — test_no_trading_actions_beyond_two_rules: мок-исполнитель
регистрирует ЛЮБОЙ свой вызов (по факту вызова, а не по списку имён методов),
через датчик прогоняются все 18 типов алертов, и список вызовов обязан быть
пуст. Страж, который проверял бы имена ("не вызывал ли close_position"),
поймал бы прошлый инцидент, а не класс проблемы: новый метод исполнителя
(частичка, трейл, перенос в БУ) прошёл бы мимо него.

ВТОРОЙ ПО ВАЖНОСТИ — test_event_rate_bounded_over_time: property-тест на
ведение трёх состояний событийного бюджета (последнее normal-событие,
последнее critical-событие, все события за минуту). Проверка одного вызова
event_budget не отличает "состояние ведётся" от "состояние не ведётся" —
ловит это только последовательность тиков с постоянно истинным условием.
"""
import dataclasses
import datetime as dt
import io
import json

import numpy as np
import pandas as pd
import pytest

import scripts.alert_watch as aw
from scripts.close_watch import find_orphans
from scripts.risk_gate_cli import build_gate_inputs
from trader_lib.alerts import ALERT_TYPES, write_alerts_atomic
from trader_lib.config import load_config
from trader_lib.journal import read_records
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc
# 13:00 UTC — внутри фазы NY (12:15–16:00 в config/trader.config.json), чтобы
# session_phase был известен, а не None
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
SERVER_OFFSET_H = 3  # cfg.risk.server_utc_offset_hours

PERMISSIVE = {"min_seconds_between_events": 0, "min_seconds_between_critical_events": 0,
              "max_events_per_minute": 1000, "max_events_per_day": 1000}


# --------------------------------------------------------------------------
# фикстуры окружения
# --------------------------------------------------------------------------

def _cfg(tmp_path, **alerts_overrides):
    """Настоящий конфиг проекта, но state_dir — во временную папку теста."""
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    if alerts_overrides:
        cfg = dataclasses.replace(cfg, alerts=dataclasses.replace(cfg.alerts, **alerts_overrides))
    return cfg


def _baselines(tmp_path, *, day=NOW, equity=10000.0, initial=10000.0):
    """day_baseline.json + account_init.json — то, что читает build_gate_inputs."""
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": day.date().isoformat(), "equity": equity, "initial_balance": initial}),
        encoding="utf-8")
    (tmp_path / "account_init.json").write_text(json.dumps({"initial_balance": initial}),
                                                encoding="utf-8")


def _bars(*, last_bar_utc=NOW, n=120, tf_seconds=300, start=2400.0, step=0.05,
          spread=20, gap=None, vol=0.6):
    """Бары в СЕРВЕРНОМ времени (UTC+3, как отдаёт MT5 у этого брокера).

    step>0 → устойчивый up-тренд (EMA12 над EMA26). gap — разрыв на открытии
    ПОСЛЕДНЕГО (формирующегося) бара, close при этом остаётся на линии тренда,
    чтобы цена и ATR не поехали.

    Время баров НАИВНОЕ (без таймзоны) — ровно как отдаёт MetaTrader5: датчик
    обязан сам перевести его в UTC по cfg.risk.server_utc_offset_hours.
    """
    end_server = (last_bar_utc + dt.timedelta(hours=SERVER_OFFSET_H)).replace(tzinfo=None)
    times = pd.date_range(end=end_server, periods=n, freq=f"{tf_seconds}s")
    close = start + np.arange(n) * step
    open_ = np.concatenate([[close[0]], close[:-1]]).astype(float)
    high = close + vol
    low = close - vol
    if gap:
        open_[-1] = close[-2] + gap
        high[-1] = max(high[-1], open_[-1])
        low[-1] = min(low[-1], open_[-1])
    return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low,
                         "close": close, "tick_volume": 200, "spread": spread})


def pos(ticket, *, symbol="XAUUSD", ptype=0, volume=0.1, price_open=2400.0,
        sl=2395.0, tp=0.0, price_current=2405.0, profit=50.0):
    return {"ticket": ticket, "symbol": symbol, "type": ptype, "volume": volume,
            "price_open": price_open, "sl": sl, "tp": tp,
            "price_current": price_current, "profit": profit, "magic": 0}


def decision(ticket, *, ts=None, symbol="XAUUSD", side="buy", entry=2400.0, sl=2395.0):
    """decision-запись журнала в том объёме, который читает датчик."""
    ts = ts or (NOW - dt.timedelta(hours=3))
    return {"type": "decision", "ts": ts.isoformat(), "trade_id": str(ticket),
            "symbol": symbol, "side": side, "entry": entry, "sl": sl, "risk_usd": 40.0}


def _journal(tmp_path, records):
    (tmp_path / "journal.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def _alerts_doc(*items, expires=None):
    return {"version": 1, "written_by": "claude-opus-5",
            "written_utc": (NOW - dt.timedelta(hours=1)).isoformat(),
            "expires_utc": expires, "alerts": list(items)}


def _write_alerts(tmp_path, *items, expires=None):
    write_alerts_atomic(tmp_path / "alerts.json", _alerts_doc(*items, expires=expires))


class RecordingExecutor:
    """Регистрирует ЛЮБОЙ вызов — перехват идёт через __getattr__, поэтому
    новый, ещё не придуманный торговый метод (close_partial, trail_sl,
    move_to_breakeven) тоже попадёт в self.calls, а не проскочит мимо стража.
    results: {имя метода: результат | Exception | callable}."""

    def __init__(self, results=None):
        self.calls = []
        self._results = dict(results or {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self.calls.append({"method": name, "args": args, "kwargs": kwargs})
            r = self._results.get(name, {"ok": True})
            if isinstance(r, Exception):
                raise r
            if callable(r):
                return r(*args, **kwargs)
            return r
        return _call

    @property
    def methods(self):
        return [c["method"] for c in self.calls]


class FlakyMarket(FakeMarket):
    """Первые fail_calls обращений к positions() падают — имитация обрыва MT5."""

    def __init__(self, *a, fail_calls=1, **kw):
        super().__init__(*a, **kw)
        self._left = fail_calls

    def positions(self):
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("MT5 disconnected")
        return super().positions()


class TogglableMarket(FakeMarket):
    """Связь с терминалом можно рвать и восстанавливать между тиками."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.broken = False

    def account_info(self):
        if self.broken:
            raise RuntimeError("MT5 disconnected")
        return super().account_info()


class CountingMarket(FakeMarket):
    """Считает обращения к брокеру — снимок мира на тик должен быть один."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = {}

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def account_info(self):
        self._count("account_info")
        return super().account_info()

    def positions(self):
        self._count("positions")
        return super().positions()


class DriftingAccountMarket(FakeMarket):
    """equity меняется между вызовами account_info — как на живом счёте."""

    def __init__(self, *a, equities=(), **kw):
        super().__init__(*a, **kw)
        self._equities = list(equities)
        self._last = None

    def account_info(self):
        if self._equities:
            self._last = self._equities.pop(0)
        return {"balance": 10000.0, "equity": self._last}


def _watch(tmp_path, cfg, market, executor=None, out=None):
    return aw.AlertWatch(market, cfg, executor=executor or RecordingExecutor(),
                         out=out if out is not None else io.StringIO(), log=io.StringIO())


def _lines(out):
    return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


def _events_file(tmp_path):
    return read_records(tmp_path / "alert_events.jsonl")


# --------------------------------------------------------------------------
# датчик: срабатывание, снимок, журнал
# --------------------------------------------------------------------------

def test_price_alert_fires_once_with_snapshot(tmp_path):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(111)])
    _write_alerts(tmp_path, {"id": "h1-trigger", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0, "once": True, "priority": "normal",
                             "note": "гипотеза H1: закрепление выше уровня"})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    res = w.tick(NOW)

    assert len(res["delivered"]) == 1, res["events"]
    line = _lines(out)
    assert len(line) == 1, "одно срабатывание — ровно одна строка в stdout"
    ev = line[0]
    assert ev["alert_id"] == "h1-trigger"
    assert ev["alert_type"] == "price_above"
    assert ev["note"] == "гипотеза H1: закрепление выше уровня"  # текст модели дословно
    snap = ev["snapshot"]
    assert snap["account"]["equity"] == 10000.0
    assert snap["gate"]["verdict"] in ("OK", "THROTTLE", "HALT_NEW", "FORCE_FLAT")
    assert snap["session_phase"] == "NY"
    assert snap["symbols"]["XAUUSD"]["price"] == pytest.approx(2405.95, abs=0.01)
    assert [p["ticket"] for p in snap["positions"]] == [111]
    assert snap["positions"][0]["r_multiple"] == pytest.approx(1.0)

    # once → второй тик молчит, состояние разоружения ушло на диск
    res2 = w.tick(NOW + dt.timedelta(minutes=5))
    assert res2["events"] == []
    on_disk = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert on_disk["alerts"][0]["_state"]["armed"] is False


def test_alert_event_written_to_journal(tmp_path):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    _write_alerts(tmp_path, {"id": "h1", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0, "note": "заметка"})
    w = _watch(tmp_path, cfg, market)

    w.tick(NOW)

    recs = _events_file(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["type"] == "alert_event"
    assert r["alert_id"] == "h1"
    assert r["alert_type"] == "price_above"
    assert r["model_id"] == cfg.model.id
    assert r["priority"] == "normal"
    assert r["delivered"] is True
    assert r["note"] == "заметка"
    assert r["snapshot"]["account"]["equity"] == 10000.0


def test_position_1R_alert_fires(tmp_path):
    """Позиция дошла до +1R. R считается от ИСХОДНОГО стопа из журнала, а не
    от текущего стопа позиции — иначе перенос стопа в БУ (sl=entry) сделал бы
    знаменатель нулём и R «взорвался» бы."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(123456, entry=2400.0, sl=2390.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(123456, price_open=2400.0, sl=2400.0,
                                       price_current=2410.0, profit=100.0)])
    _write_alerts(tmp_path, {"id": "pos-1r", "type": "position_R_reaches",
                             "ticket": 123456, "level": 1.0,
                             "note": "решу сам, фиксировать или вести"})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    res = w.tick(NOW)

    assert [e["alert_id"] for e in res["delivered"]] == ["pos-1r"]
    ev = _lines(out)[0]
    assert ev["ticket"] == 123456
    assert ev["detail"]["r_multiple"] == pytest.approx(1.0)
    assert ev["note"] == "решу сам, фиксировать или вести"


def test_heartbeat_written(tmp_path):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 9990.0},
                        positions=[pos(111)])
    _journal(tmp_path, [decision(111)])
    w = _watch(tmp_path, cfg, market)

    w.tick(NOW)

    hb = json.loads((tmp_path / "watch_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["ts"] == NOW.isoformat()
    assert hb["tick"] == 1
    assert hb["walls_checked"] is True
    assert hb["wall_breached"] is False
    assert hb["equity"] == 9990.0
    assert hb["positions"] == 1
    assert hb["poll_seconds"] == cfg.alerts.poll_seconds
    assert hb["errors"] == []


def test_reloads_alerts_on_mtime_change(tmp_path):
    """Модель переписала alerts.json в конце цикла — датчик подхватил без
    перезапуска."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    _write_alerts(tmp_path, {"id": "old", "symbol": "XAUUSD", "type": "price_above",
                             "level": 1e9})
    w = _watch(tmp_path, cfg, market)
    assert w.tick(NOW)["events"] == []

    _write_alerts(tmp_path, {"id": "new-hypothesis", "symbol": "XAUUSD",
                             "type": "price_below", "level": 1e9, "note": "новая гипотеза"})
    res = w.tick(NOW + dt.timedelta(seconds=120))

    assert [e["alert_id"] for e in res["delivered"]] == ["new-hypothesis"]


def test_missing_alerts_json_is_not_an_error(tmp_path):
    """Датчик работает до того, как модель впервые поставила алерты."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market)

    res = w.tick(NOW)

    assert res["errors"] == []
    assert res["events"] == []
    hb = json.loads((tmp_path / "watch_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["alerts_error"] is None
    assert hb["alerts_count"] == 0


def test_corrupted_alerts_json_does_not_crash(tmp_path):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    (tmp_path / "alerts.json").write_text("{это не json", encoding="utf-8")
    w = _watch(tmp_path, cfg, market)

    res = w.tick(NOW)  # не должно бросить

    assert res["errors"], "порча контракта обязана быть видна в диагностике тика"
    hb = json.loads((tmp_path / "watch_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["alerts_error"]
    # модель починила файл — датчик подхватил, перезапуск не нужен
    _write_alerts(tmp_path, {"id": "fixed", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0})
    res2 = w.tick(NOW + dt.timedelta(seconds=120))
    assert [e["alert_id"] for e in res2["delivered"]] == ["fixed"]


def test_survives_market_exception(tmp_path):
    """Исключение при опросе рынка не роняет цикл: лог, продолжение."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FlakyMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                         fail_calls=1)
    _write_alerts(tmp_path, {"id": "h1", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0})
    log = io.StringIO()
    w = aw.AlertWatch(market, cfg, executor=RecordingExecutor(), out=io.StringIO(), log=log)

    res = w.tick(NOW)  # не бросает

    assert res["events"] == []
    assert any("MT5 disconnected" in e for e in res["errors"])
    assert "MT5 disconnected" in log.getvalue()
    hb = json.loads((tmp_path / "watch_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["walls_checked"] is False, "стену посчитать было не из чего — это видно модели"
    assert hb["errors"]

    res2 = w.tick(NOW + dt.timedelta(seconds=120))
    assert [e["alert_id"] for e in res2["delivered"]] == ["h1"]


def test_run_loop_survives_and_sleeps_poll_seconds(tmp_path):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = FlakyMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                         fail_calls=1)
    w = _watch(tmp_path, cfg, market)
    slept = []
    clock = {"t": NOW}

    def _now():
        clock["t"] += dt.timedelta(seconds=cfg.alerts.poll_seconds)
        return clock["t"]

    ticks = w.run(max_ticks=3, sleep_fn=slept.append, now_fn=_now)

    assert ticks == 3
    assert slept == [cfg.alerts.poll_seconds] * 3


# --------------------------------------------------------------------------
# стоп-кран: правило 1 — стена по equity
# --------------------------------------------------------------------------

def test_wall_breach_closes_all_and_emits_critical(tmp_path):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path, equity=10000.0, initial=10000.0)
    _journal(tmp_path, [decision(111), decision(222)])
    # −3.0% за день при стене 3.0% и буфере 0.3% → 3.0 >= 2.7 → стена пробита
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111), pos(222, price_open=2410.0, sl=2405.0)])
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    res = w.tick(NOW)

    assert ex.methods == ["close_position", "close_position"], ex.calls
    assert sorted(c["args"][0] for c in ex.calls) == [111, 222]
    ev = _lines(out)
    assert len(ev) == 1
    assert ev[0]["alert_type"] == "wall_breach"
    assert ev[0]["priority"] == "critical"
    assert ev[0]["action"]["rule"] == "wall_breach"
    assert sorted(ev[0]["action"]["closed"]) == [111, 222]
    assert ev[0]["snapshot"]["walls"]["breached"] is True
    assert res["actions"], "действие стоп-крана обязано попасть в диагностику тика"
    assert _events_file(tmp_path)[0]["alert_type"] == "wall_breach"


def test_wall_breach_event_not_repeated_every_tick(tmp_path):
    """Стена держится минутами; повторное событие каждую секунду остановило бы
    Monitor. Событие — на фронте, а не на каждом тике; закрытие при этом
    продолжает повторяться, пока позиции живы."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)
    w.tick(NOW + dt.timedelta(seconds=1))
    assert len(_lines(out)) == 1, "второе событие о той же стене не печатается"
    assert len(ex.calls) == 1, "повтор закрытия придушен, чтобы не долбить брокера"

    w.tick(NOW + dt.timedelta(seconds=30))
    assert len(ex.calls) == 2, "позиция всё ещё открыта — попытка закрытия повторяется"
    assert len(_lines(out)) == 1


def test_wall_takes_precedence_over_sl_restore(tmp_path):
    """Стена пробита и у позиции нет стопа: её закрывают, а не чинят ей стоп —
    иначе по одному тикету ушли бы два противоречивых приказа."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2395.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111, sl=0.0, price_current=2405.0)])
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    w.tick(NOW)

    assert ex.methods == ["close_position"]


def test_stop_valve_works_with_corrupted_alerts(tmp_path):
    """Безопасность не зависит от файла модели: битый alerts.json не отменяет
    закрытия по стене."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    (tmp_path / "alerts.json").write_text("не json вовсе", encoding="utf-8")
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    res = w.tick(NOW)

    assert ex.methods == ["close_position"]
    assert res["errors"], "порча alerts.json видна, но стоп-кран отработал"


def test_wall_breach_closes_even_orphan_positions(tmp_path):
    """Стена — единственный случай, когда закрывается ВСЁ, включая позицию без
    decision-записи: FORCE_FLAT конституции не делает исключений."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [])
    market = FakeMarket(bars=_bars(), account={"balance": 9000.0, "equity": 9000.0},
                        positions=[pos(999)])
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    w.tick(NOW)

    assert ex.methods == ["close_position"]
    assert ex.calls[0]["args"][0] == 999


# --------------------------------------------------------------------------
# стоп-кран: правило 2 — позиция без стоп-лосса
# --------------------------------------------------------------------------

@pytest.mark.parametrize("failure", [
    RuntimeError("terminal rejected modify"),
    {"ok": False, "error": "invalid stops"},
    {"retcode": 10016},
])
def test_position_without_sl_gets_sl_then_closed_if_fails(tmp_path, failure):
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2395.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(111, sl=0.0, price_current=2405.0)])
    ex = RecordingExecutor(results={"modify_sl": failure})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)

    assert ex.methods == ["modify_sl", "close_position"], ex.calls
    assert ex.calls[0]["args"] == (111, 2395.0)
    assert ex.calls[1]["args"] == (111,)
    ev = _lines(out)[0]
    assert ev["alert_type"] == "position_without_sl"
    assert ev["priority"] == "critical"
    assert ev["ticket"] == 111
    assert ev["action"]["done"] == "closed_after_failed_sl"


def test_position_without_sl_restored_and_not_closed(tmp_path):
    """Успешная установка SL — второго действия нет: закрывать нечего."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2395.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(111, sl=0.0, price_current=2405.0)])
    ex = RecordingExecutor(results={"modify_sl": {"ok": True}})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)

    assert ex.methods == ["modify_sl"]
    assert _lines(out)[0]["action"]["done"] == "sl_restored"


@pytest.mark.parametrize("answer", [{"ok": True}, None, {"comment": "неизвестная форма"}])
def test_position_without_sl_closed_after_repeated_attempts(tmp_path, answer):
    """SL «поставился» (или ответ брокера не опознан), но на следующих тиках
    стопа по-прежнему нет — датчик не верит результату бесконечно и закрывает
    позицию. Неопознанный ответ не считается ни успехом, ни поводом закрыть
    немедленно: закрытие необратимо, а следующий тик покажет правду."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2395.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(111, sl=0.0, price_current=2405.0)])
    ex = RecordingExecutor(results={"modify_sl": answer})
    w = _watch(tmp_path, cfg, market, executor=ex)

    for i in range(4):
        w.tick(NOW + dt.timedelta(seconds=10 * i))

    assert ex.methods[:aw.MAX_SL_ATTEMPTS] == ["modify_sl"] * aw.MAX_SL_ATTEMPTS
    assert "close_position" in ex.methods


def test_journal_sl_already_breached_closes_position(tmp_path):
    """Стоп из журнала уже пробит ценой: поставить его нельзя (брокер отвергнет
    стоп по «неверной стороне»), а позиция должна была быть закрыта им же."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2395.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(111, sl=0.0, price_current=2390.0, profit=-100.0)])
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    w.tick(NOW)

    assert ex.methods == ["close_position"], "modify_sl с заведомо неверной стороной не шлём"
    assert ex.calls[0]["args"] == (111,)


def test_orphan_without_sl_is_reported_not_touched(tmp_path):
    """Позиция без decision-записи и без SL: стопа из журнала не существует,
    выдумывать его нельзя, а закрывать чужую позицию датчику запрещено (та же
    развилка, что в risk_gate_cli: orphan → HALT_NEW, не FORCE_FLAT). Модель
    будят, действий нет."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[pos(777, sl=0.0)])
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)
    w.tick(NOW + dt.timedelta(seconds=30))

    assert ex.calls == [], "чужую позицию датчик не трогает"
    ev = _lines(out)
    assert len(ev) == 1, "сообщение о ней — один раз, а не каждый тик"
    assert ev[0]["alert_type"] == "position_without_sl"
    assert ev[0]["action"]["done"] == "none"
    assert ev[0]["ticket"] == 777


# --------------------------------------------------------------------------
# доставка пробуждения: событие о действии деньгами не теряется
# --------------------------------------------------------------------------

def _critical_alert_world(tmp_path, *, equity=10000.0, positions=None):
    """Мир, где есть постоянно истинный critical-алерт модели: им съедается
    интервал critical, и в эту «тень» попадают события стоп-крана."""
    _baselines(tmp_path)
    (tmp_path / "spread_median.json").write_text(json.dumps({"XAUUSD": 9.0}), encoding="utf-8")
    _write_alerts(tmp_path, {"id": "spread", "type": "spread_anomaly", "symbol": "XAUUSD",
                             "mult": 0.0, "priority": "critical"})
    account = {"balance": 10000.0, "equity": equity}
    market = FakeMarket(bars=_bars(), account=account,
                        positions=positions if positions is not None else [])
    return market, account


def test_wall_event_retried_until_delivered(tmp_path):
    """Пробуждение о пробитой стене не теряется, даже если попало в интервал
    critical: фронт считается израсходованным ТОЛЬКО после фактической
    доставки. Иначе всё выглядело так: позиции закрыты, событие придушено,
    edge потрачен — и модель не узнаёт о закрытии НИКОГДА.
    """
    cfg = _cfg(tmp_path)  # дефолтный конфиг: интервал critical 15с
    _journal(tmp_path, [decision(111)])
    market, account = _critical_alert_world(tmp_path, positions=[pos(111)])
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)  # critical-алерт модели съедает интервал
    assert [e["alert_type"] for e in _lines(out)] == ["spread_anomaly"]

    account["equity"] = 9700.0  # стена пробита
    w.tick(NOW + dt.timedelta(seconds=2))
    assert ex.methods == ["close_position"], "закрытие не ждёт бюджета событий"
    types = [e["alert_type"] for e in _lines(out)]
    assert "wall_breach" not in types, "событие придушено интервалом critical"

    w.tick(NOW + dt.timedelta(seconds=20))  # интервал прошёл — фронт всё ещё не потрачен
    types = [e["alert_type"] for e in _lines(out)]
    assert types.count("wall_breach") == 1, f"модель обязана быть разбужена: {types}"

    w.tick(NOW + dt.timedelta(seconds=40))  # рассказано — больше не повторяем
    assert [e["alert_type"] for e in _lines(out)].count("wall_breach") == 1


def test_orphan_report_retried_until_delivered(tmp_path):
    """То же для единственного сообщения о чужой позиции без стопа: в
    _sl_reported тикет попадает только по факту доставки."""
    cfg = _cfg(tmp_path)
    _journal(tmp_path, [decision(111)])
    # начинаем со своей защищённой позиции (стоп-крану делать нечего); список
    # непустой намеренно — FakeMarket подменяет пустой своим объектом, и
    # дозаписать в него позже было бы невозможно
    positions = [pos(111)]
    market, _ = _critical_alert_world(tmp_path, positions=positions)
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)
    assert [e["alert_type"] for e in _lines(out)] == ["spread_anomaly"]

    positions.append(pos(777, sl=0.0))  # у брокера появилась чужая позиция без стопа
    w.tick(NOW + dt.timedelta(seconds=2))
    assert "position_without_sl" not in [e["alert_type"] for e in _lines(out)]

    w.tick(NOW + dt.timedelta(seconds=20))
    w.tick(NOW + dt.timedelta(seconds=40))
    types = [e["alert_type"] for e in _lines(out)]
    assert types.count("position_without_sl") == 1, f"ровно одно сообщение: {types}"
    assert ex.calls == [], "чужую позицию датчик так и не тронул"


def test_action_reaches_model_after_position_is_gone(tmp_path):
    """ГЛАВНЫЙ ТЕСТ ОЧЕРЕДИ. Стоп-кран закрыл НАШУ позицию (брокер отклонил
    установку стопа), сообщение попало в интервал critical, а позиция после
    закрытия исчезла из терминала. Фронт по живому состоянию здесь не работает
    — состояния больше нет, — поэтому долг держится очередью и досылается.

    Без этого: позиция закрыта скриптом, в stdout ноль строк навсегда.
    """
    cfg = _cfg(tmp_path)
    _journal(tmp_path, [decision(77)])
    positions = [pos(77)]  # сначала позиция со стопом: стоп-крану делать нечего
    market, _ = _critical_alert_world(tmp_path, positions=positions)
    ex = RecordingExecutor(results={"modify_sl": {"ok": False, "retcode": 10016}})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)  # critical-алерт модели съедает интервал
    positions[0]["sl"] = 0.0  # стоп исчез (снят руками/сбоем терминала)
    w.tick(NOW + dt.timedelta(seconds=2))
    assert ex.methods == ["modify_sl", "close_position"], "стоп-кран действовал"
    assert "position_without_sl" not in [e["alert_type"] for e in _lines(out)]
    assert w.tick(NOW + dt.timedelta(seconds=3))["heartbeat"]["pending_undelivered"] == 1

    positions.clear()  # позиция закрыта — из терминала пропала
    res = w.tick(NOW + dt.timedelta(seconds=20))

    told = [e for e in _lines(out) if e["alert_type"] == "position_without_sl"]
    assert len(told) == 1, "модель обязана узнать о закрытии, хотя позиции уже нет"
    e = told[0]
    assert e["delayed_report"] is True and e["ticket"] == 77
    assert e["action"]["done"] == "closed_after_failed_sl"
    assert e["original_fired_utc"] == (NOW + dt.timedelta(seconds=2)).isoformat()
    assert e["delayed_by_seconds"] == 18.0
    assert "ОТЛОЖЕННЫЙ РАССКАЗ" in e["note"], "иначе это выглядит как второе закрытие"
    assert res["heartbeat"]["pending_undelivered"] == 0


def test_pending_undelivered_visible_in_heartbeat(tmp_path):
    """Модель читает пульс и до досылки: «меня ждёт нерассказанное»."""
    cfg = _cfg(tmp_path)
    _journal(tmp_path, [decision(111)])
    market, account = _critical_alert_world(tmp_path, positions=[pos(111)])
    w = _watch(tmp_path, cfg, market)

    w.tick(NOW)
    account["equity"] = 9700.0
    hb = w.tick(NOW + dt.timedelta(seconds=2))["heartbeat"]
    assert hb["pending_undelivered"] == 1
    assert hb["pending_undelivered_ids"] == ["stop-valve-wall_breach"]


def test_wall_story_told_once_with_history(tmp_path):
    """Живой рассказ по фронту и отложенная копия не имеют права стать ДВУМЯ
    сообщениями об одном закрытии — но и факты терять нельзя: к моменту
    доставки позиции уже закрыты, и свежее событие о них не расскажет."""
    cfg = _cfg(tmp_path)
    _journal(tmp_path, [decision(111)])
    positions = [pos(111)]
    market, account = _critical_alert_world(tmp_path, positions=positions)
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NOW)
    account["equity"] = 9700.0
    w.tick(NOW + dt.timedelta(seconds=2))       # закрыл, событие придушено
    positions.clear()                            # позиции закрыты и пропали
    w.tick(NOW + dt.timedelta(seconds=20))
    w.tick(NOW + dt.timedelta(seconds=40))
    w.tick(NOW + dt.timedelta(seconds=60))

    walls = [e for e in _lines(out) if e["alert_type"] == "wall_breach"]
    assert len(walls) == 1, f"ровно одно сообщение о закрытии: {len(walls)}"
    assert walls[0]["action"]["closed"] == [111], "в нём сохранён факт закрытия тикета"


def test_queue_deduplicates_by_alert_id(tmp_path):
    """Пока стена пробита, рассказ собирается каждый тик. Без склейки по
    alert_id очередь набирала бы по записи в секунду, и при переполнении
    выбрасывалось бы САМОЕ СТАРОЕ — то есть первое сообщение о закрытии."""
    cfg = _cfg(tmp_path, min_seconds_between_critical_events=10_000)
    _journal(tmp_path, [decision(111)])
    market, account = _critical_alert_world(tmp_path, positions=[pos(111)])
    w = _watch(tmp_path, cfg, market)

    w.tick(NOW)
    account["equity"] = 9700.0
    for i in range(1, 31):
        hb = w.tick(NOW + dt.timedelta(seconds=i))["heartbeat"]
    assert hb["pending_undelivered"] == 1, "30 тиков — один долг"
    assert hb["pending_undelivered_ids"] == ["stop-valve-wall_breach"]


def test_queue_overflow_is_loud(tmp_path, monkeypatch):
    """Переполнение очереди — тоже потеря сообщения о действии деньгами, и она
    обязана быть громкой: ошибка в errors и в пульсе."""
    monkeypatch.setattr(aw, "MAX_UNDELIVERED", 2)
    cfg = _cfg(tmp_path, min_seconds_between_critical_events=10_000)
    _journal(tmp_path, [decision(1), decision(2), decision(3)])
    positions = [pos(1), pos(2), pos(3)]  # со стопами: первый тик без действий
    market, _ = _critical_alert_world(tmp_path, positions=positions)
    ex = RecordingExecutor(results={"modify_sl": {"ok": False}})
    w = _watch(tmp_path, cfg, market, executor=ex)

    w.tick(NOW)  # critical-алерт модели съедает интервал
    for p in positions:
        p["sl"] = 0.0  # стопы исчезли у всех трёх разом
    res = w.tick(NOW + dt.timedelta(seconds=2))

    assert res["heartbeat"]["pending_undelivered"] == 2
    assert any("переполнена" in e for e in res["errors"]), res["errors"]
    assert any("stop-valve-position_without_sl-1" in e for e in res["errors"])


def test_flush_does_not_bypass_event_budget(tmp_path):
    """Досылка обязана считаться бюджетом наравне со всем остальным: очередь,
    обходящая потолок событий в минуту, завалила бы Monitor и убила механизм
    пробуждения — ровно то, от чего бюджет и существует."""
    cfg = _cfg(tmp_path, max_events_per_minute=2, min_seconds_between_critical_events=0,
               min_seconds_between_events=0)
    _journal(tmp_path, [decision(1), decision(2), decision(3), decision(4)])
    positions = [pos(t, sl=0.0) for t in (1, 2, 3, 4)]
    market, _ = _critical_alert_world(tmp_path, positions=positions)
    ex = RecordingExecutor(results={"modify_sl": {"ok": False}})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    for i in range(6):
        w.tick(NOW + dt.timedelta(seconds=i))
    # все шесть тиков внутри одной минуты: доставлено не больше потолка
    assert len(_lines(out)) <= 2, f"потолок в минуту обойдён: {len(_lines(out))} строк"
    # долг при этом не потерян: он ждёт в очереди, а не выброшен
    assert w.tick(NOW + dt.timedelta(seconds=6))["heartbeat"]["pending_undelivered"] > 0


def test_failed_close_is_reported_even_after_wall_is_gone(tmp_path):
    """НЕУДАЧНАЯ попытка закрытия — тоже действие деньгами, и она важнее
    удачной: «стена пробита, закрыть не смог» модель обязана узнать. Фронт
    здесь не спасает: как только equity восстановился, стена больше не
    пробита, живого рассказа не будет никогда — долг держит очередь.
    """
    cfg = _cfg(tmp_path)
    _journal(tmp_path, [decision(111)])
    market, account = _critical_alert_world(tmp_path, positions=[pos(111)])
    ex = RecordingExecutor(results={"close_position": {"ok": False, "retcode": 10018}})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)                                   # critical-алерт съел интервал
    account["equity"] = 9700.0
    w.tick(NOW + dt.timedelta(seconds=2))         # закрыть не смог, событие придушено
    assert ex.methods == ["close_position"]
    assert "wall_breach" not in [e["alert_type"] for e in _lines(out)]

    account["equity"] = 10000.0                   # equity вернулся, стена не пробита
    w.tick(NOW + dt.timedelta(seconds=20))

    walls = [e for e in _lines(out) if e["alert_type"] == "wall_breach"]
    assert len(walls) == 1, "о неудачной попытке модель обязана узнать"
    assert walls[0]["delayed_report"] is True
    assert walls[0]["action"]["failed"] == [111]


def test_waiting_debt_does_not_flood_the_event_journal(tmp_path):
    """Долг, ждущий бюджета, не имеет права писать по записи в журнал каждый
    тик: датчик крутится раз в секунду, и alert_events.jsonl пух бы на 2 КБ/с
    всё время придушения. Поэтому досылка спрашивает бюджет ДО попытки
    отправки, а не узнаёт об отказе постфактум внутри _emit.
    """
    cfg = _cfg(tmp_path, min_seconds_between_critical_events=10_000)
    _journal(tmp_path, [decision(111)])
    market, account = _critical_alert_world(tmp_path, positions=[pos(111)])
    ex = RecordingExecutor(results={"close_position": {"ok": False}})
    w = _watch(tmp_path, cfg, market, executor=ex)

    w.tick(NOW)
    account["equity"] = 9700.0
    w.tick(NOW + dt.timedelta(seconds=2))       # долг встал в очередь
    account["equity"] = 10000.0                  # стена ушла: живого рассказа больше нет
    after_enqueue = len(_events_file(tmp_path))

    for i in range(3, 13):
        w.tick(NOW + dt.timedelta(seconds=i))

    assert w.tick(NOW + dt.timedelta(seconds=13))["heartbeat"]["pending_undelivered"] == 1
    assert len(_events_file(tmp_path)) == after_enqueue, \
        "ожидание бюджета не должно оставлять следов в журнале событий"


def test_price_alerts_are_not_queued(tmp_path):
    """В очередь попадают ТОЛЬКО сообщения о действиях деньгами. Придушенный
    алерт по цене терять допустимо — условие сработает снова само."""
    cfg = _cfg(tmp_path, min_seconds_between_events=10_000)
    _baselines(tmp_path)
    _write_alerts(tmp_path,
                  {"id": "a1", "symbol": "XAUUSD", "type": "price_above", "level": 2400.0},
                  {"id": "a2", "symbol": "XAUUSD", "type": "price_below", "level": 9999.0})
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=[])
    w = _watch(tmp_path, cfg, market)
    hb = w.tick(NOW)["heartbeat"]
    assert hb["pending_undelivered"] == 0


def test_close_by_wall_precedes_diagnostics(tmp_path, monkeypatch):
    """Порядок шагов закреплён: закрытие по стене идёт РАНЬШЕ чтения журнала,
    гейта и экспозиции. Перестановка шагов проходила все тесты — а на живом
    терминале это означает, что приказ на закрытие ждёт парсинга журнала.
    """
    trace = []
    real_read = aw.read_records
    monkeypatch.setattr(aw, "read_records", lambda *a, **k: (trace.append("journal"),
                                                             real_read(*a, **k))[1])
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _journal(tmp_path, [decision(111)])
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    ex = RecordingExecutor(results={"close_position": lambda *a: trace.append("close") or
                                    {"ok": True}})
    _watch(tmp_path, cfg, market, executor=ex).tick(NOW)

    assert "close" in trace and "journal" in trace, trace
    assert trace.index("close") < trace.index("journal"), \
        f"закрытие обязано идти до диагностик, порядок был {trace}"


def test_silence_wakes_the_model_when_nothing_else_will(tmp_path):
    """СЦЕНАРИЙ ВЛАДЕЛЬЦА СЧЁТА: все условия сработали и разоружились, цена туда больше не
    пришла — и модель спит до тех пор, пока человек не напишет в чат.

    Будильник нельзя поручать тому, кто спит: если модель забыла вооружить себе
    алерт на тишину, заметить это некому. Поэтому проверка живости живёт в
    датчике — у него свои часы и отдельный процесс.
    """
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "a1", "symbol": "XAUUSD", "type": "price_above",
                             "level": 9999.0, "once": True})  # никогда не сработает
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NOW)                                   # старт: тишина только началась
    assert _lines(out) == []
    w.tick(NOW + dt.timedelta(minutes=29))
    assert _lines(out) == [], "до порога датчик молчит"

    res = w.tick(NOW + dt.timedelta(minutes=31))
    fired = [e for e in _lines(out) if e["alert_type"] == "watch_silence"]
    assert len(fired) == 1, "порог пройден — модель обязана быть разбужена"
    assert fired[0]["priority"] == "critical"
    assert fired[0]["detail"]["silent_minutes"] == 31.0
    assert fired[0]["detail"]["armed_alerts"] == 1
    assert "перепиши alerts.json" in fired[0]["note"]
    assert res["heartbeat"]["walls_checked"] is True


def test_silence_clock_resets_after_any_delivered_event(tmp_path):
    """Событие любого рода — доказательство, что механизм жив: часы тишины
    начинаются заново, а не тикают от старта."""
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "a1", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0, "once": True})
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NOW)                                   # сработал price_above
    assert [e["alert_type"] for e in _lines(out)] == ["price_above"]
    w.tick(NOW + dt.timedelta(minutes=25))        # 25 мин после события — рано
    assert len(_lines(out)) == 1
    w.tick(NOW + dt.timedelta(minutes=31))        # 31 мин после события — пора
    assert [e["alert_type"] for e in _lines(out)][-1] == "watch_silence"


def test_silence_event_does_not_repeat_every_tick(tmp_path):
    """Событие тишины само сбрасывает часы: иначе оно повторялось бы каждую
    секунду и убило бы механизм, который призвано защищать."""
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NOW)
    for i in range(31, 40):
        w.tick(NOW + dt.timedelta(minutes=i))
    assert len(_lines(out)) == 1, f"повторов быть не должно: {len(_lines(out))}"


def test_silence_checked_even_when_terminal_is_down(tmp_path):
    """Когда терминал недоступен, молчание опаснее всего: стена не считается,
    а модель об этом не знает. Проверка живости идёт вне блока стены."""
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)
    market = TogglableMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NOW)
    market.broken = True
    res = w.tick(NOW + dt.timedelta(minutes=31))
    assert [e["alert_type"] for e in _lines(out)] == ["watch_silence"]
    assert res["heartbeat"]["walls_checked"] is False


def test_silence_disabled_by_zero_threshold(tmp_path):
    cfg = _cfg(tmp_path, max_silence_minutes=0, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)
    w.tick(NOW)
    w.tick(NOW + dt.timedelta(hours=5))
    assert _lines(out) == []


def test_stop_valve_survives_broken_journal(tmp_path):
    """Битая строка в journal.jsonl (его пишет модель каждую сессию) не отменяет
    закрытие по стене: правилу 1 нужны только equity, базы и список позиций.
    Алерты по цене при этом продолжают работать, а снимок честно показывает, что
    гейт и принадлежность позиций в этот тик неизвестны."""
    cfg = _cfg(tmp_path)
    (tmp_path / "journal.jsonl").write_text('{"type": "decision", "trade_id": "111"\n',
                                            encoding="utf-8")
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    _write_alerts(tmp_path, {"id": "h1", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0})
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    res = w.tick(NOW)

    assert ex.methods == ["close_position"]
    assert any("журнал" in e for e in res["errors"]), res["errors"]
    hb = res["heartbeat"]
    assert hb["walls_checked"] is True
    assert hb["ts"] == NOW.isoformat(), "стена посчитана — пульс защиты свежий"
    assert hb["orphans"] is None, "журнал не прочитан — принадлежность неизвестна"
    delivered = {e["alert_type"] for e in res["delivered"]}
    assert {"wall_breach", "price_above"} <= delivered
    wall_event = next(e for e in res["delivered"] if e["alert_type"] == "wall_breach")
    assert wall_event["snapshot"]["gate"] is None
    assert wall_event["snapshot"]["account"]["equity"] == 9700.0


def test_heartbeat_goes_stale_when_walls_not_checked(tmp_path):
    """Пульс heartbeat — свежесть ЗАЩИТЫ, а не процесса: живой датчик со
    сломанным стоп-краном обязан выглядеть мёртвым, иначе правило «heartbeat
    старше 90с → я незащищена» не срабатывает никогда."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    market = TogglableMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market)

    market.broken = True
    hb = w.tick(NOW)["heartbeat"]
    assert hb["ts"] is None, "стена не проверена ни разу — пульса защиты нет"
    assert hb["tick_utc"] == NOW.isoformat(), "но видно, что процесс жив"
    assert hb["walls_checked"] is False
    assert hb["errors"]

    market.broken = False
    ok_at = NOW + dt.timedelta(seconds=60)
    assert w.tick(ok_at)["heartbeat"]["ts"] == ok_at.isoformat()

    market.broken = True
    later = NOW + dt.timedelta(seconds=180)
    hb = w.tick(later)["heartbeat"]
    assert hb["ts"] == ok_at.isoformat(), "пульс защиты не двигается, пока стена не считается"
    assert hb["tick_utc"] == later.isoformat()


def test_wall_error_makes_watch_visibly_blind(tmp_path, monkeypatch):
    """Ошибка расчёта стены обязана долетать до тика, а не превращаться в
    «стена не пробита». Именно поэтому в wall_state вызывается evaluate_gate, а
    не safe_evaluate_gate: обёртка отдала бы HALT_NEW, стоп-кран оказался бы
    тихо выключен, а heartbeat рапортовал бы, что стена проверена."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    ex = RecordingExecutor()

    def _boom(**kwargs):
        raise RuntimeError("лимиты не разобрались")

    monkeypatch.setattr(aw, "evaluate_gate", _boom)
    w = _watch(tmp_path, cfg, market, executor=ex)

    res = w.tick(NOW)

    assert ex.calls == [], "по непосчитанной стене действовать нельзя"
    assert res["heartbeat"]["walls_checked"] is False
    assert res["heartbeat"]["ts"] is None
    assert any("стена не посчитана" in e for e in res["errors"]), res["errors"]


def test_journal_write_failure_is_visible(tmp_path, monkeypatch):
    """Отказ записи события в журнал виден в errors и в heartbeat: действие
    деньгами без следа не имеет права остаться при «зелёном» пульсе."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    ex = RecordingExecutor()
    out = io.StringIO()

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(aw, "append_alert_event", _boom)
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    res = w.tick(NOW)

    assert ex.methods == ["close_position"]
    assert len(_lines(out)) == 1, "модель разбудили"
    assert any("журнал" in e for e in res["errors"]), res["errors"]
    assert res["heartbeat"]["errors"]


def test_one_world_snapshot_per_tick(tmp_path):
    """Счёт и позиции опрашиваются РОВНО ОДИН раз за тик: иначе стена считается
    по одному чтению, а снимок для модели — по другому."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = CountingMarket(bars=_bars(), account={"balance": 10000.0, "equity": 9950.0},
                            positions=[pos(111)])
    _write_alerts(tmp_path, {"id": "h1", "symbol": "XAUUSD", "type": "price_above",
                             "level": 2400.0})
    w = _watch(tmp_path, cfg, market)

    w.tick(NOW)

    assert market.calls == {"account_info": 1, "positions": 1}, market.calls


def test_snapshot_and_wall_use_the_same_equity(tmp_path):
    """При дрейфе equity снимок не имеет права быть внутренне противоречивым:
    модель решает по нему без дополнительного шага. Раньше стена считалась по
    9700 (позиции закрыты), а в снимке стояли прежние 10000 и вердикт гейта OK.
    """
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = DriftingAccountMarket(bars=_bars(), positions=[pos(111)],
                                   equities=[9700.0, 10000.0, 10000.0, 10000.0])
    ex = RecordingExecutor()
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)

    snap = _lines(out)[0]["snapshot"]
    assert snap["walls"]["breached"] is True
    assert snap["walls"]["daily_loss_pct"] == pytest.approx(3.0)
    assert snap["account"]["equity"] == 9700.0
    assert snap["gate"]["verdict"] == "FORCE_FLAT", "гейт видел ту же equity, что и стена"


def test_wall_numbers_match_gate_inputs(tmp_path):
    """Стена и риск-гейт считают от ОДНИХ баз equity (одна функция-источник):
    расхождение вылезло бы ровно в момент пробития стены."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path, equity=10500.0, initial=9800.0)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 9900.0})
    w = _watch(tmp_path, cfg, market)

    walls = w._poll_walls(aw._OneShotMarket(market), NOW)  # шаг 1 тика напрямую
    inputs = build_gate_inputs(market, cfg, [], now=NOW, positions=[])

    assert walls["numbers"] == {"equity": inputs["equity"],
                                "day_start_equity": inputs["day_start_equity"],
                                "initial_balance": inputs["initial_balance"]}


def test_orphan_detection_agrees_with_find_orphans(tmp_path):
    """Принадлежность позиции определяется по уже прочитанным записям, а не
    вторым разбором journal.jsonl — но по ТОМУ ЖЕ тождеству, что find_orphans."""
    positions = [pos(111), pos(777), pos(888, sl=0.0)]
    records = [decision(111)]
    _journal(tmp_path, records)

    mine = aw.orphan_tickets(positions, aw.decisions_by_ticket(records))
    theirs = {o["ticket"] for o in find_orphans(tmp_path / "journal.jsonl", positions)}

    assert mine == theirs == {777, 888}


def test_executor_is_required(tmp_path):
    """Датчик без стоп-крана не создаётся: иначе он падал бы AttributeError
    ровно в момент пробитой стены, то есть «датчик без стоп-крана» существовал
    бы как рабочий режим."""
    cfg = _cfg(tmp_path)
    market = FakeMarket(bars=_bars())

    with pytest.raises(ValueError):
        aw.AlertWatch(market, cfg, executor=None)

    class HalfExecutor:  # реализует только половину протокола
        def close_position(self, ticket):
            return {"ok": True}

    with pytest.raises(ValueError):
        aw.AlertWatch(market, cfg, executor=HalfExecutor())


def test_corrupted_events_journal_restores_restrictively(tmp_path):
    """Битая строка в alert_events.jsonl не даёт пустого восстановления бюджета:
    непрочитанная запись трактуется как «событие было, и было только что» —
    иначе защита механизма пробуждения тихо снималась бы при каждом старте."""
    cfg = _cfg(tmp_path)
    market, _ = _critical_alert_world(tmp_path)
    (tmp_path / "alert_events.jsonl").write_text('{"type": "alert_event", "priority"\n',
                                                 encoding="utf-8")
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    res = w.tick(NOW)

    assert res["delivered"] == [], "бюджет восстановлен ограничительно"
    assert any("журнал событий" in e for e in res["errors"]), res["errors"]

    w.tick(NOW + dt.timedelta(seconds=cfg.alerts.min_seconds_between_critical_events + 1))
    assert len(_lines(out)) == 1, "через интервал критическое всё же доходит"


def test_position_state_pruned_when_position_closes(tmp_path):
    """Состояние по тикетам не копится в памяти долгоживущего процесса."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2395.0)])
    positions = [pos(111, sl=0.0, price_current=2405.0)]
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
                        positions=positions)
    ex = RecordingExecutor(results={"modify_sl": {"ok": True}})
    w = _watch(tmp_path, cfg, market, executor=ex)

    w.tick(NOW)
    assert w._sl_attempts and w._last_attempt

    positions.clear()  # позиция закрылась
    w.tick(NOW + dt.timedelta(seconds=10))

    assert w._sl_attempts == {}
    assert w._sl_reported == set()
    assert w._last_attempt == {}


# --------------------------------------------------------------------------
# ГЛАВНЫЙ ТЕСТ: никаких торговых действий вне двух правил
# --------------------------------------------------------------------------

def _all_type_alerts():
    """По алерту на каждый из 18 типов. Пороги намеренно тривиальны (−1e9 /
    +1e9 / 0): семантику условий проверяет test_alerts.py, а здесь важно
    другое — что КАЖДЫЙ тип реально сработал и не вызвал ни одного действия."""
    seed = {"armed": True, "last_fired_utc": None}
    return [
        {"id": "a-price_above", "type": "price_above", "symbol": "XAUUSD", "level": -1e9},
        {"id": "a-price_below", "type": "price_below", "symbol": "XAUUSD", "level": 1e9},
        {"id": "a-price_touch", "type": "price_touch", "symbol": "XAUUSD", "level": 0.0,
         "tolerance_atr": 1e9},
        {"id": "a-atr_above", "type": "atr_pctile_above", "symbol": "XAUUSD", "level": -1.0},
        {"id": "a-atr_below", "type": "atr_pctile_below", "symbol": "XAUUSD", "level": 2.0},
        {"id": "a-trend", "type": "trend_flips", "symbol": "XAUUSD", "tf": "M5",
         "_state": {**seed, "remember": "down"}},
        {"id": "a-R-reach", "type": "position_R_reaches", "ticket": 111, "level": -1e9},
        {"id": "a-R-drop", "type": "position_R_drops_to", "ticket": 222, "level": 0.0},
        {"id": "a-time", "type": "position_time_elapsed", "ticket": 111, "minutes": 0,
         "min_progress_R": 1e9},
        {"id": "a-news", "type": "news_window_opens", "minutes_before": 1e9},
        {"id": "a-spread", "type": "spread_anomaly", "symbol": "XAUUSD", "mult": 0.0},
        # порог нормы мягче порога аномалии — иначе близнецы взаимно
        # исключают друг друга и в одном мире сработать вместе не могут
        {"id": "a-spread-norm", "type": "spread_normalizes", "symbol": "XAUUSD",
         "mult": 1e9},
        {"id": "a-gap", "type": "gap", "symbol": "XAUUSD"},
        {"id": "a-sljump", "type": "sl_jumped", "ticket": 222},
        {"id": "a-stale", "type": "data_stale", "symbol": "XAUUSD"},
        # remember намеренно не совпадает НИ С ОДНИМ вердиктом гейта: тест про
        # дисциплину датчика, а не про то, какой вердикт даст этот мир
        {"id": "a-gate", "type": "gate_verdict_changes",
         "_state": {**seed, "remember": "ПРЕДЫДУЩИЙ"}},
        {"id": "a-phase", "type": "session_phase_changes",
         "_state": {**seed, "remember": "BRIEF"}},
        {"id": "a-at", "type": "time_at_utc", "at": (NOW - dt.timedelta(minutes=1)).isoformat()},
        {"id": "a-silence", "type": "silence_timeout", "minutes": 0},
    ]


def _rich_world(tmp_path, *, last_bar_utc=None):
    last_bar_utc = last_bar_utc or (NOW - dt.timedelta(hours=1))
    """Мир, в котором истинны условия всех 18 типов сразу, но стоп-крану делать
    нечего: equity на уровне базы (стены далеко), у обеих позиций есть SL."""
    _baselines(tmp_path, equity=10000.0, initial=10000.0)
    _journal(tmp_path, [decision(111, entry=2400.0, sl=2390.0),
                        decision(222, entry=2400.0, sl=2395.0)])
    (tmp_path / "spread_median.json").write_text(json.dumps({"XAUUSD": 9.0}), encoding="utf-8")
    (tmp_path / "news_cache.json").write_text(json.dumps({"events": [
        {"name": "NFP", "utc": (NOW + dt.timedelta(minutes=10)).isoformat()}]}), encoding="utf-8")
    # уже доставленное событие в прошлом — иначе silence_timeout нечего считать
    (tmp_path / "alert_events.jsonl").write_text(json.dumps({
        "type": "alert_event", "ts": (NOW - dt.timedelta(hours=3)).isoformat(),
        "fired_utc": (NOW - dt.timedelta(hours=3)).isoformat(), "alert_id": "seed",
        "alert_type": "time_at_utc", "model_id": "claude-opus-5", "priority": "normal",
        "delivered": True}, ensure_ascii=False) + "\n", encoding="utf-8")
    # бары старые (data_stale) и с разрывом на открытии последнего бара (gap)
    bars = _bars(last_bar_utc=last_bar_utc, gap=50.0)
    positions = [pos(111, price_open=2400.0, sl=2390.0, price_current=2410.0, profit=100.0),
                 pos(222, price_open=2400.0, sl=2395.0, price_current=2390.0, profit=-100.0)]
    return FakeMarket(bars=bars, account={"balance": 10000.0, "equity": 10000.0},
                      positions=positions)


def test_no_trading_actions_beyond_two_rules(tmp_path):
    """Каждый тип условия умеет сработать, и ни один не двигает деньги.

    ДВА МИРА, А НЕ ОДИН — следствие защиты от протухшего тика (2026-08-01).
    Свежий и протухший тик по одному символу одновременно невозможны:
    price_above/price_below требуют живой цены, а data_stale существует ровно
    затем, чтобы сообщить о мёртвой. Проверяются оба состояния, объединением.
    """
    cfg = _cfg(tmp_path, **PERMISSIVE)
    ex = RecordingExecutor()
    fired = set()

    for last_bar in (NOW, NOW - dt.timedelta(hours=1)):
        market = _rich_world(tmp_path, last_bar_utc=last_bar)
        _write_alerts(tmp_path, *_all_type_alerts())
        w = _watch(tmp_path, cfg, market, executor=ex)
        fired |= {e["alert_type"] for e in w.tick(NOW)["delivered"]}

    assert fired == set(ALERT_TYPES), f"не сработали типы: {sorted(set(ALERT_TYPES) - fired)}"
    assert ex.calls == [], f"датчик совершил торговое действие вне двух правил: {ex.calls}"


def test_no_trading_actions_when_position_is_deep_in_loss(tmp_path):
    """Отдельная приманка: позиция глубоко в минусе, цена ушла за стоп, время
    вышло — всё это приходит модели алертами, но датчик не режет и не тралит."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(222, entry=2400.0, sl=2395.0)])
    market = FakeMarket(bars=_bars(), account={"balance": 9900.0, "equity": 9900.0},
                        positions=[pos(222, sl=2395.0, price_current=2380.0, profit=-200.0)])
    _write_alerts(tmp_path,
                  {"id": "drop", "type": "position_R_drops_to", "ticket": 222, "level": 1.0},
                  {"id": "jump", "type": "sl_jumped", "ticket": 222, "priority": "critical"},
                  {"id": "stall", "type": "position_time_elapsed", "ticket": 222,
                   "minutes": 1, "min_progress_R": 0.5})
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    res = w.tick(NOW)

    assert len(res["delivered"]) == 3
    assert ex.calls == []


# --------------------------------------------------------------------------
# PROPERTY: событийный бюджет ограничивает поток во времени
# --------------------------------------------------------------------------

def _rate_world(tmp_path):
    _baselines(tmp_path)
    (tmp_path / "spread_median.json").write_text(json.dumps({"XAUUSD": 9.0}), encoding="utf-8")
    return FakeMarket(bars=_bars(n=60), account={"balance": 10000.0, "equity": 10000.0})


def _drive(tmp_path, cfg, market, *, ticks, step_seconds):
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)
    for i in range(ticks):
        w.tick(NOW + dt.timedelta(seconds=i * step_seconds))
    return _lines(out)


def test_event_rate_bounded_over_time(tmp_path):
    """Постоянно истинное условие + много тиков: число выпущенных событий не
    превышает потолка. Ловит порчу ЛЮБОГО из трёх состояний бюджета —
    проверка одного вызова event_budget этого не умеет.

    Сценарий A рушится, если не вести last_critical_event_ts (было бы событие
    на каждый тик), B — если не вести recent_event_ts (потолок в минуту не
    сработал бы на пачке одновременных critical), C — если не вести
    last_event_ts/events_today (normal лился бы каждый тик).
    """
    cfg = _cfg(tmp_path)
    a = cfg.alerts

    # A. один critical, тики раз в секунду 2 минуты → интервал critical
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    cfg_a = _cfg(dir_a)
    _write_alerts(dir_a, {"id": "spread", "type": "spread_anomaly", "symbol": "XAUUSD",
                          "mult": 0.0, "priority": "critical"})
    events_a = _drive(dir_a, cfg_a, _rate_world(dir_a), ticks=120, step_seconds=1)
    cap_a = 120 // a.min_seconds_between_critical_events + 1
    assert 1 <= len(events_a) <= cap_a, f"critical: {len(events_a)} событий при потолке {cap_a}"

    # B. десять critical одновременно, тик раз в 20с 5 минут → потолок в минуту
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    cfg_b = _cfg(dir_b)
    _write_alerts(dir_b, *[{"id": f"spread-{i}", "type": "spread_anomaly", "symbol": "XAUUSD",
                            "mult": 0.0, "priority": "critical"} for i in range(10)])
    events_b = _drive(dir_b, cfg_b, _rate_world(dir_b), ticks=15, step_seconds=20)
    cap_b = a.max_events_per_minute * 5 + a.max_events_per_minute
    assert 1 <= len(events_b) <= cap_b, f"пачка critical: {len(events_b)} при потолке {cap_b}"

    # C. один normal, тик раз в 10с 10 минут → обычный интервал + дневной лимит
    dir_c = tmp_path / "c"
    dir_c.mkdir()
    cfg_c = _cfg(dir_c)
    _write_alerts(dir_c, {"id": "spread", "type": "spread_anomaly", "symbol": "XAUUSD",
                          "mult": 0.0})
    events_c = _drive(dir_c, cfg_c, _rate_world(dir_c), ticks=60, step_seconds=10)
    cap_c = min(600 // a.min_seconds_between_events + 1, a.max_events_per_day)
    assert 1 <= len(events_c) <= cap_c, f"normal: {len(events_c)} событий при потолке {cap_c}"

    # D. ДНЕВНОЙ ЛИМИТ normal — отдельный сценарий, потому что в C он
    # недостижим (интервал 60с ограничивает поток раньше), и порчу счётчика
    # events_today сценарий C структурно поймать не может: «property-тест
    # сторожит все состояния» было бы для него ложным утверждением.
    dir_d = tmp_path / "d"
    dir_d.mkdir()
    cfg_d = _cfg(dir_d, min_seconds_between_events=1, max_events_per_day=3,
                 max_events_per_minute=1000)
    _write_alerts(dir_d, {"id": "spread", "type": "spread_anomaly", "symbol": "XAUUSD",
                          "mult": 0.0})
    events_d = _drive(dir_d, cfg_d, _rate_world(dir_d), ticks=30, step_seconds=2)
    assert len(events_d) == cfg_d.alerts.max_events_per_day, (
        f"дневной лимит: {len(events_d)} событий при лимите "
        f"{cfg_d.alerts.max_events_per_day}")


def test_budget_state_survives_restart(tmp_path):
    """Три состояния бюджета восстанавливаются из журнала событий: перезапуск
    датчика не обнуляет защиту механизма пробуждения."""
    cfg = _cfg(tmp_path)
    market = _rate_world(tmp_path)
    _write_alerts(tmp_path, {"id": "spread", "type": "spread_anomaly", "symbol": "XAUUSD",
                             "mult": 0.0, "priority": "critical"})
    out1 = io.StringIO()
    w1 = _watch(tmp_path, cfg, market, out=out1)
    w1.tick(NOW)
    assert len(_lines(out1)) == 1

    out2 = io.StringIO()  # «перезапуск»: новый объект, то же состояние на диске
    w2 = _watch(tmp_path, cfg, market, out=out2)
    w2.tick(NOW + dt.timedelta(seconds=1))
    assert _lines(out2) == [], "интервал critical обязан пережить перезапуск"

    w2.tick(NOW + dt.timedelta(seconds=cfg.alerts.min_seconds_between_critical_events + 1))
    assert len(_lines(out2)) == 1


# --------------------------------------------------------------------------
# чистые функции: считаются и проверяются без цикла
# --------------------------------------------------------------------------

def test_wall_uses_buffer_not_bare_limit(tmp_path):
    """Стена срабатывает по «лимит минус буфер» (2.7%), а не по голым 3.0%:
    убыток 2.8% обязан закрывать позиции, иначе буфер существует только на
    бумаге."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 9720.0, "equity": 9720.0},
                        positions=[pos(111)])
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    res = w.tick(NOW)

    assert res["heartbeat"]["daily_loss_pct"] == pytest.approx(2.8)
    assert res["heartbeat"]["wall_breached"] is True
    assert ex.methods == ["close_position"]
    # порог, который читает модель, — тот же «лимит минус буфер», по которому
    # сработало правило, а не голый лимит из конфига
    walls = res["delivered"][0]["snapshot"]["walls"]
    assert walls["daily_flat_pct"] == pytest.approx(cfg.risk.daily_loss_limit_pct
                                                   - cfg.risk.flatten_buffer_pct)
    assert walls["total_flat_pct"] == pytest.approx(cfg.risk.total_loss_limit_pct
                                                    - cfg.risk.flatten_buffer_pct)
    assert walls["daily_flat_pct"] == pytest.approx(2.7)


def test_wall_not_breached_before_buffer(tmp_path):
    """Обратная сторона того же: 2.6% — ещё не стена, действий нет."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 9740.0, "equity": 9740.0},
                        positions=[pos(111)])
    ex = RecordingExecutor()
    w = _watch(tmp_path, cfg, market, executor=ex)

    res = w.tick(NOW)

    assert res["heartbeat"]["wall_breached"] is False
    assert ex.calls == []


def test_alert_on_unknown_data_does_not_fire(tmp_path):
    """Баров не хватило → ATR неизвестен → price_touch НЕ срабатывает и уходит
    в skipped с причиной, а price_above по известной цене работает. Правило
    «нет данных → не знаю» доходит до датчика целиком, а не только до
    features.py."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    market = FakeMarket(bars=_bars(n=10), account={"balance": 10000.0, "equity": 10000.0})
    _write_alerts(tmp_path,
                  {"id": "touch", "type": "price_touch", "symbol": "XAUUSD",
                   "level": 2400.0, "tolerance_atr": 1e9},
                  {"id": "above", "type": "price_above", "symbol": "XAUUSD", "level": -1e9})
    w = _watch(tmp_path, cfg, market)

    res = w.tick(NOW)

    assert [e["alert_id"] for e in res["delivered"]] == ["above"]
    skipped = {s["id"]: s["reason"] for s in res["skipped"]}
    assert "ATR" in skipped["touch"]


def test_position_opened_utc_prefers_journal_then_server_time():
    journal_ts = NOW - dt.timedelta(hours=2)
    p = {"time": dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC).timestamp()}

    from_journal = aw.position_opened_utc(p, {"ts": journal_ts.isoformat()},
                                          server_offset_hours=SERVER_OFFSET_H)
    from_broker = aw.position_opened_utc(p, None, server_offset_hours=SERVER_OFFSET_H)

    assert from_journal == journal_ts
    # время позиции MT5 — серверное (+3ч), значит в UTC это 10:00
    assert from_broker == dt.datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    assert aw.position_opened_utc({}, None, server_offset_hours=SERVER_OFFSET_H) is None


def test_plan_sl_action_never_invents_a_level():
    p = pos(111, sl=0.0, price_current=2405.0)

    assert aw.plan_sl_action(p, {"sl": 2395.0}) == {"action": "modify", "sl": 2395.0,
                                                    "reason": None}
    assert aw.plan_sl_action(p, {"sl": 0.0})["action"] == "close"
    assert aw.plan_sl_action(p, None)["action"] == "close"
    # стоп из журнала уже по ту сторону цены — ставить нечего, позиция закрывается
    assert aw.plan_sl_action(pos(111, sl=0.0, price_current=2390.0),
                             {"sl": 2395.0})["action"] == "close"


def test_news_windows_unparsed_is_unknown_not_empty():
    """Нечитаемый кэш новостей — это «не знаю» (None), а не «новостей нет»
    (пустой список): пустой список тихо разрешил бы вход перед релизом."""
    assert aw.news_windows(None, now=NOW) is None
    assert aw.news_windows({"events": [{"name": "NFP"}]}, now=NOW) is None  # нет времени
    assert aw.news_windows({"events": ["NFP в 13:10"]}, now=NOW) is None    # не словарь
    assert aw.news_windows({"мусор": 1}, now=NOW) is None                   # нет ключа
    assert aw.news_windows({"events": []}, now=NOW) == []
    parsed = aw.news_windows(
        {"events": [{"name": "NFP", "utc": (NOW + dt.timedelta(minutes=30)).isoformat()}]},
        now=NOW)
    assert parsed == [{"name": "NFP", "minutes_until": 30.0}]


def test_spread_median_shapes_and_unknown():
    assert aw.spread_median_points({"XAUUSD": 9.0}, "XAUUSD") == 9.0
    assert aw.spread_median_points({"XAUUSD": {"median_points": 9.0}}, "XAUUSD") == 9.0
    assert aw.spread_median_points({"EURUSD": 1.0}, "XAUUSD") is None
    assert aw.spread_median_points(None, "XAUUSD") is None


def test_session_phase_reads_config_windows(tmp_path):
    """Датчик берёт фазу из trader_lib/session.py (задача 5.3), а не считает
    её сам: две копии арифметики разошлись бы, и гейт считал бы REVIEW, пока
    алерт смены фазы ещё молчит."""
    cfg = dataclasses.replace(_cfg(tmp_path), session=dataclasses.replace(
        _cfg(tmp_path).session, phases={"LONDON": ["07:00", "11:00"],
                                        "NY": ["12:15", "16:00"]}))
    assert aw.session_phase(dt.datetime(2026, 7, 27, 8, 0, tzinfo=UTC), cfg) == "LONDON"
    assert aw.session_phase(dt.datetime(2026, 7, 27, 12, 15, tzinfo=UTC), cfg) == "NY"
    assert aw.session_phase(dt.datetime(2026, 7, 27, 11, 30, tzinfo=UTC), cfg) is None


def test_r_multiple_has_no_negative_zero():
    """«−0.0» в снимке, который читает модель, — мусор: ноль есть ноль."""
    flat_sell = pos(111, ptype=1, price_open=2400.0, price_current=2400.0, sl=2405.0)
    assert str(aw.position_r_multiple(flat_sell, {"sl": 2405.0})) == "0.0"


def test_result_ok_distinguishes_failure_from_unknown():
    assert aw._result_ok({"ok": True}) is True
    assert aw._result_ok({"retcode": 10009}) is True
    assert aw._result_ok({"retcode": 10016}) is False
    assert aw._result_ok({"error": "rejected"}) is False
    assert aw._result_ok(None) is None            # неопознанное ≠ успех и ≠ отказ
    assert aw._result_ok({"comment": "done"}) is None


def test_suppressed_stop_valve_event_is_journaled_not_lost(tmp_path):
    """Событие стоп-крана, придушенное бюджетом, всё равно попадает в журнал:
    строка в stdout — это будильник (его поток ограничен ради Monitor), а
    журнал — след того, что скрипт делал с деньгами, и он не теряется."""
    cfg = _cfg(tmp_path)
    _baselines(tmp_path)
    _journal(tmp_path, [decision(111)])
    market = FakeMarket(bars=_bars(), account={"balance": 9700.0, "equity": 9700.0},
                        positions=[pos(111)])
    ex = RecordingExecutor(results={"close_position": {"ok": False, "error": "market closed"}})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, executor=ex, out=out)

    w.tick(NOW)                                  # фронт стены: событие доставлено
    w.tick(NOW + dt.timedelta(seconds=6))        # закрытие снова не удалось, но интервал

    assert len(_lines(out)) == 1
    recs = [r for r in _events_file(tmp_path) if r["alert_type"] == "wall_breach"]
    assert [r["delivered"] for r in recs] == [True, False]
    assert recs[1]["action"]["failed"] == [111]


# --------------------------------------------------------------------------
# правило живости знает про торговые часы
# --------------------------------------------------------------------------

NIGHT = dt.datetime(2026, 7, 27, 22, 30, tzinfo=UTC)   # вне окна, рынок закрыт для нас


def test_silence_stays_quiet_at_night_when_flat(tmp_path):
    """2026-07-27 22:30: правило разбудило впустую — торговать нельзя, позиций
    нет, делать нечего. За ночь такое повторилось бы 3-4 раза и выело бы
    дневной бюджет событий. Ночью без позиций будить некого."""
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    w.tick(NIGHT + dt.timedelta(minutes=31))
    assert _lines(out) == [], "ночью без позиций правило обязано молчать"


def test_silence_still_fires_at_night_with_an_open_position(tmp_path):
    """Открытая позиция отменяет послабление целиком: позиция без присмотра
    ночью опаснее, чем днём."""
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)
    market = FakeMarket(
        bars=_bars(), account={"balance": 10000.0, "equity": 10000.0},
        positions=[{"ticket": 1, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
                    "price_open": 2400.0, "sl": 2395.0, "tp": 0.0,
                    "price_current": 2400.0, "profit": 0.0, "magic": 0}])
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    w.tick(NIGHT + dt.timedelta(minutes=31))
    assert [e["alert_type"] for e in _lines(out)] == ["watch_silence"]


def test_silence_fires_at_night_when_terminal_is_unreachable(tmp_path):
    """Терминал молчит — про позиции ничего не известно. Тогда послабление НЕ
    применяется: молчать о возможной незакрытой позиции, потому что «наверное,
    ночь», — ровно та ошибка, от которой правило и защищает."""
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)

    class Dead(FakeMarket):
        def account_info(self):
            raise RuntimeError("терминал недоступен")

    market = Dead(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    w.tick(NIGHT + dt.timedelta(minutes=31))
    assert [e["alert_type"] for e in _lines(out)] == ["watch_silence"]


def test_silence_quiet_on_weekend_after_terminal_went_down_while_flat(tmp_path):
    """РЕГРЕСС 2026-08-01 (суббота 07:05): за выходные правило дало бы ~16
    критических пробуждений подряд.

    На выходных терминал не отвечает, `positions` приходит None, и послабление
    «вне окна и без позиций — молчать» не применяется: неизвестное состояние
    позиций трактуется как опасное. Само по себе это верно, но датчик УЖЕ
    видел у брокера ноль позиций своими глазами — в пятницу перед закрытием и
    на каждом тике до обрыва. Позицию нельзя открыть мимо того же терминала,
    поэтому последнее ПРЯМОЕ наблюдение датчика (не запись в журнале, которой
    правило справедливо не доверяет) остаётся действительным.

    Защитное свойство сохраняется: если наблюдения не было вовсе или последнее
    показывало позицию — правило будит, как и раньше.
    """
    cfg = _cfg(tmp_path, max_silence_minutes=30, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path)

    class DiesLater(FakeMarket):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.alive = True

        def account_info(self):
            if not self.alive:
                raise RuntimeError("терминал недоступен: выходные")
            return super().account_info()

    market = DiesLater(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)                      # терминал жив: датчик видит ноль позиций
    market.alive = False               # рынок закрылся, терминал отвалился
    w.tick(NIGHT + dt.timedelta(minutes=31))

    assert _lines(out) == [], "после прямого наблюдения «позиций нет» правило обязано молчать"


def test_watcher_accumulates_live_spread_window(tmp_path):
    """Ф1: живую медиану спреда собирает ДАТЧИК, а не модель.

    Модель просыпается ~10-20 раз в сутки и физически не может отследить спред,
    который меняется каждую секунду. Датчик же читает его на каждом тике уже
    сейчас (build_symbol_ctx -> spread_points) — и до сих пор выбрасывал сразу
    после проверки. Накопление детерминировано, поэтому живёт в коде и стоит
    ноль пробуждений.
    """
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    # символы для контекста берутся из алертов и позиций — нужен хотя бы один
    _write_alerts(tmp_path, {"id": "x", "type": "price_above", "symbol": "XAUUSD",
                             "level": 99999.0})
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())

    w.tick(NIGHT)
    w.tick(NIGHT + dt.timedelta(seconds=1))

    assert w.live_spread.samples("XAUUSD", now=NIGHT) >= 2, "замеры обязаны накапливаться"
    assert w.live_spread.median("XAUUSD", now=NIGHT) is not None


def test_watcher_persists_live_spread_between_restarts(tmp_path):
    """Перезапуск датчика не должен обнулять базу: иначе после каждого рестарта
    гейт час работает на барной медиане — ровно та, что блокировала входы."""
    from trader_lib.spread_gate import LiveSpreadWindow

    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "x", "type": "price_above", "symbol": "XAUUSD",
                             "level": 99999.0})
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})

    w = _watch(tmp_path, cfg, market, out=io.StringIO())
    w.tick(NIGHT)
    w.live_spread.save(tmp_path / "spread_live.json")

    restored = LiveSpreadWindow.load(tmp_path / "spread_live.json")
    assert restored.samples("XAUUSD", now=NIGHT) == w.live_spread.samples("XAUUSD", now=NIGHT)


def test_live_spread_is_written_to_disk_but_not_every_tick(tmp_path):
    """Запись раз в секунду — бессмысленный износ диска; раз в минуту хватает.
    Потеря последней минуты безобидна, потеря всего окна заставила бы гейт час
    работать на барной медиане."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "x", "type": "price_above", "symbol": "XAUUSD",
                             "level": 99999.0})
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())

    assert w._save_live_spread(NIGHT) is True, "первый сброс обязан произойти"
    assert w._save_live_spread(NIGHT + dt.timedelta(seconds=5)) is False
    assert w._save_live_spread(NIGHT + dt.timedelta(seconds=61)) is True
    assert (tmp_path / "spread_live.json").exists()


# --------------------------------------------------------------------------
# КОМАНДА (Ф3): один датчик на всех, алерты у каждого свои
# --------------------------------------------------------------------------

def _team_alerts(tmp_path, trader, *items):
    """Личный alerts.json трейдера в его пространстве имён."""
    from trader_lib.workspace import trader_dir
    from trader_lib.config import load_config
    import dataclasses
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    d = trader_dir(cfg, trader, create=True)
    write_alerts_atomic(d / "alerts.json", _alerts_doc(*items))
    return d


def test_watcher_reads_alerts_of_every_trader(tmp_path):
    """ОДИН датчик на команду, а не по одному на трейдера.

    Три процесса означали бы три стоп-крана, каждый со своим представлением о
    позициях: при пробое стены они независимо бросились бы закрывать одно и то
    же. Пульс защиты тоже один — модель обязана видеть ЕДИНОЕ состояние
    защищённости, а не три разных.
    """
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _team_alerts(tmp_path, "trend",
                 {"id": "t-up", "type": "price_above", "symbol": "XAUUSD",
                  "level": 2400.0, "once": True})
    _team_alerts(tmp_path, "fade",
                 {"id": "f-dn", "type": "price_below", "symbol": "XAUUSD",
                  "level": 2500.0, "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    fired = {e["alert_id"] for e in _lines(out)}
    assert fired == {"t-up", "f-dn"}, "оба трейдера обязаны быть услышаны"


def test_event_carries_the_trader_it_belongs_to(tmp_path):
    """Без пометки авторства событие бесполезно: непонятно, кого будить и в
    чей журнал писать решение."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _team_alerts(tmp_path, "fade",
                 {"id": "f-dn", "type": "price_below", "symbol": "XAUUSD",
                  "level": 2500.0, "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    events = _lines(out)
    assert events and events[0].get("trader") == "fade", events


def test_same_alert_id_in_two_traders_does_not_collide(tmp_path):
    """Трейдеры пишут планы независимо и неизбежно назовут условия одинаково
    («h1-trigger»). Состояние разоружения одного не имеет права гасить другого.
    """
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    for who, level in (("trend", 2400.0), ("fade", 2500.0)):
        _team_alerts(tmp_path, who,
                     {"id": "h1-trigger", "type": "price_above" if who == "trend"
                      else "price_below", "symbol": "XAUUSD", "level": level,
                      "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    owners = sorted(e["trader"] for e in _lines(out))
    assert owners == ["fade", "trend"], "одноимённые условия не должны схлопываться"


def test_solo_mode_still_reads_the_root_alerts(tmp_path):
    """Одиночный режим не сломан командой: каталога traders/ нет, файл в корне."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "solo", "type": "price_above", "symbol": "XAUUSD",
                             "level": 2400.0, "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    events = _lines(out)
    assert [e["alert_id"] for e in events] == ["solo"]
    assert events[0].get("trader") is None


def _alloc(tmp_path, **quotas):
    (tmp_path / "allocation.json").write_text(json.dumps({
        "server_day": "2026-07-27",
        "traders": {name: {"instruments": ["XAUUSD"], "risk_share": 0.3,
                           "active": True, "events_quota": q}
                    for name, q in quotas.items()}}, ensure_ascii=False),
        encoding="utf-8")


def test_trader_cannot_eat_the_whole_team_event_budget(tmp_path):
    """Ф5. Дневной лимит событий — свойство подписки, один на команду. Без
    деления разговорчивый трейдер выест его целиком, и остальные оглохнут до
    конца дня, ничего об этом не узнав."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _alloc(tmp_path, trend=1, fade=5)
    for who, level in (("trend", 2400.0), ("fade", 2400.0)):
        _team_alerts(tmp_path, who,
                     {"id": f"{who}-1", "type": "price_above", "symbol": "XAUUSD",
                      "level": level, "rearm_after_minutes": 1},
                     {"id": f"{who}-2", "type": "price_above", "symbol": "XAUUSD",
                      "level": level, "rearm_after_minutes": 1})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    delivered = [e for e in _lines(out) if e.get("event") == "alert"]
    by_trader = {}
    for e in delivered:
        by_trader[e["trader"]] = by_trader.get(e["trader"], 0) + 1
    assert by_trader.get("trend", 0) <= 1, "квота 1 обязана ограничить trend"
    assert by_trader.get("fade", 0) >= 1, "fade не должен пострадать от соседа"


def test_critical_events_bypass_the_personal_quota(tmp_path):
    """Квота исчерпана, а цена подошла к стопу — молчать нельзя. Инвалидация
    и стоп-кран это безопасность, а не разговорчивость."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _alloc(tmp_path, trend=0)
    _team_alerts(tmp_path, "trend",
                 {"id": "t-critical", "type": "price_above", "symbol": "XAUUSD",
                  "level": 2400.0, "once": True, "priority": "critical"})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)

    w.tick(NIGHT)
    ids = [e["alert_id"] for e in _lines(out)]
    assert "t-critical" in ids, "критическое обязано пройти сквозь исчерпанную квоту"


def test_trader_events_land_in_his_own_journal(tmp_path):
    """РЕГРЕСС 2026-08-01, найден первой обкаткой команды.

    События трейдера писались в ОБЩИЙ alert_events.jsonl, и поле trader в
    записи оставалось пустым. Следствие: review.py --trader <имя> показывал бы
    НОЛЬ пробуждений, выглядя при этом исправным, и метрика полезности алертов
    у каждого трейдера была бы ложью.

    Это буквально тот же дефект, что чинился 27.07 («метрика пробуждений всегда
    показывала ноль»), вернувшийся в командной форме: тогда события искали не в
    том файле, теперь их не в тот файл кладут.
    """
    from trader_lib.workspace import trader_dir

    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _team_alerts(tmp_path, "trend",
                 {"id": "t-up", "type": "price_above", "symbol": "XAUUSD",
                  "level": 2000.0, "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())
    w.tick(NIGHT)

    personal = trader_dir(cfg, "trend") / "alert_events.jsonl"
    assert personal.exists(), "события трейдера обязаны лечь в ЕГО журнал"
    rows = [json.loads(x) for x in personal.read_text(encoding="utf-8").splitlines()]
    assert rows and rows[-1]["alert_id"] == "t-up"
    assert rows[-1]["trader"] == "trend", "запись обязана нести авторство"


def test_solo_events_still_go_to_the_shared_journal(tmp_path):
    """Одиночный режим не тронут: трейдера нет — файл в корне, как всю неделю."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "solo", "type": "price_above", "symbol": "XAUUSD",
                             "level": 2000.0, "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())
    w.tick(NIGHT)

    rows = [json.loads(x) for x in
            (tmp_path / "alert_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["alert_id"] == "solo" and rows[-1]["trader"] is None


def test_symbol_ctx_always_carries_tick_freshness(tmp_path):
    """СТРАЖ ДЛЯ ПОСЛАБЛЕНИЯ В _tradeable_price.

    Ценовые алерты не срабатывают на протухшем тике, но отсутствие ключа
    tick_stale целиком трактуется как «свежесть не моделируется» и пропускает
    — иначе пришлось бы требовать это поле от каждой тестовой фикстуры,
    которая проверяет совсем другое.

    Послабление безопасно ровно до тех пор, пока БОЕВОЙ контекст выставляет
    ключ всегда. Перестанет — и защита от замёрзшей цены отключится молча,
    а выглядеть всё будет исправным. Тест сторожит именно это.
    """
    cfg = _cfg(tmp_path, **PERMISSIVE)
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT), account={"balance": 10000.0, "equity": 10000.0})
    ctx = aw.build_symbol_ctx(market, cfg, "XAUUSD", now=NIGHT,
                              timeframes=aw.DEFAULT_TIMEFRAMES, median_points=None)
    assert "tick_stale" in ctx, "боевой контекст обязан нести свежесть тика"


def test_code_fingerprint_covers_the_library_not_only_the_script():
    """РЕГРЕСС 2026-08-01, найден прогоном команды.

    Отпечаток загруженного кода снимался ТОЛЬКО с alert_watch.py. Но торговая
    логика живёт в trader_lib: фикс «ценовые алерты не срабатывают на протухшем
    тике» лёг в trader_lib/alerts.py, и работающий датчик продолжал стрелять по
    замёрзшей цене — при том что brief честно доложил бы «код свежий».

    В тот раз изъян не сработал только потому, что оба файла менялись вместе.
    Правка одной библиотеки была бы невидима полностью.

    Отпечаток обязан покрывать ВЕСЬ загруженный код, а не точку входа.
    """
    import os
    from pathlib import Path

    root = Path(aw.__file__).resolve().parents[1]
    lib_newest = max(os.path.getmtime(p) for p in (root / "trader_lib").glob("*.py"))
    assert aw.CODE_MTIME >= lib_newest - 1, (
        "отпечаток кода старше файлов trader_lib — правки библиотеки не видны "
        "проверке свежести")


def test_spread_median_reads_the_shape_that_spread_gate_writes():
    """РЕГРЕСС 2026-08-01, найден трейдером-субагентом на прогоне команды.

    Ридер искал символ в КОРНЕ spread_median.json, а trader_lib/spread_gate.py
    кладёт его под ключ "medians". Результат: spread_median_points всегда None,
    и spread_anomaly уходил в skipped ПО ЛЮБОМУ символу — то есть тип условия
    не мог сработать вовсе с момента написания.

    Обе стороны были покрыты тестами. Каждая — своей формой данных, и фикстуры
    подтверждали ровно то, во что верил автор своей половины. Проверять надо
    стык, а не половины: сюда подаётся файл, который РЕАЛЬНО пишет spread_gate.
    """
    import datetime as dt
    import tempfile
    from pathlib import Path

    from trader_lib.mt5_client import FakeMarket
    from trader_lib.spread_gate import load_medians, update_medians

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "spread_median.json"
        cfg = _cfg(Path(d))
        update_medians(FakeMarket(bars=_bars(spread=17)), cfg, path,
                       now=dt.datetime(2026, 8, 1, tzinfo=UTC))
        doc = load_medians(path)

    assert aw.spread_median_points(doc, "XAUUSD") == 17.0, (
        "ридер обязан понимать форму, которую пишет spread_gate")


def test_spread_median_still_reads_the_legacy_flat_shape():
    """Плоская форма остаётся понятной: файл мог быть собран старой версией."""
    assert aw.spread_median_points({"XAUUSD": 9.0}, "XAUUSD") == 9.0
    assert aw.spread_median_points({"XAUUSD": {"median_points": 9.0}},
                                   "XAUUSD") == 9.0
    assert aw.spread_median_points({"medians": {}}, "XAUUSD") is None


def test_root_alerts_are_not_silently_ignored_when_team_exists(tmp_path):
    """РЕГРЕСС 2026-08-01, найден подготовкой к понедельнику.

    _alert_sources переключается на командные источники, как только появился
    хоть один traders/<имя>/alerts.json, и корневой файл перестаёт читаться
    МОЛЧА. Будильник «начало недели», взведённый в корне до появления команды,
    не сработал бы вовсе — и узнать об этом было бы неоткуда: датчик бодро
    рапортует о 13 источниках, среди которых его просто нет.

    Корень остаётся законным источником: в нём живут условия, не принадлежащие
    ни одному трейдеру (пробуждение директора, начало сессии). Команда его
    дополняет, а не отменяет.
    """
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "root-wake", "type": "price_above",
                             "symbol": "XAUUSD", "level": 2000.0, "once": True})
    _team_alerts(tmp_path, "trend",
                 {"id": "t-up", "type": "price_above", "symbol": "XAUUSD",
                  "level": 2000.0, "once": True})
    market = FakeMarket(bars=_bars(last_bar_utc=NIGHT),
                        account={"balance": 10000.0, "equity": 10000.0})
    out = io.StringIO()
    w = _watch(tmp_path, cfg, market, out=out)
    w.tick(NIGHT)

    fired = {e["alert_id"] for e in _lines(out)}
    assert "t-up" in fired, "командный источник обязан читаться"
    assert "root-wake" in fired, "корневой файл не должен исчезать из-за команды"


# ======== бюджет и тишина считают события ВСЕЙ команды (регресс 2026-08-03) ====
# Читался только корневой alert_events.jsonl, а события трейдеров пишутся в
# traders/<имя>/alert_events.jsonl. Отсюда два отказа сразу:
#   1) правило тишины доложило «модель молчит 305 минут» при шести доставленных
#      событиях за день и трёх непрерывно работавших трейдерах;
#   2) событийный бюджет недосчитывал события трейдеров — защита от перерасхода
#      слабела ровно по мере того, как команда работала активнее.

def _event_row(ts, *, priority="normal", trader=None):
    row = {"type": "alert_event", "alert_id": "x", "priority": priority,
           "fired_utc": ts.isoformat(), "delivered": True}
    if trader:
        row["trader"] = trader
    return row


def _write_events(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def test_тишина_учитывает_события_трейдеров(tmp_path):
    """Событие трейдера — это работа команды, а не тишина."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "x", "type": "price_above", "symbol": "XAUUSD",
                             "level": 99999.0})
    давно = NOW - dt.timedelta(hours=6)
    только_что = NOW - dt.timedelta(minutes=5)
    _write_events(tmp_path / "alert_events.jsonl", [_event_row(давно)])
    _write_events(tmp_path / "traders" / "fade" / "alert_events.jsonl",
                  [_event_row(только_что, trader="fade")])

    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())
    # проверяется именно ВОССТАНОВЛЕНИЕ, а не состояние после тика: тик сам может
    # выстрелить правилом тишины и переписать метку на «сейчас» — тогда сломанная
    # и починенная версии стали бы неразличимы
    w._restore_budget_state(NOW)
    assert w._last_event_utc() == только_что, \
        "отсчёт тишины обязан идти от последнего события ЛЮБОГО трейдера"


def test_бюджет_учитывает_события_трейдеров(tmp_path):
    """Иначе защита от перерасхода слабеет тем сильнее, чем активнее команда."""
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "x", "type": "price_above", "symbol": "XAUUSD",
                             "level": 99999.0})
    сегодня = NOW - dt.timedelta(minutes=30)
    _write_events(tmp_path / "alert_events.jsonl", [_event_row(сегодня)])
    for имя in ("trend", "fade", "range"):
        _write_events(tmp_path / "traders" / имя / "alert_events.jsonl",
                      [_event_row(сегодня, trader=имя), _event_row(сегодня, trader=имя)])

    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())
    w._restore_budget_state(NOW)
    assert w._events_today == 7, f"учтено {w._events_today} из 7 событий команды"


def test_одиночный_режим_без_папки_traders_не_сломан(tmp_path):
    cfg = _cfg(tmp_path, **PERMISSIVE)
    _baselines(tmp_path)
    _write_alerts(tmp_path, {"id": "x", "type": "price_above", "symbol": "XAUUSD",
                             "level": 99999.0})
    _write_events(tmp_path / "alert_events.jsonl",
                  [_event_row(NOW - dt.timedelta(minutes=10))])
    market = FakeMarket(bars=_bars(), account={"balance": 10000.0, "equity": 10000.0})
    w = _watch(tmp_path, cfg, market, out=io.StringIO())
    w._restore_budget_state(NOW)
    assert w._events_today == 1
