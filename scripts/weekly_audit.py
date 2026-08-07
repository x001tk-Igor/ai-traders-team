"""Недельный аудит (задача 8.3).

Отвечает на вопросы, которые не видны за один день: тактика деградирует или
просто была плохая неделя; издержки растут; режим рынка сменился и старые сетапы
перестали работать; какая модель ведёт себя лучше, если их было несколько.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ФЛАГ В ДНЕВНОМ РАЗБОРЕ. Дневной разбор считает по
одному серверному дню и отвечает «что я сделала сегодня». Недельный смотрит
динамику: медиану издержек, смену режимов, кандидатов в карантин. Разные окна и
разные вопросы; склеивать их значило бы получить отчёт, который не читают
целиком ни разу.

ЧЕГО ЗДЕСЬ НЕТ: решений. Кандидат в карантин — это кандидат, статус меняет
trader-reflect осознанно. Автоматически понижать тактику по недельной выборке
нельзя: неделя — это 5–15 сделок, и одна плохая серия отправила бы в карантин
рабочий сетап.
"""
import argparse
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir                 # noqa: E402
from trader_lib.journal import read_records                          # noqa: E402
from trader_lib.scorecard import fmt_or_na                           # noqa: E402

UTC = dt.timezone.utc

WEEK_DAYS = 7
# сколько сделок за неделю нужно, чтобы говорить о деградации тактики, а не о
# полосе: меньше — это шум, и карантин по нему выключил бы рабочий сетап
QUARANTINE_MIN_N = 5

# Пороги хода в прибыль, на которых метод наращивания позиции добавлял бы объём.
# Отсюда берётся ответ на вопрос, ради которого решение о методе отложено на
# неделю (docs/plan_team.md): сколько сделок вообще ДОХОДИТ до долива. Если их
# единицы — обсуждать нечего независимо от того, как хорош метод в теории.
ACCEL_LEVELS = (0.5, 1.0, 2.0)


def _parse(value):
    try:
        ts = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _week_rows(records, *, now):
    """Пары decision+outcome, закрытые за последние 7 дней."""
    since = now - dt.timedelta(days=WEEK_DAYS)
    decisions = {r["trade_id"]: r for r in records if r.get("type") == "decision"}
    rows = []
    for rec in records:
        if rec.get("type") != "outcome":
            continue
        ts = _parse(rec.get("close_ts"))
        if ts is None or ts < since:
            continue
        dec = decisions.get(rec["trade_id"], {})
        rows.append({**dec, "R": rec.get("R") or 0.0, "profit": rec.get("profit") or 0.0,
                     # None здесь ОБЯЗАН доехать как None: ноль означал бы
                     # «никуда не ходила», и неизмеренная сделка испортила бы
                     # долю дошедших до порога, притворившись измеренной
                     "mfe_R": rec.get("mfe_R"), "mae_R": rec.get("mae_R")})
    return rows


def _agg(rows):
    if not rows:
        return {"closed": 0, "sum_R": 0.0, "wr": None, "pnl_usd": 0.0}
    Rs = [r["R"] for r in rows]
    return {"closed": len(rows), "sum_R": round(sum(Rs), 3),
            "wr": round(sum(1 for x in Rs if x > 0) / len(Rs), 3),
            "pnl_usd": round(sum(r.get("profit") or 0.0 for r in rows), 2)}


def _excursion(rows):
    """Куда сделки ходили, прежде чем закрыться.

    Даёт то, чего не видно по R закрытия: сделка, дошедшая до +1.5R и вернувшаяся
    к стопу, и сделка, сразу пошедшая против, дают один и тот же −1.0R, но
    говорят противоположное о качестве входа.

    `given_back` — из дошедших до порога закрылись в ноль или минус. Это и есть
    вся экономика наращивания: метод осмыслен ровно настолько, насколько велика
    разница между «дошло» и «дошло и удержало».
    """
    measured = [r for r in rows if r.get("mfe_R") is not None]
    reached, given_back = {}, {}
    for lvl in ACCEL_LEVELS:
        hit = [r for r in measured if r["mfe_R"] >= lvl]
        reached[str(lvl)] = len(hit)
        given_back[str(lvl)] = sum(1 for r in hit if (r.get("R") or 0.0) <= 0)
    mfes = [r["mfe_R"] for r in measured]
    maes = [r["mae_R"] for r in measured if r.get("mae_R") is not None]
    return {
        "measured": len(measured),
        "unmeasured": len(rows) - len(measured),
        "reached": reached,
        "given_back": given_back,
        "median_mfe_R": round(statistics.median(mfes), 3) if mfes else None,
        "median_mae_R": round(statistics.median(maes), 3) if maes else None,
    }


