"""Атомарный вход (задача 4.2). Всё офлайн.

ТРИ УТВЕРЖДЕНИЯ, РАДИ КОТОРЫХ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ.

1. Ордер не уходит без следа в журнале. Проверяется не «есть ли запись
   после», а порядок: намерение записано ДО отправки (test_journal_written_
   before_order следит за последовательностью вызовов).

2. Своя сделка не выглядит чужой. trade_id обязан быть тикетом брокера,
   иначе find_orphans сочтёт нашу позицию чужой и уведёт систему в HALT_NEW
   при полностью нашей сделке (test_own_trade_is_not_detected_as_orphan).
   Тикет известен только ПОСЛЕ филла — отсюда двухфазная запись.

3. Сделки не выглядят машинными. Лоты считаются из риска и режутся вниз к
   шагу, поэтому получаются некруглыми сами: test_lots_are_not_round следит,
   чтобы никто не «улучшил» это округлением до красивых значений. Переделать
   после накопления истории будет нельзя — история уже будет машинной.
"""
import dataclasses
import datetime as dt
import json

import pytest

from scripts.enter import enter
from trader_lib.config import load_config
from trader_lib.journal import decision_from_intent, read_records
from trader_lib.mt5_client import FakeMarket

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 27, 13, 0, tzinfo=UTC)

SI = {"point": 0.01, "digits": 2, "spread": 20, "trade_contract_size": 100,
      "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
      "filling_mode": 1}


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _draft(**over):
    """То, что может сказать только модель. Всё механическое (риск, издержки,
    p_win, вердикт гейта, фаза) enter.py считает сам."""
    base = {"symbol": "XAUUSD", "side": "buy", "entry": 2400.0, "sl": 2395.0,
            "tp_plan": 2412.0, "rr": 2.4, "tactic": "ema_pullback",
            "setup_type": "ema_pullback", "setup_status": "подтверждён",
            "regime": "тренд вверх", "thesis": "откат к EMA20 в тренде",
            "confidence": 0.6, "technical_trigger": "закрытие M5 выше EMA20",
            "planned": True, "plan_hypothesis_id": "h-1"}
    base.update(over)
    return base


class Market(FakeMarket):
    def __init__(self, *, retcode=10009, sl_after_fill=None, **kw):
        super().__init__(**kw)
        self.sent = []
        self._retcode = retcode
        self._sl_after_fill = sl_after_fill
        self._ticket = 7001

    def symbol_info(self, symbol):
        return dict(SI)

    def tick(self, symbol):
        return {"bid": 2399.9, "ask": 2400.1}

    def positions(self):
        return [dict(p) for p in self._positions]

    def order_send(self, req):
        self.sent.append(dict(req))
        if req.get("action") == "TRADE_ACTION_SLTP":
            for p in self._positions:
                if p["ticket"] == req["position"]:
                    p["sl"] = req["sl"]
            return {"retcode": 10009}
        if self._retcode != 10009:
            return {"retcode": self._retcode, "comment": "scripted"}
        ticket = self._ticket
        self._ticket += 1
        sl = self._sl_after_fill if self._sl_after_fill is not None else req["sl"]
        self._positions.append({"ticket": ticket, "symbol": req["symbol"], "type": 0,
                                "volume": req["volume"], "price_open": req["price"],
                                "sl": sl, "tp": req.get("tp", 0.0),
                                "price_current": req["price"], "profit": 0.0, "magic": 0})
        return {"retcode": 10009, "order": ticket, "price": req["price"],
                "volume": req["volume"]}


def _gate(allow=True, *, max_risk_usd=100.0, reasons=(), require_setup_status="any"):
    def gate_fn(**kwargs):
        return {"allow": allow, "max_risk_usd": max_risk_usd,
                "reasons": list(reasons) or ([] if allow else ["гейт запретил"]),
                "require_setup_status": require_setup_status,
                "verdict": "OK" if allow else "HALT_NEW",
                "checks": {}, "daily_risk_remaining_usd": 250.0,
                "session_phase": "NY", "news_check": "чисто",
                "spread_at_entry": 20.0, "correlation_check": "нет пересечений"}
    return gate_fn


