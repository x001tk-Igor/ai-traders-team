import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.account import account_snapshot          # noqa: E402
from trader_lib.config import load_config, state_dir      # noqa: E402
from trader_lib.features import compute_tf_features        # noqa: E402
from trader_lib.session import server_day_key               # noqa: E402


def _day_baseline(sd, equity, cfg, *, now=None):
    """Точка отсчёта дневной просадки: equity на начало СЕРВЕРНОГО дня брокера
    (задача 5.3). Раньше день считался по UTC-полуночи, и при смещении +3ч
    baseline с 21:00 до 24:00 UTC переписывался текущим equity — то есть
    дневной лимит −3% обнулялся посреди торгового дня брокера."""
    p = Path(sd) / "day_baseline.json"
    now = now or dt.datetime.now(dt.timezone.utc)
    today = server_day_key(utc_now=now, offset_hours=cfg.risk.server_utc_offset_hours,
                           reset_hour=cfg.risk.server_day_reset_hour)
    if p.exists():
        d = json.loads(p.read_text())
        if d.get("day") == today:
            return d["equity"], d["initial_balance"]
    init_path = Path(sd) / "account_init.json"
    init = json.loads(init_path.read_text())["initial_balance"] if init_path.exists() else equity
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"day": today, "equity": equity, "initial_balance": init}))
    return equity, init


# Сколько последних ЗАКРЫТЫХ баров кладём в снимок. Без самих баров модель не
# видит форму рынка: ATR и моментум не отличают плавный откат к уровню от
# свечи-шпильки туда-обратно. Сорок — компромисс с бюджетом контекста.
SNAPSHOT_BARS = 40

# Грубая оценка токенов по длине JSON. Задача — поймать раздувание В РАЗЫ, а не
# посчитать точно: точный счётчик потребовал бы токенизатора конкретной модели,
# а контур обязан работать на любой (Anthropic/Qwen/GLM/Kimi).
CHARS_PER_TOKEN = 3


def _recent_bars(bars, *, digits, count=SNAPSHOT_BARS):
    """Последние закрытые бары компактно: короткие ключи, округление до digits
    инструмента. Пятнадцать знаков после запятой — это байты контекста, а не
    точность."""
    tail = bars.tail(count)
    out = []
    for row in tail.itertuples(index=False):
        out.append({
            "t": str(getattr(row, "time", ""))[:16],
            "o": round(float(row.open), digits), "h": round(float(row.high), digits),
            "l": round(float(row.low), digits), "c": round(float(row.close), digits)})
    return out


def build_snapshot(market, cfg, symbol, timeframes):
    si = market.symbol_info(symbol)
    acc = market.account_info()
    sd = state_dir(cfg)
    day_eq, init_bal = _day_baseline(sd, acc["equity"], cfg)
    P = cfg.perception
    digits = int(si.get("digits") or 2)
    tf = {}
    for t in timeframes:
        bars = market.copy_rates(symbol, t, P.atr_pctile_lookback + 50)
        if P.use_closed_bars_only:
            bars = bars.iloc[:-1]
        features = compute_tf_features(
            bars, point=si["point"], atr_period=P.atr_period,
            momentum_bars=P.momentum_bars, range_bars=P.range_bars,
            ema_fast=P.ema_fast, ema_slow=P.ema_slow,
            atr_pctile_lookback=P.atr_pctile_lookback,
            # смещение брокера обязано доехать до расчёта сессионных окон и
            # VWAP дня: без него «азиатская сессия» съезжает на величину
            # смещения, и снимок этого никак не покажет
            server_utc_offset_hours=cfg.risk.server_utc_offset_hours)
        # Сами бары кладём ТОЛЬКО по ТФ входа (первому в списке). Старший ТФ
        # нужен для контекста тренда, и там достаточно агрегатов: три блока по
        # 40 баров съедают весь бюджет контекста, вытесняя план дня и
        # намерение. Ключ присутствует всегда — форма ответа одна на всех путях.
        features["bars"] = (_recent_bars(bars, digits=digits)
                            if t == timeframes[0] else None)
        tf[t] = features
    acc_snap = account_snapshot(
        acc, day_start_equity=day_eq, initial_balance=init_bal,
        profit_target_pct=cfg.goal["profit_target_pct"],
        daily_limit_pct=cfg.risk.daily_loss_limit_pct,
        total_limit_pct=cfg.risk.total_loss_limit_pct, positions=market.positions())

    snap = {"symbol": symbol, "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tf": tf, "account": acc_snap, "warnings": []}

    # Бюджет контекста считаем ПО ГОТОВОМУ снимку и кладём результат внутрь
    # него же: перерасход обязан быть виден здесь, а не выясняться на слабой
    # модели обрезанным контекстом.
    #
    # Два прохода, потому что измерение самоссылочно: поля с размером сами
    # занимают место. Первый проход даёт размер полезной нагрузки, второй —
    # итоговый, уже с этими полями. Расхождение возможно только в разряде
    # самого числа (пара символов) и на решение о бюджете не влияет.
    budget = cfg.perception.snapshot_token_budget
    snap["size_chars"] = 0
    snap["est_tokens"] = 0
    snap["budget_exceeded"] = False
    size = len(json.dumps(snap, ensure_ascii=False))
    snap["size_chars"] = size
    snap["est_tokens"] = size // CHARS_PER_TOKEN
    snap["budget_exceeded"] = snap["est_tokens"] > budget
    if snap["budget_exceeded"]:
        snap["warnings"].append(
            f"снимок превысил бюджет контекста: ~{snap['est_tokens']} токенов при "
            f"пороге {budget}. Убери лишний ТФ или символ — иначе на слабой модели "
            "он вытеснит план дня и открытое намерение")
    return snap


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", default="M5,H1")
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    a = ap.parse_args()
    from trader_lib.mt5_client import live_market
    cfg = load_config(a.config)
    snap = build_snapshot(live_market(), cfg, a.symbol, a.tf.split(","))
    print(json.dumps(snap, ensure_ascii=False, indent=2))
