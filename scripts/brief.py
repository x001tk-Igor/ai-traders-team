"""Утренний брифинг (задача 6.1): что произошло, пока модель спала.

Это первое, что модель читает в сессии, и единственное место, где она узнаёт о
ночи. Отсюда три правила, которым подчинён весь модуль:

1. НЕДОСТУПНОЕ ЗНАЧЕНИЕ — null И ПРИЧИНА, НИКОГДА НЕ ДОГАДКА. Брифинг читается
   как факты: «примерно такой гэп» становится тезисом, а тезис — сделкой. Нет
   баров по символу — раздел null плюс строка в warnings, а не среднее по
   больнице.

2. ЧУЖАЯ ПОЗИЦИЯ ВСПЛЫВАЕТ ПЕРВОЙ И ЗАПРЕЩАЕТ ТОРГОВЛЮ. Позиция без
   decision-записи — либо чужая рука, либо потерянный след; и то и другое
   означает «разобраться до открытия новых». Тот же вывод, что у риск-гейта
   (orphan → HALT_NEW) и у стоп-крана датчика.

3. КАЛИБРОВКА — ПО ТЕКУЩЕЙ МОДЕЛИ. Одна модель систематически переоценивает
   свою уверенность, другая недооценивает. Смешанная калибровка не описывает ни
   одну, и haircut по ней был бы взят с потолка.

Брифинг НЕ решает и не торгует: он собирает факты и говорит, разрешены ли новые
входы прямо сейчас (allow_new) — с причинами. Решение принимает модель.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.close_watch import find_orphans, reconcile              # noqa: E402
from scripts.risk_gate_cli import _baselines                         # noqa: E402
from trader_lib.config import load_config, state_dir                 # noqa: E402
from trader_lib.entry_gate import HEARTBEAT_MAX_AGE_S                # noqa: E402
from trader_lib.journal import read_records                          # noqa: E402
from trader_lib.model_session import effective as effective_model    # noqa: E402
from trader_lib.news import load_windows                             # noqa: E402
from trader_lib.score import compute_stats                           # noqa: E402
from trader_lib.session import server_day_key, session_gate          # noqa: E402

UTC = dt.timezone.utc

# Азиатская сессия в UTC: её диапазон — опора для London Opening Breakout
ASIAN_FROM_UTC, ASIAN_TO_UTC = 0, 7
BRIEF_TF = "M5"
BRIEF_BARS = 400            # ~33 часа M5: хватает и на ночь, и на ATR


def _atr(bars, period=14):
    """ATR по закрытым барам. None, если баров мало — считать ATR по трём
    свечам и выдавать это за меру волатильности нельзя."""
    if bars is None or len(bars) < period + 2:
        return None
    h, l, c = bars["high"], bars["low"], bars["close"]
    prev = c.shift(1)
    tr = (h - l).combine((h - prev).abs(), max).combine((l - prev).abs(), max)
    return float(tr.tail(period).mean())


def _to_utc(series, offset_hours):
    """Время баров MT5 наивное и СЕРВЕРНОЕ. Перевод обязателен: без него
    «азиатская сессия» съезжает на величину смещения брокера."""
    return series - dt.timedelta(hours=offset_hours)


def _symbol_brief(market, cfg, symbol, *, now):
    """Ночь по одному символу: диапазон азиатской сессии, экстремумы, гэп."""
    offset = cfg.risk.server_utc_offset_hours
    try:
        bars = market.copy_rates(symbol, BRIEF_TF, BRIEF_BARS)
    except Exception as e:  # noqa: BLE001 - нет данных = null + причина
        return {"asian_range": None, "night": None, "gap": None,
                "atr": None, "reason": f"бары недоступны: {e}"}
    if bars is None or len(bars) == 0:
        return {"asian_range": None, "night": None, "gap": None,
                "atr": None, "reason": "бары недоступны: пустой ответ терминала"}

    atr = _atr(bars)
    times_utc = _to_utc(bars["time"], offset)
    point = market.symbol_info(symbol)["point"]

    # ночь = от начала азиатской сессии предыдущих суток до сейчас
    night_start = now.replace(hour=ASIAN_FROM_UTC, minute=0, second=0, microsecond=0)
    if now.hour < ASIAN_FROM_UTC:
        night_start -= dt.timedelta(days=1)
    night_mask = times_utc >= night_start.replace(tzinfo=None)
    night_bars = bars[night_mask]

    asian_mask = night_mask & (times_utc.dt.hour >= ASIAN_FROM_UTC) & \
        (times_utc.dt.hour < ASIAN_TO_UTC)
    asian_bars = bars[asian_mask]

    def _range(df):
        if df is None or len(df) == 0:
            return None
        hi, lo = float(df["high"].max()), float(df["low"].min())
        return {"high": hi, "low": lo, "points": round((hi - lo) / point, 1),
                "range_atr": round((hi - lo) / atr, 2) if atr else None,
                "bars": int(len(df))}

    # гэп — разрыв между закрытием предпоследнего бара и открытием последнего
    gap = None
    if len(bars) >= 2:
        delta = float(bars["open"].iloc[-1]) - float(bars["close"].iloc[-2])
        gap = {"points": round(delta / point, 1),
               "atr": round(abs(delta) / atr, 2) if atr else None,
               "direction": "вверх" if delta > 0 else ("вниз" if delta < 0 else "нет")}

    return {"asian_range": _range(asian_bars), "night": _range(night_bars),
            "gap": gap, "atr": round(atr, 5) if atr else None,
            "last_price": float(bars["close"].iloc[-1]), "reason": None}


def _scorecard_slice(records, cfg, sd):
    """Срез статистики для брифинга: статусы сетапов и калибровка ТЕКУЩЕЙ
    модели. Калибровка по всему журналу здесь была бы вредна — см. шапку.

    «Текущая» — та, что объявилась в этом сеансе, а не та, что вписана в
    конституцию: на другом ПК работает другая модель, а конфиг переносится
    как есть."""
    stats = compute_stats(records, min_n_for_confirmed=cfg.learning.min_n_for_confirmed)
    model_id, _profile = effective_model(sd, cfg)
    calib = stats["calibration_by_model"].get(model_id, [])
    return {"model_id": model_id, "overall": stats["overall"],
            "by_setup": stats["by_setup"], "calibration": calib,
            "by_regime": stats["by_regime"],
            "label_drift": stats.get("label_drift", [])}


def _watchdog(sd, *, now):
    """Состояние датчика пробуждения: жив ли и свежа ли ЗАЩИТА (не процесс)."""
    p = Path(sd) / "watch_heartbeat.json"
    if not p.exists():
        return {"alive": False, "age_s": None,
                "reason": "датчик не запущен: нет watch_heartbeat.json"}
    try:
        hb = json.loads(p.read_text(encoding="utf-8"))
        age = (now - dt.datetime.fromisoformat(hb["ts"])).total_seconds()
    except Exception as e:  # noqa: BLE001
        return {"alive": False, "age_s": None, "reason": f"пульс нечитаем: {e}"}
    alive = bool(hb.get("walls_checked")) and age <= HEARTBEAT_MAX_AGE_S

    # Живой процесс крутит тот код, который был на диске в момент его старта.
    # Правки после запуска в него не попадают — 2026-07-27 так пропало правило
    # живости, и пульс при этом выглядел идеально. Сравниваем отпечаток.
    stale_code = None
    try:
        # тем же счётом, что и сам датчик: сравнивать надо ВЕСЬ загруженный
        # код, а не только точку входа (регресс 2026-08-01)
        from scripts.alert_watch import _loaded_code_mtime
        disk = _loaded_code_mtime()
        running = hb.get("code_mtime")
        if running is None:
            stale_code = ("датчик запущен кодом БЕЗ отметки версии — он старше "
                          "правила живости. Перезапусти датчик")
        elif disk - running > 1:
            stale_code = (f"датчик крутит код от "
                          f"{dt.datetime.fromtimestamp(running, dt.timezone.utc):%m-%d %H:%M} UTC, "
                          f"а на диске лежит от "
                          f"{dt.datetime.fromtimestamp(disk, dt.timezone.utc):%m-%d %H:%M} UTC. "
                          "Правки в живой процесс не попадают: перезапусти датчик")
    except OSError:
        pass

    silence_rule = hb.get("silence_rule_minutes")
    if stale_code is None and not silence_rule:
        stale_code = ("в датчике не активно правило живости: если все условия "
                      "разоружатся, разбудить тебя будет некому")

    reason = (f"пульс защиты {age:.0f} с назад (лимит {HEARTBEAT_MAX_AGE_S} с)"
              if not alive else stale_code)
    return {"alive": alive and stale_code is None, "age_s": round(age, 1),
            "walls_checked": hb.get("walls_checked"),
            "pending_undelivered": hb.get("pending_undelivered", 0),
            "silence_rule_minutes": silence_rule,
            "stale_code": stale_code,
            "reason": reason}


def _telegram_critical(cfg, fired, *, now):
    """Критические уведомления уходят в телефон: стена, чужая позиция, мёртвый
    датчик — единственное, ради чего стоит будить человека."""
    critical = [r for r in fired if r.get("severity") == "critical"]
    if not critical:
        return
    from scripts.report import critical as _report_critical

    _report_critical(cfg, title=f"Требует внимания: {len(critical)}",
                     details=[r["message"] for r in critical],
                     action="Разберись до начала торговли", now=now)


def build_brief(market, cfg, *, now=None, symbols=None):
    """Сборка брифинга. Ничего не решает и не торгует.

    → {server_day, account, reconciliation, orphans, news, symbols, scorecard,
       open_intent, session, watchdog, allow_new, warnings}
    """
    now = now or dt.datetime.now(UTC)
    sd = Path(state_dir(cfg))
    journal_path = sd / "journal.jsonl"
    symbols = symbols or list(cfg.instruments.whitelist)
    warnings = []

    # --- сверка закрытого за ночь (до чтения журнала: она его дописывает) ---
    try:
        deals = market.history_deals(now - dt.timedelta(days=3))
        # reconcile ждёт СПИСОК сделок на позицию: у одной позиции их минимум
        # две (вход и выход), а при частичках больше, и profit суммируется по
        # всем. Одна сделка вместо списка означала бы, что сверка молча не
        # находит ни одного выхода и не пишет ни одного исхода.
        by_pos = {}
        for d in deals:
            pid = d.get("position_id")
            if pid is not None:
                by_pos.setdefault(str(pid), []).append(d)
        written = reconcile(journal_path, by_pos, market=market,
                            server_utc_offset_hours=cfg.risk.server_utc_offset_hours)
        reconciliation = {"written": written, "deals_seen": len(deals)}
    except Exception as e:  # noqa: BLE001 - сверка не удалась = честно сказать
        reconciliation = {"written": 0, "deals_seen": None, "error": str(e)}
        warnings.append(f"сверка закрытых сделок не выполнена: {e}")

    records = read_records(journal_path)

    # --- счёт и точка отсчёта дня ---
    try:
        acc = market.account_info()
        day_eq, init_bal = _baselines(str(sd), acc["equity"], now=now, cfg=cfg)
        account = {"equity": acc["equity"], "balance": acc["balance"],
                   "day_start_equity": day_eq, "initial_balance": init_bal,
                   "day_pnl_pct": round((acc["equity"] - day_eq) / day_eq * 100, 3)
                   if day_eq else None,
                   "total_pnl_pct": round((acc["equity"] - init_bal) / init_bal * 100, 3)
                   if init_bal else None}
    except Exception as e:  # noqa: BLE001
        account = None
        warnings.append(f"счёт недоступен: {e}")

    # --- чужие позиции ---
    try:
        positions = market.positions()
        orphans = find_orphans(journal_path, positions)
    except Exception as e:  # noqa: BLE001
        positions, orphans = [], []
        warnings.append(f"позиции не прочитаны: {e}")
    for o in orphans:
        warnings.append(f"ЧУЖАЯ ПОЗИЦИЯ {o['ticket']} {o['symbol']} "
                        f"{o['volume']} лот, стоп: {'есть' if o['has_sl'] else 'НЕТ'} — "
                        "новых входов не делать, пока не разобрались")

    # --- новости на сутки вперёд ---
    try:
        doc = load_windows(sd / "news_cache.json", cfg=cfg, now=now)
        horizon = now + dt.timedelta(hours=24)
        windows = [{"from": w["from"].isoformat(), "to": w["to"].isoformat(),
                    "at": w["at"].isoformat(), "title": w["title"],
                    "level": w["level"], "currencies": sorted(w["currencies"])}
                   for w in doc["windows"] if w["at"] <= horizon]
        news = {"windows": windows, "stale": doc["stale"],
                "ambiguous": doc.get("ambiguous", [])}
        if doc["stale"]:
            warnings.append("календарь новостей устарел и не обновился — "
                            "входы будут блокироваться до обновления")
    except Exception as e:  # noqa: BLE001
        news = {"windows": [], "stale": True, "error": str(e)}
        warnings.append(f"календарь не прочитан: {e}")

    # --- ночь по символам ---
    sym = {}
    for s in symbols:
        sym[s] = _symbol_brief(market, cfg, s, now=now)
        if sym[s]["reason"]:
            warnings.append(f"{s}: {sym[s]['reason']}")

    # --- статистика, намерение, сессия, датчик ---
    scorecard = _scorecard_slice(records, cfg, sd)
    intent_path = sd / "open_intent.md"
    open_intent = intent_path.read_text(encoding="utf-8") if intent_path.exists() else None
    session = session_gate(utc_now=now, cfg=cfg)
    watchdog = _watchdog(sd, now=now)
    if not watchdog["alive"]:
        warnings.append(f"датчик пробуждения: {watchdog['reason']}")

    allow_new = bool(session["allow_new"] and watchdog["alive"] and not orphans)

    # Уведомления человеку — здесь, а не «когда-нибудь»: брифинг открывает
    # сеанс и это первая точка, где видно ночное наследство (чужая позиция,
    # мёртвый датчик, пробитая стена). Без этого вызова модуль уведомлений
    # существует, но не срабатывает ни разу.
    try:
        from scripts.notify import scan as _notify_scan

        fired = _notify_scan(market, cfg, now=now)
        for rec in fired:
            warnings.append(f"уведомление [{rec['severity']}]: {rec['message']}")
        _telegram_critical(cfg, fired, now=now)
    except Exception as e:  # noqa: BLE001 - брифинг важнее уведомлений
        warnings.append(f"проверка уведомлений не выполнена: {e}")

    return {
        "generated_utc": now.isoformat(),
        "server_day": server_day_key(utc_now=now,
                                     offset_hours=cfg.risk.server_utc_offset_hours,
                                     reset_hour=cfg.risk.server_day_reset_hour),
        "account": account,
        "reconciliation": reconciliation,
        "orphans": orphans,
        "news": news,
        "symbols": sym,
        "scorecard": scorecard,
        "open_intent": open_intent,
        "session": {"phase": session["phase"], "allow_new": session["allow_new"],
                    "flat_required": session["flat_required"],
                    "reasons": session["reasons"]},
        "watchdog": watchdog,
        "allow_new": allow_new,
        "warnings": warnings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="утренний брифинг перед Лондоном")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "trader.config.json"))
    ap.add_argument("--symbols", default=None,
                    help="через запятую; по умолчанию весь whitelist")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    from trader_lib.mt5_client import live_market

    symbols = a.symbols.split(",") if a.symbols else None
    brief = build_brief(live_market(), cfg, symbols=symbols)
    print(json.dumps(brief, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