def _run(tmp_path, cfg=None, market=None, draft=None, **kw):
    cfg = cfg or _cfg(tmp_path)
    return enter(market or Market(), cfg, draft or _draft(),
                 journal_path=tmp_path / "journal.jsonl",
                 alerts_path=tmp_path / "alerts.json",
                 now=NOW, gate_fn=kw.pop("gate_fn", _gate()), **kw)


def _records(tmp_path):
    return read_records(tmp_path / "journal.jsonl")


# --------------------------------------------------------------------------
# гейт и предпроверки: ордер не уходит
# --------------------------------------------------------------------------

def test_denied_gate_does_not_send_order(tmp_path):
    m = Market()
    r = _run(tmp_path, market=m, gate_fn=_gate(False))
    assert r["ok"] is False and r["stage"] == "gate"
    assert m.sent == []
    types = [x["type"] for x in _records(tmp_path)]
    assert types == ["skip"], "отказ гейта — это осознанный пропуск, он попадает в память"


def test_skip_keeps_link_to_the_alert(tmp_path):
    """Отказ в ответ на сработавший алерт обязан ссылаться на него: иначе
    дневной разбор считает это пробуждение пустым и пометит полезное условие
    мусорным (scripts/review.py, метрика пробуждений)."""
    r = _run(tmp_path, gate_fn=_gate(False), draft=_draft(alert_id="trigger-7"))
    assert r["ok"] is False
    skip = [x for x in _records(tmp_path) if x["type"] == "skip"][0]
    assert skip["alert_id"] == "trigger-7"


def test_missing_entry_gate_module_fails_closed(tmp_path, monkeypatch):
    """Если предвходовой гейт недоступен как модуль, вход не работает вовсе.
    «Пока поторгуем без гейта» — это и есть второй путь мимо правил.

    Недоступность имитируется подменой в sys.modules: пока модуль
    существовал не всегда, тест проверял это сам по себе, а теперь обязан
    воспроизводить условие явно — иначе он молча проверял бы наличие файла."""
    import sys

    monkeypatch.setitem(sys.modules, "trader_lib.entry_gate", None)
    r = _run(tmp_path, gate_fn=None)
    assert r["ok"] is False and r["stage"] == "gate_unavailable"
    assert "5.4" in r["message"]


def test_default_gate_is_the_real_entry_gate(tmp_path):
    """Без явного gate_fn вход идёт через настоящий предвходовой гейт, а не
    через что-нибудь разрешающее. Мир здесь пустой (нет пульса датчика, баз,
    календаря) — настоящий гейт обязан запретить."""
    m = Market()
    r = _run(tmp_path, market=m, gate_fn=None)
    assert r["ok"] is False and r["stage"] == "gate"
    assert m.sent == []
    assert any("датчик" in x or "пульс" in x for x in r["gate"]["reasons"])


def test_incomplete_fields_block_before_send(tmp_path):
    m = Market()
    r = _run(tmp_path, market=m, draft=_draft(thesis=None))
    assert r["ok"] is False and r["stage"] == "journal_validation"
    assert m.sent == [], "неполная запись обязана останавливать ДО отправки"
    assert any("thesis" in p for p in r["problems"])


def test_costs_above_limit_deny(tmp_path):
    """Издержки выше max_costs_R: сделка съедает риск на входе."""
    m = Market()
    cfg = _cfg(tmp_path, risk={"max_costs_R": 0.001})
    r = _run(tmp_path, cfg=cfg, market=m)
    assert r["ok"] is False and r["stage"] == "quality"
    assert m.sent == [] and "costs_R" in r["message"]


