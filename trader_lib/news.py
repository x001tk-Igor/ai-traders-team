"""Новостные окна блокировки (задача 5.1).

ГЛАВНАЯ ЛОВУШКА ЭТОГО МОДУЛЯ — ТАЙМЗОНА. ForexFactory отдаёт время событий в
Eastern Time, и наивное `.replace(tzinfo=utc)` сдвигает каждое событие на 4–5
часов: система блокирует не те часы и «почти работает» — худший вид поломки,
потому что выглядит рабочей. Смещение НЕ константа (летом −4, зимой −5),
поэтому перевод идёт через zoneinfo, а не вычитанием часов руками.

ПОЧЕМУ ЛОГИКА ЖИВЁТ ЗДЕСЬ, А НЕ ИМПОРТИРУЕТСЯ ИЗ СКИЛЛА. Готовая реализация
есть в ~/.claude/skills/forex-calendar/tools/news_loader.py (там эта ловушка уже
починена), но на ПК трейдера этого скилла не будет — проект разворачивается как
самостоятельный пакет. Поэтому починенная логика перенесена сюда и покрыта
своими тестами на XML-фикстуре; заимствование — идея и разбор формата, не
зависимость.

ДВА УРОВНЯ ОКОН. Обычное high-событие блокирует по cfg.news.normal_window_min
(30 до, 15 после), событие из cfg.news.top_events (FOMC, NFP, CPI, ставки,
выступления глав ЦБ) — по cfg.news.top_window_min (60 до, 30 после). Событий
ниже high в окнах нет вовсе: их влияние не отличимо от обычного шума, а
блокировать по ним значит не торговать никогда.

СОБЫТИЕ БЕЗ ВРЕМЕНИ («Tentative», «All Day») НЕ ПРЕВРАЩАЕТСЯ В ОКНО. Прошлая
реализация подставляла таким событиям полдень ET — то есть блокировала
случайные полтора часа и пропускала настоящий момент выхода. Здесь такие
события возвращаются с ts_utc=None и time_known=False: модель видит, что
событие сегодня есть, и решает сама.

STALE ТРАКТУЕТСЯ ПО cfg.news.fail_mode. Кэш старше cache_max_age_hours и сеть
недоступна → при "halt_new" (и при любом НЕраспознанном значении) блокируется
всё: торговать по вчерашнему календарю значит не увидеть сегодняшний NFP.
"""
import datetime as dt
import json
import urllib.request
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
EASTERN = ZoneInfo("America/New_York")

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
# без браузерного User-Agent ForexFactory отвечает 403
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

IMPACT_MAP = {"High": "high", "Medium": "medium", "Low": "low", "Non-Economic": "low"}

# Валюты, которые нельзя вывести из имени символа механически. Всё остальное
# (6 букв: EURUSD, BTCUSD) раскладывается пополам.
SYMBOL_CURRENCIES = {
    "XAUUSD": ("XAU", "USD"), "XAGUSD": ("XAG", "USD"),
    "US30": ("USD",), "US500": ("USD",), "USTEC": ("USD",), "SP500": ("USD",),
    "NAS100": ("USD",), "DXY": ("USD",), "USDX": ("USD",), "US10Y": ("USD",),
    "BRENT": ("USD",), "XTIUSD": ("USD",), "VIX": ("USD",),
    "DE40": ("EUR",), "GER40": ("EUR",), "UK100": ("GBP",), "JP225": ("JPY",),
}


def symbol_currencies(symbol):
    """Валюты, к которым чувствителен символ. None — определить не удалось
    (тогда news_state блокирует: торговать вслепую в новостях нельзя)."""
    s = (symbol or "").upper()
    if s in SYMBOL_CURRENCIES:
        return set(SYMBOL_CURRENCIES[s])
    if len(s) == 6 and s.isalnum():
        return {s[:3], s[3:]}
    return None


