"""Стартовый playbook (задача 7.3) и макро-усилитель (7.4).

ЭТОТ ФАЙЛ ПРОВЕРЯЕТ, ЧТО PLAYBOOK ОСТАЛСЯ ШАБЛОНОМ, А НЕ СТАЛ СВОДОМ ПРАВИЛ.

Граница проекта: код запрещает риск, но не предписывает тактику. Уровни входа —
суждение модели на конкретный день, поэтому в playbook они плейсхолдеры, а
вписываются в план дня. Первая версия этих тестов требовала от playbook
КОНКРЕТНЫХ числовых уровней — то есть толкала зашить готовые тактики в
статический документ. Исправлено: теперь тест требует ровно обратного.

Что проверяется:
  * структура разбирается тем же парсером, что и план дня (иначе перенести
    гипотезу в план одним движением нельзя);
  * у каждой гипотезы есть убийца и горизонт — без них сделка держится «пока не
    станет ясно», а ясно не становится;
  * уровней НЕТ, и парсер честно об этом сообщает;
  * формулировки остаются наблюдениями, а не запретами.
"""
from pathlib import Path

from trader_lib.day_plan import alerts_from_plan, parse_day_plan
from trader_lib.news import symbol_currencies

SEED = Path(__file__).resolve().parents[1] / "docs" / "playbook_seed.md"


def _text():
    return SEED.read_text(encoding="utf-8")


def _plan():
    return parse_day_plan(_text())


def test_seed_parses_and_has_5_hypotheses():
    plan = _plan()
    assert [h["id"] for h in plan["hypotheses"]] == ["H1", "H2", "H3", "H4", "H5"]
    for h in plan["hypotheses"]:
        assert h["condition"] and h["stop"], f"{h['id']}: неполная гипотеза"
        assert h["setup_type"], h["id"]


def test_seed_is_a_template_not_an_armed_plan():
    """Уровни — плейсхолдеры, и это НАМЕРЕННО: где сегодня граница диапазона,
    решает модель. Парсер обязан честно сказать, что уровня нет, а не
    подставить число."""
    plan = _plan()
    assert plan["problems"], "шаблон без уровней обязан давать problems"
    assert all("уровень" in p for p in plan["problems"]), plan["problems"]
    # и, как следствие, вооружить датчик прямо из playbook нельзя
    assert alerts_from_plan(plan) == []
    assert "ШАБЛОНЫ" in _text() and "плейсхолдер" in _text()


def test_every_hypothesis_has_alert_type_declared():
    """Тип алерта задан — значит модели остаётся вписать уровень, а не
    придумывать способ, которым её разбудят."""
    for h in _plan()["hypotheses"]:
        assert h["alert_spec"], f"{h['id']}: нет строки «Алерт:»"
        assert any(t in h["alert_spec"] for t in
                   ("price_above", "price_below", "price_touch")), h["alert_spec"]


def test_each_hypothesis_has_a_killer_and_horizon():
    """Убийца — то, что делает гипотезу мёртвой на сегодня. Без него сделка
    держится «пока не станет ясно»."""
    for h in _plan()["hypotheses"]:
        assert h["killer"], f"{h['id']}: нет условия отмены"
        assert "алерт" in h["killer"], f"{h['id']}: убийца без алерта"
        assert h["horizon_minutes"], f"{h['id']}: нет горизонта"


def test_symbols_are_tradable_by_the_news_gate():
    """Символы гипотез обязаны опознаваться новостным гейтом: иначе вход по ним
    блокируется всегда (валюты неизвестны — торговать вслепую нельзя)."""
    for h in _plan()["hypotheses"]:
        assert symbol_currencies(h["symbol"]) is not None, h["symbol"]


# --------------------------------------------------------------------------
# гипотезы, а не правила
# --------------------------------------------------------------------------

def test_framed_as_hypotheses_competing_with_own_ideas():
    text = _text()
    assert "гипотезы, а не правила" in text
    assert "на равных" in text
    assert "изучаю" in text and "×0.2" in text


def test_no_hard_prohibitions_in_tactics():
    """Тактика не запрещается кодом и не должна запрещаться документом: строки
    про слабые места гипотезы — наблюдения, которые модель проверяет на своей
    статистике. Слово «запрещено» здесь означало бы подмену границы: код
    ограничивает риск, а не выбор тактики."""
    text = _text()
    assert "Запрещено" not in text and "запрещено" not in text
    assert "Что обычно ломает" in text
    assert "верь данным" in text


def test_own_hypotheses_have_equal_rights():
    assert "Своя гипотеза заводится тем же порядком и с теми же правами" in _text()


# --------------------------------------------------------------------------
# 7.4: макро-усилитель
# --------------------------------------------------------------------------

def test_macro_hypothesis_marked_unavailable():
    """7.4 закрыт фактом, а не кодом: живой зонд показал, что макро-символов у
    брокера нет. Гипотеза, опирающаяся на данные, которых нет, — приглашение
    их выдумать."""
    text = _text()
    assert "H5" in text and "НЕДОСТУПНА" in text
    assert "DXY" in text and "BRENT" in text
    assert "bootstrap_env" in text


def test_regime_notes_are_observations():
    text = _text()
    assert "не запреты" in text
    for hint in ("волатильность", "спред", "Сжатие", "UNCLEAR"):
        assert hint in text
