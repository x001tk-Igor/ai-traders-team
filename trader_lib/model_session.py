"""Кто именно сейчас торгует (задача владельца счёта 2026-07-27).

ПРОБЛЕМА. `model_id` штампуется в каждую запись журнала и в каждое событие, и
по нему режется статистика `by_model` и калибровка — та самая, по которой
считается haircut уверенности. До этого модуля идентичность бралась из
`config/trader.config.json`, то есть из строки, которую человек вписал когда-то
руками. Запустил контур на другой модели, забыл поправить конфиг — и журнал
пишет чужое имя. Две модели смешиваются в одном бакете, калибровка врёт обеим,
и заметить это нельзя ничем: ошибок нет, всё «работает».

ЧЕГО СДЕЛАТЬ НЕЛЬЗЯ. Определить модель из кода невозможно: Claude Code не
передаёт её имя дочерним процессам. В окружении есть версия харнесса
(AI_AGENT), PID и CLAUDE_CODE_SESSION_ID — имени модели нет. Проверено
2026-07-27. Любой, кто утверждает обратное, подставит правдоподобную строку.

ЧТО СДЕЛАТЬ МОЖНО, И В ЭТОМ ВСЯ ИДЕЯ. Модель — единственный, кто знает, кто
она. Пусть объявляет себя САМА и В МОМЕНТ НАЧАЛА РАБОТЫ, а объявление
привязывается к идентификатору сессии Claude Code. Тогда:

  * идентичность берётся не из конфига, а из текущего сеанса — человеку
    нечего править и нечего забыть;
  * на другом ПК идёт другая сессия с другим CLAUDE_CODE_SESSION_ID, и
    объявление, сделанное здесь, там просто не действует;
  * ПРОТУХШУЮ декларацию видно: session_id в файле не совпадает с текущим —
    значит объявлялась другая сессия, возможно другой моделью.

Декларацию по-прежнему нельзя проверить (модель может назваться как угодно), и
это сказано прямо. Но разница принципиальная: раньше молча врал СТАРЫЙ ФАЙЛ,
теперь врать может только сама модель про себя здесь и сейчас — а это уже не
конфликт конфигурации, а вопрос доверия к модели, которой ты и так доверил счёт.
"""
import datetime as dt
import json
import os
from pathlib import Path

SESSION_FILE = "model_session.json"
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"

# профили из конституции: strong — полные права, weak — урезанные
KNOWN_PROFILES = ("strong", "weak")


def current_session_id():
    """Идентификатор сеанса Claude Code или None вне его (например, при ручном
    запуске скрипта из консоли)."""
    return os.environ.get(SESSION_ENV) or None


def declare(sd, *, model_id, profile, now=None, session_id=None, note=None):
    """Модель объявляет себя. Вызывается ОДИН раз при старте сеанса.

    Профиль вне известных трактуется как weak — самый ограничительный: опечатка
    в имени профиля не имеет права выдать полные права.
    """
    if not (model_id or "").strip():
        raise ValueError("model_id пуст: объявление без имени бессмысленно")
    now = now or dt.datetime.now(dt.timezone.utc)
    safe_profile = profile if profile in KNOWN_PROFILES else "weak"
    doc = {"model_id": model_id.strip(), "profile": safe_profile,
           "declared_utc": now.isoformat(),
           "session_id": session_id if session_id is not None else current_session_id(),
           "harness": os.environ.get("AI_AGENT"),
           "profile_note": (None if safe_profile == profile else
                            f"профиль {profile!r} не распознан — понижен до weak")}
    if note:
        doc["note"] = note
    p = Path(sd) / SESSION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def read_declaration(sd):
    p = Path(sd) / SESSION_FILE
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - битое объявление равнозначно отсутствию
        return None
    return doc if doc.get("model_id") else None


def current(sd, cfg, *, session_id=None):
    """Кто торгует сейчас.

    → {model_id, profile, source, ok, reason}

    source: 'session' — объявлено этой сессией (норма);
            'config'  — объявления нет, взято из конституции (fallback);
            'stale'   — объявляла ДРУГАЯ сессия, доверять нельзя.

    ok=False означает, что идентичность не подтверждена текущим сеансом. Это не
    техническая придирка: неверный model_id тихо портит калибровку и by_model, а
    задним числом разделить перемешанные записи нечем.
    """
    doc = read_declaration(sd)
    live = session_id if session_id is not None else current_session_id()

    if doc is None:
        return {"model_id": cfg.model.id, "profile": cfg.model.profile,
                "source": "config", "ok": False,
                "reason": "модель не объявлена в этом сеансе — записи будут "
                          f"подписаны значением из конституции ({cfg.model.id}). "
                          "Объяви себя: python scripts/session_start.py"}

    declared_session = doc.get("session_id")
    if live and declared_session and declared_session != live:
        return {"model_id": doc["model_id"], "profile": doc["profile"],
                "source": "stale", "ok": False,
                "reason": f"объявление сделано другой сессией Claude Code "
                          f"({declared_session[:8]}…, сейчас {live[:8]}…): возможно, "
                          "работает другая модель. Объяви себя заново"}

    return {"model_id": doc["model_id"], "profile": doc["profile"],
            "source": "session", "ok": True,
            "reason": f"объявлено этим сеансом: {doc['model_id']} ({doc['profile']})"}


def effective(sd, cfg, *, session_id=None):
    """(model_id, profile) для штампа в записи. Всегда что-то возвращает —
    писать записи без подписи хуже, чем подписать значением из конституции;
    о сомнительности сообщает current()."""
    st = current(sd, cfg, session_id=session_id)
    return st["model_id"], st["profile"]
