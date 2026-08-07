"""CLI риск-гейта на новом контракте (задача 1.6): сборка девяти входов гейта
из market/cfg/journal + fail-closed на каждом пути отказа. Всё офлайн на
FakeMarket — реальный MT5 не требуется.
"""
import dataclasses
import datetime as dt
import json

import scripts.risk_gate_cli as risk_gate_cli
from scripts.close_watch import find_orphans
from trader_lib.config import load_config
from trader_lib.mt5_client import FakeMarket
from trader_lib.risk_gate import evaluate_gate

NOW = dt.datetime(2026, 7, 25, 10, 0, tzinfo=dt.timezone.utc)
TODAY_START = dt.datetime(2026, 7, 25, 0, 0, tzinfo=dt.timezone.utc)
YESTERDAY = dt.datetime(2026, 7, 24, 12, 0, tzinfo=dt.timezone.utc)


def _cfg():
    return load_config("config/trader.config.json")


def _decision(ts, trade_id="t", **extra):
    return {"type": "decision", "ts": ts.isoformat(), "trade_id": trade_id,
            "symbol": "XAUUSD", "setup_type": "s", "confidence": 0.6,
            "action": "buy", "risk_usd": 10.0, **extra}


def _outcome(close_ts, R, trade_id="t"):
    return {"type": "outcome", "close_ts": close_ts.isoformat(), "R": R, "trade_id": trade_id}


def _write_journal(path, records):
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def pos(ticket, symbol, ptype, volume, price_open, sl, tp=0.0):
    return {"ticket": ticket, "symbol": symbol, "type": ptype, "volume": volume,
            "price_open": price_open, "sl": sl, "tp": tp, "price_current": price_open,
            "profit": 0.0, "magic": 1}


class _BrokenMarket(FakeMarket):
    """Симулирует недоступность MT5: любое обращение к рынку падает."""

    def account_info(self):
        raise RuntimeError("no connection to terminal")


class _NoSymbolInfoMarket(FakeMarket):
    def symbol_info(self, symbol):
        raise RuntimeError(f"no symbol_info {symbol}")


