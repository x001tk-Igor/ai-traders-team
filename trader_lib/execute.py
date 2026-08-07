"""Исполнение приказов с гарантией стопа (задача 4.1).

ГЛАВНЫЙ ИНВАРИАНТ: после place_order позиция либо имеет стоп, либо не
существует. Третьего состояния нет. Проверка идёт ПОСЛЕ филла чтением позиции
у брокера, а не по retcode: брокер может принять ордер и отклонить стоп
(быстрый рынок, «invalid stops», требования по дистанции). Не встал со второй
попытки — позиция закрывается по рынку. Риск позиции без стопа неограничен и
не выражается числом (см. exposure.open_risk_usd), поэтому «пусть повисит,
модель разберётся» — не вариант.

МАСКИРОВКА. magic и comment НЕ параметры функций: они жёстко 0 и "" внутри.
Требование владельца счёта — сделка не должна нести следов советника в полях, которые мы
контролируем. Зонд 3.4 (2026-07-26, BTCUSD демо) показал границу возможного:
POSITION_REASON и DEAL_REASON терминал ставит сам по каналу поступления
приказа, и через order_send они не задаются — любой приказ из Python API
помечается EXPERT. владелец счёта принял это как есть (вариант A, 2026-07-26). Связь
сделки с журналом идёт двухфазной записью (задача 4.2), а не маркерами.

ОТКАЗЫ БРОКЕРА РАЗДЕЛЕНЫ НА ДВА КЛАССА, и это не косметика:
  - «цена ушла» (реквота, нет цен) — повтор со СВЕЖЕЙ ценой имеет смысл;
  - всё остальное (стопы не приняты, рынок закрыт, нет денег, автоторговля
    выключена) — повтор ничего не чинит, а прячет понятную причину за тремя
    одинаковыми отказами. Такие возвращаются сразу с человеческим сообщением.
План 4.1 относил 10027 к повторяемым; это сознательное отклонение —
10027 означает «автоторговля выключена в терминале», и три попытки её не
включат.

РЕЖИМ ЗАПОЛНЕНИЯ БЕРЁТСЯ ИЗ МАСКИ СИМВОЛА, а не перебором вслепую: маска и
константы ордера живут в разных нумерациях (SYMBOL_FILLING_FOK=1/IOC=2 против
ORDER_FILLING_FOK=0/IOC=1/RETURN=2). Слепой перебор заканчивается
неподдерживаемым режимом, чья ошибка затирает настоящую причину отказа — этим
уже один раз был испорчен зонд (см. scripts/probe_deal_reason.py).
"""
import math

# Успех: приказ исполнен / ордер размещён.
DONE_RETCODES = (10009, 10008)

# Повторять имеет смысл только это: цена ушла, пока приказ летел.
RETRY_RETCODES = (
    10004,  # REQUOTE — реквота
    10021,  # PRICE_OFF — нет цен для исполнения
    10020,  # PRICE_CHANGED — цена изменилась
)
MAX_ATTEMPTS = 3
MAX_SL_RESETS = 2

# Понятные причины вместо голого номера: сообщение читает модель и человек.
RETCODE_MESSAGES = {
    10027: "Algo Trading выключен в терминале (Сервис → Настройки → Советники) — "
           "включи его, повтор здесь не поможет",
    10018: "рынок закрыт по этому символу",
    10019: "недостаточно средств для этого объёма",
    10016: "брокер не принял уровень стопа (слишком близко к цене или не по шагу)",
    10014: "неверный объём (шаг/минимум/максимум лота)",
    10015: "неверная цена в приказе",
    10017: "торговля запрещена на стороне сервера",
    10030: "неподдерживаемый режим заполнения",
}

# Маска символа → имя режима заполнения ордера. Имена, а не числа: market —
# это протокол (FakeMarket в тестах, MT5 в бою), и константы MetaTrader5
# подставляет адаптер mt5_client, а не этот модуль.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


def _message(retcode, fallback=""):
    return RETCODE_MESSAGES.get(retcode, fallback or f"брокер отказал, retcode={retcode}")


def _fail(error, *, message="", **extra):
    out = {"ok": False, "error": error, "message": message or error}
    out.update(extra)
    return out


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _filling_modes(symbol_info):
    """Поддерживаемые режимы в порядке предпочтения. FOK строже (или весь
    объём, или ничего) — для входа это правильное поведение по умолчанию."""
    mask = int(symbol_info.get("filling_mode") or 0)
    modes = []
    if mask & SYMBOL_FILLING_FOK:
        modes.append("FOK")
    if mask & SYMBOL_FILLING_IOC:
        modes.append("IOC")
    return modes


def _position(market, ticket):
    for p in market.positions():
        if p.get("ticket") == ticket:
            return p
    return None


def _side_of(position):
    return "buy" if position.get("type") == 0 else "sell"


def _price_for(market, symbol, side):
    """Свежая цена под сторону сделки: покупаем по ask, продаём по bid."""
    tick = market.tick(symbol)
    return tick["ask"] if side == "buy" else tick["bid"]


