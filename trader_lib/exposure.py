"""Открытый риск и нетто-валютная экспозиция по открытым позициям.

Чистые функции: ничего не читают из MT5/файлов/часов — все данные приходят
аргументами (positions, symbol_info_map), всё тестируется офлайн. risk_gate
(задача 1.5) принимает готовые числа параметрами — этот модуль он НЕ
импортирует.

ОГРАНИЧЕНИЕ (осознанно принято, как и в size_position.py): стоимость пункта
считается как trade_contract_size * point — это верно для инструментов,
котируемых в валюте счёта (XAUUSD, EURUSD на USD-счёте), и НЕВЕРНО для
JPY-пар и большинства кроссов (нужен кросс-курс quote→account_currency,
которого здесь нет). net_currency_exposure дополнительно предполагает
account_currency == "USD": обе ноги валютной пары берут величину risk_usd
как есть, без конвертации. Решение задачи кросс-курсов — не в этой задаче.
"""


def open_risk_usd(positions, symbol_info_map) -> dict:
    """Фактический риск до стоп-лосса по каждой открытой позиции и сумма.

    positions       — список позиций брокера (список dict), как их отдаёт
                      mt5_client.positions()
    symbol_info_map — {symbol: symbol_info_dict}, где symbol_info_dict —
                      то, что отдаёт mt5_client.symbol_info(symbol)

    Возвращает:
      {'total_usd': float,          # сумма риска по защищённым позициям
       'unprotected': int,          # сколько позиций без стоп-лосса
       'per_ticket': {ticket: {'risk_usd': float|None, 'protected': bool}},
       'reason': str}

    Позиция БЕЗ стоп-лосса → risk_usd=None, protected=False, счётчик
    unprotected +1. Её риск НЕИЗВЕСТЕН и НЕ входит в total_usd: вызывающий
    (risk_gate) обязан трактовать unprotected > 0 как исчерпание бюджета
    и запретить новые входы (fail-closed).

    Стоп-лосс уже за точкой входа в сторону прибыли (безубыток или лучше)
    → risk_usd = 0.0, protected=True.
    """
    per_ticket = {}
    total_usd = 0.0
    unprotected = 0

    for p in positions:
        ticket = p["ticket"]
        symbol = p["symbol"]
        si = symbol_info_map[symbol]  # KeyError громко — символ вне карты это ошибка вызывающего

        # MT5 отдаёт sl=0.0, когда стопа нет (не None, не отсутствующий ключ).
        # Оба варианта — отсутствие ключа и sl=0.0 — трактуем как «нет стопа».
        sl = p.get("sl", 0.0)
        if not sl:
            per_ticket[ticket] = {"risk_usd": None, "protected": False}
            unprotected += 1
            continue

        entry = p["price_open"]
        volume = p["volume"]
        ptype = p["type"]
        point = si["point"]
        contract = si["trade_contract_size"]
        value_per_point_per_lot = contract * point  # см. ограничение в шапке модуля

        if ptype == 0:  # BUY
            distance = entry - sl
        elif ptype == 1:  # SELL
            distance = sl - entry
        else:
            raise ValueError(f"неизвестный тип позиции {ptype!r} (ticket {ticket})")

        if distance <= 0:
            # SL в безубытке или лучше (за входом в сторону прибыли) — риска нет
            risk = 0.0
        else:
            points = distance / point
            risk = points * value_per_point_per_lot * volume

        risk = round(risk, 2)
        per_ticket[ticket] = {"risk_usd": risk, "protected": True}
        total_usd += risk

    total_usd = round(total_usd, 2)
    reason = f"{len(positions)} позиций, риск ${total_usd:.2f}, без стопа: {unprotected}"
    return {"total_usd": total_usd, "unprotected": unprotected,
            "per_ticket": per_ticket, "reason": reason}


def net_currency_exposure(positions, symbol_info_map, *, account_currency="USD") -> dict:
    """Нетто-экспозиция по валютам: раскладывает каждую пару на две ноги.

    XAUUSD buy  → {'XAU': +risk, 'USD': -risk}
    EURUSD buy  → {'EUR': +risk, 'USD': -risk}
    USDJPY buy  → {'USD': +risk, 'JPY': -risk}
    (sell — знаки наоборот)

    Величина ноги = риск позиции в USD (из open_risk_usd). Позиции без SL
    в нетто не учитываются, но возвращаются отдельным счётчиком.

    Символ обязан быть ровно 6 букв БАЗА+КОТИР. (напр. EURUSD). Суффиксы
    брокера (напр. XAUUSD.m) и любая другая длина не поддержаны — падает
    ValueError, а не угадывает раскладку.

    account_currency поддерживает только "USD" (значение по умолчанию):
    величина каждой ноги берётся как risk_usd без конвертации. Конверсия
    кросс-курсов не реализована — это осознанно вне объёма модуля (см.
    ограничение в шапке файла). Любое другое значение → ValueError, а не
    тихо неверная экспозиция.

    Возвращает:
      {'net': {currency: float},    # только валюты с ненулевым нетто
       'unprotected': int,
       'reason': str}
    """
    if account_currency != "USD":
        raise ValueError(
            f"account_currency={account_currency!r} не поддержан: конвертация "
            "кросс-курсов не реализована, только счета в USD (см. ограничение "
            "в шапке trader_lib/exposure.py)")

    risk = open_risk_usd(positions, symbol_info_map)
    net = {}

    for p in positions:
        ticket = p["ticket"]
        entry = risk["per_ticket"][ticket]
        if not entry["protected"]:
            continue  # риск неизвестен — в нетто не участвует (см. open_risk_usd)

        magnitude = entry["risk_usd"]
        if magnitude == 0.0:
            continue  # безубыток — нулевая нога, вклада в нетто нет

        symbol = p["symbol"]
        if len(symbol) != 6:
            raise ValueError(
                f"не могу разобрать символ {symbol!r} на валютные ноги "
                "(ожидается ровно 6 букв БАЗАКОТИР., напр. EURUSD; суффиксы "
                "брокера вроде '.m' и нестандартная длина не поддержаны)")
        base, quote = symbol[:3], symbol[3:6]

        sign = 1.0 if p["type"] == 0 else -1.0  # BUY: база+/котир.-; SELL — наоборот
        net[base] = net.get(base, 0.0) + sign * magnitude
        net[quote] = net.get(quote, 0.0) - sign * magnitude

    net = {ccy: round(v, 2) for ccy, v in net.items() if abs(v) > 1e-9}
    reason = f"нетто по {len(net)} валютам, без стопа: {risk['unprotected']}"
    return {"net": net, "unprotected": risk["unprotected"], "reason": reason}
