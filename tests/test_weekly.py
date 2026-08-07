"""Недельный контур (задача 8.3): аудит недели, понедельничный гэп, пятничный flat.

Недельный разбор отвечает на вопросы, которые не видны за день: тактика
деградирует или просто была плохая неделя; издержки растут; режим рынка сменился
и старые сетапы больше не работают; какая из моделей ведёт себя лучше.

Понедельничный гэп и пятничный flat — два места, где календарь важнее анализа:
за выходные цена уезжает без возможности выйти по стопу, а в понедельник первый
час торгуют не участники, а разрывы ликвидности.
"""
import dataclasses
import datetime as dt
import json

import pytest

from scripts.weekly_audit import build_weekly
from trader_lib.config import load_config
from trader_lib.session import monday_gap_state, session_gate

UTC = dt.timezone.utc
# воскресенье вечером недели уже прошло: аудит снимается в пятницу вечером
FRIDAY = dt.datetime(2026, 7, 31, 19, 30, tzinfo=UTC)
MONDAY = dt.datetime(2026, 7, 27, 7, 30, tzinfo=UTC)


def _cfg(tmp_path, **over):
    cfg = load_config("config/trader.config.json")
    cfg = dataclasses.replace(cfg, account={**cfg.account, "state_dir": str(tmp_path)})
    for block, values in over.items():
        cfg = dataclasses.replace(cfg, **{block: dataclasses.replace(
            getattr(cfg, block), **values)})
    return cfg


def _dec(trade_id, *, days_ago=1, setup="ema_pullback", model="claude-opus-5",
         costs=0.02, regime="тренд", confidence=0.6, planned=True):
    return {"type": "decision", "trade_id": trade_id, "symbol": "XAUUSD",
            "ts": (FRIDAY - dt.timedelta(days=days_ago)).isoformat(),
            "setup_type": setup, "setup_status": "подтверждён", "regime": regime,
            "confidence": confidence, "model_id": model, "model_profile": "strong",
            "session_phase": "LONDON", "planned": planned, "costs_R": costs,
            "risk_usd": 100.0, "entry": 2400.0, "sl": 2395.0}


def _out(trade_id, R, *, days_ago=1):
    return {"type": "outcome", "trade_id": trade_id, "R": R,
            "close_ts": (FRIDAY - dt.timedelta(days=days_ago)).isoformat(),
            "profit": R * 100.0, "exit_reason": "tp"}


def _state(tmp_path, journal):
    (tmp_path / "journal.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in journal),
        encoding="utf-8")


def _run(tmp_path, journal=(), cfg=None):
    _state(tmp_path, list(journal))
    return build_weekly(cfg or _cfg(tmp_path), now=FRIDAY)


# --------------------------------------------------------------------------
# состав аудита
# --------------------------------------------------------------------------

def test_weekly_audit_sections(tmp_path):
    journal = []
    for i in range(6):
        journal += [_dec(f"a{i}", setup="good", days_ago=1 + i % 5),
                    _out(f"a{i}", 1.5, days_ago=1 + i % 5)]
    for i in range(6):
        journal += [_dec(f"b{i}", setup="bad", days_ago=1 + i % 5),
                    _out(f"b{i}", -1.0, days_ago=1 + i % 5)]
    r = _run(tmp_path, journal)

    for section in ("week", "trades", "winners", "losers", "costs", "regimes",
                    "quarantine_candidates", "by_model", "unplanned"):
        assert section in r, f"нет раздела {section}"
    assert "good" in {x["setup"] for x in r["winners"]}
    assert "bad" in {x["setup"] for x in r["losers"]}


def test_quarantine_candidates_need_sample(tmp_path):
    """Кандидат в карантин — тактика, которая была в плюсе и ушла в минус на
    ЗАМЕТНОЙ выборке. Две неудачные сделки — не повод."""
    journal = [_dec("x1", setup="fresh"), _out("x1", -1.0),
               _dec("x2", setup="fresh"), _out("x2", -1.0)]
    r = _run(tmp_path, journal)
    assert r["quarantine_candidates"] == []