class _MultiSymbolMarket(FakeMarket):
    """symbol_info РЕАЛЬНО зависит от символа — в отличие от базового FakeMarket,
    который игнорирует аргумент symbol и всегда отдаёт то, что задано в
    конструкторе. Нужен, чтобы поймать регрессию вида «_symbol_info_map берёт
    symbol_info первой позиции для всех символов»."""

    _INFO = {
        "XAUUSD": {"point": 0.01, "digits": 2, "spread": 20, "trade_contract_size": 100,
                   "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01},
        "EURUSD": {"point": 0.0001, "digits": 5, "spread": 10, "trade_contract_size": 100000,
                   "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01},
    }

    def symbol_info(self, symbol):
        return self._INFO[symbol]


# ============================= build_gate_inputs ============================


def test_build_gate_inputs_assembles_all_nine_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    # XAUUSD point=0.01, contract=100 → value_per_point_per_lot=1.0
    # buy: entry 2400, sl 2390 → distance 10 → 1000 points * 1.0 * 0.05 лота = 50.0
    p1 = pos(1, "XAUUSD", 0, 0.05, 2400.0, 2390.0)
    market = FakeMarket(point=0.01, digits=2, spread_points=20,
                        account={"balance": 9700.0, "equity": 9700.0},
                        positions=[p1])
    records = [
        _decision(TODAY_START + dt.timedelta(hours=1), "a", planned=True),
        _decision(TODAY_START + dt.timedelta(hours=2), "b", planned=False),
        _decision(YESTERDAY, "y", planned=False),  # вчера — не считается
    ]
    v = risk_gate_cli.build_gate_inputs(market, cfg, records, now=NOW)

    assert v["equity"] == 9700.0
    assert v["day_start_equity"] == 9700.0     # нет day_baseline.json → откат на equity
    assert v["initial_balance"] == 9700.0      # нет account_init.json → откат на equity
    assert v["limits"] is cfg.risk
    assert v["open_risk_usd"] == 50.0
    assert v["unprotected_positions"] == 0
    assert v["positions_count"] == 1
    assert v["trades_today"] == 2              # 'a' и 'b', НЕ 'y' (вчера)
    assert v["unplanned_today"] == 1           # только 'b' (planned=False)
    assert v["loss_streak_mult"] == 1.0        # журнал без outcome — серии нет
    assert v["paused_until"] is None
    assert v["halt_rest_of_day"] is False
    assert v["profile_mult"] == 1.0            # cfg.model.profile == "strong"
    assert v["now"] == NOW


def test_build_gate_inputs_uses_day_baseline_file(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    (tmp_path / "day_baseline.json").write_text(json.dumps(
        {"day": NOW.date().isoformat(), "equity": 10000.0, "initial_balance": 10500.0}))
    cfg = _cfg()
    market = FakeMarket(account={"balance": 9900.0, "equity": 9900.0})
    v = risk_gate_cli.build_gate_inputs(market, cfg, [], now=NOW)
    assert v["day_start_equity"] == 10000.0
    assert v["initial_balance"] == 10500.0
    assert v["equity"] == 9900.0


def test_build_gate_inputs_computes_streak_from_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    market = FakeMarket()
    last_loss = TODAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(hours=1), -1.0, "t1"),
        _outcome(last_loss, -1.0, "t2"),
    ]
    now = last_loss + dt.timedelta(minutes=30)
    v = risk_gate_cli.build_gate_inputs(market, cfg, records, now=now)
    assert v["loss_streak_mult"] == cfg.risk.streak["two_losses"]["risk_mult"]
    assert v["paused_until"] is None
    assert v["halt_rest_of_day"] is False


def test_profile_mult_uses_configured_model_profile(monkeypatch, tmp_path):
    # пин: profile_mult реально читается из cfg.model.profile через quality.py,
    # а не захардкожен 1.0 (урок фазы: инвариант не должен держаться на
    # совпадении дефолтных значений конфига)
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    weak_cfg = dataclasses.replace(cfg, model=dataclasses.replace(cfg.model, profile="weak"))
    market = FakeMarket()
    v = risk_gate_cli.build_gate_inputs(market, weak_cfg, [], now=NOW)
    assert v["profile_mult"] == cfg.model.profile_rules["weak"]["risk_mult"]
    assert v["profile_mult"] != 1.0


def test_mixed_protected_and_unprotected_positions(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    protected = pos(7, "XAUUSD", 0, 0.05, 2400.0, 2390.0)  # risk 50.0
    naked = pos(8, "XAUUSD", 1, 0.05, 2400.0, 0.0)         # без стопа
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0},
                        positions=[protected, naked])
    v = risk_gate_cli.build_gate_inputs(market, cfg, [], now=NOW)
    assert v["open_risk_usd"] == 50.0          # риск без стопа НЕ входит в сумму
    assert v["unprotected_positions"] == 1
    assert v["positions_count"] == 2


def test_symbol_info_map_uses_correct_info_per_symbol(monkeypatch, tmp_path):
    # Если бы _symbol_info_map брал symbol_info ПЕРВОЙ позиции для всех символов
    # (частая однострочная регрессия), EURUSD получил бы point/contract от
    # XAUUSD, и его риск посчитался бы неверно (0.05 вместо 50.0) — сумма
    # разошлась бы, и тест бы это поймал.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    p_xau = pos(1, "XAUUSD", 0, 0.05, 2400.0, 2390.0)   # point=0.01, contract=100 → risk 50.0
    p_eur = pos(2, "EURUSD", 0, 0.1, 1.1000, 1.0950)    # point=0.0001, contract=100000 → risk 50.0
    market = _MultiSymbolMarket(account={"balance": 10000.0, "equity": 10000.0},
                                positions=[p_xau, p_eur])
    v = risk_gate_cli.build_gate_inputs(market, cfg, [], now=NOW)
    assert v["open_risk_usd"] == 100.0
    assert v["unprotected_positions"] == 0
    assert v["positions_count"] == 2