def _sl_is_wrong_side(side, price, sl):
    return sl >= price if side == "buy" else sl <= price


def place_order(market, *, symbol, side, lots, entry, sl, tp=None,
                deviation_points=20):
    """Рыночный вход с гарантией стопа.

    ВНИМАНИЕ: magic и comment намеренно НЕ параметры (см. шапку модуля).
    Не возвращай их в сигнатуру «для гибкости»: единственное допустимое
    значение здесь одно, а параметр создал бы путь его изменить.

    → {ok, ticket, fill_price, slippage_points, sl_verified, sl_reset_attempts,
       attempts, retcode, error, message}
    """
    lots_f, entry_f, sl_f = _num(lots), _num(entry), _num(sl)
    if lots_f is None or lots_f <= 0:
        return _fail("lots_not_positive", message="объём должен быть больше нуля")
    if entry_f is None:
        return _fail("bad_entry", message="не задана цена входа")
    if not sl_f:  # None и 0.0 — одно и то же: стопа нет
        return _fail("sl_required",
                     message="вход без стоп-лосса запрещён конституцией")
    if sl_f == entry_f:
        return _fail("sl_equals_entry", message="стоп совпадает с входом")
    if side not in ("buy", "sell"):
        return _fail("bad_side", message=f"сторона {side!r} не buy/sell")
    if _sl_is_wrong_side(side, entry_f, sl_f):
        return _fail("sl_wrong_side",
                     message=f"стоп {sl_f} не на той стороне от входа {entry_f} для {side}")

    si = market.symbol_info(symbol)
    modes = _filling_modes(si)
    if not modes:
        return _fail("no_filling_mode",
                     message=f"символ не поддерживает ни FOK, ни IOC "
                             f"(маска {si.get('filling_mode')})")

    point = si["point"]
    result, attempts, price = None, 0, entry_f
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        req = {"action": "TRADE_ACTION_DEAL", "symbol": symbol,
               "volume": lots_f, "type": "BUY" if side == "buy" else "SELL",
               "price": price, "sl": sl_f, "tp": float(tp or 0.0),
               "deviation": deviation_points,
               # ↓ два поля маскировки: жёстко, без пути их изменить
               "magic": 0, "comment": "",
               "type_filling": modes[0], "type_time": "GTC"}
        result = market.order_send(req) or {}
        retcode = result.get("retcode")
        if retcode in DONE_RETCODES:
            break
        if retcode not in RETRY_RETCODES:
            return _fail("order_send_rejected", message=_message(retcode,
                         result.get("comment") or ""), retcode=retcode,
                         attempts=attempts)
        # цена ушла — берём свежую и пробуем снова
        price = _price_for(market, symbol, side)
    else:
        return _fail("order_send_failed",
                     message=f"брокер отказал {MAX_ATTEMPTS} раза подряд: "
                             f"{_message(result.get('retcode'))}",
                     retcode=result.get("retcode"), attempts=attempts)

    ticket = result.get("order")
    fill_price = _num(result.get("price")) or price
    # знак: положительное = исполнились ХУЖЕ намеченного
    slip = (fill_price - entry_f) if side == "buy" else (entry_f - fill_price)
    out = {"ok": True, "ticket": ticket, "fill_price": fill_price,
           "slippage_points": round(slip / point, 2), "attempts": attempts,
           "retcode": result.get("retcode"), "sl_verified": False,
           "sl_reset_attempts": 0, "error": None, "message": ""}

    # --- пост-проверка стопа: главный инвариант модуля ---
    for reset in range(MAX_SL_RESETS):
        pos = _position(market, ticket)
        if pos is None:
            # позиция уже закрылась (стоп сработал мгновенно, брокер закрыл) —
            # это не наш случай «висит без стопа»
            out["sl_verified"] = True
            out["note"] = "position_closed_immediately"
            return out
        if _num(pos.get("sl")):
            out["sl_verified"] = True
            return out
        out["sl_reset_attempts"] = reset + 1
        market.order_send({"action": "TRADE_ACTION_SLTP", "symbol": symbol,
                           "position": ticket, "sl": sl_f, "tp": float(tp or 0.0),
                           "magic": 0, "comment": ""})

    # стоп не встал — позиция без стопа существовать не должна
    closed = close_position(market, ticket=ticket)
    if closed.get("ok"):
        return _fail("sl_not_set_position_closed",
                     message="стоп не принят брокером — позиция закрыта по рынку",
                     ticket=ticket, fill_price=fill_price, sl_verified=False,
                     sl_reset_attempts=out["sl_reset_attempts"], attempts=attempts)
    return _fail("unprotected_position_left_open",
                 message="стоп не принят И закрыть не удалось: позиция без стопа "
                         f"осталась открытой ({closed.get('message')}). Ею займётся "
                         "стоп-кран датчика, но модель обязана знать сейчас",
                 ticket=ticket, fill_price=fill_price, sl_verified=False,
                 sl_reset_attempts=out["sl_reset_attempts"], attempts=attempts)


