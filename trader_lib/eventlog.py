"""Единый лог событий по времени ПК (задача владельца счёта, 2026-07-27).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ, ЕСЛИ ЕСТЬ ЖУРНАЛЫ. Журналы машинные: journal.jsonl —
контракт для статистики, alert_events.jsonl — след пробуждений, оба в UTC и
оба в JSON. Их читает код. Этот файл читает человек: одна строка на событие,
время локальное, без раскрытия JSON.

ВРЕМЯ — МЕСТНОЕ ВРЕМЯ ТОЙ МАШИНЫ, ГДЕ СТОИТ СИСТЕМА, без пересчёта. Решение
владельца счёта: на его ПК это будет его время. Внутренние метки при этом остаются в UTC
и не трогаются — от них зависит граница торгового дня и вся стена −3%. Смещение
машины записывается в шапку файла, чтобы через месяц можно было сверить.

ЗАПИСЬ В ЛОГ НИКОГДА НЕ ЛОМАЕТ ТОРГОВЛЮ. Любая ошибка глотается: диск занят,
права, кодировка — это не повод отменить закрытие позиции. Молчание лога хуже
отсутствия лога только для разбора, а не для денег.

ТИКОВ ДАТЧИКА ЗДЕСЬ НЕТ. Он делает ~3600 тиков в час; строка на каждый
превратила бы файл в шум, в котором событие не найти. Только события.
"""
import datetime as dt
import time
from pathlib import Path

# Категории. Закрытый список: свободные ярлыки расползаются, и через месяц
# grep по логу перестаёт работать.
CATEGORIES = (
    "SESSION",   # открытие и закрытие сеанса работы модели
    "WAKE",      # пробуждение по алерту
    "THINK",     # рассуждение модели без сделки (наблюдение)
    "GATE",      # решение предвходового гейта
    "ENTER",     # вход
    "EXIT",      # выход, частичка, перенос стопа
    "VALVE",     # действия стоп-крана
    "NOTIFY",    # уведомление человеку
    "ERROR",     # отказ любого узла
)

LOG_DIR = "logs"


def _local_now():
    return dt.datetime.now()


def _tz_note():
    off = -time.timezone if not time.daylight else -time.altzone
    sign = "+" if off >= 0 else "-"
    hours, rem = divmod(abs(off), 3600)
    return f"{time.tzname[0]} (UTC{sign}{hours:02d}:{rem // 60:02d})"


def log_path(sd, *, now=None):
    now = now or _local_now()
    return Path(sd) / LOG_DIR / f"{now:%Y-%m-%d}.log"


def log(sd, category, message, *, now=None, **fields):
    """Одна строка события. Возвращает записанную строку или None, если не
    получилось (вызывающему это знать не обязательно — он торгует, а не ведёт
    делопроизводство)."""
    now = now or _local_now()
    cat = category if category in CATEGORIES else "ERROR"
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    line = f"{now:%H:%M:%S.%f}"[:-3] + f" | {cat:<7} | {message}"
    if extra:
        line += f" | {extra}"
    try:
        path = log_path(sd, now=now)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(f"# лог ИИ-трейдера · {now:%Y-%m-%d} · время машины: "
                        f"{_tz_note()}\n")
                f.write("# формат: время | КАТЕГОРИЯ | событие | поля\n")
            f.write(line + "\n")
            f.flush()
    except Exception:  # noqa: BLE001 - лог не имеет права ломать торговлю
        return None
    return line


def read_log(sd, *, now=None):
    """Строки лога за день (без шапки) — для тестов и разбора."""
    path = log_path(sd, now=now)
    if not path.exists():
        return []
    return [x for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip() and not x.startswith("#")]
