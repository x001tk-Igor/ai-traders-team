"""План дня и открытое намерение (задача 6.2).

План дня — КОНТРАКТ, а не заметка. Из него растут три вещи:
  * алерты, которые разбудят модель (без них план — благое намерение);
  * признак «плановый вход» в записи журнала (внеплановые лимитированы);
  * разбор «план против факта» вечером.

Отсюда строгость разбора:

  1. ГИПОТЕЗА БЕЗ РАЗБОРЧИВОГО АЛЕРТА — НЕ ГИПОТЕЗА. Условие, которое нельзя
     превратить в алерт, никого не разбудит: модель напишет красивый план и
     проспит его. Такие попадают в plan['problems'] — громко, а не тихим
     пропуском.
  2. НИЧЕГО НЕ ДОДУМЫВАЕТСЯ. Не указан уровень — алерта нет и сказано почему.
     «Примерно 2415» не подставляется никогда: алерт с выдуманным уровнем
     разбудит модель не там и приведёт к сделке, которой она не планировала.
  3. НАМЕРЕНИЕ ЖИВЁТ В ФАЙЛЕ. Сессия прерывается, память не переживает
     перезапуск. То, что модель ведёт прямо сейчас, читается с диска
     (open_intent.md), а не из головы.

ФОРМАТ ГИПОТЕЗЫ (docs/day_plan_template.md):

    ## H1 · XAUUSD · london-range-break-short
    - **Условие:** возврат под 2415 после теста сверху, вне окна новостей
    - **Алерт:** price_below 2415 (id: h1-trigger)
    - **Вход:** закрытие M15 под 2415
    - **Стоп:** 2421 (за экстремум теста)
    - **Цели:** TP1 1R ~50%, TP2 2R ~30%, остаток трейл
    - **Убийца:** закрепление выше 2422 → гипотеза мертва (алерт h1-dead)
    - **Горизонт:** 120 мин

Разделитель в заголовке — «·» или «|». Ключи полей русские и сопоставляются с
каноническими именами таблицей FIELD_KEYS: модель пишет план на своём языке, а
код не должен зависеть от того, поставила ли она двоеточие внутри звёздочек.
"""
import re

# ключ в markdown (нижним регистром, без двоеточия) → каноническое имя
FIELD_KEYS = {
    "условие": "condition", "алерт": "alert_spec", "вход": "entry_rule",
    "стоп": "stop", "цели": "targets", "убийца": "killer", "горизонт": "horizon",
}

HEADING_RE = re.compile(r"^##\s+(?P<id>[^\s·|]+)\s*[·|]\s*(?P<symbol>[^\s·|]+)\s*"
                        r"[·|]\s*(?P<setup>.+?)\s*$")
FIELD_RE = re.compile(r"^\s*[-*]\s*\*\*(?P<key>[^:*]+)\s*:?\s*\*\*\s*:?\s*(?P<value>.*)$")

# «price_below 2415 (id: h1-trigger)»
ALERT_RE = re.compile(r"(?P<type>[a-z_]+)\s*(?P<level>-?\d+(?:[.,]\d+)?)?"
                      r"(?:.*?\(id:\s*(?P<id>[^)]+)\))?", re.IGNORECASE)
# «закрепление выше 2422 → ... (алерт h1-dead)»
KILLER_RE = re.compile(r"(?P<dir>выше|ниже|above|below)\s*(?P<level>-?\d+(?:[.,]\d+)?)"
                       r".*?\(алерт\s+(?P<id>[^)]+)\)", re.IGNORECASE)
MINUTES_RE = re.compile(r"(\d+)\s*мин")
LEVEL_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

PRICE_ALERT_TYPES = {"price_above", "price_below", "price_touch"}


def _num(text):
    if text is None:
        return None
    try:
        return float(str(text).replace(",", "."))
    except ValueError:
        return None


def _norm(value):
    return (value or "").strip().casefold()