def test_journal_p_win_below_breakeven_deny(tmp_path):
    """История говорит, что этот сетап выигрывает реже точки безубытка —
    входить в него нельзя, какой бы уверенной ни была модель."""
    j = tmp_path / "journal.jsonl"
    recs = []
    for i in range(25):
        recs.append({"type": "decision", "trade_id": str(i), "setup_type": "ema_pullback",
                     "symbol": "XAUUSD", "confidence": 0.5, "regime": "тренд вверх",
                     "model_id": "claude-opus-5", "session_phase": "NY", "planned": True})
        recs.append({"type": "outcome", "trade_id": str(i),
                     "R": 1.0 if i < 3 else -1.0, "exit_reason": "sl"})
    j.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in recs),
                 encoding="utf-8")
    m = Market()
    r = _run(tmp_path, market=m)
    assert r["ok"] is False and r["stage"] == "quality"
    assert "p_win" in r["message"] and m.sent == []


def test_zero_lots_does_not_send(tmp_path):
    """Бюджет риска не покрывает минимальный лот — сделки нет."""
    m = Market()
    r = _run(tmp_path, market=m, gate_fn=_gate(max_risk_usd=0.4))
    assert r["ok"] is False and r["stage"] == "size" and m.sent == []


# --------------------------------------------------------------------------
# профиль модели
# --------------------------------------------------------------------------

def test_weak_profile_rejects_unplanned(tmp_path):
    cfg = _cfg(tmp_path, model={"profile": "weak"})
    m = Market()
    r = _run(tmp_path, cfg=cfg, market=m,
             draft=_draft(planned=False, plan_hypothesis_id=None,
                          unplanned_reason="увидел импульс"))
    assert r["ok"] is False and r["stage"] == "profile"
    assert m.sent == []


def test_weak_profile_requires_confirmed_setup(tmp_path):
    cfg = _cfg(tmp_path, model={"profile": "weak"})
    r = _run(tmp_path, cfg=cfg, draft=_draft(setup_status="изучаю"))
    assert r["ok"] is False and r["stage"] == "profile"


def test_weak_profile_halves_risk(tmp_path):
    strong = _run(tmp_path, market=Market())
    tmp2 = tmp_path / "weak"
    tmp2.mkdir()
    weak = enter(Market(), _cfg(tmp2, model={"profile": "weak"}), _draft(),
                 journal_path=tmp2 / "journal.jsonl", alerts_path=tmp2 / "alerts.json",
                 now=NOW, gate_fn=_gate())
    assert weak["ok"] and strong["ok"]
    assert weak["risk_usd"] == pytest.approx(strong["risk_usd"] * 0.5)


def test_unrecognized_setup_status_shrinks_risk(tmp_path):
    """Опечатка модели в статусе сетапа не имеет права дать полный риск."""
    full = _run(tmp_path, market=Market())
    tmp2 = tmp_path / "typo"
    tmp2.mkdir()
    typo = enter(Market(), _cfg(tmp2), _draft(setup_status="паттвержден"),
                 journal_path=tmp2 / "journal.jsonl", alerts_path=tmp2 / "alerts.json",
                 now=NOW, gate_fn=_gate())
    assert typo["ok"] and typo["risk_usd"] < full["risk_usd"]


# --------------------------------------------------------------------------
# двухфазная запись
# --------------------------------------------------------------------------

def test_journal_written_before_order(tmp_path):
    """Порядок, а не факт: намерение обязано лечь на диск ДО order_send."""
    order = []

    class Traced(Market):
        def order_send(self, req):
            order.append("order")
            return super().order_send(req)

    m = Traced()
    m_journal = tmp_path / "journal.jsonl"

    import scripts.enter as mod
    real_intent = mod.append_intent

    def traced_intent(path, rec):
        order.append("intent")
        return real_intent(path, rec)

    mod.append_intent = traced_intent
    try:
        r = enter(m, _cfg(tmp_path), _draft(), journal_path=m_journal,
                  alerts_path=tmp_path / "alerts.json", now=NOW, gate_fn=_gate())
    finally:
        mod.append_intent = real_intent

    assert r["ok"] is True
    assert order.index("intent") < order.index("order")