def _by_key(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[r.get(key) or "unknown"].append(r)
    return {k: _agg(v) for k, v in groups.items()}


def build_weekly(cfg, *, now=None):
    """Недельный срез. Возвращает данные + готовый markdown."""
    now = now or dt.datetime.now(UTC)
    sd = Path(state_dir(cfg))
    rows = _week_rows(read_records(sd / "journal.jsonl"), now=now)

    by_setup = _by_key(rows, "setup_type")
    winners = [{"setup": k, **v} for k, v in by_setup.items() if (v["sum_R"] or 0) > 0]
    losers = [{"setup": k, **v} for k, v in by_setup.items() if (v["sum_R"] or 0) < 0]
    winners.sort(key=lambda x: -x["sum_R"])
    losers.sort(key=lambda x: x["sum_R"])

    costs = [r.get("costs_R") for r in rows if r.get("costs_R") is not None]
    costs_block = {
        "n": len(costs),
        "median_R": round(statistics.median(costs), 4) if costs else None,
        "max_R": round(max(costs), 4) if costs else None,
        "over_limit": sum(1 for c in costs if c > cfg.risk.max_costs_R),
        "limit": cfg.risk.max_costs_R,
    }

    by_model = _by_key(rows, "model_id")
    unplanned_rows = [r for r in rows if r.get("planned") is False]

    quarantine = [{"setup": k, **v} for k, v in by_setup.items()
                  if v["closed"] >= QUARANTINE_MIN_N and (v["sum_R"] or 0) < 0]

    data = {
        "generated_utc": now.isoformat(),
        "week": {"from": (now - dt.timedelta(days=WEEK_DAYS)).date().isoformat(),
                 "to": now.date().isoformat()},
        "trades": _agg(rows),
        "winners": winners,
        "losers": losers,
        "costs": costs_block,
        "regimes": _by_key(rows, "regime"),
        "sessions": _by_key(rows, "session_phase"),
        "by_model": by_model,
        "several_models": len(by_model) > 1,
        "quarantine_candidates": quarantine,
        "unplanned": {**_agg(unplanned_rows), "n": len(unplanned_rows)},
        "excursion": _excursion(rows),
    }
    data["markdown"] = _render(data, cfg)
    return data


def _render(d, cfg):
    t = d["trades"]
    lines = [
        f"# Недельный аудит · {d['week']['from']} → {d['week']['to']}",
        "",
        f"- Закрыто сделок: **{t['closed']}**  ·  ΣR: **{fmt_or_na(t['sum_R'])}**  ·  "
        f"WR: {fmt_or_na(t['wr'])}  ·  PnL: {fmt_or_na(t['pnl_usd'])}$",
        "",
        "## В плюсе",
    ]
    lines += ([f"- **{w['setup']}**: ΣR={w['sum_R']} n={w['closed']} WR={fmt_or_na(w['wr'])}"
               for w in d["winners"]] or ["- нет"])
    lines += ["", "## В минусе"]
    lines += ([f"- **{x['setup']}**: ΣR={x['sum_R']} n={x['closed']} WR={fmt_or_na(x['wr'])}"
               for x in d["losers"]] or ["- нет"])

    c = d["costs"]
    lines += ["", "## Издержки",
              f"- медиана {fmt_or_na(c['median_R'])}R  ·  максимум {fmt_or_na(c['max_R'])}R  ·  "
              f"выше предела {c['limit']}R: {c['over_limit']} сделок"]

    lines += ["", "## Режимы недели"]
    lines += ([f"- **{k}**: ΣR={v['sum_R']} n={v['closed']}"
               for k, v in d["regimes"].items()] or ["- нет данных"])

    lines += ["", "## Кандидаты в карантин (решает reflect, не этот отчёт)"]
    lines += ([f"- **{q['setup']}**: ΣR={q['sum_R']} на n={q['closed']}"
               for q in d["quarantine_candidates"]] or ["- нет"])

    e = d["excursion"]
    lines += ["", "## Ход сделок (MFE/MAE)"]
    if not e["measured"]:
        lines += [f"- не измерено ни одной сделки из {e['measured'] + e['unmeasured']}"]
    else:
        lines += [f"- измерено {e['measured']} из {e['measured'] + e['unmeasured']}"
                  f"  ·  медиана MFE {fmt_or_na(e['median_mfe_R'])}R"
                  f"  ·  медиана MAE {fmt_or_na(e['median_mae_R'])}R"]
        for lvl in ACCEL_LEVELS:
            k = str(lvl)
            n = e["reached"][k]
            lines += [f"- дошло до +{lvl}R: **{n}**"
                      + (f", из них закрылось в ноль или минус: {e['given_back'][k]}"
                         if n else "")]
        lines += ["", "> Решение о методе наращивания принимается по этим числам "
                  "(критерии — docs/plan_team.md). Отчёт их СЧИТАЕТ, но не решает."]

    u = d["unplanned"]
    lines += ["", "## Импровизация",
              f"- внеплановых входов {u['n']}, ΣR={fmt_or_na(u['sum_R'])} "
              f"(лимит в день: {cfg.risk.max_unplanned_trades_per_day})"]

    if d["several_models"]:
        lines += ["", "## Модели"]
        lines += [f"- **{k}**: ΣR={v['sum_R']} n={v['closed']} WR={fmt_or_na(v['wr'])}"
                  for k, v in d["by_model"].items()]

    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="недельный аудит")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                           / "config" / "trader.config.json"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    data = build_weekly(cfg)
    if a.json:
        print(json.dumps({k: v for k, v in data.items() if k != "markdown"},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(data["markdown"])
        if a.save:
            out = Path(state_dir(cfg)) / "reviews"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"week-{data['week']['to']}.md").write_text(data["markdown"],
                                                               encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