def test_costs_dynamics_reported(tmp_path):
    """Издержки в долях R — то, что тихо съедает результат: их надо смотреть
    в динамике, а не по одной сделке."""
    journal = [_dec("c1", costs=0.02, days_ago=5), _out("c1", 1.0, days_ago=5),
               _dec("c2", costs=0.09, days_ago=1), _out("c2", 1.0, days_ago=1)]
    r = _run(tmp_path, journal)
    assert r["costs"]["median_R"] is not None
    assert r["costs"]["max_R"] == pytest.approx(0.09)
    assert r["costs"]["over_limit"] == 0, "предел из конфига — 0.10"


def test_costs_over_limit_counted(tmp_path):
    journal = [_dec("c1", costs=0.20), _out("c1", 1.0)]
    assert _run(tmp_path, journal)["costs"]["over_limit"] == 1


def test_regime_drift_reported(tmp_path):
    """Смена режима недели объясняет, почему сетапы перестали работать."""
    journal = [_dec("r1", regime="тренд вверх", days_ago=5), _out("r1", 1.0, days_ago=5),
               _dec("r2", regime="флет", days_ago=1), _out("r2", -1.0, days_ago=1)]
    r = _run(tmp_path, journal)
    assert set(r["regimes"]) >= {"тренд вверх", "флет"}


def test_by_model_comparison_when_several(tmp_path):
    """Если за неделю работали разные модели — их надо сравнить, иначе смена
    модели не отличима от смены рынка."""
    journal = [_dec("m1", model="claude-opus-5"), _out("m1", 1.0),
               _dec("m2", model="слабая"), _out("m2", -1.0)]
    r = _run(tmp_path, journal)
    assert set(r["by_model"]) == {"claude-opus-5", "слабая"}
    assert r["several_models"] is True


def test_single_model_marked(tmp_path):
    journal = [_dec("m1"), _out("m1", 1.0)]
    assert _run(tmp_path, journal)["several_models"] is False


def test_unplanned_summary(tmp_path):
    """Окупается импровизация или это просто скука."""
    journal = [_dec("p1", planned=True), _out("p1", 1.0),
               _dec("u1", planned=False), _out("u1", -1.0),
               _dec("u2", planned=False), _out("u2", -0.5)]
    r = _run(tmp_path, journal)
    assert r["unplanned"]["n"] == 2
    assert r["unplanned"]["sum_R"] == pytest.approx(-1.5)


def test_previous_week_excluded(tmp_path):
    journal = [_dec("old", days_ago=12), _out("old", 5.0, days_ago=12),
               _dec("new", days_ago=2), _out("new", 1.0, days_ago=2)]
    r = _run(tmp_path, journal)
    assert r["trades"]["closed"] == 1 and r["trades"]["sum_R"] == pytest.approx(1.0)


def test_empty_week_does_not_crash(tmp_path):
    r = _run(tmp_path, [])
    assert r["trades"]["closed"] == 0 and r["winners"] == []
    assert r["markdown"].startswith("# Недельный аудит")


# --------------------------------------------------------------------------
# понедельничный гэп
# --------------------------------------------------------------------------

def test_monday_gap_protocol(tmp_path):
    """Гэп больше 0.5 ATR — первый час только наблюдение: цена открытия
    выходных не защищена стопом, и ликвидность в первый час не та."""
    cfg = _cfg(tmp_path)
    st = monday_gap_state(utc_now=MONDAY, gap_atr=0.9, cfg=cfg)
    assert st["observe_only"] is True and "гэп" in st["reason"]
    assert st["until"].hour == 8      # окно открывается в 07:00 → час наблюдения


def test_small_monday_gap_allows_trading(tmp_path):
    st = monday_gap_state(utc_now=MONDAY, gap_atr=0.2, cfg=_cfg(tmp_path))
    assert st["observe_only"] is False


def test_monday_gap_window_expires(tmp_path):
    late = MONDAY.replace(hour=9)
    st = monday_gap_state(utc_now=late, gap_atr=1.5, cfg=_cfg(tmp_path))
    assert st["observe_only"] is False, "после часа наблюдения запрет снимается"


def test_gap_protocol_only_on_monday(tmp_path):
    wednesday = dt.datetime(2026, 7, 29, 7, 30, tzinfo=UTC)
    st = monday_gap_state(utc_now=wednesday, gap_atr=1.5, cfg=_cfg(tmp_path))
    assert st["observe_only"] is False