def _parse_dt(date_s, time_s):
    """ET → UTC. Возвращает (datetime|None, time_known)."""
    ts = (time_s or "").strip().upper()
    if ts in ("", "TENTATIVE", "ALL DAY"):
        return None, False
    for fmt in ("%m-%d-%Y %I:%M%p", "%m-%d-%Y %H:%M"):
        try:
            naive = dt.datetime.strptime(f"{date_s} {ts}", fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=EASTERN).astimezone(UTC), True
    return None, False


def _currency_of(item):
    """Валюта события.

    В РЕАЛЬНОМ фиде ForexFactory (ff_calendar_thisweek.xml) валюта лежит в теге
    <country>, а тега <currency> нет вовсе. Читать только <currency> — значит
    получить пустую строку у КАЖДОГО события, после чего сопоставление
    «символ ↔ валюта» не сработает ни разу и новостной гейт не заблокирует
    ничего. Внешне при этом всё исправно: события разобраны, окна построены,
    ошибок нет.

    Найдено первым живым обращением к фиду (2026-07-26): 90 событий, у всех
    currency=''. Офлайн-тесты этого не видели — фикстура была написана по
    старому образцу с <currency>. Поэтому здесь принимаются ОБА тега, а тесты
    гоняются на фрагменте настоящего ответа.
    """
    for tag in ("currency", "country"):
        value = (item.findtext(tag) or "").strip().upper()
        if value:
            return value
    return ""


def parse_ff_xml(xml_bytes):
    """Список событий календаря. Битый XML → ValueError (вызывающий решает,
    что делать; load_windows трактует это как «данных нет» и уходит в stale)."""
    root = ElementTree.fromstring(xml_bytes)
    events = []
    for item in root.findall(".//event"):
        ts, known = _parse_dt((item.findtext("date") or "").strip(),
                              (item.findtext("time") or "").strip())
        events.append({
            "title": (item.findtext("title") or "").strip(),
            "currency": _currency_of(item),
            "impact": IMPACT_MAP.get((item.findtext("impact") or "Low").strip(), "low"),
            "ts_utc": ts, "time_known": known})
    return events


def _fetch(url=FF_URL, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _event_to_json(e):
    return {**e, "ts_utc": e["ts_utc"].isoformat() if e["ts_utc"] else None}


def _event_from_json(d):
    ts = d.get("ts_utc")
    return {**d, "ts_utc": dt.datetime.fromisoformat(ts) if ts else None}


def _read_cache(path, *, now, max_age_hours):
    """(события, stale). Отсутствующий/битый кэш → ([], True)."""
    p = Path(path)
    if not p.exists():
        return [], True
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        fetched = dt.datetime.fromisoformat(doc["fetched_utc"])
        events = [_event_from_json(x) for x in doc["events"]]
    except Exception:  # noqa: BLE001 - битый кэш равнозначен отсутствию кэша
        return [], True
    age_h = (now - fetched).total_seconds() / 3600.0
    return events, age_h >= max_age_hours


def _write_cache(path, events, *, now):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched_utc": now.isoformat(),
                             "events": [_event_to_json(e) for e in events]},
                            ensure_ascii=False, indent=2), encoding="utf-8")


def _level(title, top_events):
    t = (title or "").lower()
    return "top" if any(k.lower() in t for k in top_events) else "normal"