def test_trade_id_is_broker_ticket(tmp_path):
    r = _run(tmp_path)
    dec = [x for x in _records(tmp_path) if x["type"] == "decision"][0]
    assert dec["trade_id"] == str(r["ticket"]) == "7001"
    assert dec["intent_id"], "решение обязано ссылаться на своё намерение"


def test_own_trade_is_not_detected_as_orphan(tmp_path):
    """Главный практический смысл двухфазной записи."""
    from scripts.close_watch import find_orphans

    m = Market()
    r = _run(tmp_path, market=m)
    orphans = find_orphans(tmp_path / "journal.jsonl", m.positions())
    assert r["ok"] and orphans == [], f"наша сделка опознана как чужая: {orphans}"


def test_crash_between_fill_and_decision_is_recoverable(tmp_path):
    """Процесс упал сразу после филла: в журнале есть намерение, у брокера —
    позиция. Решение достраивается из намерения и тикета, ничего не потеряно."""
    m = Market()

    class Boom(Exception):
        pass

    import scripts.enter as mod
    real = mod.append_decision

    def explode(path, rec):
        raise Boom("процесс упал между филлом и записью решения")

    mod.append_decision = explode
    try:
        with pytest.raises(Boom):
            enter(m, _cfg(tmp_path), _draft(), journal_path=tmp_path / "journal.jsonl",
                  alerts_path=tmp_path / "alerts.json", now=NOW, gate_fn=_gate())
    finally:
        mod.append_decision = real

    recs = _records(tmp_path)
    intents = [x for x in recs if x["type"] == "intent"]
    assert len(intents) == 1 and not [x for x in recs if x["type"] == "decision"]
    ticket = m.positions()[0]["ticket"]
    rebuilt = decision_from_intent(intents[0], ticket=ticket)
    from trader_lib.journal import validate_decision
    assert validate_decision(rebuilt) == [], "намерения недостаточно для восстановления"
    assert rebuilt["trade_id"] == str(ticket)


def test_failed_order_marked_aborted(tmp_path):
    """Ордер не прошёл: намерение остаётся, но помечается несостоявшимся —
    иначе оно вечно выглядит как незакрытая сделка."""
    m = Market(retcode=10018)
    r = _run(tmp_path, market=m)
    assert r["ok"] is False and r["stage"] == "order"
    recs = _records(tmp_path)
    assert [x["type"] for x in recs] == ["intent", "outcome"]
    out = recs[-1]
    assert out["exit_reason"] == "aborted" and out["R"] == 0
    assert out["trade_id"] == recs[0]["intent_id"], "исход привязан к намерению"


def test_dry_run_writes_nothing_and_sends_nothing(tmp_path):
    m = Market()
    r = _run(tmp_path, market=m, dry_run=True)
    assert r["ok"] is True and r["dry_run"] is True
    assert m.sent == [] and _records(tmp_path) == []
    assert r["risk_usd"] > 0 and r["lots"] > 0, "расчёт всё равно показан"


# --------------------------------------------------------------------------
# alerts.json после входа
# --------------------------------------------------------------------------

def test_alerts_updated_on_success(tmp_path):
    """Сработавший триггер снимается, взамен встают алерты ведения позиции."""
    (tmp_path / "alerts.json").write_text(json.dumps({
        "version": 1, "written_by": "claude-opus-5", "written_utc": NOW.isoformat(),
        "expires_utc": None,
        "alerts": [{"id": "trigger-1", "symbol": "XAUUSD", "type": "price_above",
                    "level": 2400.0}]}), encoding="utf-8")
    r = _run(tmp_path, draft=_draft(alert_id="trigger-1"))
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    ids = [a["id"] for a in doc["alerts"]]
    assert "trigger-1" not in ids, "сработавший триггер обязан быть снят"
    types = {a["type"] for a in doc["alerts"]}
    assert {"position_R_reaches", "position_time_elapsed"} <= types
    assert any(a.get("ticket") == r["ticket"] for a in doc["alerts"])


