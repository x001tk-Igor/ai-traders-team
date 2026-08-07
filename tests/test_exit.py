"""Исполнение решений о выходе (задача 4.3). Всё офлайн.

Главное здесь — АТОМАРНОСТЬ СОСТАВНОГО ДЕЙСТВИЯ. «Закрыть половину и перенести
стоп в безубыток» — это два приказа брокеру, и второй может не пройти. Тихо
вернуть ok в такой ситуации значит оставить модель в уверенности, что позиция
защищена по безубытку, когда она защищена по старому стопу. Поэтому частичное
исполнение возвращает явную ошибку с обоими результатами.
"""
import dataclasses
import datetime as dt
import json

import pytest

from scripts.exit import exit_position
from trader_lib.config import load_config
from trader_lib.journal import append_decision, read_records
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)

SI = {"point": 0.01, "digits": 2, "spread": 20, "trade_contract_size": 100,
      "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
      "filling_mode": 1}


def _cfg(tmp_path):
    cfg = load_config("config/trader.config.json")
    return dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})


class Market(FakeMarket):
    def __init__(self, *, positions=None, close_retcode=10009, sltp_retcode=10009,
                 bid=2410.0, ask=2410.2):
        super().__init__(positions=list(positions or []))
        self.sent = []
        self._close_retcode = close_retcode
        self._sltp_retcode = sltp_retcode
        self._bid, self._ask = bid, ask

    def symbol_info(self, symbol):
        return dict(SI)

    def tick(self, symbol):
        return {"bid": self._bid, "ask": self._ask}

    def positions(self):
        return [dict(p) for p in self._positions]

    def order_send(self, req):
        self.sent.append(dict(req))
        if req.get("action") == "TRADE_ACTION_SLTP":
            if self._sltp_retcode != 10009:
                return {"retcode": self._sltp_retcode, "comment": "scripted"}
            for p in self._positions:
                if p["ticket"] == req["position"]:
                    p["sl"] = req["sl"]
            return {"retcode": 10009}
        if self._close_retcode != 10009:
            return {"retcode": self._close_retcode, "comment": "scripted"}
        for p in list(self._positions):
            if p["ticket"] == req["position"]:
                left = round(p["volume"] - req["volume"], 8)
                if left <= 0:
                    self._positions.remove(p)
                else:
                    p["volume"] = left
        return {"retcode": 10009, "price": self._bid}


def _pos(ticket=7001, *, volume=0.2, sl=2395.0, price_open=2400.0, ptype=0,
         price_current=2410.0, profit=200.0):
    return {"ticket": ticket, "symbol": "XAUUSD", "type": ptype, "volume": volume,
            "price_open": price_open, "sl": sl, "tp": 0.0,
            "price_current": price_current, "profit": profit, "magic": 0}


def _journal_with_decision(tmp_path, ticket=7001, **over):
    rec = {"trade_id": str(ticket), "symbol": "XAUUSD", "side": "buy",
           "regime": "тренд вверх", "tactic": "ema_pullback",
           "setup_type": "ema_pullback", "setup_status": "подтверждён",
           "thesis": "откат к EMA20", "confidence": 0.6,
           "technical_trigger": "закрытие M5 выше EMA20", "entry": 2400.0,
           "sl": 2395.0, "tp_plan": 2412.0, "risk_usd": 100.0, "rr": 2.4,
           "costs_R": 0.05, "breakeven_p": 0.31, "p_win_journal": None,
           "news_check": "чисто", "spread_at_entry": 20.0,
           "correlation_check": "нет", "daily_risk_remaining_usd": 250.0,
           "planned": True, "plan_hypothesis_id": "h-1", "gate_verdict": "OK",
           "session_phase": "NY", "model_id": "claude-opus-5",
           "model_profile": "strong"}
    rec.update(over)
    append_decision(tmp_path / "journal.jsonl", rec)
    return rec


def _alerts(tmp_path, ticket=7001):
    (tmp_path / "alerts.json").write_text(json.dumps({
        "version": 1, "written_by": "claude-opus-5", "written_utc": NOW.isoformat(),
        "expires_utc": None,
        "alerts": [
            {"id": f"pos-{ticket}-1r", "type": "position_1r", "ticket": ticket,
             "symbol": "XAUUSD"},
            {"id": f"pos-{ticket}-stall", "type": "position_stall", "ticket": ticket,
             "symbol": "XAUUSD"},
            {"id": "level-watch", "type": "price_above", "symbol": "EURUSD",
             "level": 1.1},
        ]}), encoding="utf-8")


def _run(tmp_path, market, **kw):
    return exit_position(market, _cfg(tmp_path),
                         journal_path=tmp_path / "journal.jsonl",
                         alerts_path=tmp_path / "alerts.json", now=NOW, **kw)


def _records(tmp_path):
    return read_records(tmp_path / "journal.jsonl")