def test_previous_day_decisions_not_counted(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    market = FakeMarket()
    records = [
        _decision(YESTERDAY, "y1", planned=True),
        _decision(YESTERDAY, "y2", planned=True),
        _decision(TODAY_START + dt.timedelta(hours=1), "t1", planned=True),
    ]
    v = risk_gate_cli.build_gate_inputs(market, cfg, records, now=NOW)
    assert v["trades_today"] == 1
    assert v["unplanned_today"] == 0


def test_build_gate_inputs_empty_journal_no_crash(monkeypatch, tmp_path):
    # первый запуск в жизни — journal.jsonl ещё не существовал, read_records
    # вернул бы [] (см. trader_lib/journal.py)
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    market = FakeMarket()
    v = risk_gate_cli.build_gate_inputs(market, cfg, [], now=NOW)
    assert v["trades_today"] == 0
    assert v["unplanned_today"] == 0
    assert v["loss_streak_mult"] == 1.0
    assert v["paused_until"] is None
    assert v["halt_rest_of_day"] is False


def test_planned_field_absent_counts_as_unplanned(monkeypatch, tmp_path):
    # записи старше задачи 1.6 не имеют поля planned вовсе. Решение: трактовать
    # отсутствие как «не запланировано» — это строже (быстрее исчерпывает
    # бюджет внеплановых входов), а не разрешительный дефолт planned=True.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    market = FakeMarket()
    records = [
        _decision(TODAY_START + dt.timedelta(hours=1), "old1"),  # нет planned вовсе
        _decision(TODAY_START + dt.timedelta(hours=2), "old2"),  # тоже нет
        _decision(TODAY_START + dt.timedelta(hours=3), "new1", planned=True),
        _decision(TODAY_START + dt.timedelta(hours=4), "new2", planned=False),
    ]
    v = risk_gate_cli.build_gate_inputs(market, cfg, records, now=NOW)
    assert v["trades_today"] == 4
    assert v["unplanned_today"] == 3  # old1, old2 (нет поля) + new2 (явный False)


# ==================================== run ====================================


def test_run_matches_direct_gate_call(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    # journal.jsonl ещё не существует — read_records вернёт [] (первый запуск)
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)
    expected = evaluate_gate(equity=10000.0, day_start_equity=10000.0, initial_balance=10000.0,
                             limits=cfg.risk, open_risk_usd=0.0, unprotected_positions=0,
                             positions_count=0, trades_today=0, unplanned_today=0,
                             loss_streak_mult=1.0, paused_until=None, halt_rest_of_day=False,
                             profile_mult=1.0, now=NOW)
    expected["spread_excluded"] = {}
    assert v == expected
    assert v["verdict"] == "OK"
    # кэп 0.5% от 10 000 связывает раньше бюджета/K (100)
    assert v["max_risk_per_trade_usd"] == 50.0


def test_market_unavailable_returns_full_schema_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    v = risk_gate_cli.run(_BrokenMarket(), cfg, tmp_path / "journal.jsonl", now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    # схема совпадает с тем, что сам гейт отдаёт для заблокированного вердикта
    reference = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                              limits=cfg.risk, halt_rest_of_day=True, now=NOW)
    assert set(v) - {"error"} == set(reference)
    assert v["require_setup_status"] == "confirmed"
    assert v["planned_only"] is True
    assert v["unplanned_allowed"] is False
    assert v["blocked_by"] == "gate_error"
    assert "error" in v
    assert "no connection" in v["error"]


def test_missing_symbol_info_fails_closed(monkeypatch, tmp_path):
    # символ открытой позиции есть, а symbol_info по нему получить не удалось
    # (например, отвалилось соединение между чтением positions() и symbol_info())
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    p = pos(6, "XAUUSD", 0, 0.05, 2400.0, 2390.0)
    market = _NoSymbolInfoMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[p])
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert "error" in v


