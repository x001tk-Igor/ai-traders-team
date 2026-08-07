"""Подтверждение конституции человеком (задача 8.2).

Отдельный скрипт, а не флаг в торговом контуре — намеренно. Подтверждение
лимитов должно быть отдельным осознанным действием владельца счёта: если бы его можно
было выполнить «попутно» из цикла торговли, защита превратилась бы в
формальность, которую агент проходит сам.

Показывает, ЧТО изменилось, прежде чем просить подтверждения: подтверждать
хэш, не видя разницы, — это подтверждать не глядя.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir                 # noqa: E402
from trader_lib.constitution import (                                # noqa: E402
    GUARDED_BLOCKS,
    HASH_FILE,
    check_config,
    write_ack,
)

UTC = dt.timezone.utc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="показать состояние конституции и подтвердить её")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                           / "config" / "trader.config.json"))
    ap.add_argument("--ack", action="store_true",
                    help="подтвердить текущее состояние (действие человека)")
    ap.add_argument("--note", default=None, help="почему лимиты изменены")
    a = ap.parse_args(argv)

    raw = json.loads(Path(a.config).read_text(encoding="utf-8"))
    cfg = load_config(a.config)
    ack_path = Path(state_dir(cfg)) / HASH_FILE
    verdict = check_config(raw, ack_path)

    print(f"конституция: {'СОВПАДАЕТ' if verdict['ok'] else 'РАСХОЖДЕНИЕ'}")
    print(f"  причина: {verdict['reason']}")
    print(f"  охраняемые блоки: {', '.join(GUARDED_BLOCKS)}")
    print(f"  текущий хэш: {verdict['current']}")
    print(f"  подтверждённый: {verdict['acked']}")
    if not verdict["ok"]:
        print("\nтекущие значения охраняемых блоков:")
        for block in GUARDED_BLOCKS:
            print(f"  {block}: {json.dumps(raw.get(block), ensure_ascii=False)[:300]}")

    if a.ack:
        doc = write_ack(ack_path, config_dict=raw, now=dt.datetime.now(UTC),
                        note=a.note)
        print(f"\nподтверждено: {doc['config_hash']}")
        print(f"файл: {ack_path}")
        return 0
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