# --------------------------------------------------------------------------
# закрытие
# --------------------------------------------------------------------------

def test_close_writes_outcome(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos()])
    r = _run(tmp_path, m, ticket=7001, action="close", reason="invalidation")
    assert r["ok"] is True and m.positions() == []
    out = [x for x in _records(tmp_path) if x["type"] == "outcome"]
    assert len(out) == 1
    assert out[0]["trade_id"] == "7001" and out[0]["exit_reason"] == "invalidation"
    assert out[0]["exit"] == 2410.0
    # R = (2410 − 2400) / (2400 − 2395) = 2.0
    assert out[0]["R"] == pytest.approx(2.0)


def test_reason_recorded_in_journal(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos()])
    _run(tmp_path, m, ticket=7001, action="close", reason="тезис не отработал за 90 минут")
    out = [x for x in _records(tmp_path) if x["type"] == "outcome"][0]
    assert out["exit_reason"] == "тезис не отработал за 90 минут"


def test_close_requires_reason(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos()])
    r = _run(tmp_path, m, ticket=7001, action="close", reason="")
    assert r["ok"] is False and r["error"] == "reason_required"
    assert m.sent == [], "выход без причины не пишется и не исполняется"


def test_close_of_unknown_position(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[])
    r = _run(tmp_path, m, ticket=7001, action="close", reason="invalidation")
    assert r["ok"] is False and r["error"] == "position_not_found"
    assert [x for x in _records(tmp_path) if x["type"] == "outcome"] == [], \
        "выдумывать исход по несуществующей позиции нельзя — это работа reconcile"


def test_close_without_decision_record_is_refused(tmp_path):
    """Чужая позиция: R считать не от чего, и трогать её не наше дело."""
    m = Market(positions=[_pos(ticket=9999)])
    r = _run(tmp_path, m, ticket=9999, action="close", reason="invalidation")
    assert r["ok"] is False and r["error"] == "not_our_position"
    assert m.sent == []


def test_broker_refusal_on_close_is_visible(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos()], close_retcode=10018)
    r = _run(tmp_path, m, ticket=7001, action="close", reason="invalidation")
    assert r["ok"] is False and r["error"] == "close_rejected"
    assert [x for x in _records(tmp_path) if x["type"] == "outcome"] == [], \
        "исход не пишется, пока позиция не закрыта"


# --------------------------------------------------------------------------
# частичка + перенос стопа: составное действие
# --------------------------------------------------------------------------

def test_partial_then_sl_move_atomic(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos(volume=0.2)])
    r = _run(tmp_path, m, ticket=7001, action="partial", fraction=0.5,
             new_sl=2400.0, reason="фиксация половины на 2R")
    assert r["ok"] is True
    assert r["closed_lots"] == pytest.approx(0.1)
    assert m.positions()[0]["volume"] == pytest.approx(0.1)
    assert m.positions()[0]["sl"] == 2400.0


def test_partial_done_but_sl_failed_is_explicit(tmp_path):
    """Половина зафиксирована, стоп остался старым. Модель обязана узнать
    именно это, а не «ошибку выхода»: позиция жива и защищена не там, где
    модель думает."""
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos(volume=0.2)], sltp_retcode=10016)
    r = _run(tmp_path, m, ticket=7001, action="partial", fraction=0.5,
             new_sl=2400.0, reason="фиксация половины")
    assert r["ok"] is False and r["error"] == "partial_done_sl_failed"
    assert r["closed_lots"] == pytest.approx(0.1)
    assert m.positions()[0]["sl"] == 2395.0, "стоп остался прежним"
    assert "2395" in r["message"] and "2400" in r["message"]


def test_partial_writes_trade_event_not_outcome(tmp_path):
    """Позиция ещё жива: исход писать нельзя, иначе сделка будет выглядеть
    закрытой и выпадет из сверки."""
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos(volume=0.2)])
    _run(tmp_path, m, ticket=7001, action="partial", fraction=0.5, reason="фиксация")
    types = [x["type"] for x in _records(tmp_path)]
    assert "outcome" not in types and "trade_event" in types
    ev = [x for x in _records(tmp_path) if x["type"] == "trade_event"][0]
    assert ev["trade_id"] == "7001" and ev["action"] == "partial"
    assert ev["reason"] == "фиксация" and ev["closed_lots"] == pytest.approx(0.1)


def test_partial_impossible_reports_and_writes_nothing(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos(volume=0.01)])
    r = _run(tmp_path, m, ticket=7001, action="partial", fraction=0.5, reason="фиксация")
    assert r["ok"] is False and r["error"] == "partial_not_possible"
    assert [x for x in _records(tmp_path) if x["type"] == "trade_event"] == []


# --------------------------------------------------------------------------
# перенос стопа
# --------------------------------------------------------------------------