def parse_day_plan(md_text):
    """Разбирает markdown плана дня.

    → {'hypotheses': [...], 'problems': [...], 'preamble': str}

    problems — список того, что модель написала так, что код не смог
    превратить это в алерт. Пустой план (сегодня не торгуем) — не проблема.
    """
    hypotheses, problems, preamble = [], [], []
    current = None
    last_key = None   # поле, которое может продолжиться следующей строкой

    for line in (md_text or "").splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            current = {"id": heading.group("id").strip(),
                       "symbol": heading.group("symbol").strip(),
                       "setup_type": heading.group("setup").strip(),
                       "condition": None, "alert_spec": None, "entry_rule": None,
                       "stop": None, "targets": None, "killer": None,
                       "horizon": None, "horizon_minutes": None}
            hypotheses.append(current)
            last_key = None
            continue
        if current is None:
            if line.strip():
                preamble.append(line.strip())
            continue
        field = FIELD_RE.match(line)
        if not field:
            # ПРОДОЛЖЕНИЕ ПРЕДЫДУЩЕГО ПОЛЯ. РЕГРЕСС 2026-08-01, найден
            # трейдером-субагентом: длинная формулировка «Убийца» переносилась
            # на вторую строку, и хвост «(алерт <id>)» терялся — гипотеза
            # оставалась БЕЗ условия отмены. Отказ тихий: problems пуст, план
            # выглядит разобранным, а убийцы в alerts.json просто нет. Именно
            # убийца снимает мёртвую гипотезу со стола, и его потеря оставляет
            # модель торговать по уже опровергнутому тезису.
            #
            # Перенос — свойство markdown, а не одного поля: условие и цели
            # пишутся длинно так же часто, поэтому склейка общая. Признак
            # продолжения — отступ и отсутствие своего маркера списка.
            tail = line.strip()
            if last_key and tail and line[:1] in " \t" and not tail.startswith("-"):
                current[last_key] = f"{current[last_key]} {tail}".strip()
            continue
        key = FIELD_KEYS.get(_norm(field.group("key")))
        if key:
            current[key] = field.group("value").strip()
            last_key = key

    for h in hypotheses:
        if h["horizon"]:
            m = MINUTES_RE.search(h["horizon"])
            h["horizon_minutes"] = int(m.group(1)) if m else None
        problems.extend(_alert_problems(h))

    return {"hypotheses": hypotheses, "problems": problems,
            "preamble": "\n".join(preamble)}


def _alert_spec(h):
    """(тип, уровень, id) из строки «Алерт:» или None, если не разобрано."""
    spec = h.get("alert_spec")
    if not spec:
        return None
    m = ALERT_RE.search(spec)
    if not m:
        return None
    a_type = (m.group("type") or "").lower()
    if a_type not in PRICE_ALERT_TYPES:
        return None
    level = _num(m.group("level"))
    alert_id = (m.group("id") or "").strip() or f"{h['id'].lower()}-trigger"
    return a_type, level, alert_id


def _alert_problems(h):
    """Почему из гипотезы не получится алерт. Формулировки — для модели: она
    прочитает их в брифинге и поправит план, а не будет гадать."""
    out = []
    if not h.get("alert_spec"):
        out.append(f"{h['id']}: нет строки «Алерт:» — гипотезу нечем разбудить")
        return out
    parsed = _alert_spec(h)
    if parsed is None:
        out.append(f"{h['id']}: строка «Алерт: {h['alert_spec']}» не разобрана — "
                   f"нужен тип из {sorted(PRICE_ALERT_TYPES)} и уровень")
        return out
    _, level, _ = parsed
    if level is None:
        out.append(f"{h['id']}: в алерте не указан уровень (level) — "
                   "выдумывать его нельзя, гипотеза останется без будильника")
    return out


def hypothesis_by_id(plan, hid):
    for h in plan.get("hypotheses", []):
        if h["id"] == hid:
            return h
    return None


