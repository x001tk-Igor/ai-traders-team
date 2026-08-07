def account_snapshot(acc, *, day_start_equity, initial_balance, profit_target_pct,
                     daily_limit_pct, total_limit_pct, positions):
    equity = acc["equity"]
    daily_loss_pct = max(0.0, (day_start_equity - equity) / day_start_equity * 100)
    total_loss_pct = max(0.0, (initial_balance - equity) / initial_balance * 100)
    progress = (equity - initial_balance) / initial_balance * 100
    return {
        "equity": equity, "balance": acc["balance"],
        "progress_to_target_pct": round(progress, 2),
        "target_pct": profit_target_pct,
        "daily_budget_remaining_pct": round(daily_limit_pct - daily_loss_pct, 3),
        "total_budget_remaining_pct": round(total_limit_pct - total_loss_pct, 3),
        "open_positions": positions,
    }
