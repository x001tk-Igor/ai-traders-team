import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.close_watch import find_orphans                 # noqa: E402
from trader_lib.config import load_config, state_dir        # noqa: E402
from trader_lib.exposure import open_risk_usd                # noqa: E402
from trader_lib.journal import read_records                  # noqa: E402
from trader_lib.quality import profile_risk_mult             # noqa: E402
from trader_lib.risk_gate import blocked_response, safe_evaluate_gate  # noqa: E402
from trader_lib.session import server_day_key, server_day_start_utc  # noqa: E402
from trader_lib.spread_gate import load_medians                      # noqa: E402
from trader_lib.streak import compute_streak                 # noqa: E402


def _day_start_utc(now, cfg):
    """Начало текущего торгового дня брокера, выраженное в UTC (задача 5.3).

    Раньше здесь была UTC-полночь, и при смещении брокера +3ч дневной лимит
    −3% три часа в сутки (21:00–24:00 UTC) отмерялся от вчерашнего нуля.
    Теперь точка отсчёта — начало СЕРВЕРНОГО дня; ключ дня для baseline-файла
    даёт session.server_day_key, и он же используется в perceive.py.
    """
    return server_day_start_utc(utc_now=now,
                                offset_hours=cfg.risk.server_utc_offset_hours,
                                reset_hour=cfg.risk.server_day_reset_hour)


def _baselines(sd, equity, *, now, cfg):
    """Дневная и общая точка отсчёта equity.

    Файл day_baseline.json пишет scripts/perceive.py (запускается раньше в
    цикле восприятия) — здесь только чтение. Если файла нет или он не за
    сегодня (первый запуск дня/сессии), откатываемся на текущий equity как
    day_start; initial_balance — из account_init.json, а если и его нет —
    тоже текущий equity.

    «Сегодня» — СЕРВЕРНЫЙ день брокера (задача 5.3), а не UTC-день: иначе с
    21:00 до 24:00 UTC (при смещении +3) baseline считался бы устаревшим и
    дневной лимит отмерялся бы от текущего equity вместо начала дня.
    """
    p = Path(sd) / "day_baseline.json"
    today = server_day_key(utc_now=now, offset_hours=cfg.risk.server_utc_offset_hours,
                           reset_hour=cfg.risk.server_day_reset_hour)
    if p.exists():
        d = json.loads(p.read_text())
        if d.get("day") == today:
            return d["equity"], d["initial_balance"]
    init_path = Path(sd) / "account_init.json"
    init = json.loads(init_path.read_text())["initial_balance"] if init_path.exists() else equity
    return equity, init


def _symbol_info_map(market, positions):
    """{symbol: symbol_info} для каждого символа, по которому есть открытая
    позиция — то, что требует exposure.open_risk_usd."""
    symbols = sorted({p["symbol"] for p in positions})
    return {s: market.symbol_info(s) for s in symbols}


def build_gate_inputs(market, cfg, records, *, now, positions=None):
    """Собирает все девять входов риск-гейта (задача 1.6) из рынка, конфига
    и журнала решений. Не вызывает сам гейт — только строит kwargs для
    evaluate_gate/safe_evaluate_gate. Любая ошибка (MT5 недоступен, битый
    журнал, нет symbol_info для символа открытой позиции) поднимается
    исключением — fail-closed на этом пути реализует run().

    now — timezone-aware datetime (UTC): граница дня и streak.compute_streak
    считаются от него, а не от часов машины — так функция детерминирована и
    тестируется офлайн.

    positions — опционально уже полученный список открытых позиций.
    None (по умолчанию) — получить самостоятельно через market.positions(),
    как раньше (все прямые вызовы build_gate_inputs в тестах так и делают).
    run() передаёт список явно, чтобы не опрашивать market.positions() дважды
    за один вызов (второй раз — для find_orphans): на живом MT5 это два
    round-trip с окном между ними, где набор позиций теоретически может
    измениться. Один опрос, один и тот же снимок для обеих проверок.
    """
    acc = market.account_info()
    equity = acc["equity"]
    day_start_equity, initial_balance = _baselines(state_dir(cfg), equity, now=now, cfg=cfg)

    if positions is None:
        positions = market.positions()
    exposure = open_risk_usd(positions, _symbol_info_map(market, positions))

    day_start_utc = _day_start_utc(now, cfg)
    decisions_today = [
        r for r in records
        if r["type"] == "decision" and dt.datetime.fromisoformat(r["ts"]) >= day_start_utc
    ]
    # planned отсутствует в записях старше задачи 1.6 (поля не существовало).
    # Решение: отсутствие поля трактуется как «не запланировано» — это строже
    # (быстрее исчерпывает бюджет внеплановых входов и требует plan-обоснования
    # раньше), а не разрешительный дефолт planned=True.
    unplanned_today = sum(1 for r in decisions_today if not r.get("planned", False))

    sk = compute_streak(records, now=now, day_start_utc=day_start_utc,
                        streak_cfg=cfg.risk.streak)
    prof = profile_risk_mult(cfg.model.profile, cfg.model)

    return {
        "equity": equity,
        "day_start_equity": day_start_equity,
        "initial_balance": initial_balance,
        "limits": cfg.risk,
        "open_risk_usd": exposure["total_usd"],
        "unprotected_positions": exposure["unprotected"],
        "positions_count": len(positions),
        "trades_today": len(decisions_today),
        "unplanned_today": unplanned_today,
        "loss_streak_mult": sk["risk_mult"],
        "paused_until": sk["paused_until"],
        "halt_rest_of_day": sk["halt_rest_of_day"],
        "profile_mult": prof["mult"],
        "now": now,
    }


