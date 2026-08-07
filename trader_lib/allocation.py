"""Мандаты директора на торговый день (Ф4).

ЗАЧЕМ ФАЙЛ, А НЕ ЗАПРОС К ДИРЕКТОРУ. Окно входа схлопывается за секунды —
2026-07-31 фейд-сигнал успел отработать сам, пока скрипт ловил нормализацию
спреда. Спрашивать директора в момент отправки ордера означало бы гарантированно
пропускать входы. Поэтому его решение компилируется в файл ДО открытия, а гейт
читает файл детерминированно и мгновенно. Тот же шов, что во всём контуре:
модель выбирает, код исполняет.

ЧТО РАЗДАЁТСЯ: какие инструменты трейдер сегодня торгует, активен ли он вообще
и какую долю дневного бюджета риска может израсходовать. Механизм НЕ раздаётся
— он постоянная специализация трейдера, а не сменная роль: статистика сетапа
копится по механизму, и ежедневная перетасовка не дала бы n≥20 никогда.

ГЛАВНЫЙ ИНВАРИАНТ: аллокация умеет только ОГРАНИЧИВАТЬ. Директор не может
выдать риск больше того, что разрешила конституция, — только меньше.
Оркестратор, способный поднять лимит, был бы способом обойти защиту, ради
которой он и построен.

ОТСУТСТВИЕ ФАЙЛА НЕ ОСТАНАВЛИВАЕТ ТОРГОВЛЮ. Директор мог не отработать, а
одиночный режим вообще не знает про мандаты — там риск держат остальные
проверки гейта. Зато УСТАРЕВШИЙ файл отвергается: вчерашние инструменты
назначались под вчерашнюю структуру рынка, и работать по ним — то же самое,
что торговать по вчерашнему календарю новостей.
"""
import json
from pathlib import Path

EMPTY = {"server_day": None, "traders": {}, "valid": True, "problems": [],
         "present": False}

SHARE_SUM_TOLERANCE = 1.0 + 1e-9


def load_allocation(path):
    """Документ мандатов с диска. Отсутствие или битый файл — не ошибка:
    вернётся пустой документ, который никого не ограничивает.

    Сумма долей проверяется здесь же: доли, дающие больше единицы, означали бы
    право команды израсходовать больше дневного бюджета счёта. Такой файл
    помечается невалидным целиком, а не «поправляется» — молча
    отнормировать чужие числа значило бы подменить решение директора своим.
    """
    p = Path(path)
    if not p.exists():
        return dict(EMPTY)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - битый файл равнозначен отсутствию
        return dict(EMPTY, problems=["файл аллокации не прочитан"], valid=False)

    traders = doc.get("traders") or {}
    problems = []
    total = 0.0
    for name, item in traders.items():
        try:
            total += float(item.get("risk_share") or 0.0)
        except (TypeError, ValueError):
            problems.append(f"{name}: risk_share не число")
    if total > SHARE_SUM_TOLERANCE:
        problems.append(
            f"сумма долей риска {total:.2f} больше единицы — команда получила бы "
            "право израсходовать больше дневного бюджета счёта")

    return {"server_day": doc.get("server_day"),
            "written_by": doc.get("written_by"),
            "written_utc": doc.get("written_utc"),
            "traders": traders,
            "problems": problems,
            "valid": not problems,
            "present": True}


def mandate_state(allocation, *, trader, symbol, now=None, server_day=None):
    """Вправе ли этот трейдер входить по этому инструменту.

    → {allowed, reason, instruments}
    """
    if trader is None:
        return {"allowed": True, "reason": "одиночный режим: мандаты не применяются",
                "instruments": None}
    if not (allocation or {}).get("present"):
        return {"allowed": True,
                "reason": "аллокация на сегодня не выдана — мандат не проверялся",
                "instruments": None}
    if not allocation.get("valid"):
        return {"allowed": False,
                "reason": "; ".join(allocation.get("problems") or ["аллокация невалидна"]),
                "instruments": None}

    if server_day and allocation.get("server_day") and \
            allocation["server_day"] != server_day:
        return {"allowed": False,
                "reason": (f"мандат устарел: выдан на {allocation['server_day']}, "
                           f"сегодня {server_day} — инструменты назначались под "
                           "вчерашнюю структуру рынка"),
                "instruments": None}

    item = (allocation.get("traders") or {}).get(trader)
    if item is None:
        return {"allowed": False,
                "reason": f"трейдер {trader} не заведён в аллокации на сегодня",
                "instruments": None}
    if not item.get("active", True):
        return {"allowed": False,
                "reason": f"трейдер {trader} сегодня не активен по решению директора",
                "instruments": list(item.get("instruments") or [])}

    instruments = list(item.get("instruments") or [])
    if symbol not in instruments:
        return {"allowed": False,
                "reason": (f"{symbol} вне мандата {trader} на сегодня "
                           f"({', '.join(instruments) or 'список пуст'})"),
                "instruments": instruments}
    return {"allowed": True, "reason": f"{symbol} в мандате {trader}",
            "instruments": instruments}


