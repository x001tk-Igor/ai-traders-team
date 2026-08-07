"""Журнал сделок: единственный источник правды о том, что система делала и
с каким результатом. Append-only, одна JSON-строка на запись, ensure_ascii=
False (кириллица тезисов/причин читается без escape-последовательностей).

ОБРАТНАЯ СОВМЕСТИМОСТЬ. В журнале на ПК трейдера уже есть записи старого
формата (до этой схемы) — без regime/model_id и прочих новых полей.
read_records обязан читать их без исключений: валидация схемы применяется
ТОЛЬКО на запись (append_decision/append_skip/append_alert_event), никогда
на чтение. read_records и append_outcome не тронуты этой задачей — работают
как раньше.

ДИСЦИПЛИНА ЗАПИСИ vs ДАННЫЕ МОДЕЛИ. append_decision жёстко отклоняет
неполную запись (ValueError) — лучше не войти в рынок, чем войти без следа
в журнале: поле, не попавшее в запись сейчас, не появится в ней никогда
("Дописать поле потом нельзя — данных уже нет"). Это ДИСЦИПЛИНА ПРОЦЕССА
(обязан ли ключ присутствовать), а не суждение о качестве данных модели —
поэтому она применена жёстко (raise), в отличие от quality.py, где
нераспознанные СТРОКОВЫЕ значения модели (status/profile) обрабатываются
fail-closed БЕЗ падения (там есть безопасный консервативный дефолт — минимум
известного множителя). У confidence такого дефолта нет: это число со строгой
математической областью [0,1], которое напрямую идёт в калибровку
(score.py: бакеты по confidence). Значение вне диапазона — не легитимная
редкая вероятность, а испорченное число, которое молча испортит калибровку
навсегда без возможности исправить задним числом. Поэтому confidence вне
[0,1] здесь ТОЖЕ отклоняется (см. validate_decision) — тем же кодом, что и
явно отсутствующие поля, и по той же причине.

ГАРАНТИЯ ПРО ВТОРОЙ ПУТЬ ЗАПИСИ — И ЕЁ ГРАНИЦА. _append — единственный
низкоуровневый примитив файловой записи в модуле; append_decision,
append_outcome, append_skip, append_alert_event — единственные функции,
которые его вызывают. test_no_second_decision_write_path (test_journal.py)
проверяет это СТРУКТУРНО через ast: разбирает исходник модуля, обходит все
функции верхнего уровня и находит те, в чьём теле есть вызов _append —
независимо от имени функции. Поэтому функция-обходчик ловится вне
зависимости от того, как её назвали (первая версия guard'а фильтровала по
имени `append_*decision*` — и не заметила `write_decision`/`append_dec`,
которые тоже пишут через _append, но называются иначе; текущая версия ловит
их обе, потому что смотрит на вызов, а не на имя).

Эта проверка НЕ абсолютна: guard разбирает исходник ТОЛЬКО journal.py.
Самый дешёвый обход — не переизобретать I/O, а сделать
`from trader_lib.journal import _append` в любом внешнем модуле и вызвать
его напрямую оттуда: ни байта дублирования, guard этого не увидит вообще,
потому что смотрит внутрь journal.py, а не по всему репозиторию. Более
трудоёмкий обход — заново реализовать запись в файл (свой
`open()`/`Path.write_text()`/`json.dump`), вообще не касаясь _append.
Практический риск сегодня низкий (ведущее подчёркивание в имени как сигнал
"не импортировать", и ни один внешний модуль сейчас _append не импортирует),
но это ограничение реальное, а не гипотетическое, и его нужно называть
честно, а не маскировать формулировкой вроде "требует дублировать I/O".
Ровно такая дыра — permissive второй путь мимо fail-closed — уже стоила
раунда ревью в risk_gate.py (legacy-ветка вызова возвращала OK с полным
риском вместо отказа); природа та же: пока путь "записать без проверки"
существует хоть где-то, ничто не мешает ему попасть в прод.

Тестам, которым не нужна полная схема (test_e2e_dryrun.py,
test_close_watch.py — они проверяют арифметику гейта/скоринга/reconcile, а
не состав журнала), шум из 28 полей убирает фикстура `make_decision` в
tests/conftest.py — не ослабление проверки (они пишут через настоящий
append_decision), а её однострочное удобное использование.
"""
import datetime as dt
import json
import os
from pathlib import Path

