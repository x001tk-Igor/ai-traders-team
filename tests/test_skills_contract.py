"""Сторож скиллов и loop-промта (задача 6.4).

Скиллы — не украшение, а исполняемая часть контура: это единственное место, где
записано то, что код проверить не может (назови режим, перепиши alerts.json,
бери калибровку своей модели). Такие правила исчезают из документов молча — при
переписывании абзаца, при сокращении «для краткости», при рефакторинге. Тогда
модель просто перестаёт их выполнять, и это не проявится ни одним падением.

Тест проверяет НЕ формулировки, а наличие конкретных обязательств и совпадение
имён с кодом: имя флага, имя ключа статистики, имена файлов состояния. Если
что-то из этого переименовали в коде, а в скилле забыли — падает здесь, а не
через неделю на живом счёте.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
PROMPT = ROOT / "prompts" / "loop_prompt.md"


def _text(path):
    assert path.exists(), f"нет файла {path}"
    return path.read_text(encoding="utf-8")


def _flat(path):
    """Текст со схлопнутыми пробелами и переводами строк: сторож проверяет
    смысл, а не вёрстку. Иначе перенос строки внутри фразы ломает тест, хотя
    в документе всё написано."""
    return " ".join(_text(path).split())


@pytest.mark.parametrize("name", ["trader-perceive", "trader-recall",
                                  "trader-decide", "trader-reflect"])
def test_skill_exists_with_frontmatter(name):
    text = _text(SKILLS / name / "SKILL.md")
    assert text.startswith("---"), "скилл без frontmatter не подцепится"
    assert f"name: {name}" in text
    assert "description:" in text


# --------------------------------------------------------------------------
# trader-decide: рельс 5 — без него контур засыпает навсегда
# --------------------------------------------------------------------------

def test_decide_has_alert_rearm_rail():
    """Главное правило алертной петли: перед завершением цикла переписать
    alerts.json. Если оно выпадет из скилла, модель отработает один цикл и
    больше никогда не проснётся — и ни один тест кода этого не заметит."""
    text = _text(SKILLS / "trader-decide" / "SKILL.md")
    assert "alerts.json" in text
    assert "5 рельсов" in text or "Пять рельсов" in text
    assert "уснёшь" in text or "не проснёшься" in text


def test_decide_explains_once_and_its_cost():
    """Алерт без `once` и без `rearm_after_minutes` будит раз в минуту, пока
    условие истинно, и сжигает дневной лимит за 40 минут. Контракт это
    описывает, но модель читает СКИЛЛ — и без предупреждения ошибку совершают
    (я совершил её сам при первом живом вооружении датчика 2026-07-27)."""
    text = _flat(SKILLS / "trader-decide" / "SKILL.md")
    assert "once" in text and "rearm_after_minutes" in text
    assert "40" in text, "цена ошибки должна быть названа числом, а не словами"
    assert "глух" in text or "сжигает" in text


def test_loop_prompt_warns_about_once():
    text = _flat(PROMPT)
    assert '"once": true' in text or "once: true" in text
    assert "раз в минуту" in text


def test_alert_types_in_docs_match_the_contract():
    """Имена типов в скилле и промте обязаны существовать в контракте: датчик
    молча пропускает нераспознанный тип (этот дефект уже был в задаче 4.2)."""
    import re

    from scripts.alert_watch import SL_EVENT_TYPE, WALL_EVENT_TYPE
    from trader_lib.alerts import ALERT_TYPE_FIELDS

    # события стоп-крана — не алерты модели, но в промте упоминаются законно:
    # это то, что код рассказывает о СВОИХ действиях
    known = set(ALERT_TYPE_FIELDS) | {WALL_EVENT_TYPE, SL_EVENT_TYPE}
    for path in (SKILLS / "trader-decide" / "SKILL.md", PROMPT,
                 ROOT / "docs" / "playbook_seed.md"):
        text = _text(path)
        for name in re.findall(r"`(position_[a-z_]+|price_[a-z_]+|wall_[a-z_]+)`", text):
            assert name in known, f"{path.name}: тип {name} вне контракта"


def test_decide_requires_regime_but_leaves_taxonomy_free():
    """Назвать режим — обязательно, выбрать его из чужого списка — нет.

    Таксономия модели свободна по замыслу, и на этом стоит код: regime —
    свободный текст, а `_normalize_label`/`label_drift` в score.py существуют
    именно потому, что ярлыки придумывает модель. Закрытый перечень в скилле
    противоречил бы собственному коду (и был здесь — исправлено)."""
    text = _text(SKILLS / "trader-decide" / "SKILL.md")
    assert "UNCLEAR" in text and "не торгую" in text
    assert "своими словами" in text
    assert "своя" in text or "твоя" in text
    assert "открытый" in text or "примеры" in text, \
        "список режимов обязан быть примерами, а не перечнем"
    assert "обязательно из списка" not in text.replace("не обязательно из списка", "")


def test_decide_separates_risk_limits_from_tactics():
    """Главная граница проекта: код запрещает риск, но не предписывает тактику.
    Она должна быть написана в скилле явно, иначе размывается сама собой."""
    text = _flat(SKILLS / "trader-decide" / "SKILL.md")
    assert "НЕ предписывает тактику" in text or "не предписывает тактику" in text
    assert "гипотезы, а не правила" in text
    assert "Код запрещает риск" in text


def test_decide_covers_planned_vs_unplanned_and_no_trade():
    text = _text(SKILLS / "trader-decide" / "SKILL.md")
    assert "внеплановый" in text
    assert "Отсутствие сделки" in text
    assert "инвалидац" in text.lower()


def test_decide_points_to_the_real_entry_and_exit_scripts():
    """Имена скриптов в скилле обязаны совпадать с кодом: модель вызывает
    ровно то, что здесь написано."""
    text = _text(SKILLS / "trader-decide" / "SKILL.md")
    assert "enter.py" in text and "exit.py" in text
    assert (ROOT / "scripts" / "enter.py").exists()
    assert (ROOT / "scripts" / "exit.py").exists()


# --------------------------------------------------------------------------
# trader-perceive
# --------------------------------------------------------------------------

def test_perceive_has_cold_start_focus_rule():
    """Разрешено семь пар — не значит нужно семь. Сетап подтверждается при
    n>=20; десять входов в день, размазанных по семи парам, дают полторы сделки
    на пару, и тактики месяцами висят с риском ×0.2. Правило обязано быть в
    скилле с этой арифметикой, иначе модель честно возьмёт всё разрешённое."""
    text = _flat(SKILLS / "trader-perceive" / "SKILL.md")
    assert "холодного старта" in text
    assert "20" in text and "×0.2" in text
    assert "два" in text or "двумя-тремя" in text
    # и это рекомендация, а не запрет: код ограничивает риск, не тактику
    assert "не запрет" in text or "рекомендация" in text


def test_reflect_owns_list_expansion():
    text = _flat(SKILLS / "trader-reflect" / "SKILL.md")
    assert "по одному" in text and "подтверждённый" in text


def test_perceive_requires_regime_and_null_discipline():
    text = _text(SKILLS / "trader-perceive" / "SKILL.md")
    assert "regime" in text
    assert "null" in text and "не додумывай" in text.lower()
    assert "2000 токенов" in text or "2k" in text.lower()


# --------------------------------------------------------------------------
# trader-recall: калибровка по своей модели
# --------------------------------------------------------------------------

def test_recall_uses_per_model_calibration():
    """Ключ должен называться так же, как в compute_stats: иначе модель пойдёт
    искать несуществующее поле и молча возьмёт общую калибровку."""
    from trader_lib.score import compute_stats

    text = _text(SKILLS / "trader-recall" / "SKILL.md")
    assert "calibration_by_model" in text
    assert "calibration_by_model" in compute_stats([], min_n_for_confirmed=20)


# --------------------------------------------------------------------------
# trader-reflect: три долга из ревью задачи 2.2
# --------------------------------------------------------------------------

def test_reflect_passes_progress_flag_with_real_name():
    """Долг 2.2: run_score умеет принимать прогресс к цели, но никто его не
    передавал, и scorecard честно писал «н/д». Имя флага сверяется с CLI."""
    text = _text(SKILLS / "trader-reflect" / "SKILL.md")
    assert "--progress-pct" in text
    assert "--progress-pct" in _text(ROOT / "scripts" / "run_score.py")


def test_reflect_warns_about_setup_label_fragmentation():
    """Долг 2.2: by_setup не нормализуется (на точный ключ опирается recall),
    поэтому разные написания дробят выборку и edge не находится никогда."""
    text = _text(SKILLS / "trader-reflect" / "SKILL.md")
    assert "by_setup" in text and "by_regime" in text
    assert "20" in text


def test_reflect_treats_label_drift_as_a_hint_not_a_conclusion():
    """Долг 2.2: label_drift сообщает о совпадении отпечатка, а не делает
    вывод. Неверное слияние двух сетапов необратимо."""
    text = _text(SKILLS / "trader-reflect" / "SKILL.md")
    assert "label_drift" in text
    assert "mr-fade" in text, "нужен конкретный пример ложного совпадения"
    assert "необратим" in text


def test_reflect_reviews_alert_quality():
    text = _text(SKILLS / "trader-reflect" / "SKILL.md")
    assert "noisy_alerts" in text
    assert "пуст" in text and "пробуждени" in text.lower()


def test_reflect_links_status_to_risk_multiplier():
    text = _text(SKILLS / "trader-reflect" / "SKILL.md")
    for status in ("изучаю", "подтверждён", "карантин"):
        assert status in text
    assert "0.2" in text and "0.1" in text


# --------------------------------------------------------------------------
# loop-промт: полный ритм дня
# --------------------------------------------------------------------------

def test_loop_prompt_describes_event_loop():
    text = _text(PROMPT)
    assert "Monitor(" in text and "alert_watch.py" in text
    assert "один раз" in text
    assert "alerts.json" in text


def test_loop_prompt_starts_with_model_declaration():
    """Идентичность нельзя определить кодом — модель обязана объявить себя
    сама, и это первая команда сеанса. Иначе записи подписываются строкой из
    конфига, а на другом ПК статистика двух моделей смешается молча."""
    text = _flat(PROMPT)
    assert "session_start.py" in text
    assert "--model" in text and "своё" in text
    assert "гейт не пустит" in text or "не пустит в рынок" in text


def test_loop_prompt_covers_day_rhythm():
    text = _text(PROMPT)
    for script in ("bootstrap_env.py", "brief.py", "review.py", "run_score.py"):
        assert script in text, f"в промте нет шага {script}"
    assert "day_plan.md" in text and "open_intent.md" in text


def test_loop_prompt_has_heartbeat_rule():
    text = _text(PROMPT)
    assert "90" in text and "watch_heartbeat.json" in text
    assert "pending_undelivered" in text


def test_every_script_is_reachable():
    """СТОРОЖ СВЯЗНОСТИ. Модуль, который никто не вызывает и который не назван
    ни в промте, ни в скиллах, ни в инструкциях человеку, — мёртвый груз: он
    покрыт тестами, выглядит рабочим и не срабатывает ни разу.

    Так уже случилось трижды: notify (уведомления не приходили), report
    (телеграм молчал), weekly_audit (аудит никто не запускал). Найдено аудитом
    2026-07-27, поэтому теперь это тест, а не внимательность."""
    import re

    scripts = {p.stem for p in (ROOT / "scripts").glob("*.py")
               if p.name != "__init__.py"}
    # где скрипт может быть законно назван
    haystack = " ".join(
        _text(p) for p in [
            *(ROOT / "scripts").glob("*.py"),
            *(ROOT / "trader_lib").glob("*.py"),
            PROMPT,
            *(SKILLS.glob("*/SKILL.md")),
            ROOT / "docs" / "preflight.md",
            ROOT / "docs" / "constitution.md",
            ROOT / "docs" / "model_acceptance.md",
        ] if p.exists())

    orphans = []
    for name in sorted(scripts):
        called = re.search(r"(?:import|from)\s+\S*\b" + name + r"\b", haystack)
        named = f"{name}.py" in haystack
        if not (called or named):
            orphans.append(name)
    assert orphans == [], f"построено, но никем не вызывается: {orphans}"


def test_decide_requires_a_record_for_every_wake():
    """Пробуждение без записи считается пустым, и полезный алерт помечается
    мусорным. Правило «одна из трёх записей» должно быть в скилле явно."""
    text = _flat(SKILLS / "trader-decide" / "SKILL.md")
    assert "observation" in text and "skip" in text
    assert "report.py" in text or "observed" in text
    assert "Telegram" in text or "телеграм" in text.lower()


def test_loop_prompt_scripts_exist():
    """Промт не должен звать то, чего нет: опечатка в имени скрипта
    обнаружится на живом счёте, а не здесь."""
    text = _text(PROMPT)
    for script in ("bootstrap_env.py", "brief.py", "alert_watch.py", "review.py",
                   "run_score.py"):
        assert script in text and (ROOT / "scripts" / script).exists()
