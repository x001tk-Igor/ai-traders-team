"""Открытие сеанса: модель объявляет себя и сообщает владельцу счёта (2026-07-27).

ПЕРВАЯ КОМАНДА КАЖДОЙ СЕССИИ. До неё контур считает идентичность
неподтверждённой и предвходовой гейт не пускает в рынок: неверный model_id тихо
портит калибровку и статистику by_model, а разделить перемешанные записи задним
числом нечем.

Модель указывает СВОЁ имя — то, под которым она работает в этой сессии Claude
Code. Не имя из конфига: конфиг человек правит руками и забывает, а модель
знает про себя точно. Объявление привязывается к CLAUDE_CODE_SESSION_ID, и на
другом ПК (другая сессия) оно автоматически недействительно.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trader_lib.config import load_config, state_dir                 # noqa: E402
from trader_lib.eventlog import log                                  # noqa: E402
from trader_lib.workspace import resolve_trader, trader_state_dir    # noqa: E402
from trader_lib.model_session import (                               # noqa: E402
    KNOWN_PROFILES,
    current,
    current_session_id,
    declare,
)

UTC = dt.timezone.utc


def open_session(cfg, *, model_id, profile, now=None, note=None, sender=None,
                 market=None, trader=None):
    """Объявляет модель, пишет в лог и отправляет открытие сеанса в Telegram.

    В команде объявление ЛИЧНОЕ. Три трейдера с разными моделями, пишущие в
    один model_session.json, затирали бы друг друга: последний выигрывал бы, а
    проверка идентичности докладывала бы неверную модель для двоих. Калибровка
    уверенности считается по этому полю — смешав её, разделить записи задним
    числом уже нечем.
    """
    now = now or dt.datetime.now(UTC)
    sd = trader_state_dir(cfg, trader) if trader else Path(state_dir(cfg))
    doc = declare(sd, model_id=model_id, profile=profile, now=now, note=note)
    log(sd, "SESSION", f"модель объявлена: {doc['model_id']} ({doc['profile']})",
        session=(doc.get("session_id") or "вне Claude Code"))

    equity = day_pnl = wall_left = None
    if market is not None:
        try:
            acc = market.account_info()
            equity = acc["equity"]
            base = json.loads((sd / "day_baseline.json").read_text(encoding="utf-8"))
            day_pnl = (equity - base["equity"]) / base["equity"] * 100
            wall_left = cfg.risk.daily_loss_limit_pct + day_pnl
        except Exception:  # noqa: BLE001 - счёт недоступен: сообщим без чисел
            pass

    from scripts.report import session_opened

    tg = session_opened(cfg, trader=trader, equity=equity, day_pnl_pct=day_pnl,
                        wall_left_pct=wall_left,
                        hypotheses=[f"модель: {doc['model_id']} ({doc['profile']})"],
                        armed_alerts=0, now=now, sender=sender)
    return {"declaration": doc, "telegram": tg}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="объявить модель этого сеанса и открыть сессию")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[1]
                                            / "config" / "trader.config.json"))
    # не required: --status обязан работать без объявления, иначе «посмотреть,
    # кто объявлен» невозможно без нового объявления
    ap.add_argument("--model", default=None,
                    help="имя модели, которая работает СЕЙЧАС (например claude-opus-5)")
    ap.add_argument("--profile", default="strong", choices=list(KNOWN_PROFILES),
                    help="strong — полные права, weak — урезанные (по приёмке)")
    ap.add_argument("--note", default=None)
    ap.add_argument("--status", action="store_true",
                    help="только показать, кто объявлен сейчас")
    ap.add_argument("--trader", default=None,
                    help="чьё состояние: имя трейдера команды; без него — одиночный режим")
    a = ap.parse_args(argv)
    trader = resolve_trader(a.trader)

    cfg = load_config(a.config)
    sd = trader_state_dir(cfg, trader)

    if a.status:
        st = current(sd, cfg)
        print(json.dumps({**st, "session_id": current_session_id()},
                         ensure_ascii=False, indent=2))
        return 0 if st["ok"] else 1

    if not a.model:
        ap.error("нужно --model (кто работает сейчас) или --status (кто объявлен)")

    from trader_lib.mt5_client import live_market
    try:
        market = live_market()
    except Exception:  # noqa: BLE001 - терминал недоступен: объявиться всё равно надо
        market = None

    res = open_session(cfg, model_id=a.model, profile=a.profile, note=a.note,
                       market=market, trader=trader)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