from trader_lib.risk_gate import BLOCKED_CODES, TERM_KEYS

# Обязательные поля decision-записи. Порядок — по смысловым группам (кто/что/
# почему/куда/риск/качество/контекст/план/вердикт гейта/модель), не влияет на
# поведение, но сохранён для читаемости и для тестов, перечисляющих поля.
DECISION_REQUIRED = (
    "trade_id", "symbol", "side",
    "regime",              # ярлык режима рынка от модели (её таксономия)
    "tactic", "setup_type", "setup_status",
    "thesis",               # тезис словами: почему я здесь
    "confidence",           # 0..1, обязателен — без него не работает калибровка
    "technical_trigger",
    "entry", "sl", "tp_plan",
    "risk_usd", "rr", "costs_R", "breakeven_p",
    "p_win_journal",        # частота из журнала, не оценка модели; None если выборки нет
    "news_check", "spread_at_entry",
    "correlation_check", "daily_risk_remaining_usd",
    "planned",              # bool: вход по гипотезе плана дня или внеплановый
    "plan_hypothesis_id",   # id гипотезы или None для внепланового
    "gate_verdict", "session_phase",
    "model_id", "model_profile",
)
DECISION_OPTIONAL = ("daily_bias", "macro_note", "unplanned_reason", "alert_id",
                     "blocked_by", "binding_term",
                     # технический прогон стека, а не торговое решение: такие
                     # сделки открываются и закрываются сразу, всегда дают
                     # минус на спреде и не должны копиться в серию убытков
                     # (см. trader_lib/streak.py). Ставится ТОЛЬКО в момент
                     # входа — пометить убыток смоуком задним числом нельзя
                     "smoke",
                     # ссылка на intent-запись, сделанную ДО отправки ордера
                     # (двухфазная запись, задача 4.2)
                     "intent_id")

# Намерение войти — «решение минус тикет». Пишется ДО order_send, потому что
# trade_id обязан быть тикетом брокера, а тикет известен только ПОСЛЕ филла:
# инвариант «нет ордера без следа в журнале» иначе не удержать. Состав
# выводится из DECISION_REQUIRED, а не перечисляется руками — так запись
# гарантированно достаточна, чтобы достроить decision из intent + тикет, если
# процесс упал между филлом и append_decision. Новое обязательное поле решения
# автоматически становится обязательным и для намерения.
INTENT_REQUIRED = ("intent_id",) + tuple(f for f in DECISION_REQUIRED if f != "trade_id")
OUTCOME_FIELDS = ("trade_id", "exit", "profit", "R", "exit_reason",
                  "spread_at_exit", "slippage_points", "mfe_R", "mae_R",
                  "decision_events")

# Пропуск сетапа: сделки не было, риск/уровни бессмысленны и не требуются.
SKIP_REQUIRED = ("setup_type", "reason", "confidence", "regime", "model_id", "model_profile")

# Событие алерта: что сработало и на какой модели.
ALERT_EVENT_REQUIRED = ("alert_id", "alert_type", "model_id")

# Наблюдение: модель проснулась, посмотрела и НЕ действует.
#
# Третий исход пробуждения, которого раньше не было. Без него различались
# только «вошла» (decision) и «отказалась от НАЗВАННОГО сетапа» (skip), а самый
# частый случай — «увидела, но жду» — не оставлял следа вовсе. Дневной разбор
# считал такое пробуждение ПУСТЫМ и помечал разбудивший алерт мусорным, то есть
# наказывал правильное ожидание наравне с бесполезным будильником.
#
# reasoning — рассуждение СЛОВАМИ МОДЕЛИ: чего именно ждём (отката, пробоя,
# подтверждения) и почему не входим сейчас. Это единственное место, где эта
# мысль сохраняется: в чате модель себя не проявляет, а к следующему
# пробуждению контекст может быть уже сжат.
OBSERVATION_REQUIRED = ("reasoning", "regime", "model_id")

# Действие над ОТКРЫТОЙ позицией (частичка, перенос стопа) — задача 4.3.
# Отдельный тип, а не outcome: исход закрывает сделку, и записать его при живой
# позиции значило бы вывести её из сверки с брокером. Отдельный тип, а не
# decision: решение о входе одно на сделку, и калибровка считает его один раз.
TRADE_EVENT_REQUIRED = ("trade_id", "action", "reason", "model_id")