def test_unknown_gap_is_not_a_block(tmp_path):
    """Гэп неизвестен (нет ATR или баров) — это не повод запретить день; о
    недостатке данных скажет брифинг."""
    st = monday_gap_state(utc_now=MONDAY, gap_atr=None, cfg=_cfg(tmp_path))
    assert st["observe_only"] is False and st["reason"]


# --------------------------------------------------------------------------
# пятничный flat
# --------------------------------------------------------------------------

def test_friday_flat_enforced(tmp_path):
    """После friday_flat_utc позиции не переносятся через выходные."""
    st = session_gate(utc_now=FRIDAY, cfg=_cfg(tmp_path))
    assert st["flat_required"] is True and st["allow_new"] is False


def test_friday_before_flat_allows_management(tmp_path):
    st = session_gate(utc_now=FRIDAY.replace(hour=13), cfg=_cfg(tmp_path))
    assert st["flat_required"] is False


# --------------------------------------------------------------------------
# ход сделок (MFE/MAE) — числа, по которым решается судьба наращивания
# --------------------------------------------------------------------------

def _out_mfe(trade_id, R, *, mfe, mae=-0.4, days_ago=1):
    return {**_out(trade_id, R, days_ago=days_ago), "mfe_R": mfe, "mae_R": mae}


def test_считает_сколько_сделок_дошло_до_порога_долива(tmp_path):
    """Первый из двух вопросов, ради которых решение отложено на неделю."""
    journal = [_dec("a0"), _out_mfe("a0", 1.0, mfe=1.4),
               _dec("a1"), _out_mfe("a1", -1.0, mfe=0.7),
               _dec("a2"), _out_mfe("a2", -1.0, mfe=0.1)]
    e = _run(tmp_path, journal)["excursion"]
    assert e["reached"]["0.5"] == 2      # 1.4 и 0.7
    assert e["reached"]["1.0"] == 1      # только 1.4
    assert e["reached"]["2.0"] == 0


def test_считает_сколько_из_дошедших_отдало_прибыль(tmp_path):
    """Второй вопрос: разница между «дошло» и «дошло и удержало» — это и есть
    вся экономика метода. Без неё «дошло 8 из 10» ничего не значит."""
    journal = [_dec("a0"), _out_mfe("a0", 1.0, mfe=1.2),     # дошло и удержало
               _dec("a1"), _out_mfe("a1", -1.0, mfe=1.3),    # дошло и отдало
               _dec("a2"), _out_mfe("a2", 0.0, mfe=1.1)]     # дошло и в ноль
    e = _run(tmp_path, journal)["excursion"]
    assert e["reached"]["1.0"] == 3
    assert e["given_back"]["1.0"] == 2


def test_неизмеренные_сделки_не_притворяются_измеренными(tmp_path):
    """None обязан остаться None: ноль означал бы «никуда не ходила» и занизил
    бы долю дошедших, то есть ответил бы «метод не нужен» отсутствием данных."""
    journal = [_dec("a0"), _out_mfe("a0", 1.0, mfe=1.5),
               _dec("a1"), _out("a1", -1.0)]        # старый исход без MFE
    e = _run(tmp_path, journal)["excursion"]
    assert e["measured"] == 1 and e["unmeasured"] == 1
    assert e["reached"]["1.0"] == 1
    assert e["median_mfe_R"] == 1.5      # медиана только по измеренным


def test_пустая_неделя_не_роняет_отчёт(tmp_path):
    e = _run(tmp_path, [])["excursion"]
    assert e["measured"] == 0 and e["median_mfe_R"] is None
    assert all(v == 0 for v in e["reached"].values())


def test_раздел_попадает_в_markdown(tmp_path):
    """Отчёт читает человек, а не только тест: числа обязаны быть на виду."""
    journal = [_dec("a0"), _out_mfe("a0", -1.0, mfe=1.3)]
    md = _run(tmp_path, journal)["markdown"]
    assert "Ход сделок" in md
    assert "дошло до +1.0R" in md
    assert "docs/plan_team.md" in md     # где записаны критерии решения
