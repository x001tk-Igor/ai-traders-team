"""Директорский цикл: что считает и проверяет КОД (Ф7).

РАЗДЕЛЕНИЕ ТРУДА. Директор — модель, и решения принадлежат ей: какой режим она
видит на инструменте, кому что дать, как поделить бюджет. Код здесь делает
ровно две вещи, которые модели делать не следует:

  СЧИТАЕТ — режимные числа и экономику таймфреймов. Это арифметика, и
  вычислять её заново каждую сессию значило бы платить токенами за то, что
  детерминировано.

  ПРОВЕРЯЕТ решение на связность. Не из недоверия: ошибка директора ТИХА. Две
  пары из одного кластера, розданные разным трейдерам, выглядят как
  диверсификация ровно до дня, когда обе пойдут в одну сторону. Кластерный
  потолок в гейте поймает это на втором входе, но не выпустить такой мандат
  дешевле, чем ловить последствия.

ЧЕГО ЗДЕСЬ НЕТ. Выбора инструментов, назначения режимов и раздачи долей — это
суждение директора. Код, назначающий тактику, нарушил бы шов, на котором стоит
весь контур: код держит риск, модель выбирает тактику.
"""
import datetime as dt
from pathlib import Path

import numpy as np

from trader_lib.clusters import cluster_of
from trader_lib.news import load_windows, symbol_currencies

UTC = dt.timezone.utc

SCAN_TIMEFRAMES = ("M5", "M15", "H1")
SCAN_BARS = 300
ATR_PERIOD = 14

# Сколько ATR нужно стопу, чтобы стоять ВНЕ шума таймфрейма. Урок 2026-07-27
# (стоил $37): стоп внутри размаха не защищает, а назначает время выхода.
NOISE_ATR_MULT = 1.5

# Какую долю лимита издержек разрешено занимать при ЧЕСТНОМ стопе. Касание
# лимита не годится: спред расширяется в самый неподходящий момент — на золоте
# за неделю 27–31.07 он уходил до ×9.8 от медианы. Запас в 20% не спасает от
# такого выброса, но отсекает таймфреймы, где нормальная работа идёт впритык.
VIABLE_COSTS_FRACTION = 0.8

# Во сколько раз живой спред должен превысить свою медиану, чтобы счесть рынок
# закрытым или неликвидным. Выходные у этого брокера дают ×4 по золоту.
STALE_SPREAD_MULT = 2.0

# Доля дневного бюджета событий, которую нельзя раздавать трейдерам: остаток
# держит директорские эскалации и стоп-кран.
EVENTS_RESERVE_FRACTION = 0.15


def _no_network():
    """Календарь по сети обновляет цикл восприятия (bootstrap_env), а не
    директорский разбор: сетевой таймаут не имеет права задержать открытие
    дня. Отсутствие свежего кэша здесь честно означает «предупреждать не о
    чем», и это видно в выдаче."""
    raise RuntimeError("директор не обновляет календарь по сети")


def _spread_vs_median(market, symbol, info):
    """Во сколько раз живой спред шире своей же медианы по барам. None —
    сравнить не с чем."""
    try:
        bars = market.copy_rates(symbol, "M5", 3000)
        values = np.asarray(bars["spread"], float)
        values = values[values > 0]
        if values.size == 0:
            return None
        median = float(np.median(values))
        live = float(info["spread"])
        return round(live / median, 2) if median else None
    except Exception:  # noqa: BLE001 - нет истории спреда: сравнение не сделано
        return None


def _atr(bars, period=ATR_PERIOD):
    high = np.asarray(bars["high"], float)
    low = np.asarray(bars["low"], float)
    close = np.asarray(bars["close"], float)
    if len(close) < period + 2:
        return None
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))
    value = float(np.mean(tr[-period:]))
    return value or None