# setup_status (поле decision-записи, значения из cfg.risk.status_risk_mult,
# ярлык модели на её языке) и require_setup_status гейта (trader_lib/
# risk_gate.py, домен ровно ("any", "confirmed")) — ДВА РАЗНЫХ ПОЛЯ:
# фактический статус сетапа против требования гейта к нему. Задача 5.4
# (entry_gate) обязана их сопоставлять — единственное значение setup_status,
# которое удовлетворяет require_setup_status == "confirmed", это
# "подтверждён" (см. config/trader.config.json: status_risk_mult). Константа
# фиксирует это соответствие в коде, а не в чьей-то голове, чтобы 5.4
# сравнивала через неё, а не изобретала сопоставление заново литералом.
CONFIRMED_SETUP_STATUS = "подтверждён"


def _append(path, rec):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _missing(rec, required) -> list:
    return [field for field in required if field not in rec]


def _check_confidence(rec, problems) -> None:
    """confidence вне [0,1] (или не число, включая bool) — общая проверка
    для decision И skip: обе записи фиксируют уверенность модели в
    конкретный момент (при входе или при осознанном отказе от входа), и обе
    питают одну и ту же калибровку (score.py). Испорченное значение молча
    травит калибровку навсегда независимо от того, вошла сделка или нет —
    см. обоснование в шапке модуля. Мутирует problems на месте (та же форма,
    что и остальные проверки в validate_decision/append_skip)."""
    if "confidence" in rec:
        c = rec["confidence"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
            problems.append(f"confidence={c!r} вне диапазона [0,1] или не число")


def validate_decision(rec) -> list:
    """Список проблем decision-записи: отсутствующие обязательные поля (из
    DECISION_REQUIRED) плюс три структурных инварианта поверх присутствующих
    полей — confidence вне [0,1], planned=True без plan_hypothesis_id,
    blocked_by/binding_term вне стабильных словарей risk_gate.py. Пустой
    список = запись полна и валидна для записи в журнал.

    Каждая проблема добавляется независимо — вызывающий (модель) видит все
    сразу и может исправить запись за один заход, а не по одному полю за цикл.
    """
    problems = _missing(rec, DECISION_REQUIRED)
    _check_confidence(rec, problems)

    if "planned" in rec and "plan_hypothesis_id" in rec:
        if rec["planned"] is True and rec["plan_hypothesis_id"] is None:
            problems.append("plan_hypothesis_id обязателен (не None), когда planned=True")

    if "blocked_by" in rec:
        bb = rec["blocked_by"]
        if bb is not None and bb not in BLOCKED_CODES:
            problems.append(f"blocked_by={bb!r} не из BLOCKED_CODES (risk_gate.py)")

    if "binding_term" in rec:
        bt = rec["binding_term"]
        if bt is not None and bt not in TERM_KEYS:
            problems.append(f"binding_term={bt!r} не из TERM_KEYS (risk_gate.py)")

    return problems


def append_decision(path, rec):
    """Пишет decision-запись. ValueError, если validate_decision(rec)
    непусто — запись неполного/некорректного решения запрещена: лучше не
    войти в рынок, чем войти без следа в журнале (см. шапку модуля). Ничего
    не пишется на диск, если валидация не пройдена. Единственная функция
    модуля, вызывающая _append для decision (см. test_no_second_decision_
    write_path и границу этой гарантии в шапке модуля)."""
    problems = validate_decision(rec)
    if problems:
        raise ValueError(
            "неполная decision-запись отклонена (см. шапку journal.py): " + "; ".join(problems)
        )
    out = {"type": "decision", "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **rec}
    _append(path, out)
    return out


def validate_intent(rec) -> list:
    """Проблемы intent-записи. Те же структурные инварианты, что у решения
    (confidence, planned/plan_hypothesis_id, blocked_by/binding_term), но
    вместо trade_id обязателен intent_id: тикета ещё нет."""
    problems = _missing(rec, INTENT_REQUIRED)
    _check_confidence(rec, problems)

    if "planned" in rec and "plan_hypothesis_id" in rec:
        if rec["planned"] is True and rec["plan_hypothesis_id"] is None:
            problems.append("plan_hypothesis_id обязателен (не None), когда planned=True")
    if "blocked_by" in rec:
        bb = rec["blocked_by"]
        if bb is not None and bb not in BLOCKED_CODES:
            problems.append(f"blocked_by={bb!r} не из BLOCKED_CODES (risk_gate.py)")
    if "binding_term" in rec:
        bt = rec["binding_term"]
        if bt is not None and bt not in TERM_KEYS:
            problems.append(f"binding_term={bt!r} не из TERM_KEYS (risk_gate.py)")
    return problems


def append_intent(path, rec):
    """Пишет намерение войти ДО отправки ордера. ValueError при неполной
    записи — по той же причине, что и у решения: неполное намерение не даст
    достроить решение после филла, и след будет бесполезен ровно тогда, когда
    он единственный (процесс упал между филлом и append_decision)."""
    problems = validate_intent(rec)
    if problems:
        raise ValueError(
            "неполная intent-запись отклонена (см. шапку journal.py): " + "; ".join(problems))
    out = {"type": "intent", "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **rec}
    _append(path, out)
    return out


def decision_from_intent(intent, *, ticket):
    """Достраивает decision-запись из намерения и полученного тикета.

    Существует ради одного стыка сбоя: ордер исполнен, а процесс упал до
    append_decision. Без этого в журнале осталось бы намерение без решения, а у
    брокера — позиция, которую сверка (find_orphans) сочла бы ЧУЖОЙ и увела
    систему в HALT_NEW при полностью нашей сделке.
    """
    rec = {k: v for k, v in intent.items() if k not in ("type", "ts")}
    rec["trade_id"] = str(ticket)
    rec["intent_id"] = intent["intent_id"]
    return rec


def append_skip(path, rec):
    """Пропуск названного сетапа: модель увидела сетап и решила не входить.
    Обязательны SKIP_REQUIRED (setup_type, reason, confidence, regime,
    model_id, model_profile). entry/sl/риск НЕ обязательны — сделки нет.
    confidence проверяется на диапазон [0,1] той же логикой, что и в
    decision (_check_confidence) — уверенность фиксируется и при отказе от
    входа, а не только при входе. ValueError, если чего-то из обязательного
    не хватает или confidence испорчен."""
    problems = _missing(rec, SKIP_REQUIRED)
    _check_confidence(rec, problems)
    if problems:
        raise ValueError(f"неполная skip-запись отклонена: {problems}")
    out = {"type": "skip", "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **rec}
    _append(path, out)
    return out


def append_alert_event(path, rec):
    """Что сработало (alert_id, тип, снимок) и что модель решила в ответ.
    Обязательны ALERT_EVENT_REQUIRED (alert_id, alert_type, model_id).
    ValueError, если чего-то из обязательного не хватает."""
    missing = _missing(rec, ALERT_EVENT_REQUIRED)
    if missing:
        raise ValueError(f"неполная alert_event-запись отклонена: не хватает {missing}")
    out = {"type": "alert_event", "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **rec}
    _append(path, out)
    return out


def append_observation(path, rec):
    """Пробуждение без сделки: что увидела и почему не действует.

    Обязательны OBSERVATION_REQUIRED (reasoning, regime, model_id). Пустое
    рассуждение отклоняется: запись «посмотрела, ничего» не отличима от
    отсутствия записи и только засоряет память — смысл ровно в словах модели.
    """
    problems = _missing(rec, OBSERVATION_REQUIRED)
    if not (rec.get("reasoning") or "").strip():
        problems.append("reasoning пуст: наблюдение без рассуждения бессмысленно")
    if problems:
        raise ValueError(f"неполная observation-запись отклонена: {problems}")
    out = {"type": "observation", "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
           **rec}
    _append(path, out)
    return out


def append_trade_event(path, rec):
    """Действие над открытой позицией: частичная фиксация, перенос стопа.
    Обязательны TRADE_EVENT_REQUIRED (trade_id, action, reason, model_id) —
    действие без причины не отличить потом от случайного, а ради этого
    различения журнал и ведётся."""
    missing = _missing(rec, TRADE_EVENT_REQUIRED)
    if missing:
        raise ValueError(f"неполная trade_event-запись отклонена: не хватает {missing}")
    out = {"type": "trade_event", "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **rec}
    _append(path, out)
    return out


def append_outcome(path, rec):
    rec = {"type": "outcome", "close_ts": dt.datetime.now(dt.timezone.utc).isoformat(), **rec}
    _append(path, rec)
    return rec


def read_records(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]