def load_windows(cache_path, *, cfg, now, loader=None, force=False):
    """Окна блокировки на сегодня. Сеть дёргается только когда кэш устарел:
    датчик зовёт это раз в секунду.

    `force=True` обновляет кэш независимо от его возраста — это для утренней
    подготовки, и вот зачем.

    РЕГРЕСС 2026-07-31: кэш был взят накануне в 06:30, лимит суточный. Утренний
    брифинг прошёл в 06:12 — кэшу было 23.7ч, формально свежий, сеть не
    дёрнулась. Через 18 минут кэш пересёк границу суток, и внутри дня обновить
    его стало нечем: предвходовой гейт по сети принципиально не ходит, а цикл
    восприятия за день больше не вызывается. Живой вход был заблокирован
    посреди сессии. Результат утренней подготовки не должен зависеть от того,
    на сколько минут она разминулась с границей суток.

    → {'windows': [...], 'stale': bool, 'source': 'cache'|'network',
       'ambiguous': [...события без времени...]}
    """
    loader = loader or _fetch
    events, stale = _read_cache(cache_path, now=now,
                                max_age_hours=cfg.news.cache_max_age_hours)
    stale = stale or force
    source = "cache"
    if stale:
        try:
            events = parse_ff_xml(loader())
            _write_cache(cache_path, events, now=now)
            stale, source = False, "network"
        except Exception as e:  # noqa: BLE001 - нет сети/битый XML: остаёмся stale
            source = f"недоступно: {e}"

    windows, ambiguous = [], []
    before_n, after_n = cfg.news.normal_window_min
    before_t, after_t = cfg.news.top_window_min
    for e in events:
        if e["impact"] != "high":
            continue
        if e["ts_utc"] is None:
            ambiguous.append({"title": e["title"], "currency": e["currency"]})
            continue
        level = _level(e["title"], cfg.news.top_events)
        before, after = (before_t, after_t) if level == "top" else (before_n, after_n)
        windows.append({
            "from": e["ts_utc"] - dt.timedelta(minutes=before),
            "to": e["ts_utc"] + dt.timedelta(minutes=after),
            "at": e["ts_utc"], "title": e["title"], "level": level,
            "currencies": {e["currency"]}})
    windows.sort(key=lambda w: w["from"])
    return {"windows": windows, "stale": stale, "source": source,
            "ambiguous": ambiguous, "fail_mode": cfg.news.fail_mode}


def news_state(windows_doc, *, now, symbol):
    """Можно ли входить по этому символу прямо сейчас.

    → {'blocked': bool, 'reason': str, 'next_event_in_min': int|None,
       'next_event': {...}|None}

    next_event_in_min считается до НАЧАЛА ОКНА, а не до момента события: войти
    за пять минут до начала окна значит войти в блокировку.
    """
    if windows_doc.get("stale"):
        # неизвестный fail_mode трактуется как самый строгий: опечатка в
        # конституции не имеет права открыть торговлю в новостях
        if windows_doc.get("fail_mode") != "allow":
            return {"blocked": True, "next_event_in_min": None, "next_event": None,
                    "reason": "календарь устарел и не обновился "
                              f"({windows_doc.get('source')}): торговать по вчерашнему "
                              "календарю значит не увидеть сегодняшнее событие"}
        return {"blocked": False, "next_event_in_min": None, "next_event": None,
                "reason": "календарь устарел, но fail_mode=allow"}

    currencies = symbol_currencies(symbol)
    if currencies is None:
        return {"blocked": True, "next_event_in_min": None, "next_event": None,
                "reason": f"валюты символа {symbol!r} не определены — в новостях "
                          "торговать вслепую нельзя"}

    mine = [w for w in windows_doc["windows"] if w["currencies"] & currencies]
    for w in mine:
        if w["from"] <= now <= w["to"]:
            return {"blocked": True, "next_event_in_min": 0, "next_event": w,
                    "reason": f"окно новости: {w['title']} ({w['level']}), "
                              f"{w['from'].strftime('%H:%M')}–{w['to'].strftime('%H:%M')} UTC"}

    upcoming = [w for w in mine if w["from"] > now]
    if not upcoming:
        return {"blocked": False, "next_event_in_min": None, "next_event": None,
                "reason": "новостных окон по этому символу впереди нет"}
    nxt = upcoming[0]
    minutes = int((nxt["from"] - now).total_seconds() // 60)
    return {"blocked": False, "next_event_in_min": minutes, "next_event": nxt,
            "reason": f"до окна «{nxt['title']}» {minutes} мин"}