def test_unprotected_position_halts_new_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    naked = pos(5, "XAUUSD", 0, 0.05, 2400.0, 0.0)  # sl=0.0 → нет стопа
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[naked])
    # decision-запись есть (позиция учтена в журнале, не orphan) — этот тест
    # проверяет именно unprotected_positions (задача 1.5), не orphan (2.3);
    # см. test_unmatched_naked_position_blocks_via_orphan_not_unprotected
    # ниже для непойманной (без decision) версии той же голой позиции.
    journal_path = tmp_path / "journal.jsonl"
    _write_journal(journal_path, [_decision(TODAY_START, "5")])
    v = risk_gate_cli.run(market, cfg, journal_path, now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "unprotected_positions"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_unmatched_naked_position_blocks_via_orphan_not_unprotected(monkeypatch, tmp_path):
    # та же голая позиция (sl=0.0), что и выше, но БЕЗ decision-записи —
    # orphan-проверка срабатывает раньше самого гейта, поэтому причина запрета
    # orphan_positions, а не unprotected_positions: два разных пути к одному
    # и тому же вердикту HALT_NEW должны быть различимы в отчёте по коду.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    naked = pos(5, "XAUUSD", 0, 0.05, 2400.0, 0.0)
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[naked])
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "orphan_positions"
    assert v["max_risk_per_trade_usd"] == 0.0


# ==================== v2-лимиты реально связывают через run() ====================
# (находки review 2: сборка верна, но регресс-сеть не держала эти пути)


