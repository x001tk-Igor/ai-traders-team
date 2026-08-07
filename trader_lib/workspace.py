"""Пространства имён трейдеров команды (Ф3).

У команды часть состояния личная, часть общая, и спутать их нельзя в обе
стороны:

  Общий журнал перемешал бы решения трёх механизмов в один бакет статистики.
  Разделить записи задним числом нечем — ровно так на неделе 27–31.07 пять
  ярлыков сетапов осели на восьми сделках, и ни один не дошёл до n≥20.

  Личный реестр экспозиции, наоборот, дал бы каждому трейдеру собственную
  картину мира: каждый видел бы только свои позиции, и кластерный потолок
  («одна открытая позиция на фактор риска, командой целиком») перестал бы
  работать именно тогда, когда он нужен.

Правило простое: ЛИЧНОЕ — то, что описывает решения конкретного трейдера,
ОБЩЕЕ — то, что описывает мир, одинаковый для всех.

ОДИНОЧНЫЙ РЕЖИМ НЕПРИКОСНОВЕНЕН. trader=None даёт раскладку файлов ровно как
до появления команды: на ней стоят все существующие тесты и работающая
неделя. Команда добавляет подкаталоги, а не переезжает.
"""
import os
import re
from pathlib import Path

from trader_lib.config import state_dir

TRADERS_SUBDIR = "traders"

# Мир, одинаковый для всех: рынок, счёт, конституция, пульс защиты.
SHARED_FILES = (
    "clusters.json",
    "spread_median.json",
    "spread_live.json",
    "news_cache.json",
    "account_init.json",
    "day_baseline.json",
    "config_hash.json",
    "watch_heartbeat.json",
    "env_profile.json",
    "allocation.json",
    "telegram.json",
)

# Решения конкретного трейдера: его память, его статистика, его будильники.
PERSONAL_FILES = (
    "journal.jsonl",
    "alert_events.jsonl",
    "alerts.json",
    "day_plan.md",
    "playbook.md",
    "stats.json",
    "scorecard.md",
    "model_session.json",
    "open_intent.md",
    "notifications.jsonl",
)

_NAME_RE = re.compile(r"^[A-Za-zА-Яа-я0-9_-]{1,32}$")


def _validated(trader):
    """Имя трейдера попадает в путь файловой системы — обход каталога здесь
    означал бы запись куда угодно, поэтому проверка строгая, а не косметическая.
    """
    name = (trader or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"недопустимое имя трейдера {trader!r}: ожидаются буквы, цифры, "
            "дефис и подчёркивание (до 32 символов), без разделителей пути")
    return name


def trader_dir(cfg, trader, *, create=False):
    """Каталог личного состояния трейдера."""
    path = Path(state_dir(cfg)) / TRADERS_SUBDIR / _validated(trader)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_path(cfg, name, *, trader=None, create=False):
    """Путь к файлу состояния для конкретного трейдера (или общий).

    Незнакомое имя файла считается ЛИЧНЫМ. Худшее последствие такой ошибки —
    лишняя копия у трейдера; обратная (счесть личное общим) молча перемешала бы
    журналы разных механизмов, и это уже неисправимо.
    """
    root = Path(state_dir(cfg))
    if trader is None or name in SHARED_FILES:
        return root / name
    return trader_dir(cfg, trader, create=create) / name


def trader_state_dir(cfg, trader=None, *, create=True):
    """Корень ЛИЧНОГО состояния: каталог трейдера либо общий корень в
    одиночном режиме.

    Удобен скриптам, которые работают только с личными файлами (журнал, план,
    объявление модели). Скриптам, читающим И общее (новости, базы счёта, карту
    кластеров), этот помощник НЕ подходит — им нужен workspace_path по каждому
    файлу, иначе общие файлы будут искаться в каталоге трейдера и не найдутся.
    """
    if trader is None:
        return Path(state_dir(cfg))
    return trader_dir(cfg, trader, create=create)


def list_traders(cfg):
    """Кто заведён в команде — по подкаталогам на диске."""
    root = Path(state_dir(cfg)) / TRADERS_SUBDIR
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def resolve_trader(explicit=None):
    """Кто «я» в этом процессе: аргумент CLI, иначе переменная окружения.

    Переменная нужна для скриптов, которые ещё не получили флаг: сеанс трейдера
    выставляет её один раз и не передаёт имя в каждую команду руками.
    """
    return explicit or os.environ.get("TRADER_ID") or None