def test_position_alerts_match_watcher_contract(tmp_path):
    """Алерты ведения обязаны быть понятны датчику: выдуманный тип он молча
    пропускает как нераспознанный, и позиция остаётся без наблюдения — при
    этом в файле алерты есть, а будильник не звонит. Проверяются и типы, и
    обязательные поля каждого типа."""
    from trader_lib.alerts import ALERT_TYPE_FIELDS

    r = _run(tmp_path)
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert r["ok"] and doc["alerts"]
    for a in doc["alerts"]:
        assert a["type"] in ALERT_TYPE_FIELDS, a
        for field in ALERT_TYPE_FIELDS[a["type"]]:
            assert a.get(field) is not None, f"{a['type']}: не заполнено {field} — {a}"


def test_alerts_file_absent_is_not_an_error(tmp_path):
    r = _run(tmp_path)
    assert r["ok"] is True
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert doc["alerts"], "алерты ведения ставятся даже при отсутствии файла"


# --------------------------------------------------------------------------
# сделка не должна выглядеть машинной
# --------------------------------------------------------------------------

def test_lots_are_not_round(tmp_path):
    """Одинаковые круглые лоты — такой же след советника, как magic. Лот
    считается из риска и режется вниз к шагу, поэтому получается некруглым
    сам; тест следит, чтобы это никто не «улучшил» округлением."""
    lots = []
    for i, (risk, sl) in enumerate([(97.0, 2395.3), (63.0, 2396.7), (118.0, 2393.1)]):
        d = tmp_path / f"w{i}"
        d.mkdir()
        r = enter(Market(), _cfg(d), _draft(sl=sl), journal_path=d / "journal.jsonl",
                  alerts_path=d / "alerts.json", now=NOW,
                  gate_fn=_gate(max_risk_usd=risk))
        assert r["ok"], r
        lots.append(r["lots"])
    assert len(set(lots)) == len(lots), f"лоты повторяются: {lots}"
    assert not all(abs(x * 100 % 10) < 1e-9 for x in lots), f"лоты слишком круглые: {lots}"


def test_no_magic_or_comment_in_sent_order(tmp_path):
    m = Market()
    _run(tmp_path, market=m)
    deal = [s for s in m.sent if s.get("action") == "TRADE_ACTION_DEAL"][0]
    assert deal["magic"] == 0 and deal["comment"] == ""


# --------------------------------------------------------------------------
# план частичных целей → один tp брокера
# --------------------------------------------------------------------------

def test_broker_tp_takes_the_far_target_for_long():
    """Брокер понимает один tp и закрывает им ВЕСЬ объём. Отдать ему TP1
    значило бы закрыть позицию целиком там, где по плану выходит половина."""
    from scripts.enter import broker_tp
    plan = [{"level": 4096.6, "fraction": 0.5}, {"level": 4106.0, "fraction": 0.5}]
    assert broker_tp(plan, "buy") == 4106.0


def test_broker_tp_takes_the_far_target_for_short():
    """Для шорта дальняя цель — минимальная. Одинаковый max() отдал бы шорту
    ближайшую и закрыл бы его на половине пути."""
    from scripts.enter import broker_tp
    plan = [{"level": 4070.0, "fraction": 0.5}, {"level": 4055.0, "fraction": 0.5}]
    assert broker_tp(plan, "sell") == 4055.0


def test_broker_tp_passes_through_plain_number_and_none():
    from scripts.enter import broker_tp
    assert broker_tp(None, "buy") is None
    assert broker_tp(4106.0, "buy") == 4106.0
    assert broker_tp([], "buy") is None