def test_streak_pause_blocks_via_run(monkeypatch, tmp_path):
    # серия из 3 убытков подряд сегодня → пауза (60 мин, cfg.risk.streak.
    # three_losses) ещё активна на момент now → HALT_NEW, blocked_by=paused
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    last_loss = TODAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(minutes=20), -1.0, "t1"),
        _outcome(last_loss - dt.timedelta(minutes=10), -1.0, "t2"),
        _outcome(last_loss, -1.0, "t3"),
    ]
    journal_path = tmp_path / "journal.jsonl"
    _write_journal(journal_path, records)
    now = last_loss + dt.timedelta(minutes=10)  # пауза (60 мин) ещё не истекла
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    v = risk_gate_cli.run(market, cfg, journal_path, now=now)
    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "paused"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_streak_halt_rest_of_day_blocks_via_run(monkeypatch, tmp_path):
    # серия из 4 убытков подряд сегодня → cfg.risk.streak.four_losses.
    # halt_rest_of_day=true → HALT_NEW, blocked_by=halt_rest_of_day
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    last_loss = TODAY_START + dt.timedelta(hours=9)
    records = [
        _outcome(last_loss - dt.timedelta(minutes=30), -1.0, "t1"),
        _outcome(last_loss - dt.timedelta(minutes=20), -1.0, "t2"),
        _outcome(last_loss - dt.timedelta(minutes=10), -1.0, "t3"),
        _outcome(last_loss, -1.0, "t4"),
    ]
    journal_path = tmp_path / "journal.jsonl"
    _write_journal(journal_path, records)
    now = last_loss + dt.timedelta(minutes=10)
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    v = risk_gate_cli.run(market, cfg, journal_path, now=now)
    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "halt_rest_of_day"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_max_open_positions_binds_via_run(monkeypatch, tmp_path):
    # cfg.risk.max_open_positions == 3. Риск каждой позиции намеренно МАЛ и не
    # исчерпывает лимит открытого риска (30 из 150 доступных) — блокирующей
    # причиной обязан быть именно лимит позиций, не открытый риск.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    assert cfg.risk.max_open_positions == 3
    positions = [pos(i, "XAUUSD", 0, 0.05, 2400.0, 2398.0) for i in range(1, 4)]  # risk 10.0 each
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=positions)
    # все три позиции учтены в журнале (не orphan) — тест проверяет именно
    # лимит одновременных позиций (задача 1.5), не orphan (2.3)
    journal_path = tmp_path / "journal.jsonl"
    _write_journal(journal_path, [_decision(TODAY_START, str(i)) for i in range(1, 4)])
    v = risk_gate_cli.run(market, cfg, journal_path, now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "max_open_positions"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_open_risk_exhausted_binds_via_run(monkeypatch, tmp_path):
    # cfg.risk.max_open_risk_pct == 1.5% * equity(10000) == 150.0. Одна позиция
    # съедает РОВНО весь лимит открытого риска: distance=30 → 3000 points *
    # 1.0 (value_per_point_per_lot XAUUSD) * 0.05 лота = 150.0. positions_count
    # (1) остаётся ниже max_open_positions (3), так что связывающей причиной
    # обязан быть именно открытый риск, а не лимит позиций.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    assert cfg.risk.max_open_risk_pct == 1.5
    p = pos(1, "XAUUSD", 0, 0.05, 2400.0, 2370.0)
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[p])
    # позиция учтена в журнале (не orphan) — тест проверяет именно лимит
    # открытого риска (задача 1.5), не orphan (2.3)
    journal_path = tmp_path / "journal.jsonl"
    _write_journal(journal_path, [_decision(TODAY_START, "1")])
    v = risk_gate_cli.run(market, cfg, journal_path, now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "open_risk_exhausted"
    assert v["max_risk_per_trade_usd"] == 0.0


def test_corrupted_journal_file_fails_closed(monkeypatch, tmp_path):
    # journal.jsonl повреждён (например, обрыв записи при сбое) — JSONDecodeError
    # обязан деградировать до HALT_NEW полной схемы, а не уронить CLI целиком
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text("{не валидный json\n", encoding="utf-8")
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    v = risk_gate_cli.run(market, cfg, journal_path, now=NOW)
    assert v["verdict"] == "HALT_NEW"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert v["require_setup_status"] == "confirmed"
    assert v["planned_only"] is True
    assert "error" in v


# ============================ orphan-позиции (задача 2.3) ============================


def test_orphans_block_new_entries_via_run(monkeypatch, tmp_path):
    # позиция у брокера, decision-записи о ней в журнале нет вовсе — CLI
    # обязан отказать ДО обращения к гейту: HALT_NEW, blocked_by=orphan_positions,
    # полная схема ответа — та же, что и у остальных запрещающих путей CLI.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    p = pos(321, "XAUUSD", 0, 0.05, 2400.0, 2390.0)
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[p])
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)

    assert v["verdict"] == "HALT_NEW"
    assert v["blocked_by"] == "orphan_positions"
    assert v["max_risk_per_trade_usd"] == 0.0
    assert v["require_setup_status"] == "confirmed"
    assert v["planned_only"] is True
    assert v["unplanned_allowed"] is False

    # полная схема — то же множество ключей, что у любого другого блокирующего
    # ответа гейта (без "error": это осознанный business-отказ, не исключение)
    reference = evaluate_gate(equity=10000, day_start_equity=10000, initial_balance=10000,
                              limits=cfg.risk, halt_rest_of_day=True, now=NOW)
    assert set(v) == set(reference)
    assert "error" not in v


def test_orphan_reason_names_ticket_and_symbol_via_run(monkeypatch, tmp_path):
    # причина запрета должна называть конкретную позицию — тикет и символ —
    # чтобы её можно было найти в терминале, не заходя в MT5
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    p = pos(654321, "GBPUSD", 0, 0.05, 1.27, 1.26)
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[p])
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)

    assert v["blocked_by"] == "orphan_positions"
    assert any("654321" in r for r in v["reasons"])
    assert any("GBPUSD" in r for r in v["reasons"])


def test_multiple_orphans_all_named_via_run(monkeypatch, tmp_path):
    # несколько orphan одновременно — все обязаны быть перечислены, а не
    # только первая найденная
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    p1 = pos(111, "XAUUSD", 0, 0.05, 2400.0, 2390.0)
    p2 = pos(222, "EURUSD", 1, 0.10, 1.10, 1.11)
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[p1, p2])
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)

    assert v["blocked_by"] == "orphan_positions"
    reason_text = " ".join(v["reasons"])
    assert "111" in reason_text and "XAUUSD" in reason_text
    assert "222" in reason_text and "EURUSD" in reason_text