def modify_sl(market, *, ticket, new_sl):
    """Перенос стопа. Разрешён, только если риск НЕ растёт.

    Позиция без стопа — особый случай: там любой стоп есть уменьшение риска, и
    именно этим путём стоп-кран датчика восстанавливает пропавший стоп.
    """
    sl_f = _num(new_sl)
    if not sl_f:
        return _fail("sl_required", message="перенос стопа в ноль — это снятие стопа")
    pos = _position(market, ticket)
    if pos is None:
        return _fail("position_not_found", message=f"позиции {ticket} нет у брокера")

    side = _side_of(pos)
    ref = _num(pos.get("price_current")) or _num(pos.get("price_open"))
    if _sl_is_wrong_side(side, ref, sl_f):
        return _fail("sl_wrong_side",
                     message=f"стоп {sl_f} не на той стороне от цены {ref} для {side}")

    current = _num(pos.get("sl"))
    if current:  # у позиции стоп уже есть — сравниваем риск
        widening = sl_f < current if side == "buy" else sl_f > current
        if widening:
            return _fail("sl_widening_forbidden",
                         message=f"стоп {sl_f} расширяет риск против текущего {current}: "
                                 "риск на сделку уже выдан гейтом и израсходован")

    res = market.order_send({"action": "TRADE_ACTION_SLTP", "symbol": pos["symbol"],
                             "position": ticket, "sl": sl_f,
                             "tp": _num(pos.get("tp")) or 0.0,
                             "magic": 0, "comment": ""}) or {}
    retcode = res.get("retcode")
    if retcode not in DONE_RETCODES:
        return _fail("modify_rejected", message=_message(retcode, res.get("comment") or ""),
                     retcode=retcode)
    return {"ok": True, "ticket": ticket, "sl": sl_f, "retcode": retcode}


def close_partial(market, *, ticket, fraction):
    """Частичное закрытие. Объём приводится к шагу лота ВНИЗ.

    Если закрываемая часть или остаток меньше минимального лота — честный
    отказ, а не «закруглим до минимума»: округление изменило бы решение модели
    (она просила половину, а получила бы всё или ничего).
    """
    f = _num(fraction)
    if f is None or not (0 < f < 1):
        return _fail("bad_fraction", message="доля закрытия должна быть между 0 и 1")
    pos = _position(market, ticket)
    if pos is None:
        return _fail("position_not_found", message=f"позиции {ticket} нет у брокера")

    si = market.symbol_info(pos["symbol"])
    step, vmin = si["volume_step"], si["volume_min"]
    volume = _num(pos["volume"])
    part = math.floor((volume * f) / step) * step
    part = round(part, 8)
    left = round(volume - part, 8)
    if part < vmin or left < vmin:
        return _fail("partial_not_possible",
                     message=f"частичка {part} при остатке {left} не проходит по "
                             f"минимальному лоту {vmin}: решай — закрывать целиком "
                             "или вести целиком",
                     closed_lots=0.0, would_leave=left)

    res = _send_close(market, pos, part)
    retcode = res.get("retcode")
    if retcode not in DONE_RETCODES:
        return _fail("close_rejected", message=_message(retcode, res.get("comment") or ""),
                     retcode=retcode, closed_lots=0.0)
    return {"ok": True, "ticket": ticket, "closed_lots": part, "left_lots": left,
            "price": res.get("price"), "retcode": retcode}


def close_position(market, *, ticket):
    """Закрытие по рынку. Отсутствие позиции — не ошибка: она могла закрыться
    по стопу секунду назад, и стоп-кран зовёт это как раз в такие моменты."""
    pos = _position(market, ticket)
    if pos is None:
        return {"ok": True, "ticket": ticket, "note": "position_already_closed"}
    res = _send_close(market, pos, _num(pos["volume"]))
    retcode = res.get("retcode")
    if retcode not in DONE_RETCODES:
        return _fail("close_rejected", message=_message(retcode, res.get("comment") or ""),
                     retcode=retcode, ticket=ticket)
    return {"ok": True, "ticket": ticket, "closed_lots": _num(pos["volume"]),
            "price": res.get("price"), "retcode": retcode}


def _send_close(market, pos, volume):
    """Встречный приказ по позиции. magic/comment пустые и здесь: выход —
    такая же сделка в истории, как вход."""
    symbol = pos["symbol"]
    side = _side_of(pos)
    si = market.symbol_info(symbol)
    modes = _filling_modes(si)
    tick = market.tick(symbol)
    return market.order_send({
        "action": "TRADE_ACTION_DEAL", "symbol": symbol, "volume": volume,
        "type": "SELL" if side == "buy" else "BUY", "position": pos["ticket"],
        "price": tick["bid"] if side == "buy" else tick["ask"],
        "deviation": 50, "magic": 0, "comment": "",
        "type_filling": modes[0] if modes else "IOC", "type_time": "GTC"}) or {}