def events_quota(allocation, trader, *, total):
    """Сколько событий в сутки полагается этому трейдеру (Ф5).

    Дневной лимит событий — свойство ПОДПИСКИ, а не трейдера: он один на всю
    команду. Без деления первый же разговорчивый трейдер выест его целиком, и
    остальные оглохнут до конца дня, ничего об этом не узнав.

    Без явной квоты бюджет делится поровну, но НЕ весь: знаменатель на единицу
    больше числа трейдеров, и остаток служит резервом. Директорские эскалации
    и стоп-кран не должны упираться в то, что трейдеры выбрали лимит подчистую.
    """
    if trader is None or not (allocation or {}).get("present"):
        return total
    traders = allocation.get("traders") or {}
    item = traders.get(trader)
    if item is None:
        return 0
    explicit = item.get("events_quota")
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            pass
    return total // (len(traders) + 1)


def risk_cap_usd(allocation, *, trader, constitution_max, daily_budget,
                 spent_today=0.0):
    """Сколько этот трейдер может рискнуть в одной сделке прямо сейчас.

    Считается как минимум из трёх величин: разрешение конституции, доля
    дневного бюджета и её неизрасходованный остаток. Аллокация только
    ограничивает — поднять конституционный максимум она не может ни при каких
    долях (см. инвариант в шапке модуля).
    """
    if trader is None or not (allocation or {}).get("present"):
        return constitution_max
    item = (allocation.get("traders") or {}).get(trader)
    if item is None or not allocation.get("valid"):
        return 0.0
    try:
        share = float(item.get("risk_share") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    remaining = max(0.0, share * float(daily_budget) - float(spent_today or 0.0))
    return min(float(constitution_max), remaining)


# Имена трейдеров, данные владельцем счёта 2026-08-03.
#
# ПОЧЕМУ ИМЯ ОТДЕЛЬНО ОТ ИДЕНТИФИКАТОРА, а не вместо него. Идентификатор
# (`trend`, `fade`, `range`) — это путь на диске, ключ в раздаче, поле в каждой
# записи журнала и в каждом алерте. Он говорит, ЧТО ДЕЛАЕТ КОД: строка
# `traders/range/alerts.json` в логе читается без словаря, а `traders/Оррин/...`
# потребовала бы держать соответствие в голове при каждом разборе.
#
# Имя же адресовано человеку и живёт в канале и отчётах. Это не компромисс: у
# двух сущностей разные читатели и разные требования. Переименование id обошлось
# бы переносом трёх папок состояния с журналами и тридцатью условиями живого
# датчика — риск без выигрыша.
#
# Направленность сохранена В САМОМ ОТОБРАЖЕНИИ по прямому требованию: имя без
# механизма заставляло бы вспоминать, кто из них кто, ровно в тот момент, когда
# читаешь тревожное сообщение и вспоминать некогда.
MECHANISM_WORDS = {"trend": "тренд", "fade": "фейд", "range": "диапазон"}


def display_name(allocation, trader):
    """Как трейдер зовётся для человека: «Вэйран · тренд».

    Имя берётся из раздачи (поле `display_name`), механизм — из таблицы выше или
    из самого идентификатора. Нет ни того, ни другого → возвращается id: пустое
    место в отчёте хуже некрасивого, а выдумывать имя на лету значит получить
    в канале два разных обозначения одного трейдера.
    """
    if not trader:
        return "—"
    rec = ((allocation or {}).get("traders") or {}).get(trader) or {}
    name = rec.get("display_name")
    word = MECHANISM_WORDS.get(trader, trader)
    if not name:
        return trader
    return f"{name} · {word}"