def is_planned(plan, *, symbol, setup_type):
    """(плановый ли вход, id гипотезы). Сравнение по символу и ярлыку сетапа
    без учёта регистра и лишних пробелов: ярлык пишет модель руками, и опечатка
    в регистре не должна превращать плановый вход во внеплановый (у них разные
    лимиты)."""
    for h in plan.get("hypotheses", []):
        if _norm(h["symbol"]) == _norm(symbol) and \
                _norm(h["setup_type"]) == _norm(setup_type):
            return True, h["id"]
    return False, None


def alerts_from_plan(plan):
    """Черновик alerts.json из гипотез: триггер, убийца, горизонт.

    Гипотезы, для которых алерт не разобран или уровень не указан,
    ПРОПУСКАЮТСЯ — они уже перечислены в plan['problems']. Молча подставить
    уровень значило бы разбудить модель не там.
    """
    alerts = []
    for h in plan.get("hypotheses", []):
        parsed = _alert_spec(h)
        if parsed and parsed[1] is not None:
            a_type, level, alert_id = parsed
            alerts.append({"id": alert_id, "type": a_type, "symbol": h["symbol"],
                           "level": level, "priority": "normal",
                           "hypothesis_id": h["id"],
                           "note": h.get("condition") or ""})
        killer = KILLER_RE.search(h.get("killer") or "")
        if killer:
            direction = killer.group("dir").lower()
            alerts.append({
                "id": killer.group("id").strip(),
                "type": "price_above" if direction in ("выше", "above") else "price_below",
                "symbol": h["symbol"], "level": _num(killer.group("level")),
                # смерть гипотезы важнее обычного срабатывания: она отменяет
                # план, по которому модель собиралась действовать
                "priority": "critical", "hypothesis_id": h["id"],
                "note": f"гипотеза {h['id']} мертва: {h.get('killer')}"})
        # ГОРИЗОНТ ИЗ ПЛАНА АЛЕРТОМ ЗДЕСЬ НЕ СТАНОВИТСЯ. Тип
        # position_time_elapsed требует тикет, а на момент планирования позиции
        # не существует. Горизонт живёт в самой гипотезе (horizon_minutes) и
        # превращается в алерт при входе — scripts/enter.py ставит его уже с
        # настоящим тикетом. Выпустить здесь алерт без тикета значило бы отдать
        # датчику условие, которое он молча пропустит.
    return alerts


# --------------------------------------------------------------------------
# открытое намерение
# --------------------------------------------------------------------------

INTENT_FIELDS = ("hypothesis_id", "symbol", "side", "state", "entry", "sl",
                 "note", "updated_utc")
INTENT_LABELS = {"hypothesis_id": "Гипотеза", "symbol": "Символ", "side": "Сторона",
                 "state": "Состояние", "entry": "Вход", "sl": "Стоп",
                 "note": "Заметка", "updated_utc": "Обновлено"}
INTENT_BY_LABEL = {v.casefold(): k for k, v in INTENT_LABELS.items()}


def render_open_intent(intent):
    """Намерение → markdown. Формат человекочитаемый: его читает и модель, и
    владелец счёта, когда разбирается, что происходило."""
    lines = ["## Открытое намерение", ""]
    for key in INTENT_FIELDS:
        value = intent.get(key)
        if value is not None:
            lines.append(f"- **{INTENT_LABELS[key]}:** {value}")
    return "\n".join(lines) + "\n"


def parse_open_intent(md_text):
    """markdown → намерение. Пустой текст → None (намерения нет).

    Отсутствующее поле = None, а не пропуск ключа: вызывающий читает намерение
    по фиксированному набору ключей и не должен различать «поля нет» и «поле
    пустое» — обе ситуации означают «неизвестно».
    """
    if not (md_text or "").strip():
        return None
    out = {k: None for k in INTENT_FIELDS}
    for line in md_text.splitlines():
        m = FIELD_RE.match(line)
        if not m:
            continue
        key = INTENT_BY_LABEL.get(_norm(m.group("key")))
        if key:
            value = m.group("value").strip()
            out[key] = _num(value) if key in ("entry", "sl") else value
    return out
