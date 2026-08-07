"""Контракт alerts.json и вычисление срабатываний — «алертная петля».

Модель работает по подписке (не по API): её внимание дорого и ограничено.
Вместо непрерывного цикла модель в конце каждой сессии записывает
alerts.json — условия, по которым её следует разбудить. Датчик (задача
3.2, НЕ этот модуль) крутится раз в секунду, читает alerts.json, зовёт
evaluate() и печатает строку только при срабатывании. Пока условия молчат
— расход нулевой.

ЭТОТ МОДУЛЬ НЕ ВОДИТЕЛЬ, А ДАТЧИК. evaluate() не содержит ни одного
торгового суждения — только сравнение чисел из ctx с порогами, которые
задала модель в alerts.json. Пороги, уровни, типы — исключительно от
модели; код не решает, что "плохо" или "хорошо", он решает только "число
X относительно порога Y — истина или ложь".

ЧИСТОТА. load_alerts читает файл (I/O, но не решение), write_alerts_atomic
пишет файл. evaluate() — ЧИСТАЯ функция: не читает MT5/файлы/часы, ничего
не пишет в журнал. Всё приходит аргументами (alerts, ctx, now, budget), всё
тестируется офлайн.

СОСТОЯНИЕ РАЗОРУЖЕНИЯ ЖИВЁТ ВНУТРИ КАЖДОГО АЛЕРТА, В ПОЛЕ `_state`
(зарезервированный префикс, не часть словаря модели). evaluate() получает
alerts (то, что вернул load_alerts) и возвращает updated_alerts с обновлённым
_state по каждому алерту; вызывающий (датчик, задача 3.2) обязан вызвать
write_alerts_atomic(path, updated_alerts) после каждого тика, чтобы
разоружение/перевооружение/память о предыдущем значении (для trend_flips,
gate_verdict_changes, session_phase_changes) пережили перезапуск датчика —
без этого шага состояние живёт только в памяти процесса и теряется при
падении/перезапуске. Когда МОДЕЛЬ перезаписывает alerts.json свежим набором
условий (конец следующей сессии), _state естественно отсутствует у новых
записей — это НАМЕРЕННЫЙ сброс: новый цикл наблюдения начинается с чистого
листа, а не наследует состояние прошлых гипотез.

РАЗВИЛКА once vs rearm_after_minutes (см. docs/alerts_schema.md за полным
обоснованием). Срабатывание разоружает алерт (state.armed=False), ЕСЛИ
задан once=true ИЛИ задан rearm_after_minutes (то есть таймер перевооружения
без once тоже подразумевает разоружение — иначе он бессмысленен: нечего
перевооружать, если алерт и так остаётся вооружённым). once=true БЕЗ
rearm_after_minutes — разоружение постоянное (до перезаписи файла моделью).
rearm_after_minutes — сколько бы ни было once, по истечении N минут после
last_fired_utc алерт снова вооружается. Ни once, ни rearm_after_minutes не
заданы → алерт не разоружается вовсе и УЧАСТВУЕТ В ПРОВЕРКЕ УСЛОВИЯ на
каждом тике, пока условие истинно. Единственное, что тогда сдерживает его от
срабатывания на каждом тике — событийный бюджет (см. event_budget): для
normal это дневной лимит и min_seconds_between_events, для critical — свой,
короткий интервал min_seconds_between_critical_events (дневной лимит на
critical НЕ действует), и для обоих — абсолютный потолок max_events_per_minute,
который не обходит никто. Такой алерт — режим "напоминай, пока не разрулено",
а НЕ "разбуди один раз": модель, ставящая его без once/rearm, получит
повторные пробуждения с частотой, заданной интервалом её яруса, а не одно
событие. См. docs/alerts_schema.md за разбором инцидента, который это
уточнение чинит (critical без интервала = десятки срабатываний в минуту =
авто-останов Monitor).

ГАЛЛЮЦИНАЦИИ ЗАПРЕЩЕНЫ. Если поле в ctx отсутствует или равно None там, где
условие требует число, — алерт НЕ срабатывает и попадает в updated_alerts
["skipped"] с причиной. evaluate() никогда не угадывает и не подставляет
правдоподобное значение вместо отсутствующего.
"""
import datetime as dt
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