def test_no_orphans_reaches_gate_normally_via_run(monkeypatch, tmp_path):
    # позиция ЕСТЬ decision-запись в журнале — orphan-проверка молчит,
    # verdict решает сам гейт (OK на чистом счёте без открытого риска у
    # лимитов)
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    p = pos(9, "XAUUSD", 0, 0.01, 2400.0, 2398.0)  # маленький риск, лимиты не задеты
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[p])
    journal_path = tmp_path / "journal.jsonl"
    _write_journal(journal_path, [_decision(TODAY_START, "9")])
    v = risk_gate_cli.run(market, cfg, journal_path, now=NOW)
    assert v["blocked_by"] != "orphan_positions"
    assert v["verdict"] == "OK"


def test_orphan_without_sl_lands_in_both_orphan_list_and_unprotected_exposure(monkeypatch, tmp_path):
    # Одна и та же непровязанная (нет decision-записи) позиция без стопа —
    # ДВЕ независимые проверки обязаны увидеть её по своим причинам:
    # find_orphans (сопоставление с журналом) и exposure/build_gate_inputs
    # (риск без стопа неизвестен). Это те самые «две разные причины запрета»
    # из задания — run() выбирает только orphan (она проверяется первой), но
    # обе диагностики должны быть верны независимо, если считать их напрямую.
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    naked = pos(777, "XAUUSD", 0, 0.05, 2400.0, 0.0)  # sl=0.0, decision-записи нет
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0}, positions=[naked])
    journal_path = tmp_path / "journal.jsonl"

    orphans = find_orphans(journal_path, market.positions())
    assert len(orphans) == 1
    assert orphans[0]["ticket"] == 777
    assert orphans[0]["has_sl"] is False

    inputs = risk_gate_cli.build_gate_inputs(market, cfg, [], now=NOW)
    assert inputs["unprotected_positions"] == 1

    # и подтверждаем, какая из двух причин реально выигрывает через run()
    v = risk_gate_cli.run(market, cfg, journal_path, now=NOW)
    assert v["blocked_by"] == "orphan_positions"


# --------------------------------------------------------------------------
# видимость исключения по спреду — раньше здесь была слепая зона
# --------------------------------------------------------------------------

def test_run_surfaces_spread_excluded_symbols(monkeypatch, tmp_path):
    """РЕГРЕСС 2026-07-28: этот CLI весь день отвечал «гейт: OK», пока
    enter.py отклонял каждую попытку входа — XAUUSD был исключён гистерезисом
    spread_gate с предыдущего дня, а evaluate_gate() про спред символа ничего
    не знает (стены и риск — не то же самое, что цена входа). Модель читала
    «OK» как «можно торговать» и не видела реальной причины отказов."""
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    (tmp_path / "spread_median.json").write_text(json.dumps({
        "medians": {"XAUUSD": 19.0}, "samples": {}, "source": {},
        "excluded": {"XAUUSD": {"since": "2026-07-27T13:57:27+00:00",
                                "ratio": 3.0, "median": 19.0, "spread_points": 57.0}},
    }), encoding="utf-8")
    cfg = _cfg()
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)
    assert v["verdict"] == "OK", "стены сами по себе не нарушены"
    assert "XAUUSD" in v["spread_excluded"]
    assert v["spread_excluded"]["XAUUSD"]["ratio"] == 3.0


def test_run_reports_no_spread_exclusions_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_gate_cli, "state_dir", lambda cfg: str(tmp_path))
    cfg = _cfg()
    market = FakeMarket(account={"balance": 10000.0, "equity": 10000.0})
    v = risk_gate_cli.run(market, cfg, tmp_path / "journal.jsonl", now=NOW)
    assert v["spread_excluded"] == {}
