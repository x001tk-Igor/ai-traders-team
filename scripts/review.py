"""Дневной разбор (задача 6.3): что сделано за день и чему это учит.

ВТОРОЙ, НЕЗАВИСИМЫЙ ПРОХОД ПО СЫРОМУ ЖУРНАЛУ — не прихоть, а требование.
score.compute_stats по построению работает только с парами decision+outcome
(score._join), а skip и alert_event отбрасывает: смешивать «решил не входить» с
R было бы категориальной ошибкой. Но именно skip и alert_event отвечают на
вопрос «сколько раз меня будили и сколько из этих пробуждений чего-то стоили».
Поэтому здесь свой проход по journal.read_records, а contract compute_stats не
трогается (указание ревью задачи 2.2).

МЕТРИКА ПРОБУЖДЕНИЙ — ГЛАВНОЕ, ЧТО ЕСТЬ В ЭТОМ ОТЧЁТЕ. Алертная петля живёт на
подписке: каждое пустое пробуждение — это потраченный лимит и внимание, отданное
ни за что. Разбор считает три исхода доставленного события: дало вход, дало
осознанный пропуск, не дало ничего. Последние с именами алертов идут в
noisy_alerts — модель обязана перестать ставить такие условия (это же требование
записано в trader-reflect).

ЧЕГО ЗДЕСЬ НЕТ. Тиковой кривой equity: внутридневная просадка считается по
кривой накопленного R в порядке закрытия сделок. Это честное приближение, и оно
названо своим именем в отчёте — подставлять «просадку по equity», которой у нас
нет, значило бы соврать в единственном числе, ради которого этот раздел читают.
"""
import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir                 # noqa: E402
from trader_lib.day_plan import parse_day_plan                       # noqa: E402
from trader_lib.journal import read_records                          # noqa: E402
from trader_lib.news import load_windows                             # noqa: E402
from trader_lib.score import _calibration                            # noqa: E402
from trader_lib.scorecard import render_daily_report                 # noqa: E402
from trader_lib.session import server_day_key                        # noqa: E402
from trader_lib.workspace import resolve_trader, trader_state_dir    # noqa: E402

UTC = dt.timezone.utc

# Типы событий, которые производит стоп-кран: это отчёт кода о собственных
# действиях, а не условие модели. Пустым пробуждением он быть не может.
STOP_VALVE_TYPES = ("wall_breach", "position_without_sl")


def _parse_ts(value):
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _same_server_day(value, *, cfg, day):
    ts = _parse_ts(value)
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return server_day_key(utc_now=ts, offset_hours=cfg.risk.server_utc_offset_hours,
                          reset_hour=cfg.risk.server_day_reset_hour) == day


