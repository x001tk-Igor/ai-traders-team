"""Подъём собственного трек-рекорда перед решением.

КАЛИБРОВКА ОТДАЁТСЯ ПО ТЕКУЩЕЙ МОДЕЛИ, А НЕ ОБЩАЯ. Разные модели откалиброваны
по-разному: «заявленные 0.7» от одной и от другой — разные числа, и смешанный
бакет не описывает ни одну (обоснование — в шапке trader_lib/score.py). Скилл
trader-recall прямо велит брать calibration_by_model, а инструмент, которым он
для этого пользуется, до аудита 2026-07-27 возвращал глобальную: документ
говорил одно, код делал другое, и haircut считался бы по чужим данным.

Глобальная калибровка остаётся рядом под отдельным именем — она полезна для
сравнения «я против всех», но дисконтировать по ней уверенность нельзя.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir  # noqa: E402
from trader_lib.model_session import effective as effective_model  # noqa: E402
from trader_lib.workspace import resolve_trader, trader_state_dir    # noqa: E402


def pull(stats, symbol, setups, *, model_id=None):
    """Срез статистики под текущее решение.

    Нет данных по этой модели → calibration=None и явная подсказка, а НЕ
    подстановка глобальной: «данных по тебе нет» и «вот данные по всем» —
    разные ответы, и второй тихо испортил бы haircut.
    """
    by_model = stats.get("calibration_by_model") or {}
    mine = by_model.get(model_id) if model_id else None
    return {
        "model_id": model_id,
        "overall": stats.get("overall"),
        "symbol": stats.get("by_symbol", {}).get(symbol),
        "setups": {s: stats.get("by_setup", {}).get(s) for s in setups},
        # ГЛАВНОЕ поле для haircut — по своей модели
        "calibration": mine,
        "calibration_note": (
            f"калибровка модели {model_id}" if mine else
            "нет данных по этой модели — работай на голом суждении с малым "
            "риском, чужую калибровку не бери"),
        # только для сравнения, НЕ для дисконта уверенности
        "calibration_all_models": stats.get("calibration"),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--setups", default="")
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    ap.add_argument("--trader", default=None,
                    help="чьё состояние: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args()
    trader = resolve_trader(a.trader)
    cfg = load_config(a.config)
    sp = (trader_state_dir(cfg, trader) if trader
          else Path(state_dir(cfg))) / "stats.json"
    stats = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    setups = [s for s in a.setups.split(",") if s]
    # калибровка запрашивается для той модели, которая объявилась в ЭТОМ
    # сеансе. Взять имя из конституции значило бы отдать Sonnet калибровку
    # Opus — то есть ровно тот haircut, от которого этот модуль защищает
    model_id, _profile = effective_model(
        trader_state_dir(cfg, trader) if trader else Path(state_dir(cfg)), cfg)
    print(json.dumps(pull(stats, a.symbol, setups, model_id=model_id),
                     ensure_ascii=False, indent=2))
