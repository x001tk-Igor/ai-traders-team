"""Защита конституции (задача 8.2): агент не может тихо изменить свои лимиты.

ЗАЧЕМ ЭТО ВООБЩЕ НУЖНО. Все ограничения риска живут в
`config/trader.config.json`, и модель имеет к нему доступ на чтение — а на том же
ПК у неё есть инструменты записи файлов. Ничто, кроме этого модуля, не мешает ей
однажды «на минутку» поднять `daily_loss_limit_pct` до 10, чтобы не мешало
торговать. Дело не в злом умысле: достаточно рассуждения «сегодня особый день,
рынок понятный, стена мешает» — и лимит, который существует ровно для таких
рассуждений, исчезнет.

КАК УСТРОЕНО. Хэш риск-значимых блоков конфига сравнивается с подтверждённым
хэшем в `config_hash.json`. Расхождение → предвходовой гейт запрещает вход с
причиной «конституция изменена, нужно подтверждение владельца счёта». Подтверждает человек:
`python scripts/constitution_ack.py`.

ЧЕГО ЭТА ЗАЩИТА НЕ ДЕЛАЕТ И ПОЧЕМУ ЭТО НАЗВАНО ЧЕСТНО. Она не мешает изменить
конфиг — она мешает изменить его НЕЗАМЕТНО. Модель, имеющая право писать файлы,
может записать и config_hash.json; тогда защита обходится в два действия вместо
одного. Реальная граница здесь не техническая, а операционная: расхождение
попадает в уведомления и в дневной отчёт, то есть у человека остаётся след.
Утверждать большее было бы обманом — поэтому этого не написано ни в docstring,
ни в constitution.md.

ХЭШИРУЮТСЯ НЕ ВСЕ БЛОКИ. Только те, изменение которых меняет риск: risk,
session, news, alerts, instruments, constitution. Блок model (id и профиль)
меняется законно при переносе на другую модель, и требовать подтверждения на
каждую смену модели значило бы приучить человека подтверждать не глядя.
"""
import hashlib
import json
from pathlib import Path

# Блоки, изменение которых меняет риск. goal (цель +10%) и account (метки, пути)
# сюда не входят: они не ограничивают ни размер, ни частоту, ни направление.
GUARDED_BLOCKS = ("risk", "session", "news", "alerts", "instruments", "constitution")

HASH_FILE = "config_hash.json"


def config_hash(config_dict, blocks=GUARDED_BLOCKS):
    """SHA-256 канонического JSON охраняемых блоков.

    sort_keys обязателен: порядок ключей в файле не должен влиять на хэш, иначе
    переформатирование конфига выглядело бы как изменение лимитов, а человек
    привыкал бы подтверждать «ложные срабатывания».
    """
    payload = {block: config_dict.get(block) for block in blocks}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_ack(path):
    """Подтверждённый хэш или None. Битый файл равнозначен отсутствию:
    лучше потребовать подтверждение заново, чем принять мусор за согласие."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    value = doc.get("config_hash")
    return value if isinstance(value, str) and value else None


def write_ack(path, *, config_dict, now, by="владелец счёта", note=None):
    """Подтверждение текущего состояния конституции. Пишет ЧЕЛОВЕК через
    scripts/constitution_ack.py — программного пути сюда из торгового контура
    нет намеренно."""
    doc = {"config_hash": config_hash(config_dict), "acked_utc": now.isoformat(),
           "acked_by": by, "blocks": list(GUARDED_BLOCKS)}
    if note:
        doc["note"] = note
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def check_config(config_dict, ack_path):
    """→ {'ok': bool, 'reason': str, 'current': hash, 'acked': hash|None}

    Первый запуск (подтверждения нет вовсе) — НЕ ok. Иначе защита включалась бы
    только со второго раза, а первый прогон на новом ПК шёл бы с любыми
    лимитами, какие оказались в файле.
    """
    current = config_hash(config_dict)
    acked = read_ack(ack_path)
    if acked is None:
        return {"ok": False, "current": current, "acked": None,
                "reason": "конституция не подтверждена: нет config_hash.json. "
                          "Проверь лимиты и подтверди — "
                          "python scripts/constitution_ack.py"}
    if acked != current:
        return {"ok": False, "current": current, "acked": acked,
                "reason": "конституция изменена с момента подтверждения "
                          f"(подтверждён {acked[:12]}…, сейчас {current[:12]}…) — "
                          "нужно подтверждение владельца счёта, торговля остановлена"}
    return {"ok": True, "current": current, "acked": acked,
            "reason": "конституция совпадает с подтверждённой"}
