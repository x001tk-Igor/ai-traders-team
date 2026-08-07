import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir       # noqa: E402
from trader_lib.journal import read_records                 # noqa: E402
from trader_lib.score import compute_stats                    # noqa: E402
from trader_lib.scorecard import render_scorecard             # noqa: E402
from trader_lib.workspace import resolve_trader, trader_state_dir    # noqa: E402


def run(cfg, render_scorecard_flag=False, progress_to_target_pct=None, trader=None):
    """progress_to_target_pct — реальный прогресс к цели из снимка счёта
    (scripts/perceive.py → account_snapshot → progress_to_target_pct). Этот
    скрипт видит только конфиг и журнал, не живой счёт MT5 — посчитать его
    здесь самому нечем (sum_R в R-мультиплях и % от equity к цели вообще не
    одна и та же величина, это была бы фиктивная прокси-цифра, а не прогресс).
    Поэтому значение прокидывается СНАРУЖИ (--progress-pct у CLI, из уже
    свежего perceive-снимка того же цикла); по умолчанию None — честное
    «неизвестно» вместо ранее захардкоженного 0.0, которое выглядело как
    реальный нулевой прогресс. render_scorecard рендерит None как «н/д»."""
    sd = trader_state_dir(cfg, trader) if trader else Path(state_dir(cfg))
    records = read_records(sd / "journal.jsonl")
    stats = compute_stats(records, min_n_for_confirmed=cfg.learning.min_n_for_confirmed)
    (sd).mkdir(parents=True, exist_ok=True)
    (sd / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    if render_scorecard_flag:
        md = render_scorecard(stats, progress_to_target_pct=progress_to_target_pct,
                              target_pct=cfg.goal["profit_target_pct"])
        (sd / "scorecard.md").write_text(md, encoding="utf-8")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    ap.add_argument("--render-scorecard", action="store_true")
    ap.add_argument("--progress-pct", type=float, default=None,
                    help="progress_to_target_pct из свежего снимка perceive.py "
                         "(account_snapshot); опущен → scorecard честно покажет "
                         "«н/д» вместо выдуманного числа")
    ap.add_argument("--trader", default=None,
                    help="чьё состояние: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args()
    trader = resolve_trader(a.trader)
    cfg = load_config(a.config)
    s = run(cfg, a.render_scorecard, a.progress_pct, trader=trader)
    print(json.dumps(s["overall"], ensure_ascii=False))