def scan_instruments(market, cfg, *, now=None):
    """Числа по каждому инструменту whitelist: ATR по ТФ и окупаемость ТФ.

    Окупаемость считается по ЧЕСТНОМУ стопу, а не по минимальному.

    ДЕФЕКТ, НАЙДЕННЫЙ В ЭТОЙ ЖЕ ФУНКЦИИ 2026-08-01. Первая версия мерила
    «существует ли стоп, укладывающийся в лимит издержек» — и отвечала «да»
    там, где такой стоп стоял бы ВНУТРИ шума таймфрейма, то есть не защищал бы
    вовсе (урок 27.07, стоил $37). Честный стоп обязан удовлетворять обоим
    условиям сразу: `max(минимум по издержкам, 1.5 ATR за шум)`. Только после
    этого имеет смысл спрашивать, сколько он стоит.

    Годным считается ТФ, где издержки при честном стопе занимают не весь лимит,
    а его долю (VIABLE_COSTS_FRACTION): работать впритык к пределу означает
    вылетать за него при первом же расширении спреда.

    ЧТО ЭТА ФУНКЦИЯ НЕ РЕШАЕТ — на каком ТФ торговать. Выбор зависит не только
    от экономики: одинаковый ТФ по всем инструментам позволяет механизму копить
    n≥20 через весь кластер, а бюджет событий ограничивает частоту. Это
    суждение директора; здесь только числа, на которых оно принимается.

    Сломанный символ не исчезает из выдачи, а получает `reason`: пропасть молча
    он не имеет права — директор решил бы, что инструмента не существует.
    """
    now = now or dt.datetime.now(UTC)
    rows = []
    for symbol in cfg.instruments.whitelist:
        row = {"symbol": symbol, "tf": {}, "spread_usd": None,
               "spread_vs_median": None, "stale_market": False, "reason": None}
        try:
            info = market.symbol_info(symbol)
            row["spread_usd"] = float(info["spread"]) * float(info["point"])
        except Exception as e:  # noqa: BLE001 - символ недоступен: назвать причину
            row["reason"] = f"символ не прочитан: {e!r}"
            rows.append(row)
            continue

        # ЗАКРЫТЫЙ РЫНОК ДАЁТ НЕРЕПРЕЗЕНТАТИВНЫЕ ЧИСЛА. 2026-08-01 (суббота)
        # живой спред золота был 79 против будничной медианы 19 — вчетверо.
        # Скан по таким данным объявил бы негодными четыре инструмента из семи,
        # и директор снял бы их с понедельника без причины. Сказать об этом
        # обязан скан: молча выдать пессимистичную картину как факт хуже, чем
        # признать, что смотришь не на тот рынок.
        row["spread_vs_median"] = _spread_vs_median(market, symbol, info)
        if row["spread_vs_median"] and row["spread_vs_median"] >= STALE_SPREAD_MULT:
            row["stale_market"] = True
            row["reason"] = (
                f"живой спред ×{row['spread_vs_median']:.1f} к своей медиане — "
                "рынок закрыт или неликвиден, числа нерепрезентативны")

        limit = cfg.risk.max_costs_R
        cost_min = row["spread_usd"] / limit if limit else None
        for tf in SCAN_TIMEFRAMES:
            try:
                bars = market.copy_rates(symbol, tf, SCAN_BARS)
                atr = _atr(bars)
            except Exception as e:  # noqa: BLE001 - один ТФ не рушит остальные
                row["tf"][tf] = {"atr": None, "honest_stop": None, "costs_R": None,
                                 "viable": False, "reason": repr(e)}
                continue
            if not atr or not cost_min:
                row["tf"][tf] = {"atr": atr, "honest_stop": None, "costs_R": None,
                                 "viable": False,
                                 "reason": "нет ATR или лимита издержек"}
                continue
            honest = max(cost_min, NOISE_ATR_MULT * atr)
            costs = row["spread_usd"] / honest
            row["tf"][tf] = {"atr": round(atr, 5),
                             "honest_stop": round(honest, 5),
                             "honest_stop_atr": round(honest / atr, 2),
                             "costs_R": round(costs, 4),
                             "viable": costs <= limit * VIABLE_COSTS_FRACTION,
                             "reason": None}
        if not any(v.get("viable") for v in row["tf"].values()):
            row["reason"] = row["reason"] or "ни один таймфрейм не окупает спред"
        rows.append(row)
    return rows


