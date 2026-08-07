"""Уведомления владельцу счёта (задача 8.1).

Единственный канал, по которому человек узнаёт о том, что система сама
исправить не может. Два требования тянут в разные стороны, и оба обязательны:

  * НЕ ПРОПУСТИТЬ: молчание при пробитой стене или пропавшем датчике хуже
    лишнего сообщения.
  * НЕ ЗАСПАМИТЬ: двадцать одинаковых строк про один orphan за час — это и есть
    способ, которым перестают читать уведомления, а с ними и важные.

Отсюда дедупликация по паре (тип, ключ) в окне времени, а НЕ по тексту: текст
меняется от снимка к снимку (цены, проценты), событие остаётся тем же. Ключ
выбирает вызывающий: тикет для orphan-позиции, символ для спреда, день для
стены.

СПИСОК ТРИГГЕРОВ ЗАКРЫТ. Опечатка в имени означала бы уведомление, которое не
найдёт ни дедупликация, ни отчёт — поэтому неизвестный тип это ValueError, а не
запись «на всякий случай».

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ: не решает и не торгует. Он читает состояние,
которое уже посчитали другие (гейт, спред-гейт, сверка, статистика), и
превращает его в строки для человека.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.close_watch import find_orphans                        # noqa: E402
from scripts.risk_gate_cli import build_gate_inputs                 # noqa: E402
from trader_lib.config import load_config, state_dir                # noqa: E402
from trader_lib.entry_gate import HEARTBEAT_MAX_AGE_S               # noqa: E402
from trader_lib.journal import read_records                         # noqa: E402
from trader_lib.risk_gate import safe_evaluate_gate                 # noqa: E402

UTC = dt.timezone.utc

TRIGGERS = (
    "ladder_step",                 # риск урезан ступенью лестницы просадки
    "force_flat",                  # стена пробита, всё закрывается
    "spread_anomaly",              # инструмент исключён по спреду
    "data_stale",                  # данные по символу устарели
    "execution_errors",            # повторяющиеся отказы исполнения
    "orphan_position",             # позиция без записи в журнале
    "watchdog_lost",               # датчик молчит — защиты нет
    "tactic_degraded",             # подтверждённая тактика ушла в минус
    "constitution_change_needed",  # модель просит изменить лимиты
)

SEVERITY = {
    "force_flat": "critical", "watchdog_lost": "critical",
    "orphan_position": "critical", "execution_errors": "critical",
    "ladder_step": "warning", "spread_anomaly": "warning",
    "data_stale": "warning", "tactic_degraded": "warning",
    "constitution_change_needed": "warning",
}

DEDUP_MINUTES = 120
# сколько подряд отказов исполнения считается системной проблемой, а не рынком
EXECUTION_ERROR_STREAK = 3
EXECUTION_ERROR_WINDOW_MIN = 60


def read_notifications(path):
    """Уже отправленные уведомления. Битые строки пропускаются: один сбой
    записи не должен отключать канал насовсем."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _recently_sent(records, *, trigger, key, now, window_minutes):
    for rec in reversed(records):
        if rec.get("trigger") != trigger or rec.get("key") != key:
            continue
        try:
            ts = dt.datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        if (now - ts).total_seconds() <= window_minutes * 60:
            return True
    return False