def test_entry_with_partial_tp_plan_reaches_the_broker(tmp_path):
    """РЕГРЕСС 2026-07-27, живой вход: в place_order передавался сам список
    целей, и ордер падал на float(list). Прогон --dry-run этого не видел —
    в нём ордер не отправляется вовсе, поэтому тест обязан быть боевым."""
    market = Market()
    draft = _draft(tp_plan=[{"level": 2410.0, "fraction": 0.5},
                            {"level": 2420.0, "fraction": 0.5}])
    res = _run(tmp_path, market=market, draft=draft)
    assert res["ok"] is True, res
    sent = [s for s in market.sent if s.get("action") != "TRADE_ACTION_SLTP"]
    assert sent[-1]["tp"] == 2420.0


def test_position_alerts_are_one_shot(tmp_path):
    """РЕГРЕСС 2026-07-29, живая позиция: ни один из трёх алертов ведения не
    имел once=true. Диспетчер (trader_lib/alerts.py) разоружает условие
    ТОЛЬКО если once или rearm_after_minutes заданы — без этого «позиция
    прошла 1R» срабатывала заново на каждом тике, пока R>=1.0, и жгла
    дневной бюджет событий на первой же реальной сделке. Каждое условие —
    разовое решение, а не повторяющееся напоминание."""
    r = _run(tmp_path)
    doc = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    pos_alerts = [a for a in doc["alerts"] if a.get("ticket") == r["ticket"]]
    assert len(pos_alerts) == 3
    for a in pos_alerts:
        assert a.get("once") is True, f"{a['id']} может сработать повторно: {a}"


def test_broker_tp_accepts_plain_numbers_not_only_dicts():
    """РЕГРЕСС 2026-07-31, стоил ~1R на живой сделке 2287196528.

    broker_tp собирал уровни ТОЛЬКО из словарей (t["level"]). Модель написала
    в черновике естественное tp_plan=[4051.0, 4043.0] — список простых чисел,
    фильтр отсеял всё, функция вернула None, и брокер получил tp=0.0: у позиции
    не было цели ВООБЩЕ. Отказ тихий — ни исключения, ни предупреждения, а в
    журнале план целей записан как заданный.
    """
    from scripts.enter import broker_tp

    assert broker_tp([4051.0, 4043.0], "sell") == 4043.0
    assert broker_tp([4051.0, 4065.0], "buy") == 4065.0
    # смешанный список тоже не должен терять уровни
    assert broker_tp([{"level": 4051.0}, 4043.0], "sell") == 4043.0


def test_position_alerts_arm_near_tp_levels():
    """РЕГРЕСС 2026-07-31: ближние цели плана не превращались в будильники.

    Брокеру уходит только ДАЛЬНЯЯ цель (broker_tp), поэтому промежуточные
    уровни существуют лишь как замысел модели и требуют алерта. На сделке
    2287196528 цена прошла TP1=4051 насквозь (лоу 4050.11, MFE 0.94R), пока
    модель спала — и вернулась к безубытку. Событийная работа не должна
    требовать, чтобы модель смотрела на экран.
    """
    from scripts.enter import _position_alerts

    alerts = _position_alerts(
        ticket=777, symbol="XAUUSD", side="sell", entry=4058.3, sl=4067.0,
        now=NOW, model_id="test", tp_plan=[4051.0, 4043.0])
    tp_alerts = [a for a in alerts if "-tp" in a["id"]]

    # дальняя цель у брокера, будильник нужен только ближней
    assert len(tp_alerts) == 1
    a = tp_alerts[0]
    assert a["level"] == 4051.0
    assert a["type"] == "price_below"      # шорт идёт вниз к цели
    assert a["once"] is True
    assert a["ticket"] == 777


def test_position_alerts_without_tp_plan_stay_as_before():
    """Отсутствие плана целей — не ошибка: три базовых условия и ничего лишнего."""
    from scripts.enter import _position_alerts

    alerts = _position_alerts(ticket=778, symbol="XAUUSD", side="buy",
                              entry=4000.0, sl=3990.0, now=NOW, model_id="test")
    assert [a["id"] for a in alerts] == ["pos-778-1r", "pos-778-stall",
                                         "pos-778-invalidation"]


