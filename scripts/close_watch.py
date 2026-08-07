import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.alerts import load_alerts, write_alerts_atomic  # noqa: E402
from trader_lib.excursion import measure  # noqa: E402
from trader_lib.journal import append_outcome, read_records  # noqa: E402

UTC = dt.timezone.utc


def _drop_alerts_for_ticket(alerts_path, ticket, *, now, model_id="reconcile"):
    """Снимает алерты ведения по тикету — та же логика, что в exit.py при
    ручном закрытии. Нужна и здесь: позицию мог закрыть брокер напрямую
    (SL/TP), exit_position() тогда не вызывался вовсе, и алерты 1R/stall/
    инвалидации остаются висеть на уже не существующей позиции."""
    doc = load_alerts(alerts_path, now=now) or {}
    alerts = [a for a in (doc.get("alerts") or []) if a.get("ticket") != ticket]
    if len(alerts) == len(doc.get("alerts") or []):
        return  # нечего снимать — не плодим лишнюю запись файла
    write_alerts_atomic(alerts_path, {
        "version": 1, "written_by": model_id, "written_utc": now.isoformat(),
        "expires_utc": doc.get("expires_utc"), "alerts": alerts})


def _closed_utc(exit_deal, offset_hours):
    """Момент выхода в истинном UTC.

    MT5 отдаёт время сделки серверной эпохой — так, будто часы сервера и есть
    UTC (см. комментарий к history_deals в mt5_client). Поэтому смещение
    вычитается: иначе окно замера съедет на часы брокера и MFE посчитается по
    чужому участку графика.
    """
    t = exit_deal.get("time")
    if t is None:
        return None
    try:
        naive = dt.datetime.fromtimestamp(float(t), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return naive - dt.timedelta(hours=offset_hours or 0)


def reconcile(journal_path, deals_by_pos, *, alerts_path=None, now=None,
              market=None, server_utc_offset_hours=0):
    """Сверяет закрытые позиции с историей MT5, дописывает outcome. Идемпотентно.

    alerts_path — если задан, снимает алерты ведения по тикету сразу после
    записи исхода (актуально для позиций, закрытых брокером напрямую, а не
    через exit.py — тот сам снимает свои алерты при полном закрытии).

    market — если задан, по барам достраивается MFE/MAE. Именно здесь это
    нужнее всего: сюда попадают сделки, закрытые БРОКЕРОМ (стоп, тейк) в тот
    момент, когда модель спала, — то есть большинство исходов. Без замера они
    навсегда остаются парой «R и всё», и отличить «вход был плохой» от «прибыль
    отдало ведение» будет уже нечем: бары уйдут из окна истории.
    """
    now = now or dt.datetime.now(UTC)
    recs = read_records(journal_path)
    decisions = {r["trade_id"]: r for r in recs if r["type"] == "decision"}
    done = {r["trade_id"] for r in recs if r["type"] == "outcome"}
    written = 0
    for tid, d in decisions.items():
        if tid in done or tid not in deals_by_pos:
            continue
        deals = deals_by_pos[tid]
        exits = [x for x in deals if x.get("entry") == 1]
        if not exits:
            continue
        profit = sum(x["profit"] for x in deals)
        exit_price = exits[-1]["price"]
        risk = d.get("risk_usd") or 0
        R = profit / risk if risk else 0.0
        mfe = mae = None
        if market is not None:
            mfe, mae = measure(market, symbol=d.get("symbol"), side=d.get("side"),
                               entry=d.get("entry"), sl=d.get("sl"),
                               opened_utc=d.get("ts"),
                               closed_utc=_closed_utc(exits[-1], server_utc_offset_hours),
                               server_utc_offset_hours=server_utc_offset_hours)
        append_outcome(journal_path, {
            "trade_id": tid, "exit": exit_price, "profit": round(profit, 2),
            "R": round(R, 3), "exit_reason": "closed",
            "mfe_R": mfe, "mae_R": mae})
        written += 1
        if alerts_path is not None:
            try:
                _drop_alerts_for_ticket(alerts_path, int(tid), now=now)
            except (TypeError, ValueError):
                pass  # trade_id не число (не тикет брокера) — нечего снимать
    return written


def find_orphans(journal_path, positions) -> list:
    """Открытые позиции брокера, для которых в журнале нет decision-записи.

    Сопоставление — ТА ЖЕ связь, что уже использует reconcile() для закрытых
    сделок: в MT5 тикет позиции и position_id её сделок в истории — один и
    тот же номер, reconcile() сравнивает decision.trade_id со
    str(position_id) из history_deals; здесь — decision.trade_id со
    str(ticket) из positions(). Это не новое, менее надёжное предположение —
    ровно то же тождество, на которое уже опирается reconcile() (и, значит,
    вся реконсиляция закрытых сделок в этом файле). Полагается на то, что
    исполняющий сделку модуль пишет trade_id как str(ticket) полученной
    позиции. С задачи 4.2 такой модуль есть — scripts/enter.py, и он это
    требование выполняет двухфазной записью (intent до отправки, decision с
    тикетом после филла); тест test_own_trade_is_not_detected_as_orphan
    держит связку с этой функцией, а не с формулировкой в комментарии.

    decision без outcome (позиция ещё открыта и учтена) — НЕ orphan: ищем
    только сам факт наличия decision-записи, outcome для этого не нужен.

    Возвращает список описаний — по одному dict на orphan-позицию:
      {'ticket': int, 'symbol': str, 'volume': float,
       'side': 'buy'|'sell'|<исходное значение type>, 'has_sl': bool}
    Этого достаточно, чтобы найти позицию в терминале, не заходя в MT5 за
    подробностями. Порядок — как в positions(). Пустой список = расхождений
    нет. Формулировки в описании — нейтральные факты (тикет/символ/объём/
    наличие стопа), без побуждения что-либо с позицией делать: что с ней
    делать — решает не этот модуль (см. scripts/risk_gate_cli.py — orphan
    останавливает НОВЫЕ входы, но не закрывает то, что уже открыто).
    """
    recs = read_records(journal_path)
    known_trade_ids = {r["trade_id"] for r in recs if r["type"] == "decision"}

    orphans = []
    for p in positions:
        ticket = p["ticket"]
        if str(ticket) in known_trade_ids:
            continue
        # MT5 отдаёт sl=0.0, когда стопа нет (не None, не отсутствующий ключ) —
        # та же трактовка, что и в trader_lib/exposure.py.open_risk_usd.
        sl = p.get("sl", 0.0)
        ptype = p.get("type")
        side = {0: "buy", 1: "sell"}.get(ptype, ptype)
        orphans.append({
            "ticket": ticket,
            "symbol": p.get("symbol"),
            "volume": p.get("volume"),
            "side": side,
            "has_sl": bool(sl),
        })
    return orphans


def main(argv=None):
    import collections
    import datetime as dt

    from trader_lib.config import load_config
    from trader_lib.mt5_client import live_market
    from trader_lib.workspace import resolve_trader, trader_state_dir

    ap = argparse.ArgumentParser(description="сверка закрытых сделок с брокером")
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    ap.add_argument("--trader", default=None,
                    help="чьё состояние: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args(argv)
    trader = resolve_trader(a.trader)

    cfg = load_config(a.config)
    sd = trader_state_dir(cfg, trader)
    m = live_market()
    since = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = m.history_deals(since)
    by_pos = collections.defaultdict(list)
    for x in deals:
        by_pos[str(x.get("position_id"))].append(x)
    print("outcomes written:",
          reconcile(str(sd / "journal.jsonl"), by_pos,
                    alerts_path=str(sd / "alerts.json"), market=m,
                    server_utc_offset_hours=cfg.risk.server_utc_offset_hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
