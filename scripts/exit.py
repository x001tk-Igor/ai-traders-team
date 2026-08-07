"""Исполнение решений о выходе (задача 4.3).

Решает модель — этот скрипт исполняет корректно и оставляет след:

    python exit.py --ticket 123456 --action partial --fraction 0.5 --new-sl 2417.0
    python exit.py --ticket 123456 --action close --reason invalidation
    python exit.py --ticket 123456 --action move-sl --new-sl 2415.5

АТОМАРНОСТЬ СОСТАВНОГО ДЕЙСТВИЯ. «Зафиксировать половину и перенести стоп в
безубыток» — два приказа брокеру, и второй может не пройти. Вернуть ok в этом
случае значит оставить модель в уверенности, что позиция защищена по
безубытку, когда она защищена по старому стопу. Поэтому такой исход — явная
ошибка partial_done_sl_failed, в сообщении оба уровня: что было и что не стало.
Откатить частичку нельзя (обратный вход — это новая сделка по новой цене), и
делать вид, что можно, — хуже, чем честно сказать.

ЧУЖИЕ ПОЗИЦИИ НЕ ТРОГАЕМ. Нет decision-записи — нет и понимания, от чего
считать R и каким был замысел. Это тот же вывод, что в risk_gate_cli (orphan →
HALT_NEW) и в стоп-кране датчика: чужое не наше дело.

ИСХОД ПИШЕТСЯ ТОЛЬКО ПРИ ФАКТИЧЕСКОМ ЗАКРЫТИИ. Частичка и перенос стопа —
trade_event: позиция жива, и запись outcome вывела бы её из сверки с брокером.
Исход по несуществующей позиции не выдумывается вовсе — дописывание исходов
закрывшихся сделок делает reconcile (scripts/close_watch.py) по истории
брокера, а не этот скрипт по догадке.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.alerts import load_alerts, write_alerts_atomic       # noqa: E402
from trader_lib.config import load_config, state_dir                 # noqa: E402
from trader_lib.excursion import measure                             # noqa: E402
from trader_lib.execute import close_partial, close_position, modify_sl  # noqa: E402
from trader_lib.model_session import effective as effective_model    # noqa: E402
from trader_lib.journal import (                                     # noqa: E402
    append_outcome,
    append_trade_event,
    read_records,
)
from trader_lib.workspace import resolve_trader, trader_state_dir    # noqa: E402

UTC = dt.timezone.utc
ACTIONS = ("close", "partial", "move-sl")


def _fail(error, message, **extra):
    out = {"ok": False, "error": error, "message": message}
    out.update(extra)
    return out


def _decision_for(records, ticket):
    """Наше решение по этому тикету. Ищем по trade_id == str(ticket) — то же
    тождество, на котором стоят reconcile и find_orphans."""
    for rec in reversed(records):
        if rec.get("type") == "decision" and str(rec.get("trade_id")) == str(ticket):
            return rec
    return None


def _position(market, ticket):
    for p in market.positions():
        if p.get("ticket") == ticket:
            return p
    return None


def _r_multiple(decision, exit_price):
    """R закрытой части: (выход − вход) / (вход − стоп), со знаком по стороне.

    Считается от ИСХОДНОГО стопа из решения, а не от текущего: R сделки — это
    результат относительно риска, который был принят на входе. Перенос стопа в
    безубыток уменьшает потенциальный убыток, но не переписывает задним числом
    масштаб, которым мерялась сделка.
    """
    entry, sl = decision.get("entry"), decision.get("sl")
    if entry is None or sl is None or exit_price is None:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    move = (exit_price - entry) if decision.get("side") == "buy" else (entry - exit_price)
    return round(move / risk, 3)


def _drop_position_alerts(alerts_path, ticket, *, now, model_id):
    """Снимает алерты ведения закрытой позиции: иначе датчик будит модель по
    позиции, которой больше нет, и жжёт событийный бюджет."""
    doc = load_alerts(alerts_path, now=now) or {}
    alerts = [a for a in (doc.get("alerts") or []) if a.get("ticket") != ticket]
    write_alerts_atomic(alerts_path, {
        "version": 1, "written_by": model_id, "written_utc": now.isoformat(),
        "expires_utc": doc.get("expires_utc"), "alerts": alerts})


def _sync_invalidation_alert(alerts_path, ticket, *, new_sl, now, model_id):
    """Уровень инвалидации в alerts.json обязан следовать за реальным стопом
    у брокера — иначе после переноса в безубыток (1R) условие продолжает
    сторожить УЖЕ НЕДЕЙСТВУЮЩИЙ старый уровень: цену туда не пустит стоп
    брокера, сработавший раньше, и «инвалидация» превращается в мёртвый
    груз, который никогда не сработает и молча врёт о реальной границе риска.
    Найден 2026-07-29 на первой живой позиции (SL перенесён на 1R, алерт
    ведения остался на исходном стопе).
    """
    doc = load_alerts(alerts_path, now=now) or {}
    alerts = doc.get("alerts") or []
    changed = False
    for a in alerts:
        if a.get("ticket") == ticket and a.get("id") == f"pos-{ticket}-invalidation":
            a["level"] = new_sl
            changed = True
    if not changed:
        return
    write_alerts_atomic(alerts_path, {
        "version": 1, "written_by": model_id, "written_utc": now.isoformat(),
        "expires_utc": doc.get("expires_utc"), "alerts": alerts})


def exit_position(market, cfg, *, ticket, action, reason, fraction=None, new_sl=None,
                  journal_path, alerts_path=None, now=None, trader=None):
    """Один выход по решению модели.

    → {ok, action, ticket, closed_lots, exit, R, error, message}
    """
    now = now or dt.datetime.now(UTC)
    journal_path = Path(journal_path)
    alerts_path = Path(alerts_path) if alerts_path else journal_path.parent / "alerts.json"
    # подпись исхода — по ОБЪЯВИВШЕЙСЯ модели, не по конституции: иначе вход
    # подписан одной моделью, а выход другой, и статистика режется пополам
    model_id, _profile = effective_model(journal_path.parent, cfg)

    if action not in ACTIONS:
        return _fail("unknown_action", f"действие {action!r} не из {ACTIONS}")
    if not (reason or "").strip():
        return _fail("reason_required",
                     "выход без причины не исполняется: причина — единственное, "
                     "чему потом учится разбор")

    pos = _position(market, ticket)
    if pos is None:
        return _fail("position_not_found",
                     f"позиции {ticket} нет у брокера. Если она закрылась сама, исход "
                     "допишет reconcile (scripts/close_watch.py) по истории — "
                     "выдумывать его здесь нельзя")

    records = read_records(journal_path)
    decision = _decision_for(records, ticket)
    if decision is None:
        return _fail("not_our_position",
                     f"по тикету {ticket} нет decision-записи: это не наша сделка, "
                     "R считать не от чего и трогать её не наше дело")

    if action == "move-sl":
        res = modify_sl(market, ticket=ticket, new_sl=new_sl)
        if not res.get("ok"):
            return _fail(res.get("error", "modify_rejected"), res.get("message", ""),
                         action=action, ticket=ticket)
        append_trade_event(journal_path, {
            "trade_id": str(ticket), "action": "move-sl", "reason": reason,
            "model_id": model_id, "new_sl": new_sl, "old_sl": pos.get("sl")})
        _sync_invalidation_alert(alerts_path, ticket, new_sl=new_sl, now=now,
                                 model_id=model_id)
        return {"ok": True, "action": action, "ticket": ticket, "new_sl": new_sl,
                "message": "стоп перенесён"}

    if action == "partial":
        res = close_partial(market, ticket=ticket, fraction=fraction)
        if not res.get("ok"):
            return _fail(res.get("error", "close_rejected"), res.get("message", ""),
                         action=action, ticket=ticket)
        closed_lots = res["closed_lots"]
        exit_price = res.get("price")
        event = {"trade_id": str(ticket), "action": "partial", "reason": reason,
                 "model_id": model_id, "closed_lots": closed_lots,
                 "left_lots": res.get("left_lots"), "exit": exit_price,
                 "R_part": _r_multiple(decision, exit_price)}

        if new_sl is None:
            append_trade_event(journal_path, event)
            return {"ok": True, "action": action, "ticket": ticket,
                    "closed_lots": closed_lots, "left_lots": res.get("left_lots"),
                    "message": "частичка исполнена"}

        sl_res = modify_sl(market, ticket=ticket, new_sl=new_sl)
        event["new_sl"] = new_sl if sl_res.get("ok") else None
        event["sl_error"] = None if sl_res.get("ok") else sl_res.get("error")
        append_trade_event(journal_path, event)
        if not sl_res.get("ok"):
            # откатить частичку нельзя — обратный вход был бы новой сделкой по
            # новой цене; поэтому говорим ровно то, что произошло
            return _fail("partial_done_sl_failed",
                         f"частичка {closed_lots} исполнена, но стоп НЕ перенесён: "
                         f"остался {pos.get('sl')}, просили {new_sl} "
                         f"({sl_res.get('message')}). Позиция защищена не там, "
                         "где ты думаешь",
                         action=action, ticket=ticket, closed_lots=closed_lots,
                         left_lots=res.get("left_lots"), sl_result=sl_res)
        _sync_invalidation_alert(alerts_path, ticket, new_sl=new_sl, now=now,
                                 model_id=model_id)
        return {"ok": True, "action": action, "ticket": ticket,
                "closed_lots": closed_lots, "left_lots": res.get("left_lots"),
                "new_sl": new_sl, "message": "частичка исполнена, стоп перенесён"}

    # --- полное закрытие ---
    res = close_position(market, ticket=ticket)
    if not res.get("ok"):
        return _fail(res.get("error", "close_rejected"), res.get("message", ""),
                     action=action, ticket=ticket)
    exit_price = res.get("price")
    r_mult = _r_multiple(decision, exit_price)
    mfe_R, mae_R = measure(market, symbol=pos["symbol"], side=decision.get("side"),
                           entry=decision.get("entry"), sl=decision.get("sl"),
                           opened_utc=decision.get("ts"), closed_utc=now,
                           server_utc_offset_hours=cfg.risk.server_utc_offset_hours)
    append_outcome(journal_path, {
        "trade_id": str(ticket), "exit": exit_price, "profit": pos.get("profit"),
        "R": r_mult, "exit_reason": reason,
        "spread_at_exit": market.symbol_info(pos["symbol"]).get("spread"),
        "slippage_points": None, "mfe_R": mfe_R, "mae_R": mae_R,
        "decision_events": [x for x in records
                            if x.get("type") == "trade_event"
                            and str(x.get("trade_id")) == str(ticket)]})
    _drop_position_alerts(alerts_path, ticket, now=now, model_id=model_id)

    # рассказать человеку — своим try: отказ мессенджера не отменяет закрытие
    try:
        from scripts.report import exited as _report_exited

        _report_exited(cfg, trader=trader, result={"ticket": ticket, "R": r_mult,
                                    "profit": pos.get("profit"), "exit": exit_price},
                       symbol=pos["symbol"], reason=reason,
                       entry_price=decision.get("entry"), now=now)
    except Exception as e:  # noqa: BLE001
        print(f"[exit] отчёт о выходе не отправлен: {e!r}", file=sys.stderr)

    return {"ok": True, "action": action, "ticket": ticket,
            "closed_lots": res.get("closed_lots"), "exit": exit_price, "R": r_mult,
            "message": "позиция закрыта"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="исполнение решения о выходе")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "trader.config.json"))
    ap.add_argument("--ticket", type=int, required=True)
    ap.add_argument("--action", choices=ACTIONS, required=True)
    ap.add_argument("--fraction", type=float, default=None)
    ap.add_argument("--new-sl", type=float, default=None, dest="new_sl")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--trader", default=None,
                    help="чьё состояние: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args(argv)
    trader = resolve_trader(a.trader)

    cfg = load_config(a.config)
    from trader_lib.mt5_client import live_market
    sd = trader_state_dir(cfg, trader) if trader else Path(state_dir(cfg))
    res = exit_position(live_market(), cfg, ticket=a.ticket, action=a.action,
                        reason=a.reason, fraction=a.fraction, new_sl=a.new_sl,
                        journal_path=sd / "journal.jsonl", alerts_path=sd / "alerts.json",
                        trader=trader)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
