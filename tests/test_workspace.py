"""Пространства имён трейдеров (Ф3).

ЗАЧЕМ. У команды часть состояния общая, часть — личная, и путать их нельзя в
обе стороны. Общий журнал перемешал бы статистику трёх механизмов в один
бакет — ровно та ошибка, из-за которой на неделе 27–31.07 пять ярлыков сетапов
осели на восьми сделках и ни один не дошёл до n≥20. Личный реестр экспозиции,
наоборот, дал бы каждому трейдеру собственную картину мира, и кластерный
потолок перестал бы работать: каждый видел бы только свои позиции.

ЧТО ЛИЧНОЕ: журнал, план дня, алерты, плейбук, статистика, объявление модели,
открытое намерение, логи и разборы. Всё, что описывает РЕШЕНИЯ конкретного
трейдера.

ЧТО ОБЩЕЕ: календарь новостей, медианы спреда (живые и барные), карта
кластеров, базы счёта, подтверждение конституции, пульс датчика. Всё, что
описывает МИР, одинаковый для всех.

ОБРАТНАЯ СОВМЕСТИМОСТЬ ОБЯЗАТЕЛЬНА. trader=None означает одиночный режим и
раскладку файлов ровно как сейчас: на ней стоят 900+ тестов и вся работающая
неделя. Команда не имеет права ломать одиночку.
"""
import dataclasses

from trader_lib.config import load_config, state_dir
from trader_lib.workspace import (
    PERSONAL_FILES,
    SHARED_FILES,
    trader_dir,
    workspace_path,
)


def _cfg(tmp_path):
    cfg = load_config("config/trader.config.json")
    return dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})


def test_solo_mode_keeps_the_current_layout(tmp_path):
    """trader=None — файлы там же, где были всю неделю. Иначе одиночный режим
    и 900+ тестов на нём сломаются об команду."""
    cfg = _cfg(tmp_path)
    assert workspace_path(cfg, "journal.jsonl") == tmp_path / "journal.jsonl"
    assert workspace_path(cfg, "alerts.json") == tmp_path / "alerts.json"


def test_personal_files_go_under_the_trader(tmp_path):
    cfg = _cfg(tmp_path)
    for name in ("journal.jsonl", "alerts.json", "day_plan.md", "playbook.md",
                 "stats.json", "model_session.json"):
        assert workspace_path(cfg, name, trader="fade") == \
            tmp_path / "traders" / "fade" / name, name


def test_shared_files_stay_common_even_for_a_trader(tmp_path):
    """Общие файлы обязаны оставаться общими, даже когда спрашивает трейдер:
    личная карта кластеров или личный реестр позиций сделали бы кластерный
    потолок бессмысленным — каждый видел бы только себя."""
    cfg = _cfg(tmp_path)
    for name in ("clusters.json", "spread_live.json", "spread_median.json",
                 "news_cache.json", "account_init.json", "watch_heartbeat.json"):
        assert workspace_path(cfg, name, trader="fade") == tmp_path / name, name


def test_personal_and_shared_lists_do_not_overlap():
    """Файл, попавший в оба списка, вёл бы себя по-разному в зависимости от
    порядка проверок — то есть непредсказуемо."""
    assert not (set(PERSONAL_FILES) & set(SHARED_FILES))


def test_unknown_file_is_treated_as_personal(tmp_path):
    """Незнакомое имя безопаснее считать личным: худшее последствие — лишняя
    копия у трейдера. Обратная ошибка (счесть личное общим) молча перемешала бы
    журналы разных механизмов, и разделить их потом нечем."""
    cfg = _cfg(tmp_path)
    assert workspace_path(cfg, "новый_файл.json", trader="trend") == \
        tmp_path / "traders" / "trend" / "новый_файл.json"


def test_trader_dir_is_created_on_demand(tmp_path):
    cfg = _cfg(tmp_path)
    path = trader_dir(cfg, "trend", create=True)
    assert path.exists() and path == tmp_path / "traders" / "trend"


def test_trader_name_is_validated(tmp_path):
    """Имя трейдера идёт в путь файловой системы — обход каталога недопустим."""
    import pytest

    cfg = _cfg(tmp_path)
    for bad in ("../soul", "a/b", "", "  ", "."):
        with pytest.raises(ValueError):
            workspace_path(cfg, "journal.jsonl", trader=bad)


def test_shared_root_is_unchanged(tmp_path):
    """state_dir остаётся общим корнем: команда добавляет подкаталоги, а не
    переезжает."""
    cfg = _cfg(tmp_path)
    assert state_dir(cfg) == str(tmp_path)
