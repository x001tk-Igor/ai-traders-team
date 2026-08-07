"""Зонд маскировки (задача 3.4): чем терминал помечает сделку из Python API.

Требование владельца счёта: торговля должна выглядеть ручной дискреционной, а не работой
советника. Поля magic и comment мы контролируем сами (ставим 0 и ""), но
POSITION_REASON у позиции и DEAL_REASON у сделки ставит ТЕРМИНАЛ, и параметром
order_send их не задать.

Развилка по результату:
  reason == CLIENT  → маскировка полная, magic=0/comment="" достаточно;
  reason == EXPERT  → признак остаётся в самой сделке и виден брокеру в отчётах,
                      magic=0 становится косметикой. Это вопрос к владельцу счёта — торговать
                      ли так вообще, — а не к коду. Фазу 4 до его решения не начинать.

Зонд открывает и НЕМЕДЛЕННО закрывает позицию минимальным лотом. Стоимость —
спред. Отказывается работать на неучебном счёте.
"""
import argparse
import json
import sys

import MetaTrader5 as mt5

# Имена констант REASON по их значению — из документации MT5.
POSITION_REASON = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT"}
DEAL_REASON = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP",
               6: "SO", 7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT"}


def _fail(stage, detail):
    print(json.dumps({"ok": False, "stage": stage, "detail": detail},
                     ensure_ascii=False, indent=2))
    mt5.shutdown()
    sys.exit(1)


def main(symbol, allow_real):
    if not mt5.initialize():
        _fail("initialize", str(mt5.last_error()))

    acc = mt5.account_info()
    term = mt5.terminal_info()
    # Сравниваем с КОНСТАНТОЙ модуля, а не с числом: порядок значений
    # (DEMO=0, CONTEST=1, REAL=2) легко перепутать, и такая ошибка в
    # предохранителе опаснее её отсутствия.
    is_demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    if not is_demo and not allow_real:
        _fail("account_check", {
            "trade_mode": acc.trade_mode, "login": acc.login, "server": acc.server,
            "msg": "счёт не демонстрационный; запуск только с явным --allow-real"})
    if not term.trade_allowed:
        _fail("trade_allowed", "в терминале выключен Algo Trading")

    si = mt5.symbol_info(symbol)
    if si is None:
        _fail("symbol_info", f"нет символа {symbol}")
    if not si.visible:
        mt5.symbol_select(symbol, True)
        si = mt5.symbol_info(symbol)

    tick = mt5.symbol_info_tick(symbol)
    lots = si.volume_min

    # Открываем ровно так, как будет открывать боевой код: magic=0, comment="".
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots,
           "type": mt5.ORDER_TYPE_BUY, "price": tick.ask, "deviation": 30,
           "magic": 0, "comment": "", "type_time": mt5.ORDER_TIME_GTC}

    # Режимы берём из МАСКИ символа, а не перебором вслепую: маска и константы
    # живут в разных нумерациях (SYMBOL_FILLING_FOK=1/IOC=2 против
    # ORDER_FILLING_FOK=0/IOC=1/RETURN=2), и слепой перебор заканчивается
    # неподдерживаемым режимом, чья ошибка затирает настоящую причину отказа.
    supported = []
    if si.filling_mode & 1:
        supported.append(mt5.ORDER_FILLING_FOK)
    if si.filling_mode & 2:
        supported.append(mt5.ORDER_FILLING_IOC)
    if not supported:
        _fail("filling_mode", f"символ не поддерживает ни FOK, ни IOC (маска {si.filling_mode})")

    attempts = []
    res = None
    for filling in supported:
        req["type_filling"] = filling
        res = mt5.order_send(req)
        attempts.append({"filling": filling,
                         "retcode": getattr(res, "retcode", None),
                         "comment": getattr(res, "comment", None)})
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            break
    else:
        # Каждую попытку показываем целиком: иначе причина последней попытки
        # выдаёт себя за причину отказа вообще. 10018 = рынок закрыт — это не
        # ошибка кода и не повод чинить filling.
        codes = {a["retcode"] for a in attempts}
        hint = ("рынок закрыт — повторить в торговые часы"
                if mt5.TRADE_RETCODE_MARKET_CLOSED in codes else None)
        _fail("order_send", {"attempts": attempts, "hint": hint,
                             "last_error": str(mt5.last_error())})

    out = {"ok": True, "account": {"login": acc.login, "server": acc.server,
                                   "is_demo": is_demo},
           "sent": {"magic": 0, "comment": "", "lots": lots},
           "order": res.order, "deal": res.deal, "price": res.price}

    pos = [p for p in (mt5.positions_get(symbol=symbol) or [])
           if p.ticket == res.order or p.identifier == res.order]
    if pos:
        p = pos[0]
        out["position"] = {"ticket": p.ticket, "reason_code": p.reason,
                           "reason": POSITION_REASON.get(p.reason, f"?{p.reason}"),
                           "magic_seen": p.magic, "comment_seen": p.comment}

    import datetime as dt
    deals = mt5.history_deals_get(dt.datetime.now() - dt.timedelta(minutes=5),
                                  dt.datetime.now() + dt.timedelta(minutes=5)) or []
    d = [x for x in deals if x.order == res.order]
    if d:
        x = d[0]
        out["deal_record"] = {"ticket": x.ticket, "reason_code": x.reason,
                              "reason": DEAL_REASON.get(x.reason, f"?{x.reason}"),
                              "magic_seen": x.magic, "comment_seen": x.comment}

    # Немедленно закрываем — зонд не оставляет открытой позиции.
    if pos:
        p = pos[0]
        t2 = mt5.symbol_info_tick(symbol)
        creq = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL, "position": p.ticket, "price": t2.bid,
                "deviation": 30, "magic": 0, "comment": "", "type_time": mt5.ORDER_TIME_GTC}
        for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
            creq["type_filling"] = filling
            cres = mt5.order_send(creq)
            if cres is not None and cres.retcode == mt5.TRADE_RETCODE_DONE:
                break
        out["closed"] = {"retcode": getattr(cres, "retcode", None),
                         "price": getattr(cres, "price", None)}
        out["still_open"] = len(mt5.positions_get(symbol=symbol) or [])

    print(json.dumps(out, ensure_ascii=False, indent=2))
    mt5.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--allow-real", action="store_true",
                    help="разрешить запуск на НЕдемонстрационном счёте")
    a = ap.parse_args()
    main(a.symbol, a.allow_real)