def _trade_numbers(rows):
    """Числа по закрытым сделкам дня. Пустой день — не ошибка: None там, где
    метрика не определена (WR без сделок, PF без проигрышей)."""
    if not rows:
        return {"closed": 0, "still_open": 0, "sum_R": 0.0, "pnl_usd": 0.0,
                "wr": None, "avg_win_R": None, "avg_loss_R": None,
                "profit_factor": None, "max_drawdown_R": 0.0}
    Rs = [r["R"] for r in rows]
    wins = [x for x in Rs if x > 0]
    losses = [x for x in Rs if x < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equity, peak, dd = 0.0, 0.0, 0.0
    for x in Rs:
        equity += x
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return {
        "closed": len(rows),
        "sum_R": round(sum(Rs), 3),
        "pnl_usd": round(sum(r.get("profit") or 0.0 for r in rows), 2),
        "wr": round(len(wins) / len(Rs), 3),
        "avg_win_R": round(sum(wins) / len(wins), 3) if wins else None,
        "avg_loss_R": round(sum(losses) / len(losses), 3) if losses else None,
        # PF не определён без проигрышей: делить на ноль и печатать «inf» в
        # отчёте, который читает человек, — хуже честного «н/д»
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_R": round(dd, 3),
    }


def _alert_efficiency(records_today, events_today=()):
    """Пробуждения → решения. Только по ДОСТАВЛЕННЫМ событиям: придушенное
    бюджетом событие модель не видела, и винить её за него нельзя.

    События берутся из ОТДЕЛЬНОГО файла alert_events.jsonl — его пишет датчик
    (append_alert_event), и в journal.jsonl их нет. Первая версия читала только
    журнал решений, из-за чего метрика в бою всегда показывала ноль пробуждений,
    выглядя при этом исправной. Найдено первым полным смоуком на живом рынке
    2026-07-27; офлайн-тесты были слепы, потому что фикстура клала события в
    journal.jsonl. Аргумент records_today остаётся: ответы модели (decision со
    ссылкой alert_id и skip) живут именно в журнале.
    """
    pool = list(events_today) + [r for r in records_today
                                 if r.get("type") == "alert_event"]
    delivered = [r for r in pool if r.get("delivered") is not False]
    suppressed = sum(1 for r in pool if r.get("delivered") is False)
    stop_valve = [r for r in delivered if r.get("alert_type") in STOP_VALVE_TYPES]
    model_events = [r for r in delivered if r not in stop_valve]

    def answered_by(kind):
        return {r.get("alert_id") for r in records_today
                if r.get("type") == kind and r.get("alert_id")}

    answered_decision = answered_by("decision")
    answered_skip = answered_by("skip")
    # НАБЛЮДЕНИЕ — тоже ответ на пробуждение. «Увидела и жду отката» означает,
    # что алерт сработал по делу; считать его пустым значило бы наказывать
    # правильное ожидание наравне с бесполезным будильником, и reflect выбросил
    # бы условие, которое как раз работало.
    answered_observation = answered_by("observation")

    with_decision = sum(1 for r in model_events if r.get("alert_id") in answered_decision)
    with_skip = sum(1 for r in model_events
                    if r.get("alert_id") in answered_skip
                    and r.get("alert_id") not in answered_decision)
    with_observation = sum(
        1 for r in model_events
        if r.get("alert_id") in answered_observation
        and r.get("alert_id") not in answered_decision | answered_skip)
    answered_any = answered_decision | answered_skip | answered_observation
    ignored = len(model_events) - with_decision - with_skip - with_observation

    noisy = Counter(r.get("alert_id") for r in model_events
                    if r.get("alert_id") not in answered_any)
    useful = with_decision + with_skip + with_observation
    return {
        "delivered": len(delivered), "suppressed": suppressed,
        "stop_valve": len(stop_valve),
        "with_decision": with_decision, "with_skip": with_skip,
        "with_observation": with_observation, "ignored": ignored,
        "usefulness": (round(useful / len(model_events), 3) if model_events else None),
        "noisy_alerts": [{"alert_id": a, "count": c}
                         for a, c in noisy.most_common() if a],
    }


def _plan_vs_fact(plan, decisions_today, cfg):
    hyps = plan.get("hypotheses", [])
    traded_ids = {d.get("plan_hypothesis_id") for d in decisions_today
                  if d.get("planned") and d.get("plan_hypothesis_id")}
    planned = sum(1 for d in decisions_today if d.get("planned"))
    unplanned = len(decisions_today) - planned
    return {
        "hypotheses_total": len(hyps),
        "hypotheses_traded": len([h for h in hyps if h["id"] in traded_ids]),
        "untouched": [h["id"] for h in hyps if h["id"] not in traded_ids],
        "planned": planned, "unplanned": unplanned,
        "unplanned_limit": cfg.risk.max_unplanned_trades_per_day,
        "off_plan_setups": sorted({d.get("setup_type") for d in decisions_today
                                   if not d.get("planned") and d.get("setup_type")}),
    }


def _blocks(records_today, events_today):
    """Что мешало торговать — по ФАКТУ, а не по аккуратности модели.

    Раньше раздел считался только по записям `skip`. Если модель фиксировала
    пропуск как `observation` (или не фиксировала вовсе), отчёт бодро писал
    «ничего не блокировало» — в день, когда гейт был закрыт с обеда. 2026-07-27
    так и вышло: торговля стояла с 13:00 по серии убытков, а разбор дня заявил
    обратное. Отчёт, который врёт именно про то, ради чего его читают, хуже
    отсутствующего.

    Поэтому источник — снимки СОБЫТИЙ ДАТЧИКА: он пишет вердикт гейта каждым
    событием, независимо от модели. Записи `skip` остаются: они объясняют
    отказы, которых гейт не делал (собственное решение не входить).
    """
    counter = Counter()
    for r in records_today:
        if r.get("type") == "skip" and r.get("reason"):
            counter[r["reason"]] += 1

    gate_reasons = Counter()
    for e in events_today:
        gate = ((e.get("snapshot") or {}).get("gate") or {})
        if gate.get("verdict") in (None, "OK"):
            continue
        for reason in gate.get("reasons") or [gate.get("blocked_by") or gate["verdict"]]:
            gate_reasons[f"гейт: {reason}"] += 1

    return ([{"reason": reason, "count": count}
             for reason, count in gate_reasons.most_common()]
            + [{"reason": reason, "count": count}
               for reason, count in counter.most_common()])


def build_review(cfg, *, now=None, trader=None):
    """Разбор текущего серверного дня.

    → {server_day, trades, risk_used_usd, plan_vs_fact, alert_efficiency,
       calibration, blocks, news_tomorrow}
    """
    now = now or dt.datetime.now(UTC)
    sd = trader_state_dir(cfg, trader) if trader else Path(state_dir(cfg))
    day = server_day_key(utc_now=now, offset_hours=cfg.risk.server_utc_offset_hours,
                         reset_hour=cfg.risk.server_day_reset_hour)
    records = read_records(sd / "journal.jsonl")
    # события датчика — отдельный файл; в журнале решений их нет
    events = read_records(sd / "alert_events.jsonl")
    events_today = [r for r in events
                    if _same_server_day(r.get("fired_utc") or r.get("ts"),
                                        cfg=cfg, day=day)]

    today = [r for r in records
             if _same_server_day(r.get("ts") or r.get("close_ts"), cfg=cfg, day=day)]
    decisions = [r for r in today if r.get("type") == "decision"]
    outcomes_all = {r["trade_id"]: r for r in records if r.get("type") == "outcome"}

    # закрытые СЕГОДНЯ: смотрим на момент закрытия, а не входа — сделка,
    # открытая вчера и закрытая сегодня, принадлежит сегодняшнему разбору
    closed_today = [r for r in records if r.get("type") == "outcome"
                    and _same_server_day(r.get("close_ts"), cfg=cfg, day=day)]
    decisions_all = {r["trade_id"]: r for r in records if r.get("type") == "decision"}
    rows = []
    for out in closed_today:
        dec = decisions_all.get(out["trade_id"], {})
        rows.append({**dec, "R": out.get("R") or 0.0, "profit": out.get("profit")})

    trades = _trade_numbers(rows)
    trades["still_open"] = sum(1 for d in decisions if d["trade_id"] not in outcomes_all)

    risk_used = round(sum(d.get("risk_usd") or 0.0 for d in decisions), 2)
    daily_budget = None
    try:
        init = json.loads((sd / "account_init.json").read_text(encoding="utf-8"))
        daily_budget = init["initial_balance"] * cfg.risk.daily_loss_limit_pct / 100.0
    except Exception:  # noqa: BLE001 - нет файла: процент честно не считаем
        daily_budget = None

    plan_path = sd / "day_plan.md"
    plan = parse_day_plan(plan_path.read_text(encoding="utf-8")
                          if plan_path.exists() else "")

    try:
        news_doc = load_windows(sd / "news_cache.json", cfg=cfg, now=now,
                                loader=_no_network)
        start = now
        end = now + dt.timedelta(hours=36)
        news_tomorrow = [{"at": w["at"].isoformat(), "title": w["title"],
                          "level": w["level"], "currencies": sorted(w["currencies"])}
                         for w in news_doc["windows"] if start <= w["at"] <= end]
    except Exception:  # noqa: BLE001 - календарь недоступен: раздел пуст, не падаем
        news_tomorrow = []

    return {
        "generated_utc": now.isoformat(),
        "server_day": day,
        "trades": trades,
        "risk_used_usd": risk_used,
        "risk_used_pct_of_budget": (round(risk_used / daily_budget * 100, 1)
                                    if daily_budget else None),
        "plan_vs_fact": _plan_vs_fact(plan, decisions, cfg),
        "alert_efficiency": _alert_efficiency(today, events_today),
        "calibration": _calibration([r for r in rows if r.get("confidence") is not None]),
        "blocks": _blocks(today, events_today),
        "news_tomorrow": news_tomorrow,
        "plan_problems": plan.get("problems", []),
    }


def _no_network():
    """Разбор не ходит в сеть за календарём — как и предвходовой гейт:
    обновление кэша делает цикл восприятия."""
    raise RuntimeError("разбор не обновляет календарь по сети")


def main(argv=None):
    ap = argparse.ArgumentParser(description="дневной разбор")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "trader.config.json"))
    ap.add_argument("--json", action="store_true", help="выдать JSON вместо markdown")
    ap.add_argument("--save", action="store_true",
                    help="сохранить отчёт в state_dir/reviews/<день>.md")
    ap.add_argument("--now", default=None,
                    help="считать разбор на этот момент (ISO UTC), а не на "
                         "реальное текущее время — нужно поздним вечером, "
                         "когда серверный день уже успел смениться")
    ap.add_argument("--trader", default=None,
                    help="чьё состояние: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args(argv)
    trader = resolve_trader(a.trader)

    cfg = load_config(a.config)
    now = dt.datetime.fromisoformat(a.now) if a.now else None
    review = build_review(cfg, now=now, trader=trader)
    if a.json:
        print(json.dumps(review, ensure_ascii=False, indent=2, default=str))
    else:
        md = render_daily_report(review)
        print(md)
        if a.save:
            out_dir = (trader_state_dir(cfg, trader) if trader
                       else Path(state_dir(cfg))) / "reviews"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{review['server_day']}.md").write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