def news_alerts(cfg, sd, *, now=None, minutes_before=30):
    """Предупреждения о новостных окнах на сегодня — по одному на событие.

    ЗАЧЕМ ЭТО ДИРЕКТОРУ, А НЕ КАЖДОМУ ТРЕЙДЕРУ. Календарь — факт о МИРЕ,
    одинаковый для всех, как карта кластеров и медианы спреда. Трое, следящие
    за одним календарём, делают тройную работу и тратят тройной бюджет событий
    на одно и то же.

    ЗАЧЕМ ВООБЩЕ ПРЕДУПРЕЖДАТЬ. Сегодня трейдер узнаёт о новости в момент
    ОТКАЗА гейта — то есть когда уже прочитал рынок, собрал тезис и потратил на
    него решение. Предупреждение за полчаса превращает отказ в осознанное
    ожидание.

    Окна берутся из кэша, который наполняет цикл восприятия; в сеть эта
    функция не ходит.
    """
    now = now or dt.datetime.now(UTC)
    try:
        doc = load_windows(Path(sd) / "news_cache.json", cfg=cfg, now=now,
                           loader=_no_network)
    except Exception:  # noqa: BLE001 - нет календаря: предупреждать не о чем
        return []

    out = []
    for i, w in enumerate(doc.get("windows") or []):
        at = w.get("at")
        if at is None or at <= now:
            continue        # прошедшее событие будильником быть не может
        currencies = sorted(w.get("currencies") or [])
        out.append({
            "id": f"news-{i}-{at.strftime('%H%M')}",
            "type": "news_window_opens",
            "minutes_before": minutes_before,
            "once": True,
            "priority": "normal",
            "note": (f"{w.get('title')} ({', '.join(currencies)}, {w.get('level')}) "
                     f"в {at.strftime('%H:%M')} UTC — окно блокировки открывается. "
                     "Предупредить трейдеров, чьи инструменты задеты; вход в окне "
                     "гейт отклонит сам."),
            "_state": {"armed": True, "last_fired_utc": None, "remember": None},
        })
    return out


def affected_traders(allocation, currencies):
    """Кого из команды задевает событие по этим валютам.

    Адресность не косметика: трейдер по кроссу без доллара, разбуженный
    долларовой новостью, тратит событие и не получает решения — а бюджет
    пробуждений общий.

    Снятый с торговли не предупреждается: он сегодня не входит.
    """
    wanted = {c.upper() for c in (currencies or [])}
    out = []
    for name, item in ((allocation or {}).get("traders") or {}).items():
        if not item.get("active", True):
            continue
        for symbol in item.get("instruments") or []:
            if symbol_currencies(symbol) and symbol_currencies(symbol) & wanted:
                out.append(name)
                break
    return sorted(out)


def validate_allocation(allocation, *, cfg, clusters, now=None):
    """Связно ли решение директора. → {ok, problems}

    Проверяется то, что можно проверить арифметикой, и ничего сверх: код не
    судит, ХОРОШО ли выбраны инструменты — только не противоречит ли раздача
    сама себе и конституции.
    """
    problems = []
    traders = (allocation or {}).get("traders") or {}
    whitelist = set(cfg.instruments.whitelist)

    seen_symbol = {}
    seen_cluster = {}
    total_share = 0.0
    total_quota = 0

    for name, item in traders.items():
        try:
            total_share += float(item.get("risk_share") or 0.0)
        except (TypeError, ValueError):
            problems.append(f"{name}: risk_share не число")
        try:
            total_quota += int(item.get("events_quota") or 0)
        except (TypeError, ValueError):
            problems.append(f"{name}: events_quota не число")

        # Снятый с торговли фактор риска не занимает: он сегодня не торгует.
        if not item.get("active", True):
            continue

        for symbol in item.get("instruments") or []:
            if symbol not in whitelist:
                problems.append(
                    f"{name}: {symbol} нет в instruments.whitelist конституции — "
                    "список инструментов решает человек, не оркестратор")
                continue
            if symbol in seen_symbol:
                problems.append(
                    f"{symbol} роздан дважды: {seen_symbol[symbol]} и {name}")
                continue
            seen_symbol[symbol] = name

            cid = cluster_of(symbol, clusters)
            if cid is None:
                problems.append(
                    f"{name}: {symbol} нет в карте кластеров — про его корреляции "
                    "ничего не известно, а незнание не равно независимости")
                continue
            if cid in seen_cluster and seen_cluster[cid][0] != name:
                other_trader, other_symbol = seen_cluster[cid]
                problems.append(
                    f"{name} ({symbol}) и {other_trader} ({other_symbol}) в одном "
                    "кластере риска — это одна ставка на двоих, а выглядит как "
                    "диверсификация")
                continue
            seen_cluster.setdefault(cid, (name, symbol))

    if total_share > 1.0 + 1e-9:
        problems.append(
            f"сумма долей риска {total_share:.2f} больше единицы — команда получила "
            "бы право израсходовать больше дневного бюджета счёта")

    budget = cfg.alerts.max_events_per_day
    reserve = int(budget * EVENTS_RESERVE_FRACTION)
    if total_quota > budget - reserve:
        problems.append(
            f"квоты событий {total_quota} из {budget} не оставляют резерв "
            f"(нужно минимум {reserve}) — директорским эскалациям и стоп-крану "
            "не хватит места")

    return {"ok": not problems, "problems": problems,
            "checked_utc": (now or dt.datetime.now(UTC)).isoformat()}
