"""Рассуждение директора в телеграм-канал.

    python scripts/say.py --title "Событие range-vol-decay" \
        --fact "atr_pctile 0.11 при уровне 0.85" \
        --fact "спред вернулся к 20 за 90 секунд" \
        --decision "маршрутизирую трейдеру, вход не рекомендую" <<'EOF'
    Тело рассуждения. Читается со stdin, если он не пустой.
    EOF

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ. Заведён по прямой просьбе владельца счёта 2026-08-03:
он видел решения трейдеров (`wake`) и сделки (`enter`/`exit`), но не видел, на
каких числах директор маршрутизировал событие и почему. Без готового вызова
рассуждение каждый раз обрастало бы разовой питон-обвязкой — и через день
перестало бы отправляться вовсе.

РАЗДЕЛЕНИЕ ФАКТОВ И РЕШЕНИЯ намеренное. `--fact` это то, что можно перепроверить
(числа, замеры), `--decision` — то, что было выбором. Через неделю по каналу
должно быть видно, где данные, а где суждение: иначе надзор невозможен, потому
что ошибку в данных и ошибку в рассуждении лечат по-разному.

В журнал сделок это НЕ пишется: там живут только решения по деньгам, и
разбавлять их комментариями значило бы испортить метрику пробуждений.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config                          # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="рассуждение директора в телеграм")
    ap.add_argument("--config",
                    default=str(Path(__file__).resolve().parents[1] / "config" / "trader.config.json"))
    ap.add_argument("--title", required=True, help="заголовок одной строкой")
    ap.add_argument("--body", default=None, help="тело; без него читается stdin")
    ap.add_argument("--fact", action="append", default=[],
                    help="проверяемый факт (число, замер); можно повторять")
    ap.add_argument("--decision", default=None, help="что решено и почему")
    ap.add_argument("--trader", default=None,
                    help="о ком речь: id трейдера (trend/fade/range). В канал "
                         "уйдёт человеческое имя с механизмом — «Вэйран · тренд»")
    a = ap.parse_args(argv)

    body = a.body
    if body is None and not sys.stdin.isatty():
        body = sys.stdin.read().strip() or None

    from scripts.report import director

    cfg = load_config(a.config)
    title = a.title
    if a.trader:
        # имя разворачивается ЗДЕСЬ, а не в вызывающем: иначе один и тот же
        # трейдер появлялся бы в канале то как `range`, то как «Оррин», и
        # связать сообщения между собой было бы нечем
        import pathlib as _pl

        from trader_lib.allocation import display_name, load_allocation
        from trader_lib.config import state_dir
        try:
            alloc = load_allocation(_pl.Path(state_dir(cfg)) / "allocation.json")
            title = f"{display_name(alloc, a.trader)} — {title}"
        except Exception:  # noqa: BLE001 - без раздачи просто нет имени
            title = f"{a.trader} — {title}"

    res = director(cfg, title=title, body=body,
                   facts=a.fact, decision=a.decision)
    # печатаем результат: «не доставлено» обязано быть видно вызывающему, иначе
    # канал молчит, а директор уверен, что рассказал
    print(f"telegram: {res.get('reason')}"
          + (f" (в очереди {res['queued']})" if res.get("queued") else ""))
    return 0 if res.get("sent") else 1


if __name__ == "__main__":
    sys.exit(main())