def test_move_sl_widening_rejected(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos(sl=2395.0)])
    r = _run(tmp_path, m, ticket=7001, action="move-sl", new_sl=2390.0,
             reason="дать сделке воздуха")
    assert r["ok"] is False and r["error"] == "sl_widening_forbidden"
    assert m.positions()[0]["sl"] == 2395.0


def test_move_sl_to_breakeven(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos(sl=2395.0)])
    r = _run(tmp_path, m, ticket=7001, action="move-sl", new_sl=2400.0,
             reason="безубыток после 2R")
    assert r["ok"] is True and m.positions()[0]["sl"] == 2400.0
    ev = [x for x in _records(tmp_path) if x["type"] == "trade_event"][0]
    assert ev["action"] == "move-sl" and ev["new_sl"] == 2400.0


# --------------------------------------------------------------------------
# алерты после выхода
# --------------------------------------------------------------------------

def test_alerts_updated_after_exit(tmp_path):
    """Алерты ведения закрытой позиции обязаны сниматься: иначе датчик будит
    модель по позиции, которой больше нет, и жжёт событийный бюджет."""
    _journal_with_decision(tmp_path)
    _alerts(tmp_path)
    m = Market(positions=[_pos()])
    _run(tmp_path, m, ticket=7001, action="close", reason="invalidation")
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    ids = [a["id"] for a in doc["alerts"]]
    assert ids == ["level-watch"], f"остались алерты закрытой позиции: {ids}"


def test_partial_keeps_position_alerts(tmp_path):
    """Частичка — позиция жива, наблюдение снимать нельзя."""
    _journal_with_decision(tmp_path)
    _alerts(tmp_path)
    m = Market(positions=[_pos(volume=0.2)])
    _run(tmp_path, m, ticket=7001, action="partial", fraction=0.5, reason="фиксация")
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert len([a for a in doc["alerts"] if a.get("ticket") == 7001]) == 2


def test_unknown_action_is_refused(tmp_path):
    _journal_with_decision(tmp_path)
    m = Market(positions=[_pos()])
    r = _run(tmp_path, m, ticket=7001, action="усилить", reason="почему бы нет")
    assert r["ok"] is False and r["error"] == "unknown_action"
    assert m.sent == []


# --------------------------------------------------------------------------
# уровень инвалидации следует за реальным стопом
# --------------------------------------------------------------------------

def _alerts_with_invalidation(tmp_path, ticket=7001, level=2395.0):
    (tmp_path / "alerts.json").write_text(json.dumps({
        "version": 1, "written_by": "claude-opus-5", "written_utc": NOW.isoformat(),
        "expires_utc": None,
        "alerts": [
            {"id": f"pos-{ticket}-invalidation", "type": "price_below",
             "ticket": ticket, "symbol": "XAUUSD", "level": level, "once": True},
            {"id": "level-watch", "type": "price_above", "symbol": "EURUSD",
             "level": 1.1},
        ]}), encoding="utf-8")


def test_move_sl_syncs_invalidation_alert(tmp_path):
    """РЕГРЕСС 2026-07-29, живая позиция: SL перенесён на 1R в безубыток, но
    алерт инвалидации в alerts.json остался на исходном стопе — уровень
    сторожил уже недействующую границу риска, пока настоящий SL брокера
    молча защищал позицию в другом месте."""
    _journal_with_decision(tmp_path)
    _alerts_with_invalidation(tmp_path, level=2395.0)
    market = Market(positions=[_pos(sl=2395.0)])
    _run(tmp_path, market, ticket=7001, action="move-sl", new_sl=2400.0,
        reason="безубыток на 1R")
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    inv = next(a for a in doc["alerts"] if a["id"] == "pos-7001-invalidation")
    assert inv["level"] == 2400.0


def test_partial_with_sl_syncs_invalidation_alert(tmp_path):
    _journal_with_decision(tmp_path)
    _alerts_with_invalidation(tmp_path, level=2395.0)
    market = Market(positions=[_pos(sl=2395.0)])
    _run(tmp_path, market, ticket=7001, action="partial", fraction=0.5,
        new_sl=2400.0, reason="1R частичка + безубыток")
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    inv = next(a for a in doc["alerts"] if a["id"] == "pos-7001-invalidation")
    assert inv["level"] == 2400.0


def test_move_sl_without_invalidation_alert_does_not_crash(tmp_path):
    """Если алерта инвалидации нет в файле (снят вручную, старая позиция) —
    перенос стопа не должен падать."""
    _journal_with_decision(tmp_path)
    (tmp_path / "alerts.json").write_text(json.dumps({
        "version": 1, "written_by": "claude-opus-5", "written_utc": NOW.isoformat(),
        "expires_utc": None, "alerts": []}), encoding="utf-8")
    market = Market(positions=[_pos(sl=2395.0)])
    res = _run(tmp_path, market, ticket=7001, action="move-sl", new_sl=2400.0,
              reason="безубыток")
    assert res["ok"] is True
