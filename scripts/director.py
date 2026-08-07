"""Инструменты директора-оркестратора (Ф7).

ТРИ КОМАНДЫ, И НИ ОДНА НЕ ПРИНИМАЕТ ТОРГОВЫХ РЕШЕНИЙ:

  scan     — числа по каждому инструменту whitelist: ATR по трём ТФ, честный
             стоп и издержки при нём. То, на чём директор решает, кому что дать.
  validate — связность уже принятого решения: не роздан ли один кластер двоим,
             не превышена ли сумма долей, остался ли резерв бюджета событий.
  review   — сводка дня по всем трейдерам в одну таблицу.

ПОЧЕМУ ВАЛИДАЦИЯ ОТДЕЛЬНОЙ КОМАНДОЙ, А НЕ ВНУТРИ ЗАПИСИ. Мандаты пишет модель —
своим суждением, в своём формате. Проверять их обязан отдельный шаг, который
можно запустить и после ручной правки файла, и на чужой аллокации. Ошибка
директора тиха: две пары из одного кластера выглядят диверсификацией ровно до
дня, когда обе пойдут в одну сторону.

КОД ВОЗВРАТА У validate НЕНУЛЕВОЙ ПРИ ПРОБЛЕМАХ — чтобы «проверил, но не
заметил» не выглядело как успех в скрипте запуска.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.clusters import load_clusters                       # noqa: E402
from trader_lib.config import load_config, state_dir                # noqa: E402
from trader_lib.director import scan_instruments, validate_allocation  # noqa: E402
from trader_lib.mt5_client import live_market                       # noqa: E402
from trader_lib.workspace import list_traders, workspace_path       # noqa: E402

UTC = dt.timezone.utc

DEFAULT_CONFIG = str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json")


def cmd_scan(cfg, _a):
    rows = scan_instruments(live_market(), cfg, now=dt.datetime.now(UTC))
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_validate(cfg, _a):
    sd = Path(state_dir(cfg))
    alloc_path = sd / "allocation.json"
    if not alloc_path.exists():
        print(json.dumps({"ok": False,
                          "problems": [f"нет файла аллокации: {alloc_path}"]},
                         ensure_ascii=False, indent=2))
        return 1
    allocation = json.loads(alloc_path.read_text(encoding="utf-8"))
    res = validate_allocation(allocation, cfg=cfg,
                              clusters=load_clusters(sd / "clusters.json"),
                              now=dt.datetime.now(UTC))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


def cmd_review(cfg, _a):
    """Сводка дня по всем трейдерам: числа рядом, а не в трёх отчётах.

    Кросс-разбор — та часть работы директора, ради которой команда вообще
    имеет смысл: увидеть, кто жёг бюджет впустую и чей механизм сегодня не
    работал, можно только сравнив трейдеров между собой.
    """
    from scripts.review import build_review

    rows = []
    for name in list_traders(cfg):
        try:
            rows.append({"trader": name,
                         **_flat(build_review(cfg, trader=name))})
        except Exception as e:  # noqa: BLE001 - один трейдер не рушит сводку
            rows.append({"trader": name, "reason": repr(e)})
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


def _flat(review):
    t = review.get("trades") or {}
    a = review.get("alert_efficiency") or {}
    return {"closed": t.get("closed"), "sum_R": t.get("sum_R"),
            "pnl_usd": t.get("pnl_usd"), "wr": t.get("wr"),
            "wakeups": a.get("delivered"), "usefulness": a.get("usefulness"),
            "noisy": [x.get("alert_id") for x in (a.get("noisy_alerts") or [])]}


COMMANDS = {"scan": cmd_scan, "validate": cmd_validate, "review": cmd_review}


def main(argv=None):
    ap = argparse.ArgumentParser(description="инструменты директора команды")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    a = ap.parse_args(argv)
    return COMMANDS[a.command](load_config(a.config), a)


if __name__ == "__main__":
    sys.exit(main())