def test_draft_min_rr_blocks_entry_when_economics_decayed(tmp_path):
    """РЕГРЕСС 2026-07-31 (сделка 2288394009, -0.222R): порог R:R жил только в
    плане дня и в голове модели, но не в пути исполнения.

    Между расчётом входа и отправкой ордера цена ушла (спред нормализовался
    61 -> 20, цена 4046.88 -> 4048.93): риск вырос 6.88 -> 8.93, R:R упал
    1.62 -> 1.00. Скрипт пересчитал rr на живой цене и отправил ордер
    безусловно — при том что часом ранее модель ОТКАЗАЛАСЬ от входа при R:R
    0.97 как от не проходящего порог 1.5. Порог, который не может остановить
    ордер, — не правило, а украшение.

    Шов уважается: число выбирает модель (это тактика), код лишь держит её за
    её же слово (это исполнение).
    """
    market = Market()
    res = _run(tmp_path, market=market, draft=_draft(rr=1.0, min_rr=1.5))

    assert res["ok"] is False
    assert res["stage"] == "min_rr"
    assert "1.5" in res["message"]
    sent = [s for s in market.sent if s.get("action") != "TRADE_ACTION_SLTP"]
    assert sent == [], "ордер не должен уйти брокеру"


def test_draft_min_rr_allows_entry_when_threshold_met(tmp_path):
    """Порог выполнен — вход идёт как обычно, лишней придирчивости нет."""
    res = _run(tmp_path, draft=_draft(rr=1.62, min_rr=1.5))
    assert res["ok"] is True, res


def test_draft_without_min_rr_is_unconstrained(tmp_path):
    """Не объявила порог — код его не выдумывает: тактику выбирает модель."""
    res = _run(tmp_path, draft=_draft(rr=0.3))
    assert res["ok"] is True, res


# --------------------------------------------------------------------------
# КОМАНДА: вход обязан знать, ЧЕЙ он (Ф3-Ф5)
# --------------------------------------------------------------------------

def test_entry_passes_the_trader_to_the_gate(tmp_path):
    """СШИВКА, без которой вся Ф4 бесполезна.

    Мандаты, кластерный потолок и квоты проверяет гейт, но узнать, ЧЕЙ это
    вход, он может только от enter.py. Если trader не передан, check_entry
    видит None, трактует это как одиночный режим и пропускает вход по ЛЮБОМУ
    инструменту — то есть директорские мандаты существуют, но ни на что не
    влияют. Третий за неделю случай класса «написано, но не подключено»
    (update_medians, net_currency_exposure) — здесь ловим заранее.
    """
    seen = {}

    def spy_gate(**kw):
        seen.update(kw)
        return {"allow": True, "max_risk_usd": 100.0, "reasons": [],
                "require_setup_status": "any", "verdict": "OK", "checks": {},
                "session_phase": "LONDON", "news_check": "ok",
                "spread_at_entry": 20.0, "correlation_check": "нет",
                "daily_risk_remaining_usd": 1000.0, "blocked_by": None,
                "binding_term": "per_trade_cap"}

    res = _run(tmp_path, gate_fn=spy_gate, trader="fade")
    assert res["ok"] is True, res
    assert seen.get("trader") == "fade", "гейт обязан узнать автора входа"


def test_entry_without_trader_stays_solo(tmp_path):
    """Одиночный режим не сломан: trader не передан — гейт получает None."""
    seen = {}

    def spy_gate(**kw):
        seen.update(kw)
        return {"allow": True, "max_risk_usd": 100.0, "reasons": [],
                "require_setup_status": "any", "verdict": "OK", "checks": {},
                "session_phase": "LONDON", "news_check": "ok",
                "spread_at_entry": 20.0, "correlation_check": "нет",
                "daily_risk_remaining_usd": 1000.0, "blocked_by": None,
                "binding_term": "per_trade_cap"}

    _run(tmp_path, gate_fn=spy_gate)
    assert seen.get("trader") is None
