"""E2E dry-run: полная петля gate→journal→score в paper-режиме на adversarial-серии
убытков. Проверяет, что риск-стена держится и конвейер памяти работает."""
import uuid

from trader_lib.config import load_config
from trader_lib.journal import append_decision, append_outcome, read_records
from trader_lib.risk_gate import evaluate_gate
from trader_lib.score import compute_stats

# Текущий контракт гейта (задача 1.6): limits=cfg.risk вместо пяти устаревших
# kwargs — иначе новые лимиты (ladder/positions/attempts/open-risk), которые
# сейчас реально включены в проде, в этом e2e-прогоне были бы невидимы.
LIMITS = load_config("config/trader.config.json").risk


def test_e2e_wall_holds_and_pipeline_works(tmp_path, make_decision):
    journal = tmp_path / "journal.jsonl"
    equity = 10000.0
    day_start = 10000.0
    cycles = 0
    for _ in range(300):
        v = evaluate_gate(equity=equity, day_start_equity=day_start,
                          initial_balance=10000, limits=LIMITS)
        if v["verdict"] in ("FORCE_FLAT", "HALT_NEW"):
            break
        risk = v["max_risk_per_trade_usd"]
        if risk <= 0:
            break
        tid = uuid.uuid4().hex[:8]
        # append_decision (строгий, единственный путь записи) + make_decision:
        # этот e2e проверяет арифметику стены/скоринга, не состав журнала
        # (задача 2.1) — фабрика убирает шум из 28 полей, не ослабляет проверку.
        append_decision(journal, make_decision(trade_id=tid, setup_type="adverse",
                                               confidence=0.6, risk_usd=risk))
        # adversarial: каждая сделка — полный стоп (R = -1)
        equity -= risk
        append_outcome(journal, {"trade_id": tid, "R": -1.0, "exit_reason": "sl"})
        cycles += 1

    # 1) стена дня НЕ пробита
    assert (day_start - equity) / day_start * 100 < 3.0
    # 2) конвейер памяти работает
    recs = read_records(journal)
    assert len(recs) == cycles * 2
    stats = compute_stats(recs, min_n_for_confirmed=20)
    assert stats["overall"]["n"] == cycles
    assert stats["by_setup"]["adverse"]["avg_R"] == -1.0