# Обязательные поля каждого типа алерта (кроме универсальных id/type/once/
# rearm_after_minutes/priority/note/symbol-для-репортинга). Алерт известного
# типа без обязательного поля — НЕ крашит evaluate: он уходит в skipped с
# причиной (модель может ошибиться в JSON, это её ввод, а не конфиг кода).
ALERT_TYPE_FIELDS = {
    "price_above": ("symbol", "level"),
    "price_below": ("symbol", "level"),
    "price_touch": ("symbol", "level", "tolerance_atr"),
    "atr_pctile_above": ("symbol", "level"),
    "atr_pctile_below": ("symbol", "level"),
    "trend_flips": ("symbol", "tf"),
    "position_R_reaches": ("ticket", "level"),
    "position_R_drops_to": ("ticket", "level"),
    "position_time_elapsed": ("ticket", "minutes", "min_progress_R"),
    "news_window_opens": ("minutes_before",),
    "spread_anomaly": ("symbol", "mult"),
    "spread_normalizes": ("symbol", "mult"),
    "gap": ("symbol",),
    "sl_jumped": ("ticket",),
    "data_stale": ("symbol",),
    "gate_verdict_changes": (),
    "session_phase_changes": (),
    "time_at_utc": ("at",),
    "silence_timeout": ("minutes",),
}

ALERT_TYPES = tuple(ALERT_TYPE_FIELDS)

PRIORITIES = ("normal", "critical")
_DEFAULT_PRIORITY = "normal"


# --------------------------------------------------------------------------
# load_alerts / write_alerts_atomic
# --------------------------------------------------------------------------

def _empty_alerts() -> dict:
    """Пустой набор — форма файла, но alerts=[]. Возвращается и при
    отсутствующем файле, и при глобально истёкшем expires_utc (см. шапку
    load_alerts): в обоих случаях датчику нечего вооружать."""
    return {"version": 1, "written_by": None, "written_utc": None,
            "expires_utc": None, "alerts": []}


def _parse_dt(value):
    """ISO-8601 → tz-aware datetime (UTC), включая суффикс 'Z'. Наивное время
    трактуется как UTC (сенсорный контур целиком в UTC, гадать другую
    таймзону не из чего)."""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def load_alerts(path, *, now) -> dict:
    """Читает alerts.json. Отсутствующий файл → пустой набор, НЕ исключение:
    датчик обязан работать до того, как модель впервые поставила алерты.

    Если верхнеуровневый expires_utc в прошлом относительно now — ВЕСЬ файл
    отбрасывается (возвращается пустой набор), а не только "просроченные"
    отдельные алерты: expires_utc — это разовая метка "модель обещала
    вернуться и переставить условия до этого момента", а не срок жизни
    каждого условия по отдельности (per-алертного expires в контракте нет).
    Отсутствующий/пустой expires_utc — алерты не истекают.

    Malformed JSON НЕ перехватывается (исключение долетает до вызывающего):
    это не ожидаемое стационарное состояние (в отличие от отсутствующего
    файла), а признак порчи контракта — атомарная запись (write_alerts_
    atomic) существует именно для того, чтобы такого не происходило на
    штатном пути; тихо проглатывать это значило бы прятать реальный баг.
    """
    p = Path(path)
    if not p.exists():
        return _empty_alerts()

    data = json.loads(p.read_text(encoding="utf-8"))

    expires = data.get("expires_utc")
    if expires:
        if _parse_dt(expires) <= now:
            return _empty_alerts()

    data.setdefault("alerts", [])
    return data