def notify(path, *, trigger, message, key, detail=None, now=None,
           dedup_minutes=DEDUP_MINUTES):
    """Пишет уведомление, если такое же (тип+ключ) не отправлялось недавно.

    Возвращает запись или None, если придушено дедупликацией.
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"неизвестный триггер {trigger!r}; допустимо: {list(TRIGGERS)}")
    now = now or dt.datetime.now(UTC)
    path = Path(path)
    if _recently_sent(read_notifications(path), trigger=trigger, key=str(key),
                      now=now, window_minutes=dedup_minutes):
        return None
    rec = {"ts": now.isoformat(), "trigger": trigger, "key": str(key),
           "severity": SEVERITY.get(trigger, "warning"), "message": message,
           "detail": detail or {}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# --------------------------------------------------------------------------
# сканирование состояния
# --------------------------------------------------------------------------

def _read_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _check_watchdog(sd, now):
    hb = _read_json(Path(sd) / "watch_heartbeat.json")
    if hb is None:
        return ("датчик пробуждения не запущен (нет watch_heartbeat.json) — "
                "модель торгует без стоп-крана либо вообще спит", "missing")
    if not hb.get("walls_checked") or not hb.get("ts"):
        return ("датчик жив, но стена по equity не считается — защиты нет", "blind")
    try:
        age = (now - dt.datetime.fromisoformat(hb["ts"])).total_seconds()
    except (ValueError, TypeError):
        return ("метка времени пульса датчика нечитаема", "unreadable")
    if age > HEARTBEAT_MAX_AGE_S:
        return (f"пульс защиты {age:.0f} с назад (лимит {HEARTBEAT_MAX_AGE_S} с) — "
                "стоп-кран не подтверждает, что стена считается", "stale")
    return None


def _execution_failures(records, *, now):
    """Отказы исполнения стоп-крана за последний час. Одна неудача — рынок; три
    подряд — вопрос к системе или счёту."""
    failures = 0
    for rec in records:
        if rec.get("type") != "alert_event":
            continue
        try:
            ts = dt.datetime.fromisoformat(rec.get("fired_utc") or rec.get("ts"))
        except (TypeError, ValueError):
            continue
        if (now - ts).total_seconds() > EXECUTION_ERROR_WINDOW_MIN * 60:
            continue
        action = rec.get("action") or {}
        if action.get("failed"):
            failures += 1
            continue
        for part in ("close", "modify"):
            result = action.get(part)
            if isinstance(result, dict) and result.get("ok") is False:
                failures += 1
                break
    return failures


def scan(market, cfg, *, now=None):
    """Смотрит состояние и отправляет то, что человек обязан узнать.

    Возвращает список ОТПРАВЛЕННЫХ уведомлений (придушенные дедупликацией в
    список не попадают — повторный скан того же состояния молчит).
    """
    now = now or dt.datetime.now(UTC)
    sd = Path(state_dir(cfg))
    path = sd / "notifications.jsonl"
    day = now.date().isoformat()
    sent = []

    def send(**kw):
        rec = notify(path, now=now, **kw)
        if rec is not None:
            sent.append(rec)

    # --- датчик ---
    watchdog = _check_watchdog(sd, now)
    if watchdog:
        message, key = watchdog
        send(trigger="watchdog_lost", message=message, key=f"{day}:{key}")

    # --- вердикт гейта: стена и ступени лестницы ---
    try:
        records = read_records(sd / "journal.jsonl")
        inputs = build_gate_inputs(market, cfg, records, now=now)
        verdict = safe_evaluate_gate(**inputs)
    except Exception as e:  # noqa: BLE001 - о недоступности гейта тоже надо знать
        verdict, records = None, []
        send(trigger="data_stale", message=f"вердикт риск-гейта не получен: {e}",
             key=f"{day}:gate")

    if verdict:
        if verdict["verdict"] == "FORCE_FLAT":
            send(trigger="force_flat", key=day,
                 message="стена по equity пробита: все позиции закрываются, "
                         f"торговля остановлена. Причины: {'; '.join(verdict['reasons'])}",
                 detail={"blocked_by": verdict.get("blocked_by")})
        elif verdict.get("risk_mult_applied", 1.0) < 1.0:
            send(trigger="ladder_step", key=f"{day}:{verdict.get('binding_term')}",
                 message=f"риск урезан до ×{verdict['risk_mult_applied']}: "
                         f"{'; '.join(verdict['reasons']) or 'ступень лестницы просадки'}",
                 detail={"binding_term": verdict.get("binding_term")})

    # --- чужие позиции ---
    try:
        for orphan in find_orphans(sd / "journal.jsonl", market.positions()):
            send(trigger="orphan_position", key=orphan["ticket"],
                 message=f"позиция {orphan['ticket']} {orphan['symbol']} "
                         f"{orphan['volume']} лот без записи в журнале "
                         f"(стоп: {'есть' if orphan['has_sl'] else 'НЕТ'}) — "
                         "либо чужая рука, либо потерянный след",
                 detail=orphan)
    except Exception as e:  # noqa: BLE001
        send(trigger="data_stale", message=f"позиции не прочитаны: {e}",
             key=f"{day}:positions")

    # --- спред ---
    medians = _read_json(sd / "spread_median.json") or {}
    for symbol, info in (medians.get("excluded") or {}).items():
        send(trigger="spread_anomaly", key=symbol,
             message=f"{symbol} исключён по спреду с {info.get('since')}: "
                     f"×{info.get('ratio')} от медианы {info.get('median')}",
             detail=info)

    # --- отказы исполнения ---
    failures = _execution_failures(records, now=now)
    if failures >= EXECUTION_ERROR_STREAK:
        send(trigger="execution_errors", key=f"{day}:exec",
             message=f"{failures} отказов исполнения за последний час — брокер "
                     "отклоняет приказы стоп-крана, это уже не рынок",
             detail={"failures": failures})

    # --- деградация тактики ---
    stats = _read_json(sd / "stats.json") or {}
    for setup, agg in (stats.get("by_setup") or {}).items():
        if agg.get("insufficient"):
            continue
        if (agg.get("avg_R") or 0) < 0:
            send(trigger="tactic_degraded", key=setup,
                 message=f"тактика {setup} в минусе на достаточной выборке: "
                         f"n={agg['n']} avg_R={agg['avg_R']} WR={agg.get('wr')}",
                 detail=agg)

    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(description="проверить состояние и уведомить")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "trader.config.json"))
    ap.add_argument("--trigger", choices=TRIGGERS, default=None,
                    help="отправить одно уведомление вручную")
    ap.add_argument("--message", default=None)
    ap.add_argument("--key", default=None)
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    path = Path(state_dir(cfg)) / "notifications.jsonl"
    if a.trigger:
        rec = notify(path, trigger=a.trigger, message=a.message or a.trigger,
                     key=a.key or dt.datetime.now(UTC).date().isoformat())
        print(json.dumps(rec, ensure_ascii=False, indent=2) if rec
              else "придушено дедупликацией")
        return 0

    from trader_lib.mt5_client import live_market
    fired = scan(live_market(), cfg)
    print(json.dumps(fired, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