def _describe_orphan(o) -> str:
    """Нейтральная, фактическая строка для одной orphan-позиции — без
    побуждения что-либо с ней делать (урок фазы: формулировка, которую читает
    модель, способна подтолкнуть её к неверному решению)."""
    sl_note = "SL есть" if o["has_sl"] else "SL нет"
    return f"ticket {o['ticket']} {o['symbol']} {o['side']} {o['volume']} лот, {sl_note}"


def _orphans_reason(orphans) -> str:
    listing = "; ".join(_describe_orphan(o) for o in orphans)
    return (f"открытых позиций брокера без decision-записи в журнале: {len(orphans)} "
           f"({listing}) — их риск не учтён в бюджете гейта, новых входов нет до "
           "устранения расхождения (fail-closed); уже открытые позиции не трогаются")


def run(market, cfg, journal_path, *, now=None):
    """CLI-уровень: прочитать журнал, собрать входы, вызвать гейт.

    Чтение журнала — ВНУТРИ этого try/except, не до него: битый journal.jsonl
    (повреждённый JSON после сбоя записи) обязан деградировать так же, как
    недоступный MT5 или отсутствующий symbol_info символа открытой позиции —
    HALT_NEW ПОЛНОЙ схемы через trader_lib.risk_gate.blocked_response,
    никогда исключение наружу и никогда неполный словарь (см. задачу 1.6:
    старый код на MT5-failure собирал HALT_NEW из трёх ключей вручную —
    ровно та неполная схема, от которой гейт теперь защищает).

    ORPHAN-ПОЗИЦИИ (задача 2.3). После успешной сборки входов, но ДО вызова
    самого гейта, отдельно проверяется scripts.close_watch.find_orphans —
    есть ли у брокера открытая позиция без decision-записи в журнале. Если
    есть, CLI до гейта НЕ доходит: возвращается blocked_response с
    blocked_by="orphan_positions", HALT_NEW (не FORCE_FLAT — система не знает
    ЧЬЯ это позиция и зачем, закрывать её без этого знания нельзя; см. журнал
    плана задачи 2.3). Проверка — после build_gate_inputs, а не до: если
    данные сами по себе не собрались (MT5 недоступен, нет symbol_info) —
    это gate_error, а не orphan_positions, и обязано остаться таковым.
    Позиция с decision-записью, но без outcome (сделка ещё открыта и учтена)
    — не orphan, gate_error её не путает с расхождением.

    Обе эти проверки живут в одном try: ошибка при опросе market.positions()
    — тоже HALT_NEW полной схемы (тот же blocked_response(error=e), что и
    любая другая ошибка здесь).

    market.positions() опрашивается РОВНО ОДИН раз за вызов — снимок передан
    и в build_gate_inputs (экспозиция/счётчик), и в find_orphans (сверка с
    журналом). Раздельные вызовы дали бы два round-trip к живому MT5 с окном
    между ними, где набор позиций может измениться — тогда экспозиция и
    orphan-проверка считались бы по РАЗНЫМ снимкам мира.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        records = read_records(journal_path)
        positions = market.positions()
        inputs = build_gate_inputs(market, cfg, records, now=now, positions=positions)
        orphans = find_orphans(journal_path, positions)
        if orphans:
            return blocked_response(reason=_orphans_reason(orphans),
                                    blocked_by="orphan_positions")
    except Exception as e:  # noqa: BLE001 - fail-closed на уровне CLI
        return blocked_response(error=e)
    v = safe_evaluate_gate(**inputs)
    v["spread_excluded"] = _spread_excluded(cfg)
    return v


def _spread_excluded(cfg):
    """Символы, исключённые из торговли по спреду ПРЯМО СЕЙЧАС.

    2026-07-28: этот CLI весь день отвечал «OK», а реальный вход в enter.py
    отклонялся с 13:57 ПРЕДЫДУЩЕГО дня — XAUUSD был исключён гистерезисом
    spread_gate (trader_lib/spread_gate.py) и не возвращался в торговлю, пока
    спред не придёт К МЕДИАНЕ. evaluate_gate() проверяет только стены/риск, о
    спреде конкретного символа он ничего не знает — это отдельный, per-symbol
    гейт, который видит только enter.py в момент реальной попытки входа.
    Модель весь день читала «гейт: OK» как «можно торговать» и не замечала,
    что вход всё равно не пройдёт. Список исключений здесь закрывает разрыв
    дёшево, не смешивая ответственность: risk_gate_cli остаётся про
    стены/риск, но теперь честно говорит, если по спреду входить всё равно
    некуда.
    """
    try:
        doc = load_medians(Path(state_dir(cfg)) / "spread_median.json")
    except Exception:  # noqa: BLE001 - отсутствие файла не должно ронять статус
        return {}
    return doc.get("excluded", {})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    a = ap.parse_args()
    cfg = load_config(a.config)
    try:
        from trader_lib.mt5_client import live_market
        market = live_market()
    except Exception as e:  # noqa: BLE001 - fail-closed на уровне CLI
        print(json.dumps(blocked_response(error=f"market unavailable: {e}"), ensure_ascii=False))
        sys.exit(0)
    journal_path = Path(state_dir(cfg)) / "journal.jsonl"
    v = run(market, cfg, journal_path)
    print(json.dumps(v, ensure_ascii=False))