def write_alerts_atomic(path, data) -> None:
    """Запись через временный файл в той же директории + os.replace (атомарно
    и на POSIX, и на Windows — в отличие от os.rename, os.replace не падает,
    если целевой файл уже существует). Датчик крутится раз в секунду и не
    должен прочитать половину файла, пока идёт запись.

    data обязан быть JSON-сериализуемым как есть (все временные метки —
    ISO-строки, не datetime) — то же соглашение, что и у остального пакета
    (journal.py собирает записи со строковым ts до записи на диск)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# event_budget
# --------------------------------------------------------------------------

def _cfg_get(cfg_alerts, key):
    """Доступ к ключу cfg.alerts по имени — и для dict, и для dataclass
    Alerts (config.py). Индексация напрямую (без .get с дефолтом): опечатка
    в конфиге обязана падать громко, а не тихо ослаблять лимит (тот же
    принцип, что и _limit_picker в risk_gate.py)."""
    if isinstance(cfg_alerts, Mapping):
        return cfg_alerts[key]
    return getattr(cfg_alerts, key)


def event_budget(events_today, cfg_alerts, *, now, last_event_ts,
                 last_critical_event_ts, recent_event_ts) -> dict:
    """Три независимых ограничения событийного бюджета. Защищают Monitor
    (задача 3.2) от авто-останова при переборе алертов и подписку модели —
    от сжигания лимита частыми пробуждениями.

    last_critical_event_ts И recent_event_ts — ОБЯЗАТЕЛЬНЫЕ keyword-only
    параметры, БЕЗ дефолтов. Это намеренно, не недосмотр — см. инцидент:
    коллега вызвал event_budget(...) без них при проверке фикса critical-
    бюджета и получил ровно тот дефект, который фикс должен был закрыть (10
    срабатываний за 10 секунд), потому что прежние дефолты (None и ())
    молча трактовались как "критических событий ещё не было" / "за минуту
    событий не было" — то есть забывчивость вызывающего читалась как
    "ограничивать нечего", ПРЯМО ПРОТИВОПОЛОЖНО принципу всего пакета (гейт
    при ошибке — HALT_NEW, журнал отказывается писать неполную запись,
    датчик обязан молчать при нехватке данных, а не считать её нулём).
    Вызывающий здесь будет ровно один — датчик задачи 3.2, и он ОБЯЗАН вести
    все три вида состояния (последнее normal-событие, последнее critical-
    событие, все события за минуту) и передавать их каждый раз. Без
    дефолтов неполный вызов — TypeError на первом же прогоне, а не тихо
    отключённая защита; НЕ возвращай дефолты обратно "для удобства" — это
    восстановит ровно тот класс дефекта (инвариант, живущий в договорённости
    между модулями, а не в проверке), который эта правка закрывает. Подробнее
    — docs/alerts_schema.md, раздел "Три вида состояния для event_budget —
    обязанность вызывающего, не дефолт".

    ТРИ ЯРУСА, А НЕ ОДИН. Ранняя версия этого модуля пускала critical в обход
    ЛЮБОГО бюджета целиком, включая минимальный интервал — зонд на реальном
    конфиге показал: critical-алерт без once/rearm на аномалии, которая
    держится минутами (спред, пробитая стена), при опросе раз в секунду даёт
    ДЕСЯТКИ срабатываний в минуту, и именно в этот момент Monitor
    останавливается от переизбытка событий — контур пробуждения умирает
    ровно тогда, когда модель нужнее всего. "critical должен будить всегда"
    верно по намерению, но "всегда" — это "не пропустить ни одного важного
    момента", а не "каждую секунду, пока условие не сброшено".

      1. normal: дневной лимit (max_events_per_day) И обычный интервал
         (min_seconds_between_events) — оба от последнего NORMAL-события.
      2. critical: ТОЛЬКО свой интервал (min_seconds_between_critical_events,
         короче обычного — критическое нельзя откладывать на минуту), от
         последнего CRITICAL-события. Дневной лимит НЕ применяется —
         критическое событие важнее квоты, это единственное, что critical
         обходит.
      3. hard cap (max_events_per_minute) — абсолютный потолок, который НЕ
         обходит НИКТО, включая critical. Он защищает не квоту модели, а сам
         механизм пробуждения: несколько РАЗНЫХ critical-алертов могут стать
         истинными ОДНОВРЕМЕННО (в один и тот же тик) — ни дневной лимит, ни
         межсобытийный интервал (оба меряют время ОТ ПРЕДЫДУЩЕГО события) не
         защищают от одновременной пачки, когда предыдущего события ещё не
         было. evaluate() расходует hard_cap_remaining по одному на каждое
         фактическое срабатывание СРАЗУ внутри одного вызова (см. evaluate),
         поэтому пачка из N одновременно истинных critical-условий тоже
         упирается в потолок, а не проходит вся целиком.

    events_today            — сколько NORMAL-событий уже доставлено сегодня
                              (critical в этот счётчик не входит — он не
                              расходует дневной лимит и не должен считаться
                              в него вызывающим)
    cfg_alerts              — cfg.alerts (или dict с теми же ключами):
                              max_events_per_day, min_seconds_between_events,
                              min_seconds_between_critical_events,
                              max_events_per_minute
    last_event_ts           — datetime последнего доставленного NORMAL-
                              события, или None
    last_critical_event_ts  — datetime последнего доставленного CRITICAL-
                              события, или None
    recent_event_ts         — итерируемое datetime всех событий (ЛЮБОГО
                              приоритета), доставленных за последние ~минуты;
                              окно 60 секунд отфильтровывается здесь же

    Возвращает:
      {'allowed': bool, 'reason': str, 'events_left': int,        # normal
       'critical_allowed': bool, 'critical_reason': str,          # critical
       'hard_cap_remaining': int, 'hard_cap_reason': str}         # оба яруса

    events_left — остаток дневного лимита normal СТРУКТУРНО (max -
    events_today), не зависит от того, почему allowed=False сейчас: дневной
    лимит и минимальный интервал — независимые ограничения одного яруса.
    """
    max_per_day = _cfg_get(cfg_alerts, "max_events_per_day")
    min_interval_s = _cfg_get(cfg_alerts, "min_seconds_between_events")
    min_critical_interval_s = _cfg_get(cfg_alerts, "min_seconds_between_critical_events")
    max_per_minute = _cfg_get(cfg_alerts, "max_events_per_minute")

    # --- ярус 1: normal — дневной лимит + обычный интервал ---
    events_left = max(0, max_per_day - events_today)
    if events_today >= max_per_day:
        allowed, reason = False, f"дневной лимит событий исчерпан: {events_today}/{max_per_day}"
        events_left = 0
    elif last_event_ts is not None and (now - last_event_ts).total_seconds() < min_interval_s:
        elapsed_s = (now - last_event_ts).total_seconds()
        allowed, reason = False, (f"минимальный интервал между normal-событиями не прошёл: "
                                  f"{elapsed_s:.0f}с < {min_interval_s}с")
    else:
        allowed, reason = True, "в пределах бюджета"

    # --- ярус 2: critical — только свой интервал, дневной лимит не действует ---
    if last_critical_event_ts is not None and (
            (now - last_critical_event_ts).total_seconds() < min_critical_interval_s):
        elapsed_c = (now - last_critical_event_ts).total_seconds()
        critical_allowed, critical_reason = False, (
            f"минимальный интервал между critical-событиями не прошёл: "
            f"{elapsed_c:.0f}с < {min_critical_interval_s}с")
    else:
        critical_allowed, critical_reason = True, "в пределах интервала critical"

    # --- ярус 3: абсолютный потолок в минуту, общий для всех приоритетов ---
    window_start = now - dt.timedelta(seconds=60)
    count_in_window = sum(1 for ts in recent_event_ts if ts is not None and ts >= window_start)
    hard_cap_remaining = max(0, max_per_minute - count_in_window)
    if hard_cap_remaining > 0:
        hard_cap_reason = f"в пределах потолка: {count_in_window}/{max_per_minute} событий за минуту"
    else:
        hard_cap_reason = f"абсолютный потолок исчерпан: {count_in_window}/{max_per_minute} событий за минуту"

    return {"allowed": allowed, "reason": reason, "events_left": events_left,
            "critical_allowed": critical_allowed, "critical_reason": critical_reason,
            "hard_cap_remaining": hard_cap_remaining, "hard_cap_reason": hard_cap_reason}


# --------------------------------------------------------------------------
# evaluate — обработчики условий по типу
# --------------------------------------------------------------------------
# Каждый обработчик: (alert, ctx, state, now) -> (condition, reason, detail)
#   condition = True/False  — сработало / не сработало
#   condition = None        — недостаточно данных в ctx, reason обязателен
#   detail                  — числа, попавшие в решение (для fired/диагностики)
# state — изменяемый dict _state этого алерта; обработчики trend_flips/
# gate_verdict_changes/session_phase_changes мутируют state['remember']
# НЕЗАВИСИМО от того, сработал ли алерт — иначе на следующем тике не с чем
# сравнивать.

def _sym(ctx, symbol):
    return (ctx.get("symbols") or {}).get(symbol)


def _pos(ctx, ticket):
    return (ctx.get("positions") or {}).get(ticket)


def _tradeable_price(ctx, symbol):
    """Цена, по которой можно судить о движении. → (цена, причина отказа).

    ЗАМЁРЗШАЯ ЦЕНА — НЕ ДВИЖЕНИЕ, А ЕГО ОТСУТСТВИЕ. Регресс 2026-08-01,
    найденный первой обкаткой команды: в субботу последний тик был пятничным
    (возраст 15.4 часа, цена 4046.38). Трейдер спланировал уровни от закрытия
    H1-бара (4051) и взвёл price_below 4050.11 — условие «цена ниже уровня»
    оказалось истинным мгновенно, и два алерта из четырёх сгорели в первую же
    секунду, ещё до открытия рынка. За выходные так выгорает любой уровень,
    оказавшийся не с той стороны от замёрзшей цены, и в понедельник трейдер
    просыпается без будильников.

    Датчик знал о протухании (tick_stale в снимке), но ценовые обработчики
    флаг не смотрели.

    Неизвестная свежесть трактуется как протухшая: читать «не знаю» как
    «свежий» означало бы принять незнание за разрешение, а весь пакет устроен
    наоборот. Отдельный тип data_stale при этом продолжает работать — он для
    того и есть, чтобы СООБЩИТЬ о протухших данных.
    """
    sym = _sym(ctx, symbol)
    if sym is None or sym.get("price") is None:
        return None, f"нет цены по символу {symbol!r}"
    if "tick_stale" in sym and sym["tick_stale"] is not False:
        # True — тик протух; None — датчик не смог определить свежесть, и это
        # тоже не разрешение: незнание не равно свежести. Отсутствие ключа
        # целиком означает, что свежесть в этом контексте не моделируется
        # (так строят ctx часть тестов) — блокировать по этому поводу значило
        # бы требовать от вызывающего описывать измерение, которого он не
        # касается. Боевой ctx ключ выставляет ВСЕГДА, и это закреплено
        # отдельным тестом.
        return None, (f"тик по {symbol!r} протух или свежесть неизвестна — "
                      "замёрзшая цена не является движением")
    return sym["price"], None


def _h_price_above(alert, ctx, state, now):
    price, why = _tradeable_price(ctx, alert["symbol"])
    if price is None:
        return None, why, {}
    level = alert["level"]
    return price > level, None, {"price": price, "level": level}


def _h_price_below(alert, ctx, state, now):
    price, why = _tradeable_price(ctx, alert["symbol"])
    if price is None:
        return None, why, {}
    level = alert["level"]
    return price < level, None, {"price": price, "level": level}


def _h_price_touch(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    if sym is None or sym.get("price") is None or sym.get("atr") is None:
        return None, f"нет цены или ATR по символу {alert['symbol']!r}", {}
    price, atr, level = sym["price"], sym["atr"], alert["level"]
    if not (atr > 0):
        return None, f"ATR по символу {alert['symbol']!r} <= 0, tolerance неопределим", {}
    tol = alert["tolerance_atr"]
    dist_atr = abs(price - level) / atr
    return (dist_atr <= tol, None,
            {"price": price, "level": level, "dist_atr": round(dist_atr, 4)})


def _atr_pctile_for(alert, sym):
    """ATR-перцентиль нужного таймфрейма. → (значение, причина_отказа, тф).

    ПОЧЕМУ ПОЯВИЛОСЬ ПОЛЕ `tf`. Инцидент 2026-08-03, первый живой день команды.
    Контекст датчика хранит ОДИН перцентиль на символ, и эталонным таймфреймом
    выбран `DEFAULT_TIMEFRAMES[0]` = M5 — выбор, сделанный до того, как команда
    приняла стандарт H1. Трейдер видит в снимке H1-число, ставит от него порог,
    а датчик сравнивает с M5.

    В этот день цена ошибки стала предметной. Трейдер `range` нашла на USDCHF
    подтверждённый бокс (сопротивление тестировано трижды, поддержка четырежды)
    и завела единственное за день условие, способное привести к сделке: «если
    ATR сожмётся, тот же бокс даст R:R ≥ 1.5 без движения цены». Она поставила
    порог 0.75 от H1-перцентиля 0.94 — и алерт сгорел в ту же секунду, потому
    что на M5 перцентиль был 0.31. Она даже написала предупреждение про M5 в
    ноте соседнего алерта: знала о пробеле и всё равно не могла его обойти,
    выразить H1 было нечем.

    Без `tf` поведение прежнее — эталонный ТФ датчика. Молчаливой смены смысла
    у уже взведённых алертов не происходит.
    """
    tf = alert.get("tf")
    if tf is None:
        v = sym.get("atr_pctile")
        return v, (None if v is not None else "нет ATR-перцентиля"), None
    by_tf = sym.get("atr_pctile_by_tf") or {}
    if tf not in by_tf:
        # «не знаю» вместо подстановки эталонного: молча ответить числом ДРУГОГО
        # таймфрейма — ровно тот отказ, ради которого поле и заводилось
        return None, f"нет ATR-перцентиля на {tf} (есть: {sorted(by_tf) or 'ничего'})", tf
    v = by_tf[tf]
    return v, (None if v is not None else f"ATR-перцентиль на {tf} не посчитан"), tf


def _h_atr_pctile_above(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    if sym is None:
        return None, f"нет данных по символу {alert['symbol']!r}", {}
    v, why, tf = _atr_pctile_for(alert, sym)
    if v is None:
        return None, f"{why} по символу {alert['symbol']!r}", {}
    level = alert["level"]
    detail = {"atr_pctile": v, "level": level}
    if tf:
        detail["tf"] = tf
    return v > level, None, detail


def _h_atr_pctile_below(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    if sym is None:
        return None, f"нет данных по символу {alert['symbol']!r}", {}
    v, why, tf = _atr_pctile_for(alert, sym)
    if v is None:
        return None, f"{why} по символу {alert['symbol']!r}", {}
    level = alert["level"]
    detail = {"atr_pctile": v, "level": level}
    if tf:
        detail["tf"] = tf
    return v < level, None, detail


def _h_trend_flips(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    tf = alert["tf"]
    if sym is None or sym.get("trend") is None:
        return None, f"нет тренда по символу {alert['symbol']!r}", {}
    current = sym["trend"].get(tf)
    if current is None:
        return None, f"нет тренда по символу {alert['symbol']!r} на tf={tf!r}", {}

    prev = state.get("remember")
    state["remember"] = current  # обновляем всегда, даже без срабатывания

    if prev is None:
        return False, None, {}  # первое наблюдение — эталон, сравнивать не с чем

    flipped = prev in ("up", "down") and current in ("up", "down") and prev != current
    return flipped, None, {"trend_prev": prev, "trend_now": current}


def _h_position_R_reaches(alert, ctx, state, now):
    pos = _pos(ctx, alert["ticket"])
    if pos is None or pos.get("r_multiple") is None:
        return None, f"нет данных по позиции {alert['ticket']!r} (тикет не найден или R неизвестен)", {}
    r, level = pos["r_multiple"], alert["level"]
    return r >= level, None, {"r_multiple": r, "level": level}


def _h_position_R_drops_to(alert, ctx, state, now):
    pos = _pos(ctx, alert["ticket"])
    if pos is None or pos.get("r_multiple") is None:
        return None, f"нет данных по позиции {alert['ticket']!r} (тикет не найден или R неизвестен)", {}
    r, level = pos["r_multiple"], -abs(alert["level"])
    return r <= level, None, {"r_multiple": r, "level": level}


def _h_position_time_elapsed(alert, ctx, state, now):
    pos = _pos(ctx, alert["ticket"])
    if pos is None or pos.get("opened_utc") is None:
        return None, f"нет данных по позиции {alert['ticket']!r} (тикет не найден или время открытия неизвестно)", {}
    elapsed_min = (now - pos["opened_utc"]).total_seconds() / 60.0
    if elapsed_min < alert["minutes"]:
        return False, None, {"elapsed_minutes": round(elapsed_min, 1)}
    if pos.get("r_multiple") is None:
        return None, f"позиция {alert['ticket']!r}: время прошло, но R неизвестен", {}
    r = pos["r_multiple"]
    return (r < alert["min_progress_R"], None,
            {"elapsed_minutes": round(elapsed_min, 1), "r_multiple": r})


def _h_news_window_opens(alert, ctx, state, now):
    windows = ctx.get("news_windows")
    if windows is None:
        return None, "нет данных об окнах новостей", {}
    threshold = alert["minutes_before"]
    matches = [w for w in windows
              if w.get("minutes_until") is not None and 0 <= w["minutes_until"] <= threshold]
    return bool(matches), None, {"events": matches}


def _h_spread_anomaly(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    if sym is None or sym.get("spread_points") is None or sym.get("spread_median_points") is None:
        return None, f"нет данных о спреде по символу {alert['symbol']!r}", {}
    sp, med, mult = sym["spread_points"], sym["spread_median_points"], alert["mult"]
    if not (med > 0):
        return None, f"медиана спреда по символу {alert['symbol']!r} <= 0, аномалию не определить", {}
    return sp > mult * med, None, {"spread_points": sp, "median": med, "mult": mult}


def _h_gap(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    if sym is None or sym.get("bar_gap") is None:
        return None, f"нет данных о разрыве по символу {alert['symbol']!r}", {}
    return bool(sym["bar_gap"]), None, {"bar_gap": sym["bar_gap"]}


def _h_sl_jumped(alert, ctx, state, now):
    pos = _pos(ctx, alert["ticket"])
    if pos is None or pos.get("beyond_sl") is None:
        return None, f"нет данных о стопе по позиции {alert['ticket']!r} (тикет не найден или неизвестно)", {}
    return bool(pos["beyond_sl"]), None, {"beyond_sl": pos["beyond_sl"]}


def _h_data_stale(alert, ctx, state, now):
    sym = _sym(ctx, alert["symbol"])
    if sym is None or sym.get("tick_stale") is None:
        return None, f"нет данных о свежести тика по символу {alert['symbol']!r}", {}
    return bool(sym["tick_stale"]), None, {"tick_stale": sym["tick_stale"]}


def _h_spread_normalizes(alert, ctx, state, now):
    """Книга вернулась: спред опустился к норме.

    ЗЕРКАЛО spread_anomaly, и оно нужно не для симметрии. Трендовой тактике
    достаточно знать, что спред разъехался — это повод НЕ входить. Возвратной
    наоборот: разъезд лишь повод ЗАМЕТИТЬ, а вход происходит на ВОЗВРАТЕ к
    норме, потому что цена откатывает тогда, когда маркет-мейкер вернул
    котировки. Без этого типа момент входа фейда невооружаем в принципе, и
    трейдеру остаётся опрашивать спред руками на каждом ценовом пробуждении.

    Запрошено 2026-08-01 трейдером-субагентом, который отказался от входа
    именно потому, что спред стоял на пике (2.58× при его пороге распада
    1.5×), и назвал отсутствие обратного условия профильным пробелом.
    """
    sym = _sym(ctx, alert["symbol"])
    if sym is None or sym.get("spread_points") is None or sym.get("spread_median_points") is None:
        return None, f"нет данных о спреде по символу {alert['symbol']!r}", {}
    sp, med, mult = sym["spread_points"], sym["spread_median_points"], alert["mult"]
    if not (med > 0):
        return None, f"медиана спреда по символу {alert['symbol']!r} <= 0, норму не определить", {}
    return sp <= mult * med, None, {"spread_points": sp, "median": med, "mult": mult}


def _h_gate_verdict_changes(alert, ctx, state, now):
    current = ctx.get("gate_verdict")
    if current is None:
        return None, "нет вердикта гейта в ctx", {}
    prev = state.get("remember")
    state["remember"] = current
    if prev is None:
        return False, None, {}
    return current != prev, None, {"verdict_prev": prev, "verdict_now": current}


def _h_session_phase_changes(alert, ctx, state, now):
    current = ctx.get("session_phase")
    if current is None:
        return None, "нет фазы сессии в ctx", {}
    prev = state.get("remember")
    state["remember"] = current
    if prev is None:
        return False, None, {}
    return current != prev, None, {"phase_prev": prev, "phase_now": current}


def _h_time_at_utc(alert, ctx, state, now):
    raw = alert["at"]
    try:
        at = _parse_dt(raw)
    except (TypeError, ValueError):
        return None, f"'at'={raw!r} не парсится как ISO-время", {}
    return now >= at, None, {"at": at.isoformat()}


def _h_silence_timeout(alert, ctx, state, now):
    last = ctx.get("last_event_utc")
    if last is None:
        return None, "неизвестно время последнего события (last_event_utc)", {}
    elapsed_min = (now - last).total_seconds() / 60.0
    return elapsed_min >= alert["minutes"], None, {"elapsed_minutes": round(elapsed_min, 1)}


_HANDLERS = {
    "price_above": _h_price_above,
    "price_below": _h_price_below,
    "price_touch": _h_price_touch,
    "atr_pctile_above": _h_atr_pctile_above,
    "atr_pctile_below": _h_atr_pctile_below,
    "trend_flips": _h_trend_flips,
    "position_R_reaches": _h_position_R_reaches,
    "position_R_drops_to": _h_position_R_drops_to,
    "position_time_elapsed": _h_position_time_elapsed,
    "news_window_opens": _h_news_window_opens,
    "spread_anomaly": _h_spread_anomaly,
    "spread_normalizes": _h_spread_normalizes,
    "gap": _h_gap,
    "sl_jumped": _h_sl_jumped,
    "data_stale": _h_data_stale,
    "gate_verdict_changes": _h_gate_verdict_changes,
    "session_phase_changes": _h_session_phase_changes,
    "time_at_utc": _h_time_at_utc,
    "silence_timeout": _h_silence_timeout,
}
assert set(_HANDLERS) == set(ALERT_TYPE_FIELDS)


def _check_condition(atype, alert, ctx, state, now):
    """Вызывает обработчик типа и перехватывает ЛЮБОЕ исключение внутри
    него: один кривой алерт (испорченный уровень, не число там, где число)
    не имеет права уронить evaluate() и весь цикл датчика, который крутится
    раз в секунду. Это не "тихий проглот торговой ошибки" — свалившийся
    алерт уходит в skipped с текстом исключения, ничего не срабатывает и
    ничего не декларируется истинным без оснований."""
    try:
        return _HANDLERS[atype](alert, ctx, state, now)
    except Exception as e:  # noqa: BLE001 - защита датчика от одного кривого алерта
        return None, f"ошибка вычисления условия: {e!r}", {}


# --------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------

def evaluate(alerts, ctx, *, now, budget) -> tuple:
    """ЧИСТАЯ функция: (fired, updated_alerts).

    alerts — то, что вернул load_alerts (dict с ключом 'alerts': [...]).
    ctx    — снимок восприятия на этот тик (см. docs/alerts_schema.md за
             полной формой); чего в ctx нет — по тому не срабатывает.
    now    — tz-aware datetime UTC.
    budget — обычно результат event_budget(...): {'allowed', 'reason',
             'events_left', 'critical_allowed', 'critical_reason',
             'hard_cap_remaining', 'hard_cap_reason'}. normal-алерты
             подчиняются budget['allowed'] (дневной лимит + обычный
             интервал); critical — только budget['critical_allowed'] (свой,
             более короткий интервал; дневной лимит НЕ действует — это
             единственное, что critical обходит). budget['hard_cap_remaining']
             — абсолютный потолок событий в минуту, общий для ОБОИХ
             приоритетов и НЕ обходится никем: расходуется по одному на
             каждое фактическое срабатывание СРАЗУ внутри этого вызова, так
             что пачка из нескольких одновременно истинных critical-условий
             в одном тике тоже упирается в потолок, а не проходит целиком
             (см. обоснование в event_budget — ни дневной лимит, ни интервал
             не защищают от одновременной пачки, когда предыдущего события
             ещё не было).

    fired          — list[dict]: что сработало на этом тике (id, type,
                     symbol, ticket, priority, note, fired_utc, detail).
    updated_alerts — та же структура, что alerts, но:
                       - alerts: список с обновлённым _state (armed/
                         last_fired_utc/remember) по каждому алерту;
                       - skipped: list[dict] {id, type, reason} — алерты,
                         которые НЕ сработали из-за нехватки данных в ctx,
                         малформированности, разоружения или исчерпанного
                         бюджета (яруса normal/critical или абсолютного
                         потолка). Обычное "условие пока ложно" в skipped НЕ
                         попадает — это не диагностический случай, это
                         штатное ожидание.

    updated_alerts не мутирует входной alerts (новые dict/list на каждом
    уровне) — вызывающий волен сравнить старое и новое состояние.
    """
    fired = []
    skipped = []
    out_alerts = []
    hard_cap_left = budget["hard_cap_remaining"]

    for i, alert in enumerate(alerts.get("alerts", [])):
        aid = alert.get("id", f"<no-id-{i}>")
        atype = alert.get("type")
        state = dict(alert.get("_state") or {})
        state.setdefault("armed", True)
        state.setdefault("last_fired_utc", None)
        state.setdefault("remember", None)

        if atype not in ALERT_TYPE_FIELDS:
            skipped.append({"id": aid, "type": atype, "reason": f"неизвестный тип {atype!r}"})
            out_alerts.append({**alert, "_state": state})
            continue

        missing = [f for f in ALERT_TYPE_FIELDS[atype] if f not in alert]
        if missing:
            skipped.append({"id": aid, "type": atype,
                            "reason": f"не хватает обязательных полей: {missing}"})
            out_alerts.append({**alert, "_state": state})
            continue

        priority = alert.get("priority", _DEFAULT_PRIORITY)
        if priority not in PRIORITIES:
            skipped.append({"id": aid, "type": atype,
                            "reason": f"неизвестный priority={priority!r} (допустимы {PRIORITIES})"})
            out_alerts.append({**alert, "_state": state})
            continue

        once = bool(alert.get("once", False))
        rearm_after = alert.get("rearm_after_minutes")

        # перевооружение: если разоружён и таймер задан — проверяем, истёк ли он
        if not state["armed"] and rearm_after is not None and state["last_fired_utc"] is not None:
            last_fired_dt = _parse_dt(state["last_fired_utc"])
            if now >= last_fired_dt + dt.timedelta(minutes=rearm_after):
                state["armed"] = True

        if not state["armed"]:
            skipped.append({"id": aid, "type": atype, "reason": "разоружён (once сработал, ждёт перевооружения)"})
            out_alerts.append({**alert, "_state": state})
            continue

        condition, reason, detail = _check_condition(atype, alert, ctx, state, now)

        if condition is None:
            skipped.append({"id": aid, "type": atype, "reason": reason})
            out_alerts.append({**alert, "_state": state})
            continue

        if not condition:
            out_alerts.append({**alert, "_state": state})
            continue

        # условие истинно — решает бюджет своего яруса (critical обходит
        # ТОЛЬКО дневной лимит normal, свой интервал он обязан соблюдать)
        if priority == "critical":
            if not budget["critical_allowed"]:
                skipped.append({"id": aid, "type": atype,
                                "reason": f"событийный бюджет (critical) не позволяет: {budget.get('critical_reason')}"})
                out_alerts.append({**alert, "_state": state})
                continue
        elif not budget["allowed"]:
            skipped.append({"id": aid, "type": atype,
                            "reason": f"событийный бюджет не позволяет: {budget.get('reason')}"})
            out_alerts.append({**alert, "_state": state})
            continue

        # абсолютный потолок в минуту — общий для обоих ярусов, не обходит
        # никто; расходуется здесь же, внутри цикла, чтобы пачка одновременно
        # истинных условий В ЭТОМ тике тоже была ограничена, а не прошла вся
        if hard_cap_left <= 0:
            skipped.append({"id": aid, "type": atype,
                            "reason": f"абсолютный потолок событий/минуту исчерпан: {budget.get('hard_cap_reason')}"})
            out_alerts.append({**alert, "_state": state})
            continue

        fired.append({"id": aid, "type": atype, "symbol": alert.get("symbol"),
                      "ticket": alert.get("ticket"), "priority": priority,
                      "note": alert.get("note"), "fired_utc": now.isoformat(),
                      "detail": detail})
        hard_cap_left -= 1

        state["last_fired_utc"] = now.isoformat()
        if once or rearm_after is not None:
            state["armed"] = False
        out_alerts.append({**alert, "_state": state})

    updated_alerts = {**alerts, "alerts": out_alerts, "skipped": skipped}
    return fired, updated_alerts
